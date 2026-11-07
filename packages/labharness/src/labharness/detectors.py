"""Inspector signals computed from the event stream, never from model self-report.

Every detector is incremental (`observe(event) -> [detector events]`) so the loop can emit
signals live, and a pure fold over a recorded log (`detect(events)`) gives identical results,
which is how replay reproduces them.

Event inputs (types/data produced by the loop): `obs` {map_id, x, y, dialog_active, changed},
`action` {tool, args, ok, frames}, `mem.read|write|delete|reject`.
"""

import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol

CAP_REASONS = frozenset({"file_cap", "files_cap", "total_cap", "history_cap"})


@dataclass(frozen=True)
class LoopEvent:
    type: str
    step: int
    data: dict[str, Any]


class Detector(Protocol):
    def observe(self, e: LoopEvent) -> list[LoopEvent]: ...


def _signal(step: int, kind: str, **data: Any) -> LoopEvent:
    return LoopEvent("detector", step, {"kind": kind, **data})


class PositionLoop:
    """Same map+xy for `streak` consecutive observations, or bouncing between at most two
    cells over a `window`. Each episode is reported once and re-arms when the pattern breaks."""

    def __init__(self, streak: int = 8, window: int = 12) -> None:
        self.streak, self.window = streak, window
        self._recent: deque[tuple[int, int, int]] = deque(maxlen=window)
        self._run = 0
        self._last: tuple[int, int, int] | None = None
        self._stuck_fired = False
        self._osc_fired = False

    def observe(self, e: LoopEvent) -> list[LoopEvent]:
        if e.type != "obs":
            return []
        pos = (e.data["map_id"], e.data["x"], e.data["y"])
        out: list[LoopEvent] = []
        self._run = self._run + 1 if pos == self._last else 1
        self._last = pos
        self._recent.append(pos)
        if self._run < self.streak:
            self._stuck_fired = False
        elif not self._stuck_fired:
            self._stuck_fired = True
            out.append(_signal(e.step, "loop", pattern="stuck", position=list(pos), count=self._run))
        distinct = set(self._recent)
        oscillating = len(self._recent) == self.window and len(distinct) == 2 and self._run < self.streak
        if not oscillating:
            self._osc_fired = False
        elif not self._osc_fired:
            self._osc_fired = True
            out.append(_signal(e.step, "loop", pattern="oscillation", positions=sorted(list(p) for p in distinct),
                               window=self.window))
        return out


class FailedActions:
    """The same action failing `limit` times in a row. Failing = rejected by validation, or a
    press that changed nothing observable (map, position, dialogue)."""

    def __init__(self, limit: int = 4) -> None:
        self.limit = limit
        self._pending: list[tuple[str, bool]] = []
        self._key: str | None = None
        self._count = 0
        self._fired = False

    def observe(self, e: LoopEvent) -> list[LoopEvent]:
        if e.type == "action":
            key = json.dumps([e.data["tool"], e.data.get("args")], sort_keys=True, default=str)
            self._pending.append((key, bool(e.data["ok"])))
            return []
        if e.type != "obs":
            return []
        out: list[LoopEvent] = []
        no_effect = not e.data.get("changed")
        pending, self._pending = self._pending, []
        for key, ok in pending:
            is_press = key.startswith('["press"')
            # The step's state change cannot be attributed to one call, so only a step with a
            # single game action counts a no-effect press as a failure.
            failed = (not ok) or (is_press and ok and no_effect and len(pending) == 1)
            if not failed:
                self._key, self._count, self._fired = None, 0, False
                continue
            if key == self._key:
                self._count += 1
            else:
                self._key, self._count, self._fired = key, 1, False
            if self._count >= self.limit and not self._fired:
                self._fired = True
                tool, args = json.loads(key)
                out.append(_signal(e.step, "repeated_failed_action", tool=tool, args=args, count=self._count))
        return out


