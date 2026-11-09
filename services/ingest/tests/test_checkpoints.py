"""Checkpoint blobs, the `checkpoint` event, and the parent endpoints a forked run starts from."""

import hashlib
import json
from typing import Any

import pytest

from conftest import Env, ev, make_env


def raw(type_: str, at: int, data: dict[str, Any]) -> dict[str, Any]:
    return {"type": type_, "step": at, "data": data}
from ingest.repository import ParentRef

STATE = b"emulator-state-" * 500
CTX = {"v": 1, "steps": [{"observation": "Step 3", "has_image": False, "assistant": [], "results": []}], "transcript": ["step 1"],
       "session_no": 2, "boundary": True}


def cp_event(step: int = 3, state: bytes = STATE, rev: int = 2, **over: Any) -> dict[str, Any]:
    return raw("checkpoint", step, {"step": step, "reason": "boundary", "session": 2, "memory_rev": rev, "state_sha256": hashlib.sha256(state).hexdigest(),
                                    "frames": 100, "tracker": {"seen": 0, "first_step": {}}, "snapshot_sha256": "a" * 64, "ram_sha256": "b" * 64,
                                    "rom_sha256": "c" * 64, "semantics_sha256": "d" * 64, "pyboy_version": "2.9.0", "config_hash": "e" * 64,
                                    "trace_len": 4, **over})


def put(env: Env, run: str, step: int, kind: str, data: bytes, ctype: str | None = None, scope: str = "worker"):  # type: ignore[no-untyped-def]
    ctype = ctype or ("application/json" if kind == "context" else "application/octet-stream")
    return env.client.put(f"/v1/runs/{run}/checkpoints/{step}/{kind}", content=data, headers={**env.headers(run, scope), "content-type": ctype})


def upload(env: Env, run: str, step: int = 3, state: bytes = STATE) -> None:
    assert put(env, run, step, "state", state).status_code == 200
    assert put(env, run, step, "context", json.dumps(CTX).encode()).status_code == 200


def test_a_checkpoint_needs_its_blobs_first_and_gets_a_database_row(env: Env) -> None:
    run = env.run()
    env.start(run)
    # event before the blobs: refused, nothing is logged
    r = env.post(run, 2, [cp_event()])
    assert r.status_code == 422 and env.repo.checkpoints.get(run) is None
    upload(env, run)
    ok = env.post(run, 2, [raw("mem.write", 1, {"path": "a.md", "version": 1, "step": 1, "rev": 1, "content": "x", "chars": 1, "after_sha256": "f" * 64}),
                           cp_event()])
    assert ok.status_code == 200, ok.text
    (row,) = env.repo.checkpoints[run]
    assert row["step"] == 3 and row["savestate_key"] == f"runs/{run}/checkpoints/0000003.state" and row["memory_version"] == 2
    assert row["transcript_offset"] == 4 and row["config_hash"] == "e" * 64            # seq of the checkpoint event: queued? starting, running, mem.write, checkpoint


def test_the_event_must_match_the_uploaded_state(env: Env) -> None:
    run = env.run()
    env.start(run)
    upload(env, run)
    assert env.post(run, 2, [cp_event(state=b"some other state")]).status_code == 422
    assert env.post(run, 2, [cp_event(step=4)]).status_code == 422                     # no blobs for step 4
    for bad in (cp_event(rev=True), raw("checkpoint", 3, {"step": "3", "state_sha256": "x", "config_hash": "e", "memory_rev": 1})):
        assert env.post(run, 2, [bad]).status_code == 422
    assert env.repo.checkpoints.get(run) is None


