import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from accounts.eraser import TOMBSTONE_ID, AccountEraser

DB_URL = os.environ.get("MEMORYLAB_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
Conn = psycopg.Connection[dict[str, Any]]


class FakeKiller:
    """Stands in for the orchestrator: a kill moves the run to `killed` unless `stuck` (it never finishes)."""

    def __init__(self, conn: Conn) -> None:
        self.conn, self.killed, self.stuck = conn, [], False

    def kill(self, run_id: str, actor: str, reason: str) -> str:
        self.killed.append((run_id, actor, reason))
        if not self.stuck:
            self.conn.execute("update public.runs set status = 'killed' where id = %s", (run_id,))
        return "killed"


class FakeAuth:
    """Stands in for GoTrue's admin delete: removes the auth user (and so cascades) unless `keep`."""

    def __init__(self, conn: Conn) -> None:
        self.conn, self.deleted, self.keep = conn, [], False

    def delete_user(self, user_id: str) -> None:
        self.deleted.append(user_id)
        if not self.keep:
            self.conn.execute("delete from auth.users where id = %s", (user_id,))


class World:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn
        self.users: list[str] = []
        self.runs: list[str] = []
        self.killer, self.auth = FakeKiller(conn), FakeAuth(conn)
        self.ticks = 0
        self.eraser = AccountEraser(DB_URL, self.killer, self.auth, wait_s=3, poll_s=1,
                                    sleep=self._sleep, clock=self._clock)

    def _sleep(self, s: float) -> None:
        self.ticks += int(s)

    def _clock(self) -> float:
        return float(self.ticks)

    def user(self) -> str:
        uid = str(uuid.uuid4())
        self.conn.execute("insert into auth.users (id, email, instance_id, aud, role) values (%s, %s, "
                          "'00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated')",
                          (uid, f"{uid}@example.test"))
        self.users.append(uid)
        return uid

    def run(self, owner: str, visibility: str = "private", *, verified: bool | None = None, status: str = "completed",
            title: str | None = "my title", config: str = '{"secret_note": "mine"}') -> str:
        cfg = self.conn.execute("insert into public.run_configs (owner_id, config_json, config_hash, mode) values "
                                "(%s, %s::jsonb, %s, 'demo') returning id", (owner, config, "a" * 64)).fetchone()["id"]  # type: ignore[index]
        rid = str(self.conn.execute(
            "insert into public.runs (owner_id, config_id, visibility, status, title, cap_steps, cap_tokens, cap_wall_s) "
            "values (%s, %s, %s, %s, %s, 10, 1000, 60) returning id",
            (owner, cfg, visibility, status, title)).fetchone()["id"])  # type: ignore[index]
        if verified is not None:
            self.conn.execute("insert into public.run_scores values (%s, 5, 1, 1, 1, 1, 0, 0, %s)", (rid, verified))
        self.runs.append(rid)
        return rid

    def row(self, run: str) -> dict[str, Any]:
        return self.conn.execute("select owner_id::text owner, title, deleted_at is not null deleted, status, "
                                 "(select config_json::text from public.run_configs c where c.id = config_id) cfg "
                                 "from public.runs where id = %s", (run,)).fetchone()  # type: ignore[return-value]


@pytest.fixture
def world() -> Iterator[World]:
    try:
        conn = psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("MEMORYLAB_REQUIRE_DB") == "1":
            pytest.fail(f"local Supabase is required but unreachable: {exc}")
        pytest.skip("local Supabase not running")
    w = World(conn)
    yield w
    conn.execute("delete from public.runs where id = any(%s)", (w.runs,))      # tombstone-owned leftovers
    conn.execute("delete from public.run_configs where owner_id = %s and id not in (select config_id from public.runs)",
                 (TOMBSTONE_ID,))
    for uid in w.users:
        conn.execute("delete from auth.users where id = %s", (uid,))
    conn.close()
