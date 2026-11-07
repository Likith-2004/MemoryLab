import json
import uuid

import pytest
from runlog.eventlog import EventLogReader

from conftest import ev, make_env, png
from ingest.service import Limits

OTHER = str(uuid.uuid4())


# ---- authentication and authorization matrix --------------------------------------------------------
ROUTES = [
    ("POST", "/v1/runs/{rid}/events", {"batch_no": 1, "events": [{"type": "obs", "step": 0, "data": {}}]}),
    ("PUT", "/v1/runs/{rid}/frames/1", None),
    ("PUT", "/v1/runs/{rid}/manifest", {}),
    ("POST", "/v1/runs/{rid}/close", None),
]


def call(env, method, path, body, headers):  # type: ignore[no-untyped-def]
    kw: dict = {"headers": headers}  # type: ignore[type-arg]
    if path.endswith("/frames/1"):
        kw["content"] = png()
        kw["headers"] = {**headers, "content-type": "image/png"}
    elif body is not None:
        kw["json"] = body
    return env.client.request(method, path, **kw)


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_every_route_requires_a_valid_run_scoped_token(env, method, path, body) -> None:  # type: ignore[no-untyped-def]
    rid, other = env.run(), env.run()
    p = path.format(rid=rid)
    expectations = {
        "no header": ({}, 401), "wrong scheme": ({"authorization": "Basic abc"}, 401),
        "empty bearer": ({"authorization": "Bearer "}, 401), "garbage": ({"authorization": "Bearer nope"}, 401),
        "token for another run": (env.headers(other), 403),
        "orchestrator token for another run": (env.headers(other, "orchestrator"), 403),
    }
    for name, (headers, status) in expectations.items():
        r = call(env, method, p, body, headers)
        assert r.status_code == status, (name, r.text)
        assert r.headers["content-type"] == "application/problem+json" and r.json()["code"]
    env.now[0] += 10_000                                                       # expired token
    stale = {"authorization": f"Bearer {env.token(rid, ttl=60)}"}
    env.now[0] += 100
    assert call(env, method, p, body, stale).status_code == 401


def test_unknown_runs_and_malformed_ids_are_not_found(env) -> None:  # type: ignore[no-untyped-def]
    ghost = str(uuid.uuid4())
    r = env.client.post(f"/v1/runs/{ghost}/events", json={"batch_no": 1, "events": [ev("obs")]}, headers=env.headers(ghost))
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    for bad in ("nope", "../x", "1" * 40, "%2e%2e"):
        r = env.client.post(f"/v1/runs/{bad}/events", json={"batch_no": 1, "events": [ev("obs")]}, headers={"authorization": "Bearer x"})
        assert r.status_code in (404, 422), bad


def test_scope_separation_between_orchestrator_and_worker(env) -> None:  # type: ignore[no-untyped-def]
    rid = env.run()
    assert env.post(rid, 1, [ev("obs")], "orchestrator").status_code == 403         # orchestrator writes run.state/error only
    assert env.post(rid, 1, [ev("run.state", state="starting")], "worker").status_code == 422   # state not writable by worker
    env.start(rid)
    assert env.post(rid, 3, [ev("run.state", state="killed")], "worker").status_code == 422
    assert env.client.put(f"/v1/runs/{rid}/frames/1", content=png(), headers={**env.headers(rid, "orchestrator"), "content-type": "image/png"}).status_code == 403
    assert env.client.put(f"/v1/runs/{rid}/manifest", json={"run_id": rid}, headers=env.headers(rid, "worker")).status_code == 403
    assert env.client.post(f"/v1/runs/{rid}/close", headers=env.headers(rid, "worker")).status_code == 403


# ---- events ---------------------------------------------------------------------------------------------------
def test_events_are_stored_with_gapless_seq_and_server_time(env) -> None:  # type: ignore[no-untyped-def]
    rid = env.run()
    env.start(rid)
    r = env.post(rid, 3, [ev("obs", 1, snapshot={"milestones": 0}, score=0), ev("model.call", 1, tokens_total=120, usage={})])
    assert r.status_code == 200 and r.json() == {"batch_no": 3, "accepted": 2, "next_seq": 5}
    events = list(EventLogReader(env.store, rid).iter_events())
    assert [e.seq for e in events] == [1, 2, 3, 4] and all(e.run_id == rid and e.ts.endswith("Z") for e in events)
    rec = env.repo.get(rid)
    assert (rec.status, rec.event_count, rec.tokens, rec.steps) == ("running", 4, 120, 1)
    assert [i.seq for i in env.repo.index[rid]] == [1, 2, 3, 4]


