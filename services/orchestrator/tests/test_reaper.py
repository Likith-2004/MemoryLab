"""The reaper against the real local database and a Postgres-backed ingest service (MEMORYLAB_REQUIRE_DB=1 turns
an absent database into a failure)."""

import logging
import os
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from ingest.app import create_app as create_ingest_app
from ingest.pg_repository import PostgresRunRepository
from ingest.service import IngestService
from ingest.tokens import issue_token
from labharness.providers.secret import SecretKey
from psycopg.rows import dict_row
from runlog.client import HttpEventSink
from runlog.eventlog import EventLogReader
from runlog.redaction import ScrubFilter
from runlog.replay import final_state, verify_log
from runlog.store import LocalObjectStore

from orchestrator.ingest_api import IngestApi
from orchestrator.reaper import HEARTBEAT_LOST_AFTER_S, REAP_AFTER_S, Reaper

DB_URL = os.environ.get("MEMORYLAB_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
SECRET = b"unit-test-secret-unit-test-secret-32b"
TOMBSTONE = "00000000-0000-0000-0000-00000000dead"
ACTIVE = ["queued", "starting", "running", "session_boundary", "dreaming", "stopping"]


class FakeKiller:
    def __init__(self) -> None:
        self.killed: list[tuple[str, str, str]] = []
        self.fail = False

    def kill(self, run_id: str, actor: str, reason: str) -> str:
        self.killed.append((run_id, actor, reason))
        if self.fail:
            raise ConnectionError("orchestrator is down")
        return "killed"


class Seed:
    def __init__(self, conn: psycopg.Connection[dict[str, Any]], ingest: TestClient, store: LocalObjectStore) -> None:
        self.conn, self.ingest, self.store = conn, ingest, store
        self.uid = str(uuid.uuid4())
        conn.execute("insert into auth.users (id, email, instance_id, aud, role) values (%s, %s, "
                     "'00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated')",
                     (self.uid, f"{self.uid}@example.test"))
        self.runs: list[str] = []

    def run(self, status: str, age_min: float, deleted: bool = False, owner: str | None = None,
            started_min: float | None = None) -> str:
        """A run row with no events (status set directly), created `age_min` minutes ago."""
        owner = owner or self.uid
        cfg = self.conn.execute("insert into public.run_configs (owner_id, config_json, config_hash, mode) "
                                "values (%s, '{}', %s, 'demo') returning id", (owner, "a" * 64)).fetchone()["id"]  # type: ignore[index]
        row = self.conn.execute(
            "insert into public.runs (owner_id, config_id, status, cap_steps, cap_tokens, cap_wall_s, created_at, "
            "deleted_at, started_at) values (%s, %s, %s, 10, 1000, 60, now() - %s * interval '1 minute', "
            "case when %s then now() else null end, "
            "case when %s::float8 is null then null else now() - %s::float8 * interval '1 minute' end) returning id",
            (owner, cfg, status, age_min, deleted, started_min, started_min or 0)).fetchone()
        rid = str(row["id"])  # type: ignore[index]
        self.runs.append(rid)
        return rid

    def logged_run(self, state: str, event_age_min: float, age_min: float | None = None, **kw: Any) -> str:
        """A run whose events went through the real ingest service up to `state`; its events are then made
        `event_age_min` minutes old."""
        rid = self.run("queued", age_min if age_min is not None else event_age_min + 1, **kw)
        api = IngestApi(self.ingest, rid, SECRET)
        api.state("queued")
        if state != "queued":
            api.state("starting")
        if state in ("running", "stopping", "session_boundary", "dreaming"):
            token = SecretKey(issue_token(SECRET, rid, "worker", 3600, time.time()))
            sink = HttpEventSink(self.ingest, rid, token, batch_size=1)
            for s in ("running", *(() if state == "running" else (state,))):
                sink.emit("run.state", 0, {"state": s})
        self.conn.execute("update public.run_events_index set ts = now() - %s * interval '1 minute' where run_id = %s",
                          (event_age_min, rid))
        return rid

    def state(self, run: str) -> tuple[str, str | None, bool]:
        r = self.conn.execute("select status, end_reason, ended_at is not null done from public.runs where id = %s",
                              (run,)).fetchone()
        return r["status"], r["end_reason"], r["done"]  # type: ignore[index]

    def closed(self, run: str) -> bool:
        r = self.conn.execute("select closed from public.run_ingest_state where run_id = %s", (run,)).fetchone()
        return bool(r and r["closed"])

    def log(self, run: str) -> list[Any]:
        return EventLogReader(self.store, run).read_page(0, 1000)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[Any]:
    try:
        conn = psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("MEMORYLAB_REQUIRE_DB") == "1":
            pytest.fail(f"local Supabase is required but unreachable: {exc}")
        pytest.skip("local Supabase not running")
    store = LocalObjectStore(tmp_path / "objects")
    repo = PostgresRunRepository(DB_URL, max_size=6)
    ingest = TestClient(create_ingest_app(IngestService(repo, store, SECRET, clock=time.time)),
                        raise_server_exceptions=False, base_url="http://ingest.test:8000")
    seed = Seed(conn, ingest, store)
    killer = FakeKiller()
    made: list[Reaper] = []

    def reaper(**kw: Any) -> Reaper:
        ingest_factory = kw.pop("ingest", lambda run_id: IngestApi(ingest, run_id, SECRET))
        r = Reaper(DB_URL, killer=kw.pop("killer", killer), ingest=ingest_factory, **kw)
        made.append(r)
        return r

    seed.reaper, seed.killer = reaper, killer  # type: ignore[attr-defined]
    yield seed
    for r in made:
        r.close()
    conn.execute("delete from public.runs where id = any(%s)", (seed.runs,))
    conn.execute("delete from auth.users where id = %s", (seed.uid,))
    repo.close()
    conn.close()


def test_the_windows_are_eleven_and_fifteen_minutes() -> None:
    from orchestrator.config import OrchestratorConfig
    assert REAP_AFTER_S == 11 * 60 > OrchestratorConfig.__dataclass_fields__["queue_ttl_s"].default
    assert HEARTBEAT_LOST_AFTER_S == 15 * 60 > 600 + OrchestratorConfig.__dataclass_fields__["heartbeat_timeout_s"].default


def test_a_queued_run_is_failed_only_after_the_window_and_is_not_killed(world: Any) -> None:
    fresh, stale = world.run("queued", 9), world.run("queued", 12)
    failed = world.reaper().run_once()
    assert stale in failed and fresh not in failed
    assert world.state(fresh) == ("queued", None, False)
    assert world.state(stale) == ("failed", "queue_timeout", True)
    assert world.killer.killed == []                          # a queued run past the TTL is in no queue: nothing to kill


def test_a_final_run_and_a_healthy_run_are_untouched(world: Any) -> None:
    runs = {s: world.run(s, 120) for s in ("completed", "capped", "failed", "killed")}
    healthy = world.logged_run("running", event_age_min=1, age_min=120)
    failed = world.reaper().run_once()
    assert not set(runs.values()) & set(failed) and healthy not in failed
    assert world.state(runs["failed"]) == ("failed", None, False)          # an already-final row is not rewritten
    assert world.state(healthy)[0] == "running"


def test_a_deleted_queued_run_is_still_failed(world: Any) -> None:
    run = world.run("queued", 30, deleted=True)
    assert run in world.reaper().run_once()
    assert world.state(run) == ("failed", "queue_timeout", True)


def test_a_starting_run_with_no_recent_ingest_is_failed_through_ingest_and_killed(world: Any) -> None:
    silent = world.logged_run("starting", event_age_min=25)
    beating = world.logged_run("starting", event_age_min=2, age_min=30)
    young = world.logged_run("starting", event_age_min=9)
    failed = world.reaper().run_once()
    assert silent in failed and not {beating, young} & set(failed)
    assert world.state(silent) == ("failed", "startup_timeout", True)
    assert world.state(beating)[0] == "starting" and world.state(young)[0] == "starting"
    assert world.killer.killed == [(silent, "system", "startup_timeout")]
    events = world.log(silent)
    assert [e.type for e in events] == ["run.state"] * 3 and verify_log(events) == []
    assert final_state(events) == ("failed", "startup_timeout")           # the log ends in a final run.state
    assert world.closed(silent)                                           # and is closed to further appends


@pytest.mark.parametrize("state", ["running", "session_boundary", "dreaming", "stopping"])
def test_an_active_run_with_no_event_for_fifteen_minutes_is_failed_heartbeat_lost(world: Any, state: str) -> None:
    stuck = world.logged_run(state, event_age_min=16)
    ok = world.logged_run(state, event_age_min=1, age_min=60)
    failed = world.reaper().run_once()
    assert stuck in failed and ok not in failed
    assert world.state(stuck) == ("failed", "heartbeat_lost", True)
    assert world.state(ok)[0] == state
    assert world.killer.killed == [(stuck, "system", "heartbeat_lost")]
    events = world.log(stuck)
    assert verify_log(events) == [] and final_state(events) == ("failed", "heartbeat_lost")
    assert world.closed(stuck)


def test_the_boundary_is_fifteen_minutes_not_eleven(world: Any) -> None:
    thirteen = world.logged_run("running", event_age_min=13)           # past the queue window, inside the beat window
    assert thirteen not in world.reaper().run_once() and world.state(thirteen)[0] == "running"


def test_an_active_run_with_no_events_at_all_is_judged_by_its_start(world: Any) -> None:
    old, recent = world.run("running", 60, started_min=20), world.run("running", 60, started_min=3)
    failed = world.reaper().run_once()
    assert old in failed and recent not in failed
    assert world.state(old) == ("failed", "heartbeat_lost", True)
    assert world.killer.killed == [(old, "system", "heartbeat_lost")]    # no events: updated directly, still killed


def test_a_stuck_run_owned_by_the_tombstone_is_reaped_too(world: Any) -> None:
    run = world.run("running", 60, started_min=40, owner=TOMBSTONE)
    assert run in world.reaper().run_once() and world.state(run)[:2] == ("failed", "heartbeat_lost")
    n = world.conn.execute("select count(*) n from public.runs where owner_id = %s and status = any(%s)",
                           (TOMBSTONE, ACTIVE)).fetchone()["n"]
    assert n == 0                                                      # the tombstone never holds an active run


def test_a_deleted_stuck_run_is_reaped_and_its_owner_can_create_again(world: Any) -> None:
    import json
    run = world.run("running", 60, started_min=40, deleted=True)
    create = "select api_private.create_run_request('{}', %s, 'demo', 'private', 5, 100, 30, 'g', null)"

    def as_user() -> psycopg.Connection[dict[str, Any]]:
        c = psycopg.connect(DB_URL, row_factory=dict_row)
        c.execute("select set_config('request.jwt.claims', %s, true)",
                  (json.dumps({"sub": world.uid, "role": "authenticated"}),))
        c.execute("set local role authenticated")
        return c

    with as_user() as c, pytest.raises(psycopg.errors.RaiseException, match="active_run_exists"):
        c.execute(create, ("c" * 64,))                                 # stuck: the owner is locked out
    assert run in world.reaper().run_once()
    with as_user() as c:
        assert c.execute(create, ("c" * 64,)).fetchone() is not None   # reaped: they can create again
        c.rollback()


def test_two_reapers_in_parallel_fail_each_row_once(world: Any) -> None:
    runs = {world.run("queued", 20) for _ in range(40)}
    a, b = world.reaper(), world.reaper()
    barrier = threading.Barrier(2)

    def go(r: Reaper) -> list[str]:
        barrier.wait(timeout=10)
        return r.run_once()

    with ThreadPoolExecutor(2) as pool:
        got_a, got_b = pool.map(go, (a, b))
    assert {r for r in got_a + got_b if r in runs} == runs and not set(got_a) & set(got_b)


def test_two_reapers_do_not_double_end_a_run_that_has_a_log(world: Any) -> None:
    runs = {world.logged_run("running", event_age_min=20) for _ in range(6)}
    a, b = world.reaper(), world.reaper()
    barrier = threading.Barrier(2)

    def go(r: Reaper) -> list[str]:
        barrier.wait(timeout=10)
        return r.run_once()

    with ThreadPoolExecutor(2) as pool:
        got_a, got_b = pool.map(go, (a, b))
    assert {r for r in got_a + got_b if r in runs} == runs and not set(got_a) & set(got_b)
    for run in runs:
        assert verify_log(world.log(run)) == [] and final_state(world.log(run))[0] == "failed"
    assert sorted(k[0] for k in world.killer.killed if k[0] in runs) == sorted(runs)     # each killed once


def test_a_row_locked_by_another_transaction_is_skipped_not_waited_for(world: Any) -> None:
    locked, free = world.run("queued", 20), world.run("queued", 20)
    holder = psycopg.connect(DB_URL, row_factory=dict_row, autocommit=False)
    holder.execute("select 1 from public.runs where id = %s for update", (locked,))
    reaper = world.reaper()
    result: list[list[str]] = []
    t = threading.Thread(target=lambda: result.append(reaper.run_once()))
    t.start()
    t.join(timeout=8)
    try:
        assert not t.is_alive(), "reaper blocked on a locked row"          # without skip locked it waits here
        assert free in result[0] and locked not in result[0]
        assert world.state(locked) == ("queued", None, False)
    finally:
        holder.rollback()
        holder.close()
        t.join(timeout=8)
    assert locked in reaper.run_once() and world.state(locked) == ("failed", "queue_timeout", True)


def test_a_second_pass_does_nothing(world: Any) -> None:
    run = world.run("queued", 20)
    reaper = world.reaper()
    assert run in reaper.run_once()
    assert run not in reaper.run_once()


def test_when_ingest_cannot_end_the_run_the_row_is_still_failed(world: Any) -> None:
    run = world.logged_run("running", event_age_min=20)

    class Down:
        def state(self, state: str, reason: str | None = None) -> None:
            raise ConnectionError("ingest is down")

        def close(self) -> dict[str, Any]:
            raise AssertionError("not reached")

    assert run in world.reaper(ingest=lambda _id: Down()).run_once()
    assert world.state(run) == ("failed", "heartbeat_lost", True)
    assert [e.data.get("state") for e in world.log(run)][-1] == "running"      # the log could not be ended


def test_a_log_that_cannot_be_exported_is_still_closed(world: Any) -> None:
    run = world.logged_run("starting", event_age_min=25)                   # no sealed manifest: close is refused
    assert run in world.reaper().run_once()
    assert world.closed(run) and final_state(world.log(run)) == ("failed", "startup_timeout")


def test_a_dead_orchestrator_does_not_stop_the_row_from_ending(world: Any) -> None:
    world.killer.fail = True
    run = world.logged_run("running", event_age_min=20)
    assert run in world.reaper().run_once()
    assert world.state(run) == ("failed", "heartbeat_lost", True) and world.killer.killed


def test_a_scrubbing_log_handler_does_not_break_a_pass(world: Any) -> None:
    """Regression: ScrubFilter used to str() every log argument, so `%d` raised once a worker test had installed it."""
    handler = logging.StreamHandler()
    handler.addFilter(ScrubFilter())
    logging.getLogger().addHandler(handler)
    try:
        run = world.run("queued", 20)
        assert run in world.reaper().run_once()
    finally:
        logging.getLogger().removeHandler(handler)


def test_the_reaper_acts_as_service_role_only(world: Any) -> None:
    """The reaper's own login need not be privileged: it assumes service_role for the write."""
    world.conn.execute("do $$ begin if not exists (select 1 from pg_roles where rolname = 'reaper_probe') then "
                       "create role reaper_probe login password 'probe' nobypassrls; end if; end $$")
    world.conn.execute("grant service_role to reaper_probe")
    run = world.run("queued", 20)
    url = DB_URL.replace("postgres:postgres@", "reaper_probe:probe@")
    r = Reaper(url, killer=world.killer, ingest=lambda _id: IngestApi(world.ingest, _id, SECRET))
    try:
        assert run in r.run_once()
    finally:
        r.close()
        world.conn.execute("revoke service_role from reaper_probe")
        world.conn.execute("drop role reaper_probe")
