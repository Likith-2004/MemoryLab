"""The reaper against the real local database (MEMORYLAB_REQUIRE_DB=1 makes an absent one a failure)."""

import os
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from orchestrator.reaper import REAP_AFTER_S, Reaper

DB_URL = os.environ.get("MEMORYLAB_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


class Seed:
    def __init__(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        self.conn = conn
        self.users: list[str] = []
        self.uid = str(uuid.uuid4())
        conn.execute("insert into auth.users (id, email, instance_id, aud, role) values (%s, %s, "
                     "'00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated')",
                     (self.uid, f"{self.uid}@example.test"))

    def run(self, status: str, age_min: float, deleted: bool = False) -> str:
        cfg = self.conn.execute("insert into public.run_configs (owner_id, config_json, config_hash, mode) "
                                "values (%s, '{}', %s, 'demo') returning id", (self.uid, "a" * 64)).fetchone()["id"]  # type: ignore[index]
        row = self.conn.execute(
            "insert into public.runs (owner_id, config_id, status, cap_steps, cap_tokens, cap_wall_s, created_at, deleted_at) "
            "values (%s, %s, %s, 10, 1000, 60, now() - make_interval(mins => %s), "
            "case when %s then now() else null end) returning id", (self.uid, cfg, status, age_min, deleted)).fetchone()
        return str(row["id"])  # type: ignore[index]

    def event(self, run: str, age_min: float) -> None:
        self.conn.execute("insert into public.run_events_index (run_id, seq, type, step, ts, blob_offset) "
                          "values (%s, 1, 'run.start', 0, now() - make_interval(mins => %s), 0)", (run, age_min))

    def state(self, run: str) -> tuple[str, str | None, bool]:
        r = self.conn.execute("select status, end_reason, ended_at is not null done from public.runs where id = %s",
                              (run,)).fetchone()
        return r["status"], r["end_reason"], r["done"]  # type: ignore[index]


@pytest.fixture
def seed() -> Iterator[Seed]:
    try:
        conn = psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("MEMORYLAB_REQUIRE_DB") == "1":
            pytest.fail(f"local Supabase is required but unreachable: {exc}")
        pytest.skip("local Supabase not running")
    s = Seed(conn)
    yield s
    conn.execute("delete from auth.users where id = %s", (s.uid,))
    conn.close()


@pytest.fixture
def reaper() -> Iterator[Reaper]:
    r = Reaper(DB_URL)
    yield r
    r.close()


def test_the_window_is_eleven_minutes_one_more_than_the_queue_ttl() -> None:
    from orchestrator.config import OrchestratorConfig
    assert REAP_AFTER_S == 11 * 60 > OrchestratorConfig.__dataclass_fields__["queue_ttl_s"].default


def test_a_queued_run_is_failed_only_after_the_window(seed: Seed, reaper: Reaper) -> None:
    fresh, stale = seed.run("queued", 9), seed.run("queued", 12)
    failed = reaper.run_once()
    assert stale in failed and fresh not in failed
    assert seed.state(fresh) == ("queued", None, False)
    assert seed.state(stale) == ("failed", "queue_timeout", True)


def test_other_statuses_are_untouched(seed: Seed, reaper: Reaper) -> None:
    runs = {s: seed.run(s, 120) for s in ("running", "session_boundary", "dreaming", "stopping", "completed",
                                          "capped", "failed", "killed")}
    failed = reaper.run_once()
    assert not set(runs.values()) & set(failed)
    assert seed.state(runs["running"]) == ("running", None, False)
    assert seed.state(runs["failed"]) == ("failed", None, False)          # an already-final row is not rewritten


def test_a_deleted_queued_run_is_still_failed(seed: Seed, reaper: Reaper) -> None:
    run = seed.run("queued", 30, deleted=True)
    assert run in reaper.run_once()
    assert seed.state(run) == ("failed", "queue_timeout", True)


def test_a_starting_run_with_no_recent_ingest_is_failed(seed: Seed, reaper: Reaper) -> None:
    silent, silent_old_event = seed.run("starting", 12), seed.run("starting", 30)
    beating, young = seed.run("starting", 30), seed.run("starting", 9)
    seed.event(silent_old_event, 25)
    seed.event(beating, 2)
    failed = reaper.run_once()
    assert {silent, silent_old_event} <= set(failed) and not {beating, young} & set(failed)
    assert seed.state(silent) == ("failed", "startup_timeout", True)
    assert seed.state(beating) == ("starting", None, False)
    assert seed.state(young) == ("starting", None, False)


def test_two_reapers_in_parallel_fail_each_row_once(seed: Seed) -> None:
    runs = {seed.run("queued", 20) for _ in range(40)}
    a, b = Reaper(DB_URL), Reaper(DB_URL)
    barrier = threading.Barrier(2)

    def go(r: Reaper) -> list[str]:
        barrier.wait(timeout=10)
        return r.run_once()

    try:
        with ThreadPoolExecutor(2) as pool:
            got_a, got_b = pool.map(go, (a, b))
    finally:
        a.close()
        b.close()
    mine = {r for r in got_a + got_b if r in runs}
    assert mine == runs and not set(got_a) & set(got_b)


def test_a_row_locked_by_another_transaction_is_skipped_not_waited_for(seed: Seed, reaper: Reaper) -> None:
    locked, free = seed.run("queued", 20), seed.run("queued", 20)
    holder = psycopg.connect(DB_URL, row_factory=dict_row, autocommit=False)
    holder.execute("select 1 from public.runs where id = %s for update", (locked,))
    result: list[list[str]] = []
    t = threading.Thread(target=lambda: result.append(reaper.run_once()))
    t.start()
    t.join(timeout=8)
    try:
        assert not t.is_alive(), "reaper blocked on a locked row"          # without skip locked it waits here
        assert free in result[0] and locked not in result[0]
        assert seed.state(locked) == ("queued", None, False)
    finally:
        holder.rollback()
        holder.close()
        t.join(timeout=8)
    assert locked in reaper.run_once() and seed.state(locked) == ("failed", "queue_timeout", True)


def test_a_second_pass_does_nothing(seed: Seed, reaper: Reaper) -> None:
    run = seed.run("queued", 20)
    assert run in reaper.run_once()
    assert run not in reaper.run_once()


def test_the_reaper_acts_as_service_role_only(seed: Seed) -> None:
    """The reaper's own login need not be privileged: it assumes service_role for the write."""
    seed.conn.execute("do $$ begin if not exists (select 1 from pg_roles where rolname = 'reaper_probe') then "
                      "create role reaper_probe login password 'probe' nobypassrls; end if; end $$")
    seed.conn.execute("grant service_role to reaper_probe")
    run = seed.run("queued", 20)
    url = DB_URL.replace("postgres:postgres@", "reaper_probe:probe@")
    r = Reaper(url)
    try:
        assert run in r.run_once()
    finally:
        r.close()
        seed.conn.execute("revoke service_role from reaper_probe")
        seed.conn.execute("drop role reaper_probe")


def test_a_scrubbing_log_handler_does_not_break_a_pass(seed: Seed, reaper: Reaper) -> None:
    """Regression: ScrubFilter used to str() every log argument, so `%d` raised once a worker test had installed it."""
    import logging

    from runlog.redaction import ScrubFilter
    handler = logging.StreamHandler()
    handler.addFilter(ScrubFilter())
    logging.getLogger().addHandler(handler)
    try:
        run = seed.run("queued", 20)
        assert run in reaper.run_once()
    finally:
        logging.getLogger().removeHandler(handler)
