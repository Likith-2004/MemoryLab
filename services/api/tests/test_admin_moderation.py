"""Admin reports, unverify, and the audit trail of every admin action."""

import hashlib
import uuid
from typing import Any

import psycopg
import pytest

from api.orchestrator_client import UpstreamError
from conftest import Env, bearer


def audit_rows(env: Env, actor: str) -> list[dict[str, Any]]:
    return env.admin.execute("select actor_id::text actor, action, target, ip_hash, reason from public.audit_log "
                             "where actor_id = %s order by id", (actor,)).fetchall()


def report(env: Env, owner: str, reporter: str, reason: str = "spam", visibility: str = "public") -> tuple[str, str]:
    run = env.run(owner, visibility)
    rid = str(env.admin.execute("insert into public.reports (reporter_id, run_id, reason) values (%s, %s, %s) returning id",
                                (reporter, run, reason)).fetchone()["id"])  # type: ignore[index]
    return rid, run


def resolve(env: Env, who: str, rid: str, status: str = "actioned", reason: str = "confirmed") -> Any:
    return env.client.patch(f"/api/v1/admin/reports/{rid}", json={"status": status, "reason": reason}, headers=bearer(who))


IP = hashlib.sha256(b"test-salt" + b"testclient").hexdigest()[:32]


# ---------------------------------------------------------------- access

ROUTES = [("GET", "/api/v1/admin/reports", None),
          ("PATCH", "/api/v1/admin/reports/{id}", {"status": "actioned", "reason": "x"}),
          ("POST", "/api/v1/admin/leaderboard/{id}/unverify", {"reason": "x"})]


@pytest.mark.parametrize("method,path,payload", ROUTES)
def test_the_new_admin_routes_do_not_exist_for_non_admins_and_need_a_token(env: Env, method: str, path: str,
                                                                          payload: Any) -> None:
    user, owner = env.user(), env.user()
    rid, run = report(env, owner, user)
    url = path.format(id=rid if "reports" in path else run)
    assert env.client.request(method, url, json=payload, headers=bearer(user)).status_code == 404
    assert env.client.request(method, url, json=payload).status_code == 401
    assert env.admin.execute("select status from public.reports where id = %s", (rid,)).fetchone()["status"] == "open"  # type: ignore[index]
    assert audit_rows(env, user) == []


@pytest.mark.parametrize("method,path,payload", ROUTES)
def test_a_revoked_admin_session_is_refused_at_once(env: Env, method: str, path: str, payload: Any) -> None:
    admin, owner = env.user("admin"), env.user()
    rid, run = report(env, owner, owner)
    headers = bearer(admin)
    env.sessions.revoked.add(headers["authorization"].split()[1])
    url = path.format(id=rid if "reports" in path else run)
    r = env.client.request(method, url, json=payload, headers=headers)
    assert r.status_code == 401
    assert env.admin.execute("select status from public.reports where id = %s", (rid,)).fetchone()["status"] == "open"  # type: ignore[index]
    assert audit_rows(env, admin) == []


def test_demoted_admins_lose_the_routes_on_the_next_request(env: Env) -> None:
    admin, owner = env.user("admin"), env.user()
    rid, _ = report(env, owner, owner)
    assert env.client.get("/api/v1/admin/reports", headers=bearer(admin)).status_code == 200
    env.admin.execute("update public.profiles set role = 'user' where id = %s", (admin,))
    assert resolve(env, admin, rid).status_code == 404
    assert env.client.get("/api/v1/admin/reports", headers=bearer(admin)).status_code == 404


# ---------------------------------------------------------------- reports

