"""Fixtures for tests against the local Supabase stack (`supabase start --workdir infra`).

Set MEMORYLAB_REQUIRE_DB=1 (the gate does) to make an unreachable database a failure, not a skip.
"""

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

DB_URL = os.environ.get("MEMORYLAB_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


@pytest.fixture(scope="session")
def admin_conn() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    try:
        conn = psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("MEMORYLAB_REQUIRE_DB") == "1":
            pytest.fail(f"local Supabase is required but unreachable: {exc}")
        pytest.skip("local Supabase not running (supabase start --workdir infra)")
    yield conn
    conn.close()


class Db:
    """A transaction rolled back at the end of each test; `as_` switches role and JWT claims."""

    def __init__(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        self.conn = conn
        self.conn.execute("begin")

    def new_user(self, role: str = "user") -> str:
        uid = str(uuid.uuid4())
        self.conn.execute(
            "insert into auth.users (id, email, instance_id, aud, role) "
            "values (%s, %s, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated')",
            (uid, f"{uid}@example.test"))
        if role == "admin":
            self.conn.execute("update public.profiles set role = 'admin' where id = %s", (uid,))
        return uid

    def as_(self, user: str | None) -> None:
        """None = anon. The claims are what Supabase's PostgREST sets for a verified JWT."""
        self.conn.execute("reset role")
        if user is None:
            claims = {"role": "anon"}
            role = "anon"
        else:
            claims = {"sub": user, "role": "authenticated"}
            role = "authenticated"
        self.conn.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
        self.conn.execute(f"set local role {role}")

    def as_service(self) -> None:
        self.conn.execute("reset role")
        self.conn.execute("select set_config('request.jwt.claims', %s, true)",
                          (json.dumps({"role": "service_role"}),))
        self.conn.execute("set local role service_role")

    def q(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self.conn.execute(sql, args).fetchall()  # type: ignore[arg-type]

    def x(self, sql: str, *args: Any) -> None:
        self.conn.execute(sql, args)  # type: ignore[arg-type]

    def make_run(self, owner: str, visibility: str = "private", **cols: Any) -> str:
        """Seed a run as the service role (RLS bypass), then return to the previous caller-less state."""
        self.as_service()
        cfg = self.q("insert into public.run_configs (owner_id, config_json, config_hash, mode) "
                     "values (%s, '{}', %s, 'demo') returning id", owner, "a" * 64)[0]["id"]
        row = self.q("insert into public.runs (owner_id, config_id, visibility, cap_steps, cap_tokens, cap_wall_s) "
                     "values (%s, %s, %s, 10, 1000, 60) returning id", owner, cfg, visibility)[0]
        run_id = str(row["id"])
        for k, v in cols.items():
            self.x(f"update public.runs set {k} = %s where id = %s", v, run_id)
        self.conn.execute("reset role")
        return run_id

    def close(self) -> None:
        self.conn.execute("rollback")


@pytest.fixture
def db(admin_conn: psycopg.Connection[dict[str, Any]]) -> Iterator[Db]:
    d = Db(admin_conn)
    yield d
    d.close()
