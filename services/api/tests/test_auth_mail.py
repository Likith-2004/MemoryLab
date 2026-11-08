# ruff: noqa: F811  (imported fixtures are redefined as test parameters, which is how pytest injects them)
"""POST /auth/magic-link and /auth/recover: one answer for every address (GoTrue's own /otp tells known from unknown)."""

import threading
import time
from typing import Any

import httpx
import pytest

from api.mailjobs import MailJobs, address_tag
from conftest import Env
from test_gotrue import Auth, auth, real, real_factory, stack  # noqa: F401  (fixtures)

PATHS = ["/api/v1/auth/magic-link", "/api/v1/auth/recover"]


@pytest.mark.xfail(strict=True, reason="GoTrue's /otp answers 422 otp_disabled for an unknown address and 200 for a known one "
                   "(enumeration). The CLI and the web app use POST /api/v1/auth/magic-link; the gateway must block /auth/v1/otp "
                   "next to /auth/v1/signup (DECISIONS M6-4, HANDOFF). strict: flips to a failure if GoTrue ever fixes it.")
def test_raw_gotrue_otp_does_not_leak_existence(auth: Auth) -> None:
    existing, unknown = auth.email(), auth.email()
    auth.admin_create(existing)
    body: dict[str, Any] = {"create_user": False}
    a = httpx.post(f"{auth.base}/otp", headers=auth.pub, json={"email": existing, **body})
    b = httpx.post(f"{auth.base}/otp", headers=auth.pub, json={"email": unknown, **body})
    assert a.status_code == b.status_code


@pytest.mark.parametrize("path", PATHS)
def test_known_unknown_and_rate_limited_addresses_get_the_same_answer(real: Env, auth: Auth, path: str) -> None:
    known, unknown = auth.email(), auth.email()
    auth.admin_create(known)
    answers = [real.client.post(path, json={"email": e}) for e in (known, unknown, known, known)]   # the repeats hit GoTrue's mail limit
    assert {a.status_code for a in answers} == {202}
    assert len({a.text for a in answers}) == 1 and "If an account exists" in answers[0].text
    assert len({frozenset(dict(a.headers).keys() - {"date"}) for a in answers}) == 1


def test_a_magic_link_is_really_sent_to_a_known_address_and_to_nobody_else(real: Env, auth: Auth) -> None:
    known, unknown = auth.email(), auth.email()
    auth.admin_create(known)
    assert real.client.post(PATHS[0], json={"email": known}).status_code == 202
    assert real.client.post(PATHS[0], json={"email": unknown}).status_code == 202
    for _ in range(40):
        if auth.mails_to(known):
            break
        time.sleep(0.25)
    assert auth.mails_to(known) and auth.mails_to(unknown) == []
    assert real.admin.execute("select count(*) n from auth.users where email = %s", (unknown,)).fetchone()["n"] == 0  # type: ignore[index]


@pytest.mark.parametrize("path", PATHS)
def test_the_address_is_checked_only_for_shape_and_never_echoed(env: Env, path: str) -> None:
    for bad in ({}, {"email": ""}, {"email": "not-an-email"}, {"email": "a\x00b@example.test"}, {"email": "x" * 300 + "@e.co"},
                {"email": "a@example.test", "extra": 1}):
        assert env.client.post(path, json=bad).status_code == 422, bad
    r = env.client.post(path, json={"email": "Person@Example.test"})
    assert r.status_code == 202 and "person@example.test" not in r.text.lower()
    assert (env.gateway.magic if "magic" in path else env.gateway.recovered) == ["person@example.test"]   # lower-cased


@pytest.mark.parametrize("path", PATHS)
def test_the_limits_apply_per_ip_and_per_address_to_every_address_alike(make_env: Any, path: str) -> None:
    env: Env = make_env(signup_per_ip_per_hour=3)
    codes = [env.client.post(path, json={"email": f"a{i}@example.test"}).status_code for i in range(5)]
    assert codes == [202, 202, 202, 429, 429]                                    # per IP, whoever the addresses are
    env2: Env = make_env(signup_per_ip_per_hour=100)
    codes = [env2.client.post(path, json={"email": "same@example.test"}).status_code for _ in range(7)]
    assert codes == [202] * 5 + [429, 429]                                        # per address (mailbox flooding)
    r = env2.client.post(path, json={"email": "same@example.test"})
    assert r.headers["retry-after"] == "3600" and r.json()["code"] == "rate_limited"


@pytest.mark.parametrize("path", PATHS)
def test_an_unavailable_auth_service_is_not_visible_to_the_caller(env: Env, path: str) -> None:
    env.gateway.down = True
    r = env.client.post(path, json={"email": "a@example.test"})
    assert r.status_code == 202 and "If an account exists" in r.text


