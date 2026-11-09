"""Launch checklist: the API answers every kind of response with the security headers and never with a stack trace or an exception text."""

import uuid
from typing import Any

import pytest

from conftest import Env, bearer

REQUIRED = {
    "x-content-type-options": "nosniff",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
    "referrer-policy": "no-referrer",
}


def check(r: Any) -> None:
    h = r.headers
    for name, value in REQUIRED.items():
        assert h.get(name) == value, (r.request.url, name, h.get(name))
    assert "camera=()" in h["permissions-policy"] and "geolocation=()" in h["permissions-policy"]
    assert h["strict-transport-security"].startswith("max-age=") and int(h["strict-transport-security"].split("=")[1].split(";")[0]) >= 31536000
    assert h["cache-control"] == "no-store"
    body = r.text
    for leak in ("Traceback", 'File "', "site-packages", "psycopg", "RuntimeError", "/home/"):
        assert leak not in body, (r.request.url, leak)


@pytest.mark.parametrize("method,path,status", [
    ("GET", "/api/v1/health", 200),
    ("GET", "/api/v1/does-not-exist", 404),
    ("GET", "/api/v1/me", 401),
    ("POST", "/api/v1/runs", 401),
    ("GET", "/openapi.json", 404),
    ("GET", "/docs", 404),
])
def test_every_kind_of_response_carries_the_headers(env: Env, method: str, path: str, status: int) -> None:
    r = env.client.request(method, path)
    assert r.status_code == status, r.text
    check(r)


def test_a_validation_error_and_a_run_not_found_carry_them_and_echo_nothing(env: Env) -> None:
    u = env.user()
    r = env.client.post("/api/v1/runs", json={"api_key": "sk-ant-SHOULD-NOT-BE-ECHOED", "bogus": 1}, headers=bearer(u))
    assert r.status_code == 422 and "SHOULD-NOT-BE-ECHOED" not in r.text
    check(r)
    r = env.client.get(f"/api/v1/runs/{uuid.uuid4()}", headers=bearer(u))
    assert r.status_code == 404
    check(r)


def test_an_unexpected_error_is_a_generic_500_with_an_incident_id_and_the_headers(env: Env) -> None:
    @env.client.app.get("/api/v1/__boom")  # type: ignore[attr-defined]
    def boom() -> None:
        raise RuntimeError("secret internal detail in /home/fool/app.py with sk-ant-LEAK")

    r = env.client.get("/api/v1/__boom")
    assert r.status_code == 500 and r.json()["code"] == "internal" and len(r.json()["incident"]) == 12
    assert "secret internal detail" not in r.text and "sk-ant-LEAK" not in r.text
    check(r)
    assert r.headers["content-type"].startswith("application/problem+json")
