import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from labharness.detectors import (
    CapRejections,
    DetectorSet,
    FailedActions,
    LoopEvent,
    MemoryChurn,
    MemoryPlayRatio,
    PositionLoop,
    StaleNoteRead,
    detect,
)


def obs(step: int, pos: tuple[int, int, int] = (1, 0, 0), changed: bool = True) -> LoopEvent:
    return LoopEvent("obs", step, {"map_id": pos[0], "x": pos[1], "y": pos[2], "changed": changed})


def action(step: int, tool: str = "press", args: object = "Right", ok: bool = True, frames: int = 8) -> LoopEvent:
    return LoopEvent("action", step, {"tool": tool, "args": args, "ok": ok, "frames": frames})


def feed(det, events):  # type: ignore[no-untyped-def]
    out = []
    for e in events:
        out += det.observe(e)
    return out


# ---- position loops ------------------------------------------------------------------------------
def test_stuck_loop_fires_once_at_the_threshold_and_rearms() -> None:
    det = PositionLoop(streak=4)
    out = feed(det, [obs(i) for i in range(1, 9)])
    assert [(e.step, e.data["pattern"], e.data["count"]) for e in out] == [(4, "stuck", 4)]
    out = feed(det, [obs(9, (1, 1, 0)), *[obs(i, (1, 1, 0)) for i in range(10, 14)]])
    assert len(out) == 1 and out[0].data["position"] == [1, 1, 0]  # a new episode after moving


def test_stuck_below_threshold_and_moving_do_not_fire() -> None:
    det = PositionLoop(streak=4)
    assert feed(det, [obs(i) for i in range(1, 4)]) == []
    assert feed(PositionLoop(streak=4), [obs(i, (1, i, 0)) for i in range(1, 30)]) == []


def test_oscillation_between_two_cells_is_a_loop() -> None:
    det = PositionLoop(streak=8, window=6)
    out = feed(det, [obs(i, (1, i % 2, 0)) for i in range(1, 12)])
    assert len(out) == 1 and out[0].data["pattern"] == "oscillation" and out[0].step == 6
    assert feed(PositionLoop(streak=8, window=6), [obs(i, (1, i % 3, 0)) for i in range(1, 20)]) == []  # three cells: fine


# ---- failed actions ------------------------------------------------------------------------------
def test_repeated_noeffect_press_fires_once_per_streak() -> None:
    det = FailedActions(limit=3)
    events = []
    for i in range(1, 7):
        events += [action(i), obs(i, changed=False)]
    out = feed(det, events)
    assert len(out) == 1 and out[0].data["count"] == 3 and out[0].data["tool"] == "press"


def test_successful_or_varied_actions_do_not_fire() -> None:
    det = FailedActions(limit=3)
    events = []
    for i in range(1, 9):  # effect every time
        events += [action(i), obs(i, changed=True)]
    assert feed(det, events) == []
    det = FailedActions(limit=3)
    events = []
    for i in range(1, 9):  # alternating args: never 3 identical in a row
        events += [action(i, args="Right" if i % 2 else "Left"), obs(i, changed=False)]
    assert feed(det, events) == []


def test_rejected_actions_count_and_waits_do_not() -> None:
    det = FailedActions(limit=3)
    out = feed(det, [action(1, ok=False, frames=0), action(1, ok=False, frames=0), action(1, ok=False, frames=0), obs(1)])
    assert len(out) == 1
    det = FailedActions(limit=3)
    waits = []
    for i in range(1, 8):
        waits += [action(i, "wait", 30), obs(i, changed=False)]
    assert feed(det, waits) == []  # waiting without change is not a failed action


def test_a_success_resets_the_streak() -> None:
    det = FailedActions(limit=3)
    events = [action(1), obs(1, changed=False), action(2), obs(2, changed=False), action(3), obs(3, changed=True),
              action(4), obs(4, changed=False), action(5), obs(5, changed=False)]
    assert feed(det, events) == []


# ---- memory ratio ---------------------------------------------------------------------------------
def test_memory_vs_play_ratio() -> None:
    det = MemoryPlayRatio(window=10, high=0.5, low=0.4)
    events = []
    for i in range(1, 13):  # memory-only steps
        events += [LoopEvent("mem.write", i, {"path": "a"}), obs(i)]
    out = feed(det, events)
    assert len(out) == 1 and out[0].data["ratio"] > 0.5 and out[0].step == 10
    det = MemoryPlayRatio(window=10)
    events = []
    for i in range(1, 30):  # play every step, memory alongside it
        events += [LoopEvent("mem.read", i, {}), action(i), obs(i)]
    assert feed(det, events) == []


