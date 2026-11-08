import io
import json
import threading
import time
import zipfile

import pytest
from labharness import load_bundle
from runlog.bundle import verify_bundle
from runlog.eventlog import EventLogReader
from runlog.replay import reexecute, verify_log

from conftest import CANARY, RUN_CONFIG, FakeProcess, make_config, submission
from orchestrator.core import SubmitError
from orchestrator.ingest_api import IngestApiError
from orchestrator.scheduler import BreakerOpen, Duplicate, UserBusy


def states(world, run_id):  # type: ignore[no-untyped-def]
    return [(e.data["state"], e.data.get("reason")) for e in EventLogReader(world.store, run_id).iter_events() if e.type == "run.state"]


def go(world, orch, sub, timeout=90):  # type: ignore[no-untyped-def]
    orch.submit(sub)
    orch.tick()
    assert orch.wait_idle(timeout), "orchestrator did not go idle"


def beating(proc: FakeProcess, period: float = 0.05) -> threading.Thread:
    def beat() -> None:
        n = 0
        while not proc.killed and proc.poll() is None:
            n += 1
            proc.emit(json.dumps({"hb": n}).encode() + b"\n")
            time.sleep(period)

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    return t


# ---- the happy path, with the REAL worker behind the process interface -----------------------------------
def test_a_run_goes_from_queued_to_a_verified_bundle(world) -> None:  # type: ignore[no-untyped-def]
    run_id = world.register()
    orch = world.orch()
    go(world, orch, submission(run_id))
    assert states(world, run_id) == [("queued", None), ("starting", None), ("running", None), ("stopping", "steps"), ("capped", "steps")]
    rec = world.repo.get(run_id)
    assert rec.closed and rec.manifest_sealed and rec.steps == 4 and rec.bundle_sha256
    manifest = json.loads(world.store.get(f"runs/{run_id}/manifest.json"))
    assert manifest["provider"] == "fake" and manifest["mode"] == "demo" and manifest["run_id"] == run_id
    blob = world.store.get(f"runs/{run_id}/bundle.zip")
    assert verify_bundle(blob).ok
    events = list(EventLogReader(world.store, run_id).iter_events())
    assert verify_log(events) == []
    assert reexecute(events, load_bundle(world.make.__self__ if False else __import__("conftest").GAMES / "fixture"))["ok"]
    assert world.docker.removed == [run_id] and orch.scheduler.snapshot() == {"queued": [], "active": [], "breaker": {"open": False, "reason": ""}}


def test_the_key_goes_only_to_the_workers_stdin(world) -> None:  # type: ignore[no-untyped-def]
    run_id = world.register()
    orch = world.orch()
    go(world, orch, submission(run_id))
    argv = world.launched[0]
    assert CANARY not in " ".join(argv)                                          # not in the command line
    stdin = bytes(world.procs[0].stdin_seen).decode().splitlines()
    assert len(stdin) == 1 and CANARY in stdin[0]                                # one job line, which carries it
    assert json.loads(stdin[0])["provider"]["api_key"] == CANARY and "ingest" in json.loads(stdin[0])
    blob = b"".join(p.read_bytes() for p in world.store.root.rglob("*") if p.is_file())
    zipped = [zipfile.ZipFile(io.BytesIO(world.store.get(f"runs/{run_id}/bundle.zip"))).read(n)
              for n in zipfile.ZipFile(io.BytesIO(world.store.get(f"runs/{run_id}/bundle.zip"))).namelist()]
    assert CANARY.encode() not in blob and all(CANARY.encode() not in z for z in zipped)
    assert CANARY not in repr(world.repo.runs) + repr(world.repo.memory_rows) + repr(world.repo.index)
    assert all(CANARY not in str(a) for a in world.audit)


def test_the_orchestrator_discards_the_key_when_the_run_ends(world) -> None:  # type: ignore[no-untyped-def]
    run_id = world.register()
    orch = world.orch()
    orch.submit(submission(run_id))
    request = orch.scheduler._queue[0]
    assert not request.key_box.empty
    orch.tick()
    assert orch.wait_idle(60) and request.key_box.empty


def test_two_users_run_concurrently(world) -> None:  # type: ignore[no-untyped-def]
    a, b = world.register(), world.register()
    orch = world.orch()
    orch.submit(submission(a, user="u1", ip="i1"))
    orch.submit(submission(b, user="u2", ip="i2"))
    orch.tick()
    assert orch.wait_idle(120)
    assert states(world, a)[-1] == ("capped", "steps") and states(world, b)[-1] == ("capped", "steps")
    assert world.repo.get(a).closed and world.repo.get(b).closed and sorted(world.docker.removed) == sorted([a, b])


