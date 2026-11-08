"""Create-path abuse: parallel creates, refused submits, quota accounting."""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest

from api.auth import User
from api.db import Database
from api.orchestrator_client import UpstreamError
from conftest import API_DB_URL, Env, bearer
from test_runs_create import body, finish


def post(env: Env, uid: str, game: str, **over: Any) -> Any:
    return env.client.post("/api/v1/runs", json=body(env, game, **over), headers=bearer(uid))


def rows(env: Env, uid: str) -> list[tuple[str, str | None]]:
    return [(r["status"], r["end_reason"]) for r in env.admin.execute(
        "select status, end_reason from public.runs where owner_id = %s", (uid,)).fetchall()]


def test_parallel_creates_for_one_user_accept_exactly_one(env: Env) -> None:
    uid, game = env.user(), env.game()
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: post(env, uid, game), range(8)))
    assert sorted(r.status_code for r in results) == [202] + [409] * 7
    assert all(r.json()["code"] == "rate_limited" and "active run" in r.json()["title"]
               for r in results if r.status_code == 409)
    assert rows(env, uid) == [("queued", None)] and len(env.orch.submitted) == 1
    for _ in range(4):                                      # the 7 refused attempts used none of the 5/h quota
        finish(env, uid)
        assert post(env, uid, game).status_code == 202
    finish(env, uid)
    assert post(env, uid, game).status_code == 429


def test_a_second_create_while_a_run_is_active_is_409_before_any_work(env: Env) -> None:
    uid, game = env.user(), env.game()
    env.run(uid, status="running")
    r = post(env, uid, game)
    assert (r.status_code, r.json()["code"]) == (409, "rate_limited")
    assert env.keys.calls == [] and rows(env, uid) == [("running", None)]     # no key call, no new row


def test_two_threads_racing_into_the_create_function_make_one_run(env: Env) -> None:
    uid = env.user()
    db = Database(API_DB_URL, max_size=4)
    barrier = threading.Barrier(2)

    def attempt(_: int) -> str:
        with db.session(User(uid)) as conn:
            barrier.wait(timeout=10)
            try:
                conn.execute("select api_private.create_run_request('{}', %s, 'demo', 'private', 5, 100, 30, 'g', null)",
                             ("c" * 64,))
            except psycopg.errors.RaiseException as exc:
                return str(exc.diag.message_primary)
            return "ok"

    try:
        with ThreadPoolExecutor(2) as pool:
            out = sorted(pool.map(attempt, range(2)))
    finally:
        db.close()
    assert out == ["active_run_exists", "ok"]
    assert len(rows(env, uid)) == 1


@pytest.mark.parametrize("code,status", [("capacity_full", 503), ("breaker_open", 409)])
def test_a_refused_submit_fails_the_row_and_costs_no_quota(env: Env, code: str, status: int) -> None:
    uid, game = env.user(), env.game()
    env.orch.fail_with = UpstreamError(status, code, 30)
    for _ in range(6):                                      # more than the 5/h quota
        r = post(env, uid, game)
        assert (r.status_code, r.json()["code"]) == (status, code)
    assert rows(env, uid) == [("failed", "submit_refused")] * 6
    assert env.admin.execute("select count(*) n from public.runs where owner_id = %s and ended_at is not null",
                             (uid,)).fetchone()["n"] == 6        # type: ignore[index]
    env.orch.fail_with = None
    assert post(env, uid, game).status_code == 202


def test_other_refusals_still_count_against_the_quota(env: Env) -> None:
    uid, game = env.user(), env.game()
    env.orch.fail_with = UpstreamError(429, "rate_limited")
    assert [post(env, uid, game).status_code for _ in range(5)] == [429] * 5
    env.orch.fail_with = None
    r = post(env, uid, game)
    assert r.status_code == 429 and r.json()["title"] == "Too many runs"      # our own limiter, no refund


def test_an_unexpected_submit_failure_does_not_leave_a_queued_row(env: Env) -> None:
    uid, game = env.user(), env.game()

    def boom(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("down")

    env.orch.submit = boom  # type: ignore[method-assign]
    assert post(env, uid, game).status_code == 500
    assert rows(env, uid) == [("failed", "submit_refused")]


def test_the_ip_limit_refusing_a_create_does_not_use_the_users_quota(make_env: Any) -> None:
    from fastapi.testclient import TestClient
    env: Env = make_env(run_create_per_ip_per_hour=1)
    a, b, game = env.user(), env.user(), env.game()
    assert post(env, a, game).status_code == 202                  # takes the one slot for this IP
    for _ in range(5):
        r = post(env, b, game)
        assert (r.status_code, r.json()["title"]) == (429, "Too many runs")
    other_ip = TestClient(env.client.app, client=("203.0.113.9", 4444), raise_server_exceptions=False)
    r = other_ip.post("/api/v1/runs", json=body(env, game), headers=bearer(b))
    assert r.status_code == 202                                   # b's own 5/h quota was untouched


def test_limiter_refund_takes_back_one_hit() -> None:
    from api.limits import RateLimiter
    lim = RateLimiter()
    assert lim.allow_all([("k", 1, 60), ("j", 1, 60)])
    assert not lim.allow_all([("k", 1, 60), ("z", 1, 60)])
    assert lim.count(("z",), 60) == 0                               # a refusal by k left z untouched
    lim.refund("k")
    assert lim.allow_all([("k", 1, 60)])
    lim.refund("never-used")                                        # no-op
