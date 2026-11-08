# ruff: noqa: F811  (imported fixtures are redefined as test parameters, which is how pytest injects them)
"""DELETE /me end to end: the real accounts service on the real database, real GoTrue for tokens, sessions and the
final user deletion."""

from typing import Any

import pytest
from accounts.eraser import TOMBSTONE_ID

from conftest import CANARY_KEY, Env
from test_gotrue import Auth, auth, real, real_factory, stack  # noqa: F401  (fixtures)
from test_runs_create import body


def hdr(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def dump(env: Env) -> dict[str, str]:
    tables = [r["relname"] for r in env.admin.execute(
        "select relname from pg_class where relnamespace = 'public'::regnamespace and relkind = 'r'").fetchall()]
    return {t: env.admin.execute(f"select coalesce(string_agg(x::text, ' '), '') s from public.{t} x").fetchone()["s"]  # type: ignore[index]
            for t in tables}


@pytest.fixture
def cleanup(real: Env) -> Any:
    runs: list[str] = []
    yield runs
    real.admin.execute("delete from public.runs where id = any(%s)", (runs,))     # tombstone-owned leftovers
    real.admin.execute("delete from public.run_configs where owner_id = %s and id not in "
                       "(select config_id from public.runs)", (TOMBSTONE_ID,))


def test_deleting_an_account_end_to_end(real: Env, auth: Auth, cleanup: list[str]) -> None:
    a_mail, b_mail, c_mail = auth.email(), auth.email(), auth.email()
    a, b = auth.admin_create(a_mail), auth.admin_create(b_mail)
    a_tokens, b_tokens = auth.login(a_mail).json(), auth.login(b_mail).json()
    game = real.game()
    real.orch.fail_with = None

    # A starts a run through the API with the canary key (it stays queued: the orchestrator here is a fake)
    created = real.client.post("/api/v1/runs", json=body(real, game), headers=hdr(a_tokens["access_token"]))
    assert created.status_code == 202
    active = created.json()["run_id"]
    kept, private, unverified = (real.run(a, "public"), real.run(a, "private"), real.run(a, "public"))
    cleanup += [active, kept, private, unverified]
    real.admin.execute("update public.runs set title = 'mine' where id = any(%s)", ([kept, private],))
    real.admin.execute("insert into public.run_scores values (%s, 5, 1, 1, 1, 1, 0, 0, true)", (kept,))
    b_run = real.run(b, "public")
    real.admin.execute("insert into public.run_scores values (%s, 5, 1, 1, 1, 1, 0, 0, true)", (b_run,))
    real.admin.execute("update public.runs set title = 'bs' where id = %s", (b_run,))
    real.admin.execute("insert into public.experiments (owner_id, title, variable) values (%s, 'e', 'v')", (a,))

    r = real.client.request("DELETE", "/api/v1/me", json={"confirm": "DELETE"}, headers=hdr(a_tokens["access_token"]))
    assert r.status_code == 204, r.text

    # the active run was killed first; the verified public run survived, anonymised; everything else is deleted
    rows = {str(x["id"]): x for x in real.admin.execute(
        "select id, owner_id::text owner, title, deleted_at is not null deleted, status from public.runs "
        "where id = any(%s)", ([active, kept, private, unverified],)).fetchall()}
    assert rows[active]["status"] == "killed" and rows[active]["deleted"]
    assert (rows[kept]["owner"], rows[kept]["title"], rows[kept]["deleted"]) == (TOMBSTONE_ID, None, False)
    assert rows[private]["deleted"] and rows[unverified]["deleted"] and rows[private]["title"] is None
    # another user's data is untouched
    row = real.admin.execute("select owner_id::text owner, title, deleted_at from public.runs where id = %s",
                             (b_run,)).fetchone()
    assert row == {"owner": b, "title": "bs", "deleted_at": None}
    assert real.client.get("/api/v1/me", headers=hdr(b_tokens["access_token"])).status_code == 200
    # the anonymised run is still public and on the leaderboard
    assert real.client.get(f"/api/v1/runs/{kept}").status_code == 200
    assert kept in {x["id"] for x in real.client.get("/api/v1/leaderboard").json()}
    assert real.client.get(f"/api/v1/runs/{private}", headers=hdr(b_tokens["access_token"])).status_code == 404

    # the account is gone: no sign-in, no refresh, and the old token no longer works for another delete
    assert auth.login(a_mail).status_code == 400
    assert auth.refresh(a_tokens["refresh_token"]).status_code in (400, 401)
    again = real.client.request("DELETE", "/api/v1/me", json={"confirm": "DELETE"}, headers=hdr(a_tokens["access_token"]))
    assert again.status_code == 401
    assert real.admin.execute("select count(*) n from auth.users where id = %s", (a,)).fetchone()["n"] == 0  # type: ignore[index]

    # no trace of the canary key or of A's id in any public table
    for table, text in dump(real).items():
        assert CANARY_KEY not in text, table
        assert a not in text, table

    # the tombstone holds no active run, and a fresh account can start a run: no stale lock survived
    n = real.admin.execute("select count(*) n from public.runs where owner_id = %s and status = any(%s)",
                           (TOMBSTONE_ID, ["queued", "starting", "running", "session_boundary", "dreaming",
                                           "stopping"])).fetchone()["n"]
    assert n == 0
    c = auth.admin_create(c_mail)
    c_tokens = auth.login(c_mail).json()
    fresh = real.client.post("/api/v1/runs", json=body(real, game), headers=hdr(c_tokens["access_token"]))
    assert fresh.status_code == 202
    cleanup.append(fresh.json()["run_id"])
    assert c


def test_an_account_whose_run_will_not_die_is_not_erased(real: Env, auth: Auth, cleanup: list[str]) -> None:
    """If the kill does not take effect the API answers 409 and nothing is changed."""
    mail = auth.email()
    a = auth.admin_create(mail)
    tokens = auth.login(mail).json()
    run = real.run(a, "public", status="running")
    cleanup.append(run)
    real.admin.execute("insert into public.run_scores values (%s, 5, 1, 1, 1, 1, 0, 0, true)", (run,))
    real.kill_works = False  # type: ignore[attr-defined]
    r = real.client.request("DELETE", "/api/v1/me", json={"confirm": "DELETE"}, headers=hdr(tokens["access_token"]))
    assert (r.status_code, r.json()["code"]) == (409, "runs_still_active")
    row = real.admin.execute("select owner_id::text o, title from public.runs where id = %s", (run,)).fetchone()
    assert row["o"] == a                                                          # type: ignore[index]
    assert real.client.get("/api/v1/me", headers=hdr(tokens["access_token"])).status_code == 200
    assert auth.login(mail).status_code == 200                                    # still a working account
    real.kill_works = True  # type: ignore[attr-defined]
    assert real.client.request("DELETE", "/api/v1/me", json={"confirm": "DELETE"},
                               headers=hdr(tokens["access_token"])).status_code == 204     # a retry succeeds