def test_graceful_stop_ends_the_run_completed(world) -> None:  # type: ignore[no-untyped-def]
    run_id = world.register()
    script = [{"calls": [["wait", {"frames": 600}]], "tokens": 10}] * 300
    config = {**RUN_CONFIG, "caps": {"max_steps": 300}}
    orch = world.orch()
    orch.submit(submission(run_id, run_config=config, provider={"name": "fake", "script": script}))
    orch.tick()
    deadline = time.time() + 30
    while time.time() < deadline and world.repo.get(run_id).steps < 2:
        time.sleep(0.05)
    assert orch.stop(run_id, "owner") == "stopping"
    assert orch.wait_idle(60)
    assert states(world, run_id)[-1] == ("completed", "user_stop") and world.repo.get(run_id).steps < 300
    assert {"action": "stop", "actor": "owner", "target": run_id} in world.audit


# ---- admission and validation ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("change", "code"),
    [({"run_config": {**RUN_CONFIG, "caps": {"max_steps": 2001}}}, "caps_exceed_limit"),
     ({"run_config": {**RUN_CONFIG, "caps": {"max_tokens": 4_000_001}}}, "caps_exceed_limit"),
     ({"run_config": {**RUN_CONFIG, "caps": {"max_wall_s": 7201.0}}}, "caps_exceed_limit"),
     ({"run_config": {**RUN_CONFIG, "caps": {"max_tool_calls_per_step": 9}}}, "caps_exceed_limit"),
     ({"run_config": {**RUN_CONFIG, "caps": {"context_steps": 21}}}, "caps_exceed_limit"),
     ({"run_config": {**RUN_CONFIG, "bogus": 1}}, "validation_failed"),
     ({"run_config": {**RUN_CONFIG, "mode": "ranked"}}, "validation_failed"),
     ({"run_config": {**RUN_CONFIG, "preset_ref": "nope@9"}}, "validation_failed"),
     ({"game": "../etc"}, "validation_failed"), ({"game": "missing"}, "validation_failed"),
     ({"preset_text": "extra instructions"}, "validation_failed"),
     ({"run_config": {**RUN_CONFIG, "preset_ref": "custom@0", "mode": "benchmark"}, "preset_text": "x"}, "validation_failed"),
     ({"run_config": {**RUN_CONFIG, "preset_ref": "custom@0"}}, "validation_failed"),
     ({"provider": {"name": "fake", "api_key": "short"}}, "key_rejected")],
)
def test_invalid_submissions_are_rejected_before_anything_is_queued(world, change: dict, code: str) -> None:  # type: ignore[no-untyped-def,type-arg]
    run_id = world.register()
    orch = world.orch()
    base = submission(run_id)
    sub = type(base)(**{**base.__dict__, **change})
    with pytest.raises(SubmitError) as info:
        orch.submit(sub)
    assert info.value.code == code and orch.scheduler.snapshot()["queued"] == []
    assert world.repo.get(run_id).event_count == 0                              # nothing was written to the run log


def test_real_providers_need_an_allowlisted_model_and_a_key() -> None:
    import httpx

    from conftest import SECRET
    from orchestrator.core import Orchestrator
    cfg = make_config(allow_fake_provider=False)
    orch = Orchestrator(cfg, httpx.Client(), docker=None)
    real = {**RUN_CONFIG, "provider": "anthropic", "model_id": "claude-sonnet-5-5"}
    ok = submission("11111111-2222-3333-4444-555555555555", run_config=real,
                    provider={"name": "anthropic", "api_key": "sk-ant-api03-" + "a" * 40})
    config, _, key, _ = orch._validate(ok)
    assert config.model_id == "claude-sonnet-5-5" and key is not None and SECRET
    for bad in ({"provider": {"name": "anthropic"}},                                         # no key
                {"provider": {"name": "openai", "api_key": "sk-" + "a" * 40}},               # not allowlisted
                {"run_config": {**real, "model_id": "gpt-4o"}},                              # model not allowlisted
                {"run_config": {**real, "provider": "openai"}},                              # config/provider mismatch
                {"provider": {"name": "fake", "api_key": CANARY}}):                          # fake is test-only
        sub = type(ok)(**{**ok.__dict__, **bad})
        with pytest.raises(SubmitError):
            orch._validate(sub)