class MemoryPlayRatio:
    """Share of the last `window` steps that used memory tools but made no game move."""

    def __init__(self, window: int = 20, high: float = 0.5, low: float = 0.4) -> None:
        self.window, self.high, self.low = window, high, low
        self._steps: deque[bool] = deque(maxlen=window)
        self._mem = 0
        self._play = 0
        self._fired = False

    def observe(self, e: LoopEvent) -> list[LoopEvent]:
        if e.type.startswith("mem."):
            self._mem += 1
        elif e.type == "action" and e.data.get("frames", 0) > 0:
            self._play += 1
        elif e.type == "obs":
            self._steps.append(self._mem > 0 and self._play == 0)
            self._mem = self._play = 0
            if len(self._steps) < self.window:
                return []
            ratio = sum(self._steps) / self.window
            if ratio > self.high and not self._fired:
                self._fired = True
                return [_signal(e.step, "memory_play_ratio", ratio=round(ratio, 3), window=self.window)]
            if ratio < self.low:
                self._fired = False
        return []


class MemoryChurn:
    """A file deleted within `horizon` steps of its last write."""

    def __init__(self, horizon: int = 10) -> None:
        self.horizon = horizon
        self._written: dict[str, int] = {}

    def observe(self, e: LoopEvent) -> list[LoopEvent]:
        if e.type == "mem.write":
            self._written[e.data["path"]] = e.step
        elif e.type == "mem.delete":
            at = self._written.pop(e.data["path"], None)
            if at is not None and e.step - at <= self.horizon:
                return [_signal(e.step, "memory_churn", path=e.data["path"], written_step=at, deleted_step=e.step)]
        return []


class StaleNoteRead:
    """Proxy for 'note contradicted by later state': a note is read while the player is on a
    different map than the one it was written on, and it has not been rewritten since."""

    def __init__(self) -> None:
        self._map: int | None = None
        self._written: dict[str, tuple[int, int]] = {}  # path -> (step, map at write)
        self._reported: set[tuple[str, int, int]] = set()

    def observe(self, e: LoopEvent) -> list[LoopEvent]:
        if e.type == "obs":
            self._map = e.data["map_id"]
        elif e.type == "mem.write" and self._map is not None:
            self._written[e.data["path"]] = (e.step, self._map)
        elif e.type == "mem.delete":
            self._written.pop(e.data["path"], None)
        elif e.type == "mem.read" and e.data.get("op") == "read" and e.data.get("found"):
            info = self._written.get(e.data["path"])
            if info and self._map is not None and info[1] != self._map:
                key = (e.data["path"], info[0], self._map)
                if key not in self._reported:
                    self._reported.add(key)
                    return [_signal(e.step, "stale_note_read", path=e.data["path"], written_step=info[0],
                                    written_map=info[1], current_map=self._map)]
        return []


class CapRejections:
    """Escalating notice as `mem.reject` cap errors pile up."""

    def __init__(self, thresholds: tuple[int, ...] = (3, 10, 25)) -> None:
        self.thresholds = thresholds
        self._count = 0

    def observe(self, e: LoopEvent) -> list[LoopEvent]:
        if e.type == "mem.reject" and e.data.get("reason") in CAP_REASONS:
            self._count += 1
            if self._count in self.thresholds:
                return [_signal(e.step, "cap_rejections", count=self._count)]
        return []


class DetectorSet:
    def __init__(self) -> None:
        self._detectors: list[Detector] = [PositionLoop(), FailedActions(), MemoryPlayRatio(), MemoryChurn(), StaleNoteRead(), CapRejections()]

    def observe(self, event: LoopEvent) -> list[LoopEvent]:
        out: list[LoopEvent] = []
        for d in self._detectors:
            out.extend(d.observe(event))
        return out


def detect(events: list[LoopEvent]) -> list[LoopEvent]:
    """Recompute every signal from a recorded event list (replay)."""
    ds = DetectorSet()
    out: list[LoopEvent] = []
    for e in events:
        if e.type != "detector":
            out.extend(ds.observe(e))
    return out