def test_the_gateway_maps_gotrue_answers_to_one_outcome() -> None:
    from api.auth import AuthUnavailable, GoTrueGateway

    def gateway(status: int, code: str = "") -> GoTrueGateway:
        g = GoTrueGateway("http://auth.test", "k")
        g._http = httpx.Client(base_url="http://auth.test", transport=httpx.MockTransport(
            lambda req: httpx.Response(status, json={"error_code": code})))
        return g

    for status, code in ((200, ""), (422, "otp_disabled"), (429, "over_email_send_rate_limit")):
        gateway(status, code).magic_link("a@b.co")
        gateway(status, code).recover("a@b.co")
    for status in (500, 502, 503):
        with pytest.raises(AuthUnavailable):
            gateway(status).magic_link("a@b.co")
        with pytest.raises(AuthUnavailable):
            gateway(status).recover("a@b.co")

    class Refused(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

    g = GoTrueGateway("http://auth.test", "k")
    g._http = httpx.Client(base_url="http://auth.test", transport=Refused())
    with pytest.raises(AuthUnavailable):
        g.magic_link("a@b.co")


# ---------------------------------------------------------------- timing: the answer never waits for GoTrue

class SlowGateway:
    """Known addresses take 300 ms (GoTrue sends the mail synchronously), unknown ones return at once."""

    def __init__(self) -> None:
        self.known = {"known@example.test"}
        self.calls: list[tuple[str, str]] = []
        self.done = threading.Event()

    def _work(self, kind: str, email: str) -> None:
        if email in self.known:
            time.sleep(0.3)
        self.calls.append((kind, email))
        if len(self.calls) >= self.expect:
            self.done.set()

    expect = 1

    def signup(self, email: str, password: str) -> bool:
        self._work("signup", email)
        return email not in self.known

    def recover(self, email: str) -> None:
        self._work("recover", email)

    def magic_link(self, email: str) -> None:
        self._work("magic", email)


def timed_client(make_env: Any) -> tuple[Env, SlowGateway, MailJobs]:
    from api.mailjobs import MailJobs
    env: Env = make_env()
    gw, jobs = SlowGateway(), MailJobs()
    from fastapi.testclient import TestClient

    from api.app import create_app
    app = create_app(env.settings, verifier=None, db=None, orchestrator=env.orch, key_validator=env.keys,  # type: ignore[arg-type]
                     session_checker=env.sessions, auth_gateway=gw, accounts=env.accounts, mail_jobs=jobs, ip_salt=b"t")
    return Env(env.admin, TestClient(app, raise_server_exceptions=False), env.orch, env.keys, env.sessions, gw,  # type: ignore[arg-type]
               env.accounts, env.store, env.signer, env.clock, env.settings, [], []), gw, jobs


@pytest.mark.parametrize("path,body", [("/api/v1/auth/magic-link", {}), ("/api/v1/auth/recover", {}),
                                       ("/api/v1/auth/signup", {"password": "a-long-password-1"})])
def test_response_time_does_not_depend_on_whether_the_address_exists(make_env: Any, path: str, body: dict[str, str]) -> None:
    env, gw, jobs = timed_client(make_env)
    try:
        gw.expect = 2 + (1 if "signup" in path else 0)               # signup of a known address recovers as well
        durations, answers = {}, {}
        for label, email in (("known", "known@example.test"), ("unknown", "unknown@example.test")):
            t = time.perf_counter()
            r = env.client.post(path, json={"email": email, **body})
            durations[label] = time.perf_counter() - t
            answers[label] = (r.status_code, r.text)
        assert durations["known"] < 0.05 and durations["unknown"] < 0.05, durations
        assert answers["known"] == answers["unknown"] and answers["known"][0] == 202
        assert gw.done.wait(5) and jobs.drain()                       # the GoTrue calls happen afterwards
        assert {e for _, e in gw.calls} == {"known@example.test", "unknown@example.test"}
    finally:
        jobs.close()


def test_the_background_call_uses_the_lowercased_address_and_never_logs_it(make_env: Any, caplog: pytest.LogCaptureFixture) -> None:
    import logging
    env, gw, jobs = timed_client(make_env)
    caplog.set_level(logging.DEBUG)
    try:
        gw.expect = 1
        env.client.post("/api/v1/auth/magic-link", json={"email": "Mixed.Case@Example.test"})
        assert gw.done.wait(5) and jobs.drain()
        assert gw.calls == [("magic", "mixed.case@example.test")]
        env.gateway.down = False
        jobs.submit("test", "Victim@Example.test", lambda: (_ for _ in ()).throw(RuntimeError("boom victim@example.test")))
        assert jobs.drain() and jobs.failed == 1
        text = "\n".join(r.getMessage() for r in caplog.records).lower()
        assert "mail job failed" in text and "example.test" not in text and "victim" not in text
        assert address_tag("Victim@Example.test") in text
    finally:
        jobs.close()


def test_a_full_queue_drops_jobs_and_counts_them() -> None:
    gate = threading.Event()
    jobs = MailJobs(workers=1, max_pending=2)
    try:
        for i in range(5):
            jobs.submit("x", f"a{i}@example.test", lambda: gate.wait(2))
        assert jobs.dropped == 3
        gate.set()
        assert jobs.drain()
    finally:
        jobs.close()