def test_admission_limits_surface_through_submit(world) -> None:  # type: ignore[no-untyped-def]
    a, b = world.register(), world.register()
    orch = world.orch()
    orch.submit(submission(a, user="same"))
    with pytest.raises(UserBusy):
        orch.submit(submission(b, user="same"))
    assert world.repo.get(b).event_count == 0
    orch.set_breaker(open_=True, reason="provider outage", actor="admin")
    with pytest.raises(BreakerOpen):
        orch.submit(submission(b, user="other", ip="other"))
    assert {"action": "breaker", "actor": "admin", "open": True, "reason": "provider outage"} in world.audit
    orch.tick()
    assert orch.scheduler.snapshot()["active"] == []                                   # the breaker stops new starts too


def test_a_run_that_cannot_be_queued_in_ingest_is_not_left_in_the_queue(world) -> None:  # type: ignore[no-untyped-def]
    orch = world.orch()
    unregistered = "99999999-2222-3333-4444-555555555555"
    with pytest.raises(IngestApiError):                                                 # ingest answers 404: nothing to attach to
        orch.submit(submission(unregistered))
    assert orch.scheduler.snapshot()["queued"] == [] and orch.scheduler.snapshot()["active"] == []


# ---- failure paths ----------------------------------------------------------------------------------------------------
def test_queue_expiry_fails_the_run_and_destroys_the_key(world) -> None:  # type: ignore[no-untyped-def]
    now = [0.0]
    run_id = world.register()
    orch = world.orch(make_config(queue_ttl_s=600), clock=lambda: now[0])
    orch.set_breaker(open_=True, reason="hold", actor="a")
    orch.scheduler.set_breaker(open_=False, reason="", actor="a")                       # allow submit, then stall the start
    orch.submit(submission(run_id))
    request = orch.scheduler._queue[0]
    now[0] = 601.0
    orch.tick()
    assert request.key_box.empty and states(world, run_id) == [("queued", None), ("failed", "key_expired")]
    assert world.launched == [] and orch.scheduler.snapshot()["queued"] == []


def test_worker_crash_before_any_event_is_recorded_as_failed(world) -> None:  # type: ignore[no-untyped-def]
    run_id = world.register()
    orch = world.orch(launcher=lambda argv: FakeProcess([], exit_code=137, run_for=0.1))
    go(world, orch, submission(run_id))
    assert states(world, run_id) == [("queued", None), ("starting", None), ("failed", "worker_exit_137")]
    assert world.repo.get(run_id).closed and world.docker.removed == [run_id]


def test_a_workers_fatal_type_becomes_the_reason(world) -> None:  # type: ignore[no-untyped-def]
    run_id = world.register()
    orch = world.orch(launcher=lambda argv: FakeProcess([b'{"fatal":"IngestRejected"}\n'], exit_code=3, run_for=0.1))
    go(world, orch, submission(run_id))
    assert states(world, run_id)[-1] == ("failed", "IngestRejected")


def test_missed_heartbeats_kill_the_container_and_fail_the_run(world) -> None:  # type: ignore[no-untyped-def]
    run_id = world.register()
    orch = world.orch(make_config(heartbeat_timeout_s=0.4, startup_grace_s=5.0),
                      launcher=lambda argv: FakeProcess([b'{"hb":1}\n'], hang=True))
    go(world, orch, submission(run_id), timeout=30)
    assert states(world, run_id)[-1] == ("failed", "heartbeat_timeout")
    assert world.docker.killed == [run_id] and world.docker.removed == [run_id] and world.procs[0].killed


def test_a_worker_that_never_beats_is_killed_after_the_startup_grace(world) -> None:  # type: ignore[no-untyped-def]
    run_id = world.register()
    orch = world.orch(make_config(startup_grace_s=0.4), launcher=lambda argv: FakeProcess([], hang=True))
    go(world, orch, submission(run_id), timeout=30)
    assert states(world, run_id)[-1] == ("failed", "startup_timeout") and world.docker.killed == [run_id]


def test_the_wall_clock_kills_a_worker_that_ignores_its_own_cap(world) -> None:  # type: ignore[no-untyped-def]
    run_id = world.register()
    config = {**RUN_CONFIG, "caps": {"max_steps": 4, "max_wall_s": 0.3}}

    def launch(argv):  # type: ignore[no-untyped-def]
        proc = FakeProcess([b'{"hb":1}\n'], hang=True)
        beating(proc)
        return proc

    orch = world.orch(make_config(wall_grace_s=0.2), launcher=launch)
    go(world, orch, submission(run_id, run_config=config), timeout=30)
    assert states(world, run_id)[-1] == ("failed", "wall_clock") and world.docker.killed == [run_id]