def test_ratio_rearms_after_recovery() -> None:
    det = MemoryPlayRatio(window=5, high=0.5, low=0.4)
    events = []
    for i in range(1, 6):
        events += [LoopEvent("mem.read", i, {}), obs(i)]
    for i in range(6, 12):
        events += [action(i), obs(i)]
    for i in range(12, 18):
        events += [LoopEvent("mem.read", i, {}), obs(i)]
    assert len(feed(det, events)) == 2


# ---- churn, stale reads, cap rejections ---------------------------------------------------------------
def test_churn_is_a_quick_delete_after_write() -> None:
    det = MemoryChurn(horizon=5)
    out = feed(det, [LoopEvent("mem.write", 1, {"path": "a"}), LoopEvent("mem.delete", 4, {"path": "a"})])
    assert out[0].data == {"kind": "memory_churn", "path": "a", "written_step": 1, "deleted_step": 4}
    assert feed(MemoryChurn(horizon=5), [LoopEvent("mem.write", 1, {"path": "a"}), LoopEvent("mem.delete", 20, {"path": "a"})]) == []
    assert feed(MemoryChurn(), [LoopEvent("mem.delete", 3, {"path": "never_written"})]) == []


def read(step: int, path: str = "a", found: bool = True) -> LoopEvent:
    return LoopEvent("mem.read", step, {"op": "read", "path": path, "found": found})


def test_stale_read_after_a_map_change_and_only_once() -> None:
    det = StaleNoteRead()
    out = feed(det, [obs(1, (1, 0, 0)), LoopEvent("mem.write", 1, {"path": "a"}), read(2), obs(3, (2, 0, 0)), read(4), read(5)])
    assert len(out) == 1 and out[0].data == {"kind": "stale_note_read", "path": "a", "written_step": 1,
                                             "written_map": 1, "current_map": 2}
    out = feed(det, [LoopEvent("mem.write", 6, {"path": "a"}), read(7)])  # rewritten on the new map: fresh
    assert out == []


def test_not_found_reads_and_deleted_notes_are_not_stale() -> None:
    det = StaleNoteRead()
    assert feed(det, [obs(1), LoopEvent("mem.write", 1, {"path": "a"}), obs(2, (2, 0, 0)), read(3, found=False)]) == []
    det = StaleNoteRead()
    assert feed(det, [obs(1), LoopEvent("mem.write", 1, {"path": "a"}), LoopEvent("mem.delete", 1, {"path": "a"}),
                      obs(2, (2, 0, 0)), read(3)]) == []


def test_cap_rejection_thresholds() -> None:
    det = CapRejections(thresholds=(2, 4))
    rej = [LoopEvent("mem.reject", i, {"reason": "total_cap"}) for i in range(1, 6)]
    assert [e.data["count"] for e in feed(det, rej)] == [2, 4]
    other = [LoopEvent("mem.reject", i, {"reason": "invalid_path"}) for i in range(10)]
    assert feed(CapRejections(thresholds=(1,)), other) == []


# ---- incremental == batch ---------------------------------------------------------------------------------
event_strategy = st.one_of(
    st.builds(lambda s, m, x: obs(s, (m, x, 0), changed=x % 2 == 0), st.integers(0, 50), st.integers(1, 2), st.integers(0, 2)),
    st.builds(lambda s, ok, a: action(s, args=a, ok=ok), st.integers(0, 50), st.booleans(), st.sampled_from(["L", "R"])),
    st.builds(lambda s, p: LoopEvent("mem.write", s, {"path": p}), st.integers(0, 50), st.sampled_from(["a", "b"])),
    st.builds(lambda s, p: LoopEvent("mem.delete", s, {"path": p}), st.integers(0, 50), st.sampled_from(["a", "b"])),
    st.builds(lambda s, p: read(s, p), st.integers(0, 50), st.sampled_from(["a", "b"])),
    st.builds(lambda s: LoopEvent("mem.reject", s, {"reason": "file_cap"}), st.integers(0, 50)),
)


@settings(max_examples=300, deadline=None)
@given(st.lists(event_strategy, max_size=80))
def test_batch_detection_equals_live_detection(events: list[LoopEvent]) -> None:
    live = DetectorSet()
    online = [s for e in events for s in live.observe(e)]
    assert detect(events) == online
    assert detect(events + [LoopEvent("detector", 0, {"kind": "x"})]) == online  # detector events are ignored on replay


def test_detectors_ignore_unrelated_events() -> None:
    assert detect([LoopEvent("model.call", 1, {}), LoopEvent("cap.hit", 1, {}), LoopEvent("run.state", 0, {})]) == []


@pytest.mark.parametrize("cls", [PositionLoop, FailedActions, MemoryPlayRatio, MemoryChurn, StaleNoteRead, CapRejections])
def test_every_detector_tolerates_an_empty_stream(cls: type) -> None:
    assert cls().observe(LoopEvent("model.summary", 1, {"text": "x"})) == []