def test_clients_cannot_supply_seq_ts_or_run_id(env) -> None:  # type: ignore[no-untyped-def]
    rid = env.run()
    for extra in ({"seq": 5}, {"ts": "x"}, {"run_id": OTHER}):
        r = env.client.post(f"/v1/runs/{rid}/events", json={"batch_no": 1, "events": [{**ev("obs"), **extra}]}, headers=env.headers(rid))
        assert r.status_code == 422
    r = env.client.post(f"/v1/runs/{rid}/events", json={"batch_no": 1, "events": [ev("obs")], "extra": 1}, headers=env.headers(rid))
    assert r.status_code == 422


def test_retry_of_the_last_batch_is_acknowledged_not_duplicated(env) -> None:  # type: ignore[no-untyped-def]
    rid = env.run()
    env.start(rid)
    first = env.post(rid, 3, [ev("obs", 1, snapshot={"milestones": 0}, score=0)])
    again = env.post(rid, 3, [ev("obs", 1, snapshot={"milestones": 0}, score=0)])
    assert first.json() == again.json()
    assert len(list(EventLogReader(env.store, rid).iter_events())) == 3
    assert env.post(rid, 5, [ev("obs", 2, snapshot={"milestones": 0}, score=0)]).status_code == 409     # skipped ahead
    assert env.post(rid, 2, [ev("obs", 2, snapshot={"milestones": 0}, score=0)]).status_code == 409     # too old
    assert env.post(rid, 4, [ev("obs", 2, snapshot={"milestones": 0}, score=0)]).status_code == 200


def test_lifecycle_is_enforced_by_ingest(env) -> None:  # type: ignore[no-untyped-def]
    rid = env.run()
    assert env.post(rid, 1, [ev("run.state", state="running")], "worker").status_code == 422        # no start yet
    env.start(rid)
    assert env.post(rid, 3, [ev("run.state", state="completed")]).status_code == 422                  # must stop first
    assert env.post(rid, 3, [ev("run.state", state="stopping"), ev("run.state", state="capped")]).status_code == 200
    assert env.post(rid, 4, [ev("obs", 1, snapshot={"milestones": 0}, score=0)]).status_code == 409  # after the end
    rid2 = env.run()
    env.start(rid2)
    same_batch = [ev("run.state", state="stopping"), ev("run.state", state="failed"), ev("error", code="late")]
    assert env.post(rid2, 3, same_batch).status_code == 422                                          # after the end, same batch
    assert len(list(EventLogReader(env.store, rid2).iter_events())) == 2


def test_a_rejected_batch_writes_nothing_and_does_not_advance(env) -> None:  # type: ignore[no-untyped-def]
    rid = env.run()
    env.start(rid)
    bad = env.post(rid, 3, [ev("obs", 1, snapshot={"milestones": 0}, score=0), ev("bogus")])
    assert bad.status_code == 422 and bad.json()["code"] == "validation_failed" and "bogus" not in bad.text
    assert env.repo.get(rid).next_batch_no == 3 and len(list(EventLogReader(env.store, rid).iter_events())) == 2
    assert env.post(rid, 3, [ev("obs", 1, snapshot={"milestones": 0}, score=0)]).status_code == 200


@pytest.mark.parametrize(
    "event",
    [{"type": "obs", "step": -1, "data": {}}, {"type": "obs", "step": 0, "data": []}, {"type": "obs", "step": "0", "data": {}},
     {"type": "nope", "step": 0, "data": {}}, {"type": "obs", "step": 0, "data": {"a": float("inf")}},
     {"type": "obs", "step": 0, "data": {"a": "x" * 40_000}}, {"type": "x" * 100, "step": 0, "data": {}}, {"step": 0, "data": {}}],
)
def test_invalid_events_are_422_without_echoing_input(env, event: dict) -> None:  # type: ignore[type-arg]
    rid = env.run()
    r = env.client.post(f"/v1/runs/{rid}/events", content=json.dumps({"batch_no": 1, "events": [event]}, allow_nan=True),
                        headers={**env.headers(rid), "content-type": "application/json"})
    assert r.status_code == 422 and r.json()["code"] == "validation_failed"
    assert "xxxx" not in r.text and "inf" not in r.text.lower().replace("information", "")


def test_batch_size_limits(tmp_path) -> None:  # type: ignore[no-untyped-def]
    env = make_env(tmp_path)
    rid = env.run()
    assert env.post(rid, 1, [], "orchestrator").status_code == 422
    assert env.post(rid, 1, [ev("error", code="x")] * 101, "orchestrator").status_code == 422