def test_admin_kill_ends_the_run_killed_and_is_audited(world) -> None:  # type: ignore[no-untyped-def]
    run_id = world.register()
    holder = {}

    def launch(argv):  # type: ignore[no-untyped-def]
        proc = FakeProcess([b'{"hb":1}\n'], hang=True)
        beating(proc)
        holder["p"] = proc
        return proc

    orch = world.orch(launcher=launch)
    orch.submit(submission(run_id))
    orch.tick()
    deadline = time.time() + 10
    while time.time() < deadline and run_id not in orch._sandboxes:
        time.sleep(0.02)
    with pytest.raises(ValueError, match="reason"):
        orch.kill(run_id, "admin", " ")
    assert orch.kill(run_id, "admin", "abuse report") == "killed"
    assert orch.wait_idle(30)
    assert states(world, run_id)[-1] == ("killed", "admin_kill") and holder["p"].killed
    assert {"action": "kill", "actor": "admin", "target": run_id, "reason": "abuse report"} in world.audit


def test_cancelling_a_queued_run_kills_it_without_starting_anything(world) -> None:  # type: ignore[no-untyped-def]
    run_id, other = world.register(), world.register()
    orch = world.orch(make_config(max_concurrent=1))
    orch.set_breaker(open_=True, reason="drain", actor="a")
    orch.scheduler.set_breaker(open_=False, reason="", actor="a")
    orch.submit(submission(run_id, user="u1", ip="i1"))
    request = orch.scheduler._queue[0]
    assert orch.kill(run_id, "admin", "spam") == "killed" and request.key_box.empty
    assert states(world, run_id) == [("queued", None), ("killed", "admin_kill")] and world.launched == []
    orch.submit(submission(other, user="u2", ip="i2"))
    assert orch.stop(other, "u2") == "cancelled" and states(world, other)[-1] == ("killed", "user_cancelled")
    assert orch.stop("00000000-0000-0000-0000-00000000dead", "x") == "unknown"


def test_a_non_internal_network_is_refused_and_nothing_is_launched(world) -> None:  # type: ignore[no-untyped-def]
    world.docker.network_ok = False
    run_id = world.register()
    orch = world.orch()
    go(world, orch, submission(run_id))
    assert world.launched == [] and states(world, run_id) == [("queued", None), ("failed", "DockerError")]
    assert world.docker.removed == [run_id]                                           # cleanup still runs


def test_the_orchestrator_never_leaves_a_run_unfinished_after_an_internal_error(world, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from orchestrator import core
    run_id = world.register()
    monkeypatch.setattr(core, "build_manifest", lambda **_k: (_ for _ in ()).throw(RuntimeError(f"boom {CANARY}")))
    orch = world.orch()
    go(world, orch, submission(run_id))
    assert states(world, run_id)[-1] == ("failed", "RuntimeError") and CANARY not in repr(world.repo.runs)


def test_submit_is_idempotent_per_run_id(world) -> None:  # type: ignore[no-untyped-def]
    run_id = world.register()
    orch = world.orch()
    first = orch.submit(submission(run_id))
    again = orch.submit(submission(run_id))                          # the API retries an ambiguous submit
    assert first["status"] == "queued" and "duplicate" not in first
    assert again == {"run_id": run_id, "status": "queued", "duplicate": True}
    assert orch.scheduler.snapshot()["queued"] == [run_id]           # queued once
    assert states(world, run_id) == [("queued", None)]               # one queued event, not two
    orch.tick()
    assert orch.wait_idle(30)
    assert orch.submit(submission(run_id)) == {"run_id": run_id, "status": "finished", "duplicate": True}
    assert len(world.launched) == 1                                   # and started exactly once


def test_a_repeated_submit_for_another_user_is_still_a_conflict(world) -> None:  # type: ignore[no-untyped-def]
    run_id = world.register()
    orch = world.orch()
    orch.submit(submission(run_id, user="owner"))
    with pytest.raises(Duplicate):
        orch.submit(submission(run_id, user="someone-else", ip="other"))
    assert orch.scheduler.snapshot()["queued"] == [run_id]
