# ruff: noqa: F811  (imported fixtures are redefined as test parameters, which is how pytest injects them)
"""The tombstone account lives in auth.users, so GoTrue has to be able to read it: a value it cannot scan (an
'infinity' ban, NULL token columns) broke the admin user list for the whole project."""

import httpx
from accounts.eraser import TOMBSTONE_ID

from conftest import Env
from test_gotrue import PASSWORD, Auth, auth, real, stack  # noqa: F401  (fixtures)

TOMBSTONE_EMAIL = "erased-user@memorylab.invalid"


def test_the_admin_user_list_still_works_with_the_tombstone_present(auth: Auth) -> None:
    r = auth.http.get(f"{auth.base}/admin/users?per_page=1000", headers=auth.secret)
    assert r.status_code == 200, r.text
    assert TOMBSTONE_EMAIL in {u["email"] for u in r.json()["users"]}
    one = auth.http.get(f"{auth.base}/admin/users/{TOMBSTONE_ID}", headers=auth.secret)
    assert one.status_code == 200 and one.json()["email"] == TOMBSTONE_EMAIL


def test_signing_in_as_the_tombstone_fails_like_any_bad_login(auth: Auth) -> None:
    r = auth.login(TOMBSTONE_EMAIL, PASSWORD)
    assert r.status_code == 400 and r.json()["error_code"] == "invalid_credentials"
    wrong = auth.login(auth.email(), PASSWORD)
    assert (r.status_code, r.json()["error_code"], r.json()["msg"]) == (
        wrong.status_code, wrong.json()["error_code"], wrong.json()["msg"])


def test_recovery_and_otp_for_the_tombstone_do_not_error(auth: Auth) -> None:
    assert auth.recover(TOMBSTONE_EMAIL).status_code == 200
    r = httpx.post(f"{auth.base}/otp", headers=auth.pub, json={"email": TOMBSTONE_EMAIL, "create_user": False})
    assert r.status_code < 500, r.text


def test_api_signup_with_the_tombstones_email_still_answers_the_uniform_202(real: Env) -> None:
    for email in (TOMBSTONE_EMAIL, f"{TOMBSTONE_ID[-8:]}@example.test"):
        r = real.client.post("/api/v1/auth/signup", json={"email": email, "password": PASSWORD})
        assert r.status_code == 202, (email, r.status_code, r.text)
    assert real.client.post("/api/v1/auth/signup", json={"email": TOMBSTONE_EMAIL, "password": PASSWORD}).json() == \
        real.client.post("/api/v1/auth/signup", json={"email": "someone-new@example.test", "password": PASSWORD}).json()


def test_a_rapid_repeat_signup_for_a_pending_account_is_still_the_uniform_202(real: Env, auth: Auth) -> None:
    """GoTrue rate-limits a second signup mail for the same address (429 over_email_send_rate_limit). That must not
    turn into a 503, which would tell a caller the address already has an account."""
    email = auth.email()
    first = real.client.post("/api/v1/auth/signup", json={"email": email, "password": PASSWORD})
    again = real.client.post("/api/v1/auth/signup", json={"email": email, "password": PASSWORD})
    assert (first.status_code, again.status_code) == (202, 202) and first.json() == again.json()
    real.admin.execute("delete from auth.users where email = %s", (email,))


def test_the_gateway_maps_only_the_mail_rate_limit_to_exists() -> None:
    import httpx

    from api.auth import AuthUnavailable, GoTrueGateway

    def gateway(status: int, code: str) -> GoTrueGateway:
        g = GoTrueGateway("http://auth.test", "k")
        g._http = httpx.Client(base_url="http://auth.test", transport=httpx.MockTransport(
            lambda req: httpx.Response(status, json={"error_code": code})))
        return g

    assert gateway(429, "over_email_send_rate_limit").signup("a@b.co", "x" * 12) is False
    assert gateway(422, "user_already_exists").signup("a@b.co", "x" * 12) is False
    assert gateway(200, "").signup("a@b.co", "x" * 12) is True
    for status, code in ((429, "over_request_rate_limit"), (500, "unexpected_failure"), (422, "weak_password")):
        with __import__("pytest").raises(AuthUnavailable):
            gateway(status, code).signup("a@b.co", "x" * 12)


def test_the_tombstone_row_has_values_gotrue_can_scan(real: Env) -> None:
    row = real.admin.execute(
        "select banned_until, created_at, updated_at, confirmation_token, recovery_token, email_change_token_new, email_change, "
        "email_change_token_current, phone_change, phone_change_token, reauthentication_token "
        "from auth.users where id = %s", (TOMBSTONE_ID,)).fetchone()
    assert row is not None
    assert row["banned_until"].year == 9999 and row["banned_until"].isoformat().startswith("9999-12-31")
    assert all(v is not None for v in row.values()), row                 # GoTrue rewrites tokens, but never to NULL
