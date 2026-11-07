"""Produce a real run (fake provider, real emulator) into an object store for artifact tests."""

import itertools
import uuid
from typing import Any

from labharness import Limits, Session
from labharness.agent import AgentLoop, Caps, RunConfig
from labharness.memory import MemoryConfig
from labharness.presets import get_preset
from labharness.prompt import ObservationConfig
from labharness.providers import FakeProvider, ScriptedStep, Usage
from labharness.screenshots import ScreenshotPolicy

from runlog.client import LocalLogSink
from runlog.eventlog import EventLogWriter
from runlog.manifest import build_manifest, seal_manifest

HIDE = ObservationConfig(hide_extras=("input_frames", "frame_counter"))


def step(*calls: tuple[str, dict[str, Any]], text: str = "", tokens: int = 100) -> ScriptedStep:
    return ScriptedStep(tuple(calls), text, Usage(input_tokens=tokens - 20, output_tokens=20))


def press(*buttons: str, hold: int = 1) -> tuple[str, dict[str, Any]]:
    return ("press", {"buttons": list(buttons), "hold_frames": hold})


def default_script() -> list[ScriptedStep]:
    return [
        step(press("Right", "Right", "Right"), ("mem_write", {"path": "notes/a.md", "content": "go right\n"}), text="east"),
        step(press("Down", "Down", "Down")),                                  # reaches the first milestone
        step(("mem_read", {"path": "notes/a.md"}), press("Start", hold=3)),
        step(press("B", hold=2), ("shell", {"cmd": "ls"})),                   # unknown tool, logged
        step(("mem_write", {"path": "notes/a.md", "content": "go right\ndoor at 3,3\n"}), press("A")),
        step(("mem_delete", {"path": "notes/a.md"})),
        step(press("Left")), step(press("Left")), step(press("Left")), step(press("Left")),   # no-effect streak
    ]


def make_clock():  # type: ignore[no-untyped-def]
    """Deterministic, strictly increasing timestamps (one second apart)."""
    from datetime import UTC, datetime, timedelta
    counter = itertools.count(1)
    start = datetime(2026, 11, 8, tzinfo=UTC)
    return lambda: (start + timedelta(seconds=next(counter))).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def produce_run(game_bundle, store, *, script=None, run_id=None, max_steps=None, extra_event=None, content_filter=None):  # type: ignore[no-untyped-def]
    """Seal the manifest, write orchestrator 'starting', run the loop into the log, close the log."""
    run_id = run_id or str(uuid.uuid4())
    script = script if script is not None else default_script()
    config = RunConfig("fake", "fake-model", "general@1", MemoryConfig(), observation=HIDE,
                       screenshots=ScreenshotPolicy("every_k_or_change", k=5, frame_scale=1),
                       caps=Caps(max_steps=max_steps or len(script)))
    preset = get_preset("general@1")
    writer = EventLogWriter(store, run_id, clock=make_clock())
    with Session(game_bundle, Limits(), config.screenshots) as session:
        manifest = build_manifest(run_id=run_id, config=config, bundle=game_bundle, preset=preset,
                                  pyboy_version="2.9.0", max_total_frames=session.limits.max_total_frames)
        seal_manifest(store, run_id, manifest)
        writer.append([("run.state", 0, {"state": "starting"})])
        if extra_event:
            writer.append([extra_event])
        loop = AgentLoop(session, FakeProvider(script), config, preset, LocalLogSink(writer), run_id=run_id,
                         content_filter=content_filter)
        result = loop.run()
        frames = [(r.step, r.frame_png) for r in []]  # noqa: F841 - frames are uploaded by the worker in production
    writer.close()
    return run_id, result, manifest, writer
