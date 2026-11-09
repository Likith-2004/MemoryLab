"""Sessions and the dream job as the log, replay, verification and export see them."""

import json
import uuid
from typing import Any

from labharness import Limits, Session
from labharness.agent import AgentLoop, Caps, ListSink, RunConfig, SessionsConfig
from labharness.memory import MemoryConfig
from labharness.presets import get_preset
from labharness.providers import FakeProvider
from labharness.screenshots import ScreenshotPolicy

from helpers import HIDE, press, produce_run, step
from runlog.client import LocalLogSink
from runlog.eventlog import EventLogReader, EventLogWriter
from runlog.manifest import build_manifest, manifest_sha256, seal_manifest
from runlog.replay import compute_score, memory_rows, memory_tree_at, verify_log
from runlog.store import LocalObjectStore


def mem(path: str, content: str) -> tuple[str, dict[str, Any]]:
    return ("mem_write", {"path": path, "content": content})


def run_logged(game_bundle, tmp_path, script, sessions, steps=5, max_tokens=4_000_000):  # type: ignore[no-untyped-def]
    store = LocalObjectStore(tmp_path / "s")
    run_id = str(uuid.uuid4())
    config = RunConfig("fake", "fake-model", "general@1", MemoryConfig(), observation=HIDE, screenshots=ScreenshotPolicy("never"),
                       caps=Caps(max_steps=steps, max_tokens=max_tokens), sessions=sessions)
    writer = EventLogWriter(store, run_id)
    writer.append([("run.state", 0, {"state": "starting"})])
    with Session(game_bundle, Limits(), config.screenshots) as session:
        AgentLoop(session, FakeProvider(script), config, get_preset("general@1"), LocalLogSink(writer), run_id=run_id).run()
    writer.close()
    return list(EventLogReader(store, run_id).iter_events()), config


def test_dream_writes_are_memory_mutations_for_replay_and_verification(game_bundle, tmp_path) -> None:  # type: ignore[no-untyped-def]
    script = [step(mem("a.md", "one")), step(mem("a.md", "two")), step(("wait", {"frames": 1})),
              step(mem("a.md", "three"), ("mem_delete", {"path": "zzz"}), mem("global.md", "pin")), step(text="end"), step(("wait", {"frames": 1})), step(("wait", {"frames": 1}))]
    events, _ = run_logged(game_bundle, tmp_path, script, SessionsConfig(session_len=3, dream=True))
    assert verify_log(events) == []                                                         # lifecycle with session_boundary/dreaming is legal, versions consecutive
    rows = memory_rows(events)
    assert [(r["path"], r["version"], r["rev"]) for r in rows] == [("a.md", 1, 1), ("a.md", 2, 2), ("a.md", 3, 3), ("global.md", 1, 4)]
    tree = memory_tree_at(events)
    assert tree["a.md"]["content"] == "three" and tree["global.md"]["content"] == "pin"     # the dream's rows are in the tree


def test_a_dream_only_change_leaves_the_agents_own_memory_metrics_untouched(game_bundle, tmp_path) -> None:  # type: ignore[no-untyped-def]
    agent_steps = [step(mem("a.md", "one")), step(mem("b.md", "two")), step(("wait", {"frames": 1}))]
    tail = [step(("wait", {"frames": 1})), step(("wait", {"frames": 1}))]
    without, _ = run_logged(game_bundle, tmp_path / "x", agent_steps + tail, SessionsConfig(session_len=3), steps=5)
    with_dream, _ = run_logged(game_bundle, tmp_path / "y", agent_steps + [step(mem("a.md", "merged"), ("mem_delete", {"path": "b.md"}), mem("global.md", "g")),
                                                                      step(text="end")] + tail, SessionsConfig(session_len=3, dream=True), steps=5)
    a, b = compute_score(without), compute_score(with_dream)
    assert a["memory_ops"] == b["memory_ops"] == 2 and a["dream_ops"] == 0 and b["dream_ops"] == 3
    assert a["progress_score"] == b["progress_score"] and a["steps"] == b["steps"] == 5
    assert b["tokens"] > a["tokens"]                                                        # the dream's tokens show up in the run's total
    assert sum(1 for e in with_dream if e.type in ("mem.write", "mem.delete")) == 2         # the agent's own events are still exactly its own
    assert sum(1 for e in with_dream if e.type in ("dream.write", "dream.delete")) == 3
    # the loop detectors react to the agent's memory events only
    assert [e.data.get("signal", e.type) for e in with_dream if e.type == "detector"] == [
        e.data.get("signal", e.type) for e in without if e.type == "detector"]


