# ruff: noqa: F811  (imported fixtures are redefined as test parameters, which is how pytest injects them)
"""A token outlives an erase by up to its TTL. Every write with it is a 401 (never a 500 from a foreign key)."""

import uuid
from typing import Any

import pytest
from accounts.eraser import TOMBSTONE_ID

from conftest import Env
from test_fork import CAPS, fork_body, source
from test_gotrue import Auth, auth, real, stack  # noqa: F401  (fixtures)
from test_runs_create import body


def hdr(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# One row per user write route. New routes (experiments, ...) are added here so they are covered too.
WRITES: list[tuple[str, str, Any]] = [
    ("POST", "/api/v1/runs/{run}/report", {"reason": "x"}),
    ("PATCH", "/api/v1/runs/{run}", {"title": "t"}),
    ("POST", "/api/v1/runs/{run}/stop", None),
    ("DELETE", "/api/v1/runs/{run}", None),
    ("POST", "/api/v1/runs", "CREATE"),
    ("POST", "/api/v1/runs/{run}/fork", "FORK"),
    ("POST", "/api/v1/runs/{run}/stream-ticket", None),
    ("POST", "/api/v1/experiments", "EXPERIMENT"),
    ("PATCH", "/api/v1/experiments/{run}", {"title": "t"}),            # {run} is just a uuid here: the 401 comes first
    ("DELETE", "/api/v1/me", {"confirm": "DELETE"}),
]


@pytest.fixture
def erased(real: Env, auth: Auth) -> Any:
    """Account B, erased through the real accounts service; B's access token is still unexpired. A owns a public run
    with a checkpoint that B's token might try to act on."""
    a_mail, b_mail = auth.email(), auth.email()
    a, b = auth.admin_create(a_mail), auth.admin_create(b_mail)
    b_token = auth.login(b_mail).json()["access_token"]
    game = real.game()
    run, cp = source(real, a, game, "public")
    gone = hdr(b_token)
    r = real.client.request("DELETE", "/api/v1/me", json={"confirm": "DELETE"}, headers=gone)
    assert r.status_code == 204, r.text
    yield {"headers": gone, "run": run, "cp": cp, "game": game, "b": b}
    real.admin.execute("delete from public.runs where owner_id = %s", (TOMBSTONE_ID,))
    real.admin.execute("delete from public.run_configs where owner_id = %s", (TOMBSTONE_ID,))


@pytest.mark.parametrize("method,path,payload", WRITES)
def test_every_write_with_a_token_of_an_erased_account_is_a_401(real: Env, erased: Any, method: str, path: str,
                                                                payload: Any) -> None:
    if payload == "CREATE":
        payload = body(real, erased["game"])
    elif payload == "FORK":
        payload = fork_body(erased["cp"])
        assert payload["caps"] == CAPS
    elif payload == "EXPERIMENT":
        payload = {"title": "t", "variable": "preset_ref", "run_ids": [erased["run"]]}
    r = real.client.request(method, path.format(run=erased["run"]), json=payload, headers=erased["headers"])
    assert r.status_code == 401, (method, path, r.status_code, r.text)
    assert r.json()["code"] == "unauthenticated"
    assert real.orch.submitted == []


def test_reads_and_the_profile_of_an_erased_account_are_not_500s(real: Env, erased: Any) -> None:
    assert real.client.get("/api/v1/me", headers=erased["headers"]).status_code == 401
    assert real.client.get("/api/v1/runs", headers=erased["headers"]).status_code in (200, 401)
    assert real.client.get(f"/api/v1/runs/{erased['run']}", headers=erased["headers"]).status_code == 200  # a public run


def test_the_db_write_functions_refuse_a_caller_with_no_profile_or_an_erased_one(real: Env) -> None:
    """Defence in depth under the API: the functions themselves raise account_erased (never a foreign-key error)."""
    import json

    import psycopg

    from conftest import API_DB_URL
    owner, ghost = real.user(), str(uuid.uuid4())                       # `ghost` has no profile at all
    run = real.run(owner, "public")
    real.admin.execute("update public.profiles set erased_at = now() where id = %s", (owner,))
    for who in (ghost, owner):
        for sql, args in (
                ("select api_private.file_report(%s, 'x')", (run,)),
                ("select api_private.create_run_request('{}', %s, 'demo', 'private', 5, 100, 30, 'g', null)", ("c" * 64,))):
            with psycopg.connect(API_DB_URL) as c:
                c.execute("select set_config('request.jwt.claims', %s, true)",
                          (json.dumps({"sub": who, "role": "authenticated"}),))
                c.execute("set local role authenticated")
                with pytest.raises(psycopg.errors.RaiseException, match="account_erased"):
                    c.execute(sql, args)


@pytest.mark.parametrize("method,path,payload", WRITES[:-1])
def test_an_account_marked_erased_but_not_yet_deleted_cannot_write_either(env: Env, method: str, path: str,
                                                                          payload: Any) -> None:
    """The window inside the erase: erased_at is set, the profile row still exists."""
    from conftest import bearer
    owner, ghost, game = env.user(), env.user(), env.game()
    run, cp = source(env, owner, game, "public")
    env.admin.execute("update public.profiles set erased_at = now() where id = %s", (ghost,))
    if payload == "CREATE":
        payload = body(env, game)
    elif payload == "FORK":
        payload = fork_body(cp)
    elif payload == "EXPERIMENT":
        payload = {"title": "t", "variable": "preset_ref", "run_ids": [run]}
    r = env.client.request(method, path.format(run=run), json=payload, headers=bearer(ghost))
    assert r.status_code == 401, (method, path, r.status_code, r.text)
    assert env.orch.submitted == [] and env.keys.calls == []
