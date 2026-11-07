import io
import json

import pytest
from PIL import Image

from helpers import route
from labharness import Limits, Press, Session, Wait, parse_action
from labharness.errors import (
    CheckpointError,
    EmulatorFailure,
    FrameLimitExceeded,
    InvalidAction,
    StepLimitExceeded,
)
from labharness.screenshots import ScreenshotPolicy


def test_route_reaches_all_milestones_with_expected_score(bundle) -> None:  # type: ignore[no-untyped-def]
    with Session(bundle) as s:
        trace = s.run_script(route())
    assert trace.score == 5 and trace.tiebreak_steps == 15
    first = {bit: step for bit, step in s.tracker.first_step.items()}
    assert first == {0: 2, 1: 8, 2: 9, 3: 12, 8: 15}
    final = trace.entries[-1]["snapshot"]
    assert (final["map_id"], final["x"], final["y"]) == (2, 4, 14)
    assert final["milestones"] == 0x10F and final["extras"]["flags"]["set"] == ["pressed_a"]
    assert final["anomalies"] == []


def test_step_zero_is_the_fixed_start_state(bundle) -> None:  # type: ignore[no-untyped-def]
    with Session(bundle) as s:
        rec = s.open()
    snap = rec.snapshot
    assert rec.step == 0 and rec.frames == 0 and rec.action is None and rec.score == 0
    assert (snap.map_id, snap.x, snap.y, snap.dialog_active, snap.milestones) == (1, 0, 0, False, 0)
    assert snap.extras["party"][0] == {"species": 1, "level": 5, "hp": 20, "max_hp": 20}


def test_step_cap_stops_exactly_and_leaves_state_untouched(bundle) -> None:  # type: ignore[no-untyped-def]
    with Session(bundle, Limits(max_steps=3)) as s:
        s.open()
        for _ in range(3):
            s.execute(Wait(1))
        digest = s.emulator.ram_digest()
        with pytest.raises(StepLimitExceeded):
            s.execute(Wait(1))
        assert s.step == 3 and s.emulator.ram_digest() == digest


def test_frame_cap_through_session(bundle) -> None:  # type: ignore[no-untyped-def]
    with Session(bundle, Limits(max_total_frames=50)) as s:
        s.open()
        s.execute(Wait(50))
        with pytest.raises(FrameLimitExceeded):
            s.execute(Wait(1))
        assert s.step == 1 and s.emulator.frames == 50


def test_execute_before_open_fails(bundle) -> None:  # type: ignore[no-untyped-def]
    with Session(bundle) as s:
        with pytest.raises(EmulatorFailure):
            s.execute(Wait(1))
        with pytest.raises(CheckpointError):
            s.checkpoint()


def test_invalid_model_output_never_reaches_the_emulator(bundle) -> None:  # type: ignore[no-untyped-def]
    with Session(bundle) as s:
        s.open()
        digest = s.emulator.ram_digest()
        for raw in ({"tool": "press", "buttons": ["A"] * 9, "hold_frames": 1},
                    {"tool": "press", "buttons": ["A"], "hold_frames": 61},
                    {"tool": "wait", "frames": 601}, {"tool": "mem_read", "path": "x"}):
            with pytest.raises(InvalidAction):
                s.execute(parse_action(raw))
        assert s.step == 0 and s.emulator.ram_digest() == digest and s.emulator.frames == 0


def test_screenshot_policy_integration(bundle) -> None:  # type: ignore[no-untyped-def]
    policy = ScreenshotPolicy("every_k_or_change", k=100, frame_scale=2)
    with Session(bundle, screenshots=policy) as s:
        records = [s.open()] + [s.execute(a) for a in route()]
    captured = [r.step for r in records if r.frame_png]
    # step 0; dialogue opens (3) and closes (5); map change (8); dialogue opens (9) and closes (10)
    assert captured == [0, 3, 5, 8, 9, 10]
    img = Image.open(io.BytesIO(records[0].frame_png))  # type: ignore[arg-type]
    assert img.size == (320, 288) and img.mode == "RGB"
    assert all((r.frame_png is None) == (r.frame_sha256 is None) for r in records)


def test_screenshots_never_and_scale(bundle) -> None:  # type: ignore[no-untyped-def]
    with Session(bundle, screenshots=ScreenshotPolicy("never")) as s:
        assert s.open().frame_png is None and s.execute(Wait(5)).frame_png is None
    with Session(bundle, screenshots=ScreenshotPolicy("every_k", k=1, frame_scale=1)) as s:
        rec = s.open()
        assert Image.open(io.BytesIO(rec.frame_png)).size == (160, 144)  # type: ignore[arg-type]


def test_frames_follow_the_game_state(bundle) -> None:  # type: ignore[no-untyped-def]
    """The map-2 palette differs, so a frame before and after the map change must differ."""
    policy = ScreenshotPolicy("every_k", k=1, frame_scale=1)
    with Session(bundle, screenshots=policy) as s:
        before = s.open()
        for button_group in (("Right",) * 8, ("Right",) * 8):
            rec = s.execute(Press(button_group, 1))
    assert rec.snapshot.map_id == 2 and before.frame_sha256 != rec.frame_sha256


def test_trace_is_json_serialisable_and_manifest_is_complete(bundle) -> None:  # type: ignore[no-untyped-def]
    with Session(bundle) as s:
        trace = s.run_script(route()[:3])
        m = s.manifest()
    json.dumps(list(trace.entries))
    assert m["game_seed"] is None and m["pyboy_version"] == "2.9.0" and m["release_frames"] == 7
    assert m["game"]["fixture"] is True and m["game"]["rom_sha256"] == bundle.rom_info.sha256
    assert m["start_state_sha256"] == bundle.start_state_sha256
    assert "seed" not in m and trace.manifest_sha256 == s.manifest_sha256()


def test_fake_provider_can_drive_the_session(bundle) -> None:  # type: ignore[no-untyped-def]
    """No model API is needed: any iterator of tool-call dicts works as a scripted provider."""
    calls = iter([{"tool": "press", "buttons": ["Right", "Down"], "hold_frames": 2},
                  {"tool": "wait", "frames": 10}])
    with Session(bundle) as s:
        trace = s.run_script(parse_action(c) for c in calls)
    assert len(trace.entries) == 3 and trace.entries[1]["frames"] == 2 * (2 + 7)