def test_event_and_byte_quotas(tmp_path) -> None:  # type: ignore[no-untyped-def]
    env = make_env(tmp_path, Limits(max_events_per_run=4, max_bytes_per_run=2_000))
    rid = env.run()
    env.start(rid)                                                                         # 2 events
    assert env.post(rid, 3, [ev("model.summary", 1, text="x" * 1500)]).status_code == 200
    r = env.post(rid, 4, [ev("model.summary", 1, text="y" * 1500)])
    assert r.status_code == 429 and r.json()["code"] == "quota_exceeded"
    assert env.post(rid, 4, [ev("error", 1, code="a"), ev("error", 1, code="b"), ev("error", 1, code="c")]).status_code == 429
    assert len(list(EventLogReader(env.store, rid).iter_events())) == 3                  # nothing written by the refused batches


def test_body_size_limits_by_content_length_and_by_streaming(tmp_path) -> None:  # type: ignore[no-untyped-def]
    env = make_env(tmp_path, Limits(max_body_bytes=2048))
    rid = env.run()
    big = json.dumps({"batch_no": 1, "events": [ev("error", code="x" * 5000)]})
    r = env.client.post(f"/v1/runs/{rid}/events", content=big, headers={**env.headers(rid), "content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"
    chunks = (c for c in (b'{"batch_no":1,"events":[', b"x" * 3000))
    r = env.client.post(f"/v1/runs/{rid}/events", content=chunks, headers={**env.headers(rid), "content-type": "application/json"})
    assert r.status_code == 413
    assert env.client.post(f"/v1/runs/{rid}/events", content=b"1", headers={**env.headers(rid), "content-length": "abc"}).status_code == 413


def test_memory_events_become_memory_rows(env) -> None:  # type: ignore[no-untyped-def]
    rid = env.run()
    env.start(rid)
    write = {"type": "mem.write", "step": 1, "data": {"path": "a.md", "version": 1, "step": 1, "rev": 1, "content": "hi",
                                                    "chars": 2, "after_sha256": "x", "before_sha256": None, "created": True, "diff": ""}}
    delete = {"type": "mem.delete", "step": 2, "data": {"path": "a.md", "version": 2, "step": 2, "rev": 2,
                                                      "before_sha256": "x", "chars": 2}}
    assert env.post(rid, 3, [write, delete]).status_code == 200
    rows = env.repo.memory_rows[rid]
    assert [(r["path"], r["version"], r["deleted"], r["content"]) for r in rows] == [("a.md", 1, False, "hi"), ("a.md", 2, True, "")]


def test_ingest_scrubs_credential_patterns_it_receives(env) -> None:  # type: ignore[no-untyped-def]
    rid = env.run()
    env.start(rid)
    key = "sk-ant-api03-Zq8vN2xLm4Kp7RtWc1YbH6dFg3JsAe5UoIi9PlXzC0nVq"
    assert env.post(rid, 3, [ev("model.summary", 1, text=f"oops {key}")]).status_code == 200
    stored = [e for e in EventLogReader(env.store, rid).iter_events() if e.type == "model.summary"][0]
    assert key not in json.dumps(stored.data) and "[REDACTED]" in stored.data["text"]
    assert env.service.redactor.hits == 1


# ---- frames -----------------------------------------------------------------------------------------------------
def put_frame(env, rid, step, data, ctype="image/png", scope="worker"):  # type: ignore[no-untyped-def]
    return env.client.put(f"/v1/runs/{rid}/frames/{step}", content=data, headers={**env.headers(rid, scope), "content-type": ctype})


def test_frame_upload_and_validation(env) -> None:  # type: ignore[no-untyped-def]
    rid = env.run()
    env.start(rid)
    assert put_frame(env, rid, 5, png()).status_code == 200
    assert env.store.get(f"runs/{rid}/frames/0000005.png") == png()
    assert put_frame(env, rid, 5, png()).status_code == 200                            # same bytes again: idempotent
    assert put_frame(env, rid, 5, png(color=(9, 9, 9))).status_code == 409             # different bytes: refused
    assert env.repo.get(rid).frames == 1
    bad = {"not png": (b"GIF89a....", "image/png"), "wrong type": (png(), "text/plain"), "empty": (b"", "image/png"),
           "rgba": (png(mode="RGBA", color=(1, 2, 3, 4)), "image/png"), "huge dims": (png((1000, 1000)), "image/png"),
           "truncated": (png()[:40], "image/png"), "oversize": (b"\x89PNG\r\n\x1a\n" + b"0" * 300_000, "image/png")}
    for name, (data, ctype) in bad.items():
        assert put_frame(env, rid, 6, data, ctype).status_code in (413, 422), name
    assert put_frame(env, rid, -1, png()).status_code in (404, 422)
    assert not env.store.exists(f"runs/{rid}/frames/0000006.png")


def test_frame_quota(tmp_path) -> None:  # type: ignore[no-untyped-def]
    env = make_env(tmp_path, Limits(max_frames_per_run=2))
    rid = env.run()
    for step in (1, 2):
        assert put_frame(env, rid, step, png(color=(step, 0, 0))).status_code == 200
    assert put_frame(env, rid, 3, png()).status_code == 429


# ---- manifest, close, export ----------------------------------------------------------------------------------------
def manifest(rid: str, **extra: object) -> dict:  # type: ignore[type-arg]
    return {"run_id": rid, "mode": "demo", "harness_version": "0.2.0", **extra}


def test_manifest_is_sealed_once_and_checked(env) -> None:  # type: ignore[no-untyped-def]
    rid = env.run()
    h = env.headers(rid, "orchestrator")
    assert env.client.put(f"/v1/runs/{rid}/manifest", json=manifest(OTHER), headers=h).status_code == 422
    assert env.client.put(f"/v1/runs/{rid}/manifest", content=b"nope", headers={**h, "content-type": "application/json"}).status_code == 422
    assert env.client.put(f"/v1/runs/{rid}/manifest", json=[1], headers=h).status_code == 422
    leaky = manifest(rid, note="sk-ant-api03-Zq8vN2xLm4Kp7RtWc1YbH6dFg3JsAe5UoIi9PlXzC0nVq")
    assert env.client.put(f"/v1/runs/{rid}/manifest", json=leaky, headers=h).status_code == 422
    assert env.client.put(f"/v1/runs/{rid}/manifest", json=manifest(rid), headers=h).status_code == 200
    assert env.client.put(f"/v1/runs/{rid}/manifest", json=manifest(rid), headers=h).status_code == 409
    assert env.repo.get(rid).manifest_sealed


def test_close_requires_an_ended_run_and_a_manifest_then_builds_the_bundle(env) -> None:  # type: ignore[no-untyped-def]
    rid = env.run()
    h = env.headers(rid, "orchestrator")
    assert env.client.post(f"/v1/runs/{rid}/close", headers=h).status_code == 409           # not ended
    env.start(rid)
    assert env.post(rid, 3, [ev("obs", 1, snapshot={"milestones": 3}, score=2, ram_sha256="a"),
                             ev("model.call", 1, tokens_total=50, usage={}),
                             ev("run.state", 1, state="stopping", reason="steps"), ev("run.state", 1, state="capped", reason="steps")]).status_code == 200
    assert env.client.post(f"/v1/runs/{rid}/close", headers=h).status_code == 409           # no manifest
    assert env.client.put(f"/v1/runs/{rid}/manifest", json=manifest(rid), headers=h).status_code == 200
    r = env.client.post(f"/v1/runs/{rid}/close", headers=h)
    assert r.status_code == 200 and r.json()["closed"] is True and len(r.json()["bundle_sha256"]) == 64
    assert env.client.post(f"/v1/runs/{rid}/close", headers=h).json() == r.json()           # idempotent
    assert env.post(rid, 4, [ev("obs", 2)]).status_code == 409                               # closed: no more writes
    assert put_frame(env, rid, 9, png()).status_code == 409
    from runlog.bundle import verify_bundle
    report = verify_bundle(env.store.get(f"runs/{rid}/bundle.zip"))                          # sparse test events: must report, not crash
    assert not report.ok and all("malformed" in p or "mismatch" in p or p for p in report.problems)
    assert env.repo.get(rid).bundle_sha256 == r.json()["bundle_sha256"]


# ---- hygiene ----------------------------------------------------------------------------------------------------------
def test_no_docs_endpoints_and_health_is_bare(env) -> None:  # type: ignore[no-untyped-def]
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert env.client.get(path).status_code == 404
    r = env.client.get("/healthz")
    assert r.json() == {"status": "ok"} and r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_internal_errors_are_opaque(env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[no-untyped-def]
    rid = env.run()

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("secret internal detail sk-ant-api03-Zq8vN2xLm4Kp7RtWc1YbH6dFg3JsAe5UoIi9PlXzC0nVq /home/x/y")

    monkeypatch.setattr(env.service, "append", boom)
    r = env.post(rid, 1, [ev("run.state", state="starting")], "orchestrator")
    body = r.json()
    assert r.status_code == 500 and body["code"] == "internal" and set(body) == {"type", "title", "status", "code", "incident"}
    assert "secret" not in r.text and "RuntimeError" not in r.text and "/home" not in r.text
    assert "sk-ant" not in caplog.text and "secret internal detail" not in caplog.text


def test_responses_carry_no_stack_traces_or_server_banner(env) -> None:  # type: ignore[no-untyped-def]
    r = env.client.post("/v1/runs/not-a-run/events", content=b"{", headers={"authorization": "Bearer x", "content-type": "application/json"})
    assert "Traceback" not in r.text and "server" not in {k.lower() for k in r.headers if k.lower() == "x-powered-by"}
