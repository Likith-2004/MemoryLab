"""Auth tests against the real local GoTrue (Supabase Auth): token verification with the real JWKS,
email confirmation, enumeration, sign-out and refresh revocation, admin session re-check."""

import os
import secrets
import statistics
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from api.auth import GoTrueGateway, GoTrueSessionChecker, JwtVerifier, http_jwks_fetcher
from conftest import API_DB_URL, Env

INFRA = Path(__file__).resolve().parents[3] / "infra"
PASSWORD = "pw-" + secrets.token_urlsafe(18)             # per-run throwaway for local test users


def _status() -> dict[str, str]:
    env = {k: os.environ[k] for k in ("API_URL", "PUBLISHABLE_KEY", "SECRET_KEY", "MAILPIT_URL") if k in os.environ}
    if len(env) == 4:
        return env
    try:
        out = subprocess.run([str(Path.home() / ".local/bin/supabase"), "status", "--workdir", str(INFRA), "-o", "env"],
                             capture_output=True, text=True, timeout=30, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return {}
    return {k: v.strip('"') for k, _, v in (line.partition("=") for line in out.splitlines())}


@pytest.fixture(scope="module")
def stack() -> dict[str, str]:
    st = _status()
    try:
        httpx.get(st["API_URL"] + "/auth/v1/health", headers={"apikey": st["PUBLISHABLE_KEY"]}, timeout=3).raise_for_status()
        httpx.get(st["MAILPIT_URL"] + "/api/v1/messages", timeout=3).raise_for_status()
    except (KeyError, httpx.HTTPError):
        if os.environ.get("MEMORYLAB_REQUIRE_DB") == "1":
            pytest.fail("local Supabase with mail (mailpit) is required for the GoTrue tests")
        pytest.skip("local Supabase not running")
    return st


class Auth:
    def __init__(self, st: dict[str, str]) -> None:
        self.base = st["API_URL"] + "/auth/v1"
        self.mail = st["MAILPIT_URL"]
        self.pub = {"apikey": st["PUBLISHABLE_KEY"]}
        self.secret = {"apikey": st["SECRET_KEY"], "authorization": "Bearer " + st["SECRET_KEY"]}
        self.http = httpx.Client(timeout=15)
        self.created: list[str] = []

    def email(self) -> str:
        return f"{uuid.uuid4().hex[:12]}@example.test"

    def admin_create(self, email: str, confirmed: bool = True) -> str:
        r = self.http.post(f"{self.base}/admin/users", headers=self.secret,
                           json={"email": email, "password": PASSWORD, "email_confirm": confirmed})
        r.raise_for_status()
        self.created.append(r.json()["id"])
        return str(r.json()["id"])

    def login(self, email: str, password: str = PASSWORD) -> httpx.Response:
        return self.http.post(f"{self.base}/token?grant_type=password", headers=self.pub,
                              json={"email": email, "password": password})

    def signup(self, email: str) -> httpx.Response:
        return self.http.post(f"{self.base}/signup", headers=self.pub, json={"email": email, "password": PASSWORD})

    def recover(self, email: str) -> httpx.Response:
        return self.http.post(f"{self.base}/recover", headers=self.pub, json={"email": email})

    def logout(self, token: str, scope: str = "local") -> httpx.Response:
        return self.http.post(f"{self.base}/logout?scope={scope}", headers={**self.pub, "authorization": f"Bearer {token}"})

    def refresh(self, refresh_token: str) -> httpx.Response:
        return self.http.post(f"{self.base}/token?grant_type=refresh_token", headers=self.pub,
                              json={"refresh_token": refresh_token})

    def mails_to(self, email: str) -> list[dict[str, Any]]:
        msgs = self.http.get(f"{self.mail}/api/v1/messages").json()["messages"]
        return [m for m in msgs if any(t["Address"] == email for t in m["To"])]


@pytest.fixture
def auth(stack: dict[str, str]) -> Iterator[Auth]:
    a = Auth(stack)
    yield a
    for uid in a.created:
        a.http.delete(f"{a.base}/admin/users/{uid}", headers=a.secret)




@pytest.fixture
def real(make_env: Any, stack: dict[str, str], tmp_path: Any) -> Env:
    """The API wired to the real JWKS and the real session checker (no fakes for identity)."""
    from fastapi.testclient import TestClient
    from runlog.store import LocalObjectStore

    from api.app import create_app
    from api.db import Database
    from conftest import FakeKeys, FakeOrchestrator, make_settings

    base: Env = make_env()
    settings = make_settings()
    verifier = JwtVerifier(http_jwks_fetcher(stack["API_URL"] + "/auth/v1/.well-known/jwks.json"),
                           audience="authenticated", issuer=None, algorithms=settings.allowed_algorithms)
    db = Database(API_DB_URL, max_size=3)
    orch = FakeOrchestrator()
    app = create_app(settings, verifier=verifier, db=db, orchestrator=orch, key_validator=FakeKeys(),
                     session_checker=GoTrueSessionChecker(stack["API_URL"] + "/auth/v1", stack["PUBLISHABLE_KEY"]),
                     auth_gateway=GoTrueGateway(stack["API_URL"] + "/auth/v1", stack["PUBLISHABLE_KEY"]),
                     store=LocalObjectStore(tmp_path / "s2"), ip_salt=b"x")
    base.client = TestClient(app, raise_server_exceptions=False)
    base.orch = orch
    return base


def test_a_real_gotrue_token_passes_the_api_and_identifies_the_user(real: Env, auth: Auth) -> None:
    email = auth.email()
    uid = auth.admin_create(email)
    tokens = auth.login(email).json()
    r = real.client.get("/api/v1/me", headers={"authorization": f"Bearer {tokens['access_token']}"})
    assert r.status_code == 200 and r.json()["id"] == uid          # profile row came from the auth.users trigger


def test_the_real_access_token_ttl_is_15_minutes(auth: Auth) -> None:
    email = auth.email()
    auth.admin_create(email)
    assert auth.login(email).json()["expires_in"] == 900


def test_email_confirmation_is_required_before_sign_in(auth: Auth) -> None:
    email = auth.email()
    auth.admin_create(email, confirmed=False)
    r = auth.login(email)
    assert r.status_code == 400 and r.json()["error_code"] == "email_not_confirmed"


def shape(resp: httpx.Response) -> tuple[int, tuple[str, ...]]:
    try:
        keys = tuple(sorted(resp.json())) if isinstance(resp.json(), dict) else ()
    except ValueError:
        keys = ()
    return resp.status_code, keys


def test_login_failure_is_identical_for_unknown_email_and_wrong_password(auth: Auth) -> None:
    email = auth.email()
    auth.admin_create(email)
    wrong_pw, unknown = auth.login(email, "definitely-wrong-password"), auth.login(auth.email(), "definitely-wrong-password")
    assert shape(wrong_pw) == shape(unknown) and wrong_pw.status_code == 400
    assert wrong_pw.json()["error_code"] == unknown.json()["error_code"] == "invalid_credentials"
    assert wrong_pw.json()["msg"] == unknown.json()["msg"]


def test_password_reset_answers_identically_for_known_and_unknown_emails(auth: Auth) -> None:
    known = auth.email()
    auth.admin_create(known)
    a, b = auth.recover(known), auth.recover(auth.email())
    assert shape(a) == shape(b) and a.status_code == 200 and a.json() == b.json()


@pytest.mark.xfail(strict=True, reason="GoTrue itself answers 422 user_already_exists for a confirmed email "
                   "(enumeration). The web client must use POST /api/v1/auth/signup and the gateway must block "
                   "/auth/v1/signup (DECISIONS M6-4). strict: this flips to a failure if GoTrue ever fixes it.")
def test_raw_gotrue_signup_does_not_leak_existence(auth: Auth) -> None:
    existing, fresh = auth.email(), auth.email()
    auth.admin_create(existing)
    assert auth.signup(existing).status_code == auth.signup(fresh).status_code


def test_api_signup_is_indistinguishable_for_new_and_existing_emails(real: Env, auth: Auth) -> None:
    existing, fresh = auth.email(), auth.email()
    auth.admin_create(existing)
    body = {"password": PASSWORD}
    a = real.client.post("/api/v1/auth/signup", json={"email": existing, **body})
    b = real.client.post("/api/v1/auth/signup", json={"email": fresh, **body})
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (202, a.json())
    assert dict(a.headers).keys() - {"date"} == dict(b.headers).keys() - {"date"}
    uid = real.admin.execute("select id from auth.users where email = %s", (fresh,)).fetchone()["id"]  # type: ignore[index]
    auth.created.append(str(uid))
    # the existing account got a recovery email instead of an error; the new one got a confirmation email
    time.sleep(1.0)
    assert auth.mails_to(existing) and auth.mails_to(fresh)


def test_api_signup_timing_does_not_separate_new_from_existing(real: Env, auth: Auth) -> None:
    """Coarse guard, not a proof: medians of 5 samples each must be within 2x and 250 ms of each other."""
    existing = auth.email()
    auth.admin_create(existing)
    fresh: list[str] = []

    def sample(email: str) -> float:
        t = time.perf_counter()
        real.client.post("/api/v1/auth/signup", json={"email": email, "password": PASSWORD})
        return time.perf_counter() - t

    k = statistics.median(sample(existing) for _ in range(5))
    u = statistics.median(sample(fresh.append(e) or e) for e in (auth.email() for _ in range(5)))
    real.admin.execute("delete from auth.users where email = any(%s)", (fresh,))
    assert max(k, u) < 2 * min(k, u) + 0.25, f"existing={k:.3f}s new={u:.3f}s"


def test_login_timing_does_not_separate_known_from_unknown_accounts(auth: Auth) -> None:
    """Coarse guard, not a proof: medians of 12 samples each must be within 2x and 150 ms of each other."""
    known = auth.email()
    auth.admin_create(known)

    def sample(email: str) -> float:
        t = time.perf_counter()
        auth.login(email, "definitely-wrong-password")
        return time.perf_counter() - t

    sample(known)                                                          # warm up
    k = statistics.median(sample(known) for _ in range(12))
    u = statistics.median(sample(auth.email()) for _ in range(12))
    assert max(k, u) < 2 * min(k, u) + 0.15, f"known={k:.3f}s unknown={u:.3f}s"


def test_sign_out_revokes_the_refresh_token_and_api_access_ends_by_ttl(real: Env, auth: Auth) -> None:
    email = auth.email()
    auth.admin_create(email)
    tokens = auth.login(email).json()
    assert auth.logout(tokens["access_token"]).status_code == 204
    r = auth.refresh(tokens["refresh_token"])
    assert r.status_code in (400, 401) and "access_token" not in r.json()     # refresh no longer works
    # The access JWT itself is stateless: it dies by exp (<= 15 min). Documented, not hidden:
    me = real.client.get("/api/v1/me", headers={"authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200


def test_global_sign_out_kills_the_second_device(auth: Auth) -> None:
    email = auth.email()
    auth.admin_create(email)
    one, two = auth.login(email).json(), auth.login(email).json()
    assert auth.logout(one["access_token"], scope="global").status_code == 204
    assert auth.refresh(two["refresh_token"]).status_code in (400, 401)


def test_a_signed_out_admin_loses_admin_routes_at_once(real: Env, auth: Auth) -> None:
    email = auth.email()
    uid = auth.admin_create(email)
    real.admin.execute("update public.profiles set role = 'admin' where id = %s", (uid,))
    tokens = auth.login(email).json()
    h = {"authorization": f"Bearer {tokens['access_token']}"}
    assert real.client.get("/api/v1/admin/runs", headers=h).status_code == 200
    auth.logout(tokens["access_token"])
    assert real.client.get("/api/v1/admin/runs", headers=h).status_code == 401   # not at access-token expiry
    assert real.client.get("/api/v1/me", headers=h).status_code == 200          # ordinary routes: bounded by TTL


def test_the_real_anon_and_service_keys_are_not_user_sessions(real: Env, stack: dict[str, str]) -> None:
    for key in (stack["PUBLISHABLE_KEY"], stack["SECRET_KEY"]):
        assert real.client.get("/api/v1/me", headers={"authorization": f"Bearer {key}"}).status_code == 401