def test_the_report_queue_lists_open_reports_with_their_run_newest_first(env: Env) -> None:
    admin, owner, reporter = env.user("admin"), env.user(), env.user()
    first, run1 = report(env, owner, reporter, "first")
    second, run2 = report(env, owner, reporter, "second", "private")            # an admin sees private runs too
    body = env.client.get("/api/v1/admin/reports", headers=bearer(admin)).json()
    mine = [r for r in body["reports"] if r["id"] in (first, second)]
    assert [r["id"] for r in mine] == [second, first]
    top = mine[0]
    assert (top["reason"], top["status"], top["run_id"], top["run_owner_id"], top["run_visibility"], top["reporter_id"]) == (
        "second", "open", run2, owner, "private", reporter)
    assert run1 and "resolved_at" in top and top["run_deleted"] is False


def test_the_queue_filters_by_status_and_paginates(env: Env) -> None:
    admin, owner, reporter = env.user("admin"), env.user(), env.user()
    ids = [report(env, owner, reporter, f"r{i}")[0] for i in range(5)]
    assert resolve(env, admin, ids[0], "actioned").status_code == 200 and resolve(env, admin, ids[1], "dismissed").status_code == 200
    h = bearer(admin)

    def listed(status: str) -> set[str]:
        out: set[str] = set()
        before = None
        while True:
            params: dict[str, Any] = {"status": status, "limit": 2}
            if before:
                params["before"] = before
            body = env.client.get("/api/v1/admin/reports", params=params, headers=h).json()
            out |= {r["id"] for r in body["reports"]}
            before = body["next"]
            if not before:
                return out & set(ids)

    assert listed("open") == set(ids[2:]) and listed("actioned") == {ids[0]} and listed("dismissed") == {ids[1]}
    assert listed("all") == set(ids)
    for bad in ({"status": "closed"}, {"limit": 0}, {"limit": 101}, {"before": "garbage"}, {"status": "open\x00"}):
        assert env.client.get("/api/v1/admin/reports", params=bad, headers=h).status_code == 422


def test_resolving_moves_open_to_actioned_or_dismissed_once_and_audits_it(env: Env) -> None:
    admin, owner, reporter = env.user("admin"), env.user(), env.user()
    a, _ = report(env, owner, reporter)
    b, _ = report(env, owner, reporter)
    assert resolve(env, admin, a, "actioned", "removed the run").json() == {"id": a, "status": "actioned"}
    assert resolve(env, admin, b, "dismissed", "not abuse").json() == {"id": b, "status": "dismissed"}
    row = env.admin.execute("select status, resolved_by::text rb, resolved_at is not null done, resolution_reason "
                            "from public.reports where id = %s", (a,)).fetchone()
    assert row == {"status": "actioned", "rb": admin, "done": True, "resolution_reason": "removed the run"}
    assert audit_rows(env, admin) == [
        {"actor": admin, "action": "report.actioned", "target": a, "ip_hash": IP, "reason": "removed the run"},
        {"actor": admin, "action": "report.dismissed", "target": b, "ip_hash": IP, "reason": "not abuse"}]
    for status in ("actioned", "dismissed"):                                    # a closed report is closed, either way
        r = resolve(env, admin, a, status)
        assert (r.status_code, r.json()["code"]) == (409, "report_closed")
    assert len(audit_rows(env, admin)) == 2                                     # refused attempts add no audit rows


@pytest.mark.parametrize("payload", [{"status": "open", "reason": "x"}, {"status": "closed", "reason": "x"}, {"status": "actioned"},
                                     {"reason": "x"}, {"status": "actioned", "reason": ""}, {"status": "actioned", "reason": "x" * 501},
                                     {"status": "actioned", "reason": "a\x00b"}, {"status": "actioned", "reason": "x", "extra": 1},
                                     {"status": ["actioned"], "reason": "x"}])
def test_a_reason_and_a_legal_status_are_required(env: Env, payload: dict[str, Any]) -> None:
    admin, owner = env.user("admin"), env.user()
    rid, _ = report(env, owner, owner)
    r = env.client.patch(f"/api/v1/admin/reports/{rid}", json=payload, headers=bearer(admin))
    assert r.status_code == 422
    assert env.admin.execute("select status from public.reports where id = %s", (rid,)).fetchone()["status"] == "open"  # type: ignore[index]
    assert audit_rows(env, admin) == []