def test_replaying_the_detectors_over_a_dreaming_run_agrees(game_bundle, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from runlog.replay import verify_detectors
    script = [step(mem("a.md", "x")), step(("mem_delete", {"path": "a.md"})), step(("wait", {"frames": 1})), step(mem("b.md", "y")), step(text="end"),
              step(("wait", {"frames": 1})), step(("wait", {"frames": 1}))]
    events, _ = run_logged(game_bundle, tmp_path, script, SessionsConfig(session_len=3, dream=True))
    assert verify_detectors(events) == []


def test_the_manifest_hash_is_the_same_with_an_absent_or_an_explicit_default_sessions_block(game_bundle, tmp_path) -> None:  # type: ignore[no-untyped-def]
    absent = RunConfig("fake", "fake-model", "general@1")
    explicit = RunConfig.from_dict({"provider": "fake", "model_id": "fake-model", "preset_ref": "general@1", "mode": "demo",
                                    "sessions": {"session_len": None, "dream": False, "dream_max_tokens": 60000, "dream_max_turns": 6,
                                                 "checkpoint_every": None}})
    kw: dict[str, Any] = {"run_id": "r1", "bundle": game_bundle, "preset": get_preset("general@1"), "pyboy_version": "2.9.0", "max_total_frames": 1000}
    ma, me = build_manifest(config=absent, **kw), build_manifest(config=explicit, **kw)
    assert ma["config_hash"] == me["config_hash"] and manifest_sha256(ma) == manifest_sha256(me) and ma["sessions"] == {"session_len": None, "dream": False}
    on = build_manifest(config=RunConfig("fake", "fake-model", "general@1", sessions=SessionsConfig(session_len=4, dream=True)), **kw)
    assert on["sessions"]["dream_prompt"]["version"] == "dream@1" and on["config_hash"] != ma["config_hash"]
    seal_manifest(LocalObjectStore(tmp_path / "m"), "r1", on)
    assert json.dumps(on)                                                                       # serialisable


def test_a_default_run_still_produces_the_same_log_as_before(game_bundle, tmp_path) -> None:  # type: ignore[no-untyped-def]
    run_id, result, manifest, writer = produce_run(game_bundle, LocalObjectStore(tmp_path / "p"), run_id=str(uuid.uuid4()))
    events = list(EventLogReader(LocalObjectStore(tmp_path / "p"), run_id).iter_events())
    assert verify_log(events) == [] and manifest["sessions"] == {"session_len": None, "dream": False}
    assert not any(e.type in ("session.boundary", "checkpoint", "fork.start") or e.type.startswith("dream.") for e in events)
    assert press and ListSink and result.steps > 0


def test_the_dreams_tokens_are_in_the_score_even_when_the_dream_is_the_last_thing_that_spends(game_bundle, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from helpers import step as hstep
    script = [hstep(("wait", {"frames": 1}))] * 3 + [hstep(mem("g.md", "x"), tokens=250)]
    events, _ = run_logged(game_bundle, tmp_path, script, SessionsConfig(session_len=3, dream=True), steps=10, max_tokens=550)
    assert compute_score(events)["tokens"] == 550 and compute_score(events)["final_state"] == "capped"
    assert events[-1].type == "run.state" and not any(e.type == "model.call" and e.data["tokens_total"] == 550 for e in events)