def test_only_the_worker_uploads_and_only_for_its_own_run_while_it_runs(env: Env) -> None:
    run, other = env.run(), env.run()
    env.start(run)
    assert put(env, run, 3, "state", STATE, scope="orchestrator").status_code == 403
    r = env.client.put(f"/v1/runs/{run}/checkpoints/3/state", content=STATE, headers={**env.headers(other), "content-type": "application/octet-stream"})
    assert r.status_code == 403
    assert env.client.put(f"/v1/runs/{run}/checkpoints/3/state", content=STATE, headers={"content-type": "application/octet-stream"}).status_code == 401
    upload(env, run)
    assert put(env, run, 3, "state", STATE).status_code == 200                           # the same bytes again: idempotent
    assert put(env, run, 3, "state", STATE + b"x").status_code == 409                    # different bytes for the same step: refused
    env.post(run, 2, [ev("run.state", 3, state="stopping"), ev("run.state", 3, state="completed")])
    assert put(env, run, 9, "state", STATE).status_code == 409                           # the run has ended


@pytest.mark.parametrize("kind,data,ctype,status", [
    ("state", b"", "application/octet-stream", 422), ("state", b"x" * (3 * 1024 * 1024 + 1), "application/octet-stream", 413),
    ("state", STATE, "text/plain", 422), ("context", b"not json", "application/json", 422), ("context", b"{}", "text/plain", 422),
    ("context", json.dumps({"leak": "sk-ant-api03-" + "A" * 40}).encode(), "application/json", 422),
    ("other", STATE, "application/octet-stream", 422)])
def test_bad_checkpoint_uploads_are_refused(env: Env, kind: str, data: bytes, ctype: str, status: int) -> None:
    run = env.run()
    env.start(run)
    r = put(env, run, 3, kind, data, ctype)
    assert r.status_code in (status, 413, 422) and r.status_code >= 400
    assert not env.store.exists(f"runs/{run}/checkpoints/0000003.state") or kind == "state" and ctype == "application/octet-stream" and data == STATE


def test_a_forked_run_reads_its_parents_checkpoint_context_and_memory_history(env: Env) -> None:
    parent = env.run()
    env.start(parent)
    mem = [raw("mem.write", 1, {"path": "a.md", "version": 1, "step": 1, "rev": 1, "content": "one", "chars": 3, "after_sha256": "f" * 64}),
           raw("dream.write", 2, {"path": "a.md", "version": 2, "step": 2, "rev": 2, "content": "two", "chars": 3, "after_sha256": "f" * 64, "kind": "correct"}),
           raw("mem.delete", 2, {"path": "a.md", "version": 3, "step": 2, "rev": 3, "chars": 0})]
    upload(env, parent)
    assert env.post(parent, 2, [*mem, cp_event(rev=3), raw("mem.write", 4, {"path": "late.md", "version": 1, "step": 4, "rev": 4, "content": "x", "chars": 1, "after_sha256": "f" * 64})]).status_code == 200
    child = env.run()
    env.repo.parents[child] = ParentRef(parent, 3, 6, "cp-1")        # seq of the checkpoint event: 3 lifecycle + 3 memory events = 6? (queued, starting, running)
    got = env.client.get(f"/v1/runs/{child}/parent", headers=env.headers(child))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["parent_run_id"] == parent and body["checkpoint"]["step"] == 3 and body["context"]["session_no"] == 2
    assert [(m["type"], m["data"]["version"]) for m in body["memory_events"]] == [("mem.write", 1), ("dream.write", 2), ("mem.delete", 3)]
    assert "late.md" not in json.dumps(body)                                           # nothing after the checkpoint
    state = env.client.get(f"/v1/runs/{child}/parent/state", headers=env.headers(child))
    assert state.status_code == 200 and state.content == STATE and state.headers["content-type"] == "application/octet-stream"