def test_a_blank_reason_is_refused_by_the_database_too(env: Env) -> None:
    admin, owner = env.user("admin"), env.user()
    rid, _ = report(env, owner, owner)
    assert resolve(env, admin, rid, "actioned", "   ").status_code == 422        # whitespace passes the schema, not the function
    h = bearer(admin)
    assert env.client.post(f"/api/v1/admin/leaderboard/{uuid.uuid4()}/unverify", json={"reason": "  "}, headers=h).status_code == 422
    assert env.client.post(f"/api/v1/admin/runs/{uuid.uuid4()}/kill", json={"reason": "  "}, headers=h).status_code == 422
    assert env.client.post("/api/v1/admin/breaker", json={"open": True, "reason": "  "}, headers=h).status_code == 422
    assert audit_rows(env, admin) == [] and env.orch.killed == [] and env.orch.breaker_calls == []
    assert env.admin.execute("select status from public.reports where id = %s", (rid,)).fetchone()["status"] == "open"  # type: ignore[index]


def test_an_unknown_report_is_a_404(env: Env) -> None:
    admin = env.user("admin")
    assert resolve(env, admin, str(uuid.uuid4())).status_code == 404
    assert env.client.patch("/api/v1/admin/reports/not-a-uuid", json={"status": "actioned", "reason": "x"},
                            headers=bearer(admin)).status_code == 404
    assert audit_rows(env, admin) == []


def test_the_report_transitions_are_enforced_by_a_trigger_for_every_writer(env: Env) -> None:
    owner, admin = env.user(), env.user("admin")
    rid, run = report(env, owner, owner)
    sql = "update public.reports set {} where id = %s"
    full = "status = %s, resolved_at = now(), resolved_by = %s, resolution_reason = 'x'"
    for bad in (("status = 'actioned'", ()), ("status = 'open', resolution_reason = 'sneaky'", ()),
                ("status = 'bogus'", ()), ("reason = 'edited'", ()), ("run_id = gen_random_uuid()", ()),
                ("reporter_id = %s", (admin,))):
        with pytest.raises((psycopg.errors.RaiseException, psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation)):
            env.admin.execute(sql.format(bad[0]), (*bad[1], rid))
    env.admin.execute(sql.format(full), ("dismissed", admin, rid))             # open -> dismissed with who and why: allowed
    for status in ("open", "actioned", "dismissed"):
        with pytest.raises(psycopg.errors.RaiseException, match="report_bad_transition|report_resolution_incomplete"):
            env.admin.execute(sql.format(full), (status, admin, rid))
    with pytest.raises(psycopg.errors.RaiseException, match="report_bad_transition"):
        env.admin.execute(sql.format("resolution_reason = 'rewritten'"), (rid,))
    assert run


def test_an_admin_function_refuses_a_non_admin_even_when_called_directly(env: Env) -> None:
    import json

    from conftest import API_DB_URL
    user, owner = env.user(), env.user()
    rid, run = report(env, owner, owner)
    for sql, args in (("select api_private.admin_resolve_report(%s, 'actioned', 'x', 'h')", (rid,)),
                      ("select api_private.admin_unverify(%s, 'x', 'h')", (run,)),
                      ("select api_private.admin_audit('a', 't', 'x', 'h')", ())):
        with psycopg.connect(API_DB_URL) as c:
            c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": user, "role": "authenticated"}),))
            c.execute("set local role authenticated")
            with pytest.raises(psycopg.errors.RaiseException, match="not_admin"):
                c.execute(sql, args)


# ---------------------------------------------------------------- unverify

def verified_run(env: Env, owner: str) -> str:
    run = env.run(owner, "public")
    env.admin.execute("insert into public.run_scores values (%s, 9, 1, 1, 1, 1, 0, 0, true)", (run,))
    return run


