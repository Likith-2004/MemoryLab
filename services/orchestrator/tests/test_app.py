import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from conftest import CANARY, RUN_CONFIG, make_config
from orchestrator.app import create_app
from orchestrator.core import Orchestrator

TOKEN = "api-token-api-token-api-token-api-token"
H = {"authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def api(world):  # type: ignore[no-untyped-def]
    orch = world.orch()
    client = TestClient(create_app(orch, TOKEN.encode()), raise_server_exceptions=False)
    return world, orch, client


def body(run_id: str, **over: object) -> dict:  # type: ignore[type-arg]
    script = [{"calls": [["wait", {"frames": 1}]], "tokens": 10}]
    base = {"run_id": run_id, "user_id": "user-1", "ip_hash": "ip-1", "game": "fixture", "run_config": RUN_CONFIG,
            "provider": {"name": "fake", "api_key": CANARY, "script": script}}
    return {**base, **over}


ENDPOINTS = [("POST", "/internal/runs", {"x": 1}), ("POST", "/internal/runs/abc/stop", {"actor": "a"}),
             ("POST", "/internal/runs/abc/kill", {"actor": "a", "reason": "r"}), ("GET", "/internal/runs", None),
             ("POST", "/internal/breaker", {"open": False, "actor": "a"})]


@pytest.mark.parametrize(("method", "path", "payload"), ENDPOINTS)
def test_every_internal_endpoint_requires_the_service_token_before_anything_else(api, method, path, payload) -> None:  # type: ignore[no-untyped-def]
    """Exactly 401 for bad credentials, even with a malformed body: no schema feedback to the unauthenticated."""
    _, _, client = api
    for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": "Basic " + TOKEN}, {"authorization": TOKEN}, {"authorization": "Bearer "}):
        r = client.request(method, path, headers=headers, **({"json": payload} if payload else {}))
        assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
        assert r.headers["content-type"] == "application/problem+json"


def test_health_needs_no_token_and_there_are_no_docs(api) -> None:  # type: ignore[no-untyped-def]
    _, _, client = api
    assert client.get("/internal/health").json() == {"status": "ok"}
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_submit_queues_the_run_and_never_echoes_the_key(api, caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[no-untyped-def]
    world, orch, client = api
    caplog.set_level(logging.DEBUG)
    run_id = world.register()
    r = client.post("/internal/runs", json=body(run_id), headers=H)
    assert r.status_code == 202 and r.json() == {"run_id": run_id, "status": "queued", "position": 0}
    assert CANARY not in r.text and CANARY not in caplog.text
    assert orch.scheduler.snapshot()["queued"] == [run_id]
    assert client.get("/internal/runs", headers=H).json()["queued"] == [run_id] and CANARY not in client.get("/internal/runs", headers=H).text


@pytest.mark.parametrize(
    ("payload", "status"),
    [({"bogus": 1}, 422), ({"run_id": 5}, 422), ({"provider": {"name": "fake", "api_key": 123}}, 422),
     ({"provider": {"name": "fake", "api_key": CANARY, "extra": 1}}, 422), ({"run_config": []}, 422),
     ({"user_id": ""}, 422), ({"preset_text": "x" * 4001}, 422)],
)
def test_malformed_bodies_are_422_and_never_echo_what_was_sent(api, payload: dict, status: int, caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[no-untyped-def,type-arg]
    world, _, client = api
    r = client.post("/internal/runs", json={**body(world.register()), **payload}, headers=H)
    assert r.status_code == status and r.json()["code"] == "validation_failed"
    assert CANARY not in r.text and CANARY not in caplog.text and "detail" not in r.json()


def test_validation_errors_carry_stable_codes(api) -> None:  # type: ignore[no-untyped-def]
    world, _, client = api
    r = client.post("/internal/runs", json=body(world.register(), run_config={**RUN_CONFIG, "caps": {"max_steps": 99999}}), headers=H)
    assert r.status_code == 422 and r.json()["code"] == "caps_exceed_limit"
    r = client.post("/internal/runs", json=body(world.register(), provider={"name": "fake", "api_key": "short"}), headers=H)
    assert r.status_code == 422 and r.json()["code"] == "key_rejected" and "short" not in r.text


def test_admission_errors_map_to_http_with_retry_after(api) -> None:  # type: ignore[no-untyped-def]
    world, orch, client = api
    orch.cfg = make_config(max_queue=1)
    orch.scheduler.cfg = orch.cfg
    a, b, c = world.register(), world.register(), world.register()
    assert client.post("/internal/runs", json=body(a), headers=H).status_code == 202
    r = client.post("/internal/runs", json=body(b), headers=H)                                    # same user
    assert r.status_code == 429 and r.json()["code"] == "user_busy"
    r = client.post("/internal/runs", json=body(b, user_id="user-2", ip_hash="ip-2"), headers=H)  # queue full
    assert r.status_code == 429 and r.json()["code"] == "capacity_full" and r.headers["retry-after"] == "60"
    assert client.post("/internal/runs", json=body(a, user_id="u3", ip_hash="i3"), headers=H).status_code in (409, 429)
    client.post("/internal/breaker", json={"open": True, "actor": "admin", "reason": "outage"}, headers=H)
    r = client.post("/internal/runs", json=body(c, user_id="user-9", ip_hash="ip-9"), headers=H)
    assert r.status_code == 503 and r.json()["code"] == "breaker_open"


def test_breaker_needs_a_reason_to_open_and_is_audited(api) -> None:  # type: ignore[no-untyped-def]
    world, _, client = api
    assert client.post("/internal/breaker", json={"open": True, "actor": "admin"}, headers=H).status_code == 422
    r = client.post("/internal/breaker", json={"open": True, "actor": "admin", "reason": "provider outage"}, headers=H)
    assert r.json() == {"open": True, "reason": "provider outage"}
    assert client.post("/internal/breaker", json={"open": False, "actor": "admin"}, headers=H).json() == {"open": False, "reason": ""}
    assert [a["open"] for a in world.audit if a["action"] == "breaker"] == [True, False]


def test_stop_and_kill_endpoints(api) -> None:  # type: ignore[no-untyped-def]
    world, orch, client = api
    run_id = world.register()
    client.post("/internal/runs", json=body(run_id), headers=H)
    assert client.post(f"/internal/runs/{run_id}/kill", json={"actor": "admin", "reason": ""}, headers=H).status_code == 422
    assert client.post(f"/internal/runs/{run_id}/kill", json={"actor": "admin", "reason": "spam"}, headers=H).json() == {"result": "killed"}
    assert client.post(f"/internal/runs/{uuid.uuid4()}/stop", json={"actor": "u"}, headers=H).json() == {"result": "unknown"}
    assert orch.scheduler.snapshot()["queued"] == []


def test_oversized_bodies_are_413_and_internal_errors_are_opaque(api, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[no-untyped-def]
    world, orch, client = api
    assert client.post("/internal/runs", content=b"x" * 600_000, headers={**H, "content-type": "application/json"}).status_code == 413

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError(f"explosion with {CANARY} and /home/user/secret")

    monkeypatch.setattr(Orchestrator, "submit", boom)
    r = client.post("/internal/runs", json=body(world.register()), headers=H)
    assert r.status_code == 500 and r.json()["code"] == "internal" and set(r.json()) == {"type", "title", "status", "code", "incident"}
    assert CANARY not in r.text and CANARY not in caplog.text and "/home" not in r.text


def test_a_short_service_token_is_refused_at_startup(api) -> None:  # type: ignore[no-untyped-def]
    _, orch, _ = api
    with pytest.raises(ValueError):
        create_app(orch, b"short")
