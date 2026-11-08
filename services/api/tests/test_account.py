"""DELETE /me through the API with a fake accounts service (routing, confirmation, session re-check, limits)."""

from typing import Any

import pytest

from api.accounts_client import AccountsError
from conftest import Env, bearer


def delete(env: Env, uid: str, payload: Any = None, headers: dict[str, str] | None = None) -> Any:
    return env.client.request("DELETE", "/api/v1/me", json={"confirm": "DELETE"} if payload is None else payload,
                              headers=headers or bearer(uid))


def test_a_confirmed_delete_erases_exactly_the_callers_account(env: Env) -> None:
    uid, other = env.user(), env.user()
    r = delete(env, uid)
    assert r.status_code == 204 and r.content == b""
    assert env.accounts.erased == [uid] and other not in env.accounts.erased       # the id comes from the verified token


@pytest.mark.parametrize("payload", [{}, {"confirm": "delete"}, {"confirm": ""}, {"confirm": True},
                                     {"confirm": "DELETE", "user_id": "x"}, {"confirm": "DELETE\x00"}, []])
def test_the_confirmation_is_exact_and_nothing_else_is_accepted(env: Env, payload: Any) -> None:
    uid = env.user()
    assert delete(env, uid, payload).status_code == 422
    assert env.accounts.erased == []


def test_a_missing_body_is_422_and_a_missing_token_is_401(env: Env) -> None:
    uid = env.user()
    assert env.client.request("DELETE", "/api/v1/me", headers=bearer(uid)).status_code == 422
    assert env.client.request("DELETE", "/api/v1/me", json={"confirm": "DELETE"}).status_code == 401
    assert env.accounts.erased == []


def test_a_signed_out_session_cannot_erase_even_with_an_unexpired_token(env: Env) -> None:
    uid = env.user()
    headers = bearer(uid)
    env.sessions.revoked.add(headers["authorization"].split()[1])
    assert delete(env, uid, headers=headers).status_code == 401
    assert delete(env, uid).status_code == 204                                    # a fresh token is a different session
    assert env.accounts.erased == [uid]                                            # only the second call got through


def test_the_service_answers_are_mapped_without_detail(env: Env) -> None:
    uid = env.user()
    env.accounts.fail = AccountsError(409, "runs_still_active", 5)
    r = delete(env, uid)
    assert (r.status_code, r.json()["code"], r.headers["retry-after"]) == (409, "runs_still_active", "5")
    env.accounts.fail = AccountsError(502, "internal", 30)
    r = delete(env, uid)
    assert (r.status_code, r.json()["code"]) == (502, "internal") and "secret" not in r.text


def test_account_deletion_attempts_are_limited_to_three_an_hour(env: Env) -> None:
    uid = env.user()
    env.accounts.fail = AccountsError(503, "internal", 30)
    assert [delete(env, uid).status_code for _ in range(3)] == [503] * 3
    r = delete(env, uid)
    assert r.status_code == 429 and r.headers["retry-after"] == "3600"
    other = env.user()
    assert delete(env, other).status_code == 503                                  # per user


def test_the_write_limit_applies_to_account_deletion(make_env: Any) -> None:
    env: Env = make_env(write_limit_per_min=2)
    uid = env.user()
    env.accounts.fail = AccountsError(503, "internal", 30)
    assert [delete(env, uid).status_code for _ in range(3)] == [503, 503, 429]


def test_the_api_package_holds_no_service_role_access() -> None:
    """Erasure needs the service role; the API must reach it only through the accounts service."""
    from pathlib import Path

    import api
    for path in Path(api.__file__).parent.glob("*.py"):
        text = path.read_text()
        assert "service_role" not in text and "SERVICE_KEY" not in text and "SECRET_KEY" not in text, path.name


def test_an_erased_account_cannot_start_a_run_with_a_token_that_is_still_valid(env: Env) -> None:
    from conftest import bearer as _bearer
    from test_runs_create import body
    uid, game = env.user(), env.game()
    env.admin.execute("update public.profiles set erased_at = now() where id = %s", (uid,))
    r = env.client.post("/api/v1/runs", json=body(env, game), headers=_bearer(uid))
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
    assert env.admin.execute("select count(*) n from public.runs where owner_id = %s", (uid,)).fetchone()["n"] == 0  # type: ignore[index]
    assert env.orch.submitted == []