def test_unverify_takes_a_run_off_the_leaderboard_and_audits_it(env: Env) -> None:
    admin, owner = env.user("admin"), env.user()
    run = verified_run(env, owner)
    assert run in {r["id"] for r in env.client.get("/api/v1/leaderboard").json()}
    r = env.client.post(f"/api/v1/admin/leaderboard/{run}/unverify", json={"reason": "cheating"}, headers=bearer(admin))
    assert r.status_code == 200 and r.json() == {"run_id": run, "status": "unverified"}
    assert run not in {x["id"] for x in env.client.get("/api/v1/leaderboard").json()}
    row = env.admin.execute("select verified, unverified_by::text ub, unverified_at is not null done, unverified_reason "
                            "from public.run_scores where run_id = %s", (run,)).fetchone()
    assert row == {"verified": False, "ub": admin, "done": True, "unverified_reason": "cheating"}
    assert audit_rows(env, admin) == [{"actor": admin, "action": "leaderboard.unverify", "target": run, "ip_hash": IP,
                                       "reason": "cheating"}]
    # the benchmark lock is gone: the owner may now make it private
    assert env.client.patch(f"/api/v1/runs/{run}", json={"visibility": "private"}, headers=bearer(owner)).status_code == 200


def test_unverify_needs_a_reason_a_verified_run_and_an_existing_run(env: Env) -> None:
    admin, owner = env.user("admin"), env.user()
    run = verified_run(env, owner)
    plain = env.run(owner, "public")
    env.admin.execute("insert into public.run_scores values (%s, 1, 1, 1, 1, 1, 0, 0, false)", (plain,))
    h = bearer(admin)
    for payload in ({}, {"reason": ""}, {"reason": "x" * 501}, {"reason": "a\x00b"}, {"reason": "x", "extra": 1}):
        assert env.client.post(f"/api/v1/admin/leaderboard/{run}/unverify", json=payload, headers=h).status_code == 422
    nv = env.client.post(f"/api/v1/admin/leaderboard/{plain}/unverify", json={"reason": "x"}, headers=h)
    assert (nv.status_code, nv.json()["code"]) == (409, "not_verified")
    assert env.client.post(f"/api/v1/admin/leaderboard/{env.run(owner)}/unverify", json={"reason": "x"}, headers=h).status_code == 404
    assert env.client.post(f"/api/v1/admin/leaderboard/{uuid.uuid4()}/unverify", json={"reason": "x"}, headers=h).status_code == 404
    assert env.client.post("/api/v1/admin/leaderboard/not-a-uuid/unverify", json={"reason": "x"}, headers=h).status_code == 404
    assert env.admin.execute("select verified from public.run_scores where run_id = %s", (run,)).fetchone()["verified"] is True  # type: ignore[index]
    assert audit_rows(env, admin) == []
    assert env.client.post(f"/api/v1/admin/leaderboard/{run}/unverify", json={"reason": "ok"}, headers=h).status_code == 200
    again = env.client.post(f"/api/v1/admin/leaderboard/{run}/unverify", json={"reason": "again"}, headers=h)
    assert (again.status_code, again.json()["code"]) == (409, "not_verified")
    assert len(audit_rows(env, admin)) == 1


def test_an_unverified_run_cannot_be_re_verified_by_a_later_score_write(env: Env) -> None:
    admin, owner = env.user("admin"), env.user()
    run = verified_run(env, owner)
    env.client.post(f"/api/v1/admin/leaderboard/{run}/unverify", json={"reason": "cheating"}, headers=bearer(admin))
    with pytest.raises(psycopg.errors.RaiseException, match="unverified_locked"):
        env.admin.execute("update public.run_scores set verified = true where run_id = %s", (run,))
    env.admin.execute("update public.run_scores set progress_score = 10 where run_id = %s", (run,))      # other edits are fine


# ---------------------------------------------------------------- every admin action is audited

