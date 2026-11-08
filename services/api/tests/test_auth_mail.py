# ruff: noqa: F811  (imported fixtures are redefined as test parameters, which is how pytest injects them)
"""POST /auth/magic-link and /auth/recover: one answer for every address (GoTrue's own /otp tells known from unknown)."""

import time
from typing import Any

import httpx
import pytest

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
def test_503_only_when_the_auth_service_is_unavailable(env: Env, path: str) -> None:
    env.gateway.down = True
    r = env.client.post(path, json={"email": "a@example.test"})
    assert (r.status_code, r.json()["code"]) == (503, "internal")


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
