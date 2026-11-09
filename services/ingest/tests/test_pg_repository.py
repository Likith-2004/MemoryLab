"""PostgresRunRepository against the local Supabase database, plus the ingest flow end to end into real tables.

MEMORYLAB_REQUIRE_DB=1 (the gate) turns an unreachable database into a failure instead of a skip."""

import os
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from runlog.eventlog import IndexEntry
from runlog.store import LocalObjectStore

from conftest import SECRET, Env, ev
from ingest.app import create_app
from ingest.pg_repository import PostgresRunRepository
from ingest.repository import RunRecord
from ingest.service import IngestService

DB_URL = os.environ.get("MEMORYLAB_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


@pytest.fixture
def admin() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    try:
        conn = psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("MEMORYLAB_REQUIRE_DB") == "1":
            pytest.fail(f"local Supabase is required but unreachable: {exc}")
        pytest.skip("local Supabase not running")
    yield conn
    conn.close()


@pytest.fixture
def repo() -> Iterator[PostgresRunRepository]:
    r = PostgresRunRepository(DB_URL, max_size=6)
    yield r
    r.close()


@pytest.fixture
def make_run(admin: psycopg.Connection[dict[str, Any]]) -> Iterator[Any]:
    users: list[str] = []

    def make() -> str:
        uid = str(uuid.uuid4())
        admin.execute("insert into auth.users (id, email, instance_id, aud, role, created_at, updated_at, confirmation_token, recovery_token, email_change_token_new, email_change) values (%s, %s, "
                      "'00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', now(), now(), '', '', '', '')",
                      (uid, f"{uid}@example.test"))
        users.append(uid)
        cfg = admin.execute("insert into public.run_configs (owner_id, config_json, config_hash, mode) "
                            "values (%s, '{}', %s, 'demo') returning id", (uid, "a" * 64)).fetchone()["id"]  # type: ignore[index]
        return str(admin.execute("insert into public.runs (owner_id, config_id, cap_steps, cap_tokens, cap_wall_s) "
                                 "values (%s, %s, 10, 1000, 60) returning id", (uid, cfg)).fetchone()["id"])  # type: ignore[index]

    yield make
    for uid in users:
        admin.execute("delete from auth.users where id = %s", (uid,))


def test_unknown_run_is_none_and_a_fresh_run_starts_from_the_lifecycle_origin(repo: PostgresRunRepository, make_run: Any) -> None:
    assert repo.get(str(uuid.uuid4())) is None
    rec = repo.get(make_run())
    assert rec is not None and rec.status is None and rec.next_batch_no == {"worker": 1, "orchestrator": 1}
    assert (rec.event_count, rec.closed, rec.manifest_sealed, rec.last_ack) == (0, False, False, {})


def test_save_and_get_round_trip_and_final_state_closes_the_run_row(
        repo: PostgresRunRepository, make_run: Any, admin: psycopg.Connection[dict[str, Any]]) -> None:
    rid = make_run()
    rec = RunRecord(rid, status="running", next_batch_no={"worker": 4, "orchestrator": 2}, event_count=7, bytes_total=99,
                    frames=2, tokens=500, steps=6, score=3, last_ack={"worker": {"batch_no": 3, "accepted": 2}})
    repo.save(rec)
    assert repo.get(rid) == rec
    row = admin.execute("select status, steps, tokens, started_at, ended_at from public.runs where id = %s", (rid,)).fetchone()
    assert (row["status"], row["steps"], row["tokens"]) == ("running", 6, 500) and row["started_at"] and row["ended_at"] is None  # type: ignore[index]
    rec.status, rec.closed, rec.bundle_sha256 = "capped", True, "b" * 64
    repo.save(rec)
    row = admin.execute("select status, ended_at, end_reason from public.runs where id = %s", (rid,)).fetchone()
    assert row["status"] == "capped" and row["ended_at"] and row["end_reason"] == "capped"  # type: ignore[index]
    assert repo.get(rid).bundle_sha256 == "b" * 64  # type: ignore[union-attr]


def test_lock_makes_read_modify_write_atomic_across_threads(repo: PostgresRunRepository, make_run: Any) -> None:
    rid = make_run()
    repo.save(RunRecord(rid, status="running"))
    n_threads, per_thread = 6, 8

    def work() -> None:
        for _ in range(per_thread):
            with repo.lock(rid):
                rec = repo.get(rid)
                assert rec is not None
                rec.event_count += 1
                repo.save(rec)

    threads = [threading.Thread(target=work) for _ in range(n_threads)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert repo.get(rid).event_count == n_threads * per_thread  # type: ignore[union-attr]


def test_an_exception_inside_the_lock_rolls_everything_back(repo: PostgresRunRepository, make_run: Any,
                                                            admin: psycopg.Connection[dict[str, Any]]) -> None:
    rid = make_run()
    with pytest.raises(RuntimeError), repo.lock(rid):
        rec = repo.get(rid)
        assert rec is not None
        rec.event_count, rec.status = 5, "running"
        repo.save(rec)
        repo.add_index(rid, [IndexEntry(1, "run.state", 0, "2026-11-08T00:00:00.000Z", 0, 0)])
        raise RuntimeError
    assert repo.get(rid).event_count == 0  # type: ignore[union-attr]
    assert admin.execute("select count(*) n from public.run_events_index where run_id = %s", (rid,)).fetchone()["n"] == 0  # type: ignore[index]
    assert repo.lock(rid) is not None and getattr(repo._local, "conn", None) is None   # no connection left bound


def test_the_lock_is_reentrant_in_one_thread(repo: PostgresRunRepository, make_run: Any) -> None:
    rid = make_run()
    with repo.lock(rid), repo.lock(rid):
        assert repo.get(rid) is not None


class PgEnv:
    def __init__(self, env: Env, repo: PostgresRunRepository, make_run: Any) -> None:
        self.env, self.repo, self._make = env, repo, make_run

    def run(self) -> str:
        return str(self._make())


@pytest.fixture
def pg(tmp_path: Path, repo: PostgresRunRepository, make_run: Any) -> Env:
    """The same Env the in-memory suite uses, with Postgres underneath."""
    store = LocalObjectStore(tmp_path / "objects")
    now = [1_000_000.0]
    service = IngestService(repo, store, SECRET, clock=lambda: now[0])
    env = Env(repo, store, service, TestClient(create_app(service), raise_server_exceptions=False), now)  # type: ignore[arg-type]
    env.run = lambda: str(make_run())  # type: ignore[method-assign]
    return env


def test_ingest_flow_writes_events_index_memory_and_final_state_to_postgres(
        pg: Env, admin: psycopg.Connection[dict[str, Any]]) -> None:
    rid = pg.run()
    pg.start(rid)
    write = {"type": "mem.write", "step": 1, "data": {"path": "a.md", "version": 1, "step": 1, "rev": 1, "content": "hi",
                                                    "chars": 2, "after_sha256": "x", "before_sha256": None,
                                                    "created": True, "diff": ""}}
    delete = {"type": "mem.delete", "step": 2, "data": {"path": "a.md", "version": 2, "step": 2, "rev": 2,
                                                      "before_sha256": "x", "chars": 2}}
    assert pg.post(rid, 2, [ev("obs", 1, snapshot={"milestones": 0}, score=4), write, delete]).status_code == 200
    assert pg.post(rid, 3, [ev("model.call", 2, tokens_total=321)]).status_code == 200
    assert pg.post(rid, 4, [ev("run.state", state="stopping")]).status_code == 200      # the worker ends its own run
    assert pg.post(rid, 5, [ev("run.state", state="completed")]).status_code == 200

    idx = admin.execute("select seq, type, step from public.run_events_index where run_id = %s order by seq", (rid,)).fetchall()
    assert [r["seq"] for r in idx] == list(range(1, len(idx) + 1)) and {"obs", "mem.write", "mem.delete"} <= {r["type"] for r in idx}
    mem = admin.execute("select path, version, rev, deleted, content from public.memory_files where run_id = %s "
                        "order by version", (rid,)).fetchall()
    assert [(m["path"], m["version"], m["rev"], m["deleted"], m["content"]) for m in mem] == \
           [("a.md", 1, 1, False, "hi"), ("a.md", 2, 2, True, "")]
    state = admin.execute("select event_count, score from public.run_ingest_state where run_id = %s", (rid,)).fetchone()
    assert state["event_count"] == len(idx) and state["score"] == 4  # type: ignore[index]
    run = admin.execute("select tokens, steps, status from public.runs where id = %s", (rid,)).fetchone()
    assert run["tokens"] == 321 and run["steps"] == 1 and run["status"] == "completed"  # type: ignore[index]
    assert admin.execute("select ended_at is not null e from public.runs where id = %s", (rid,)).fetchone()["e"]  # type: ignore[index]


def test_a_rejected_batch_leaves_no_rows_behind(pg: Env, admin: psycopg.Connection[dict[str, Any]]) -> None:
    rid = pg.run()
    pg.start(rid)
    before = admin.execute("select count(*) n from public.run_events_index where run_id = %s", (rid,)).fetchone()["n"]  # type: ignore[index]
    bad = pg.post(rid, 5, [ev("obs", 1, snapshot={"milestones": 0}, score=0)])          # skipped ahead: 409
    assert bad.status_code == 409
    after = admin.execute("select count(*) n from public.run_events_index where run_id = %s", (rid,)).fetchone()["n"]  # type: ignore[index]
    assert before == after
    retry = pg.post(rid, 2, [ev("obs", 1, snapshot={"milestones": 0}, score=0)])
    assert retry.status_code == 200
    again = pg.post(rid, 2, [ev("obs", 1, snapshot={"milestones": 0}, score=0)])        # acked retry: not duplicated
    assert again.status_code == 200
    n = admin.execute("select count(*) n from public.run_events_index where run_id = %s", (rid,)).fetchone()["n"]  # type: ignore[index]
    assert n == before + 1


def test_checkpoints_are_rows_and_a_forks_parent_is_found_through_the_database_link(
        repo: PostgresRunRepository, make_run: Any, admin: psycopg.Connection[dict[str, Any]]) -> None:
    from ingest.repository import ParentRef
    parent, child = make_run(), make_run()
    cid = repo.add_checkpoint(parent, {"step": 7, "savestate_key": f"runs/{parent}/checkpoints/0000007.state", "memory_version": 3,
                                       "transcript_offset": 42, "config_hash": "e" * 64})
    row = admin.execute("select step, savestate_key, memory_version, transcript_offset, config_hash from public.checkpoints where id = %s",
                        (cid,)).fetchone()
    assert row == {"step": 7, "savestate_key": f"runs/{parent}/checkpoints/0000007.state", "memory_version": 3, "transcript_offset": 42,
                   "config_hash": "e" * 64}
    assert repo.parent_of(child) is None and repo.parent_of(str(uuid.uuid4())) is None
    admin.execute("update public.runs set parent_checkpoint_id = %s where id = %s", (cid, child))
    assert repo.parent_of(child) == ParentRef(parent, 7, 42, str(cid))