def test_kill_and_breaker_write_audit_rows_with_reasons_and_hashed_addresses(env: Env) -> None:
    admin, run = env.user("admin"), str(uuid.uuid4())
    h = bearer(admin)
    assert env.client.post(f"/api/v1/admin/runs/{run}/kill", json={"reason": "abuse"}, headers=h).status_code == 200
    assert env.client.post("/api/v1/admin/breaker", json={"open": True, "reason": "provider outage"}, headers=h).status_code == 200
    assert env.client.post("/api/v1/admin/breaker", json={"open": False, "reason": "provider is back"}, headers=h).status_code == 200
    rows = audit_rows(env, admin)
    assert rows == [{"actor": admin, "action": "run.kill", "target": run, "ip_hash": IP, "reason": "abuse"},
                    {"actor": admin, "action": "breaker.open", "target": "global", "ip_hash": IP, "reason": "provider outage"},
                    {"actor": admin, "action": "breaker.close", "target": "global", "ip_hash": IP, "reason": "provider is back"}]
    assert all("testclient" not in str(v) for r in rows for v in r.values())          # the raw address is never stored


def test_a_reason_is_required_for_every_admin_action_including_closing_the_breaker(env: Env) -> None:
    admin, run = env.user("admin"), str(uuid.uuid4())
    h = bearer(admin)
    for body in ({"open": False}, {"open": False, "reason": ""}, {"open": True}, {"reason": "x"}, {"open": "yes", "reason": "x"}):
        assert env.client.post("/api/v1/admin/breaker", json=body, headers=h).status_code == 422
    for body in ({}, {"reason": ""}):
        assert env.client.post(f"/api/v1/admin/runs/{run}/kill", json=body, headers=h).status_code == 422
    assert env.orch.breaker_calls == [] and env.orch.killed == [] and audit_rows(env, admin) == []


def test_an_attempt_that_fails_downstream_is_still_on_record(env: Env) -> None:
    admin, run = env.user("admin"), str(uuid.uuid4())
    env.orch.kill_fail = UpstreamError(503, "capacity_full", 30)
    r = env.client.post(f"/api/v1/admin/runs/{run}/kill", json={"reason": "abuse"}, headers=bearer(admin))
    assert r.status_code == 503
    assert [a["action"] for a in audit_rows(env, admin)] == ["run.kill"]


def test_an_action_whose_audit_row_cannot_be_written_does_not_happen(env: Env) -> None:
    admin, run = env.user("admin"), str(uuid.uuid4())
    env.admin.execute("alter table public.audit_log add constraint audit_broken check (action <> 'run.kill') not valid")
    try:
        r = env.client.post(f"/api/v1/admin/runs/{run}/kill", json={"reason": "abuse"}, headers=bearer(admin))
        assert r.status_code == 500 and env.orch.killed == []                   # failed closed: nothing was killed
    finally:
        env.admin.execute("alter table public.audit_log drop constraint audit_broken")


def test_admin_writes_are_limited_per_user(make_env: Any) -> None:
    env: Env = make_env(write_limit_per_min=3)
    admin, owner = env.user("admin"), env.user()
    ids = [report(env, owner, owner)[0] for _ in range(5)]
    codes = [resolve(env, admin, i).status_code for i in ids]
    assert codes[:3] == [200] * 3 and codes[3:] == [429, 429]


def test_the_audit_log_is_readable_by_admins_only(env: Env) -> None:
    import json

    from conftest import API_DB_URL
    admin, user = env.user("admin"), env.user()
    env.client.post(f"/api/v1/admin/runs/{uuid.uuid4()}/kill", json={"reason": "r"}, headers=bearer(admin))
    for who, expect in ((user, 0), (admin, 1)):
        with psycopg.connect(API_DB_URL) as c:
            c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": who, "role": "authenticated"}),))
            c.execute("set local role authenticated")
            n = c.execute("select count(*) from public.audit_log where actor_id = %s", (admin,)).fetchone()[0]  # type: ignore[index]
            assert (n >= 1) == bool(expect)