def test_the_parent_endpoints_belong_to_a_forked_workers_own_run_only(env: Env) -> None:
    parent, child, stranger = env.run(), env.run(), env.run()
    env.start(parent)
    upload(env, parent)
    env.post(parent, 2, [cp_event()])
    env.repo.parents[child] = ParentRef(parent, 3, 4, "cp-1")
    for path in ("parent", "parent/state"):
        assert env.client.get(f"/v1/runs/{stranger}/{path}", headers=env.headers(stranger)).status_code == 404         # no parent link
        assert env.client.get(f"/v1/runs/{child}/{path}", headers=env.headers(stranger)).status_code == 403            # another run's token
        assert env.client.get(f"/v1/runs/{child}/{path}", headers=env.headers(child, "orchestrator")).status_code == 403
        assert env.client.get(f"/v1/runs/{child}/{path}").status_code == 401
        assert env.client.get(f"/v1/runs/{parent}/{path}", headers=env.headers(parent)).status_code == 404            # a parent has no parent


def test_a_dangling_parent_reference_is_a_404_not_a_crash(env: Env) -> None:
    parent, child = env.run(), env.run()
    env.start(parent)
    env.repo.parents[child] = ParentRef(parent, 3, 99, "cp-x")
    assert env.client.get(f"/v1/runs/{child}/parent", headers=env.headers(child)).status_code == 404
    assert env.client.get(f"/v1/runs/{child}/parent/state", headers=env.headers(child)).status_code == 404


def test_tokens_from_dream_calls_update_the_run_record_and_dream_writes_become_memory_rows(env: Env) -> None:
    run = env.run()
    env.start(run)
    r = env.post(run, 2, [raw("model.call", 1, {"tokens_total": 100}),
                          raw("dream.call", 3, {"turn": 1, "dream_tokens": 250, "tokens_total": 350}),
                          raw("dream.write", 3, {"path": "g.md", "version": 1, "step": 3, "rev": 1, "content": "pin", "chars": 3, "after_sha256": "f" * 64, "kind": "promote"}),
                          raw("dream.delete", 3, {"path": "old.md", "version": 2, "step": 3, "rev": 2, "chars": 0})])
    assert r.status_code == 200, r.text
    assert env.repo.get(run).tokens == 350                                             # type: ignore[union-attr]
    assert [(m["path"], m["deleted"]) for m in env.repo.memory_rows[run]] == [("g.md", False), ("old.md", True)]


def test_the_default_limits_hold_a_real_checkpoint(tmp_path) -> None:  # type: ignore[no-untyped-def]
    e = make_env(tmp_path)
    assert e.service.limits.max_checkpoint_state_bytes >= 2 * 1024 * 1024 and e.service.limits.max_body_bytes > e.service.limits.max_checkpoint_state_bytes


def test_checkpoint_uploads_have_a_per_run_quota_counted_at_upload_per_distinct_step(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from ingest.service import Limits
    e = make_env(tmp_path, Limits(max_checkpoints_per_run=3))
    run = e.run()
    e.start(run)
    for step in (1, 2, 3):                                                                   # three orphan uploads, no checkpoint event
        assert put(e, run, step, "state", STATE).status_code == 200
    r = put(e, run, 4, "state", STATE)
    assert r.status_code == 429 and r.json()["code"] == "quota_exceeded" and not e.store.exists(f"runs/{run}/checkpoints/0000004.state")
    assert put(e, run, 4, "context", json.dumps(CTX).encode()).status_code == 429
    assert e.repo.get(run).checkpoints == 3                                                   # type: ignore[union-attr]
    # an idempotent re-upload and the context blob of a known step cost nothing; a different blob is still a conflict
    assert put(e, run, 2, "state", STATE).status_code == 200 and e.repo.get(run).checkpoints == 3  # type: ignore[union-attr]
    assert put(e, run, 2, "context", json.dumps(CTX).encode()).status_code == 200 and e.repo.get(run).checkpoints == 3  # type: ignore[union-attr]
    assert put(e, run, 2, "state", STATE + b"x").status_code == 409
    assert put(e, run, 4, "state", STATE).status_code == 429                                  # still full
    other = e.run()
    e.start(other)
    assert put(e, other, 4, "state", STATE).status_code == 200                                # the quota is per run


def test_the_default_checkpoint_quota_is_bounded() -> None:
    from ingest.service import Limits
    assert 1 <= Limits().max_checkpoints_per_run <= 200
