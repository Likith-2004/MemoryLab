"""Event model and strict validation. One JSON object per line: {seq, run_id, step, ts, type, data}."""

import json
import math
import re
from dataclasses import dataclass
from typing import Any

EVENT_TYPES = frozenset({
    "run.state", "obs", "model.call", "model.summary", "action", "mem.read", "mem.write", "mem.delete",
    "mem.reject", "session.boundary", "checkpoint", "detector", "cap.hit", "error",
})
_DREAM = re.compile(r"dream\.[a-z_]{1,24}")
RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_EVENT_BYTES = 64 * 1024
MAX_DEPTH = 8
MAX_STRING_CHARS = 32_000
MAX_KEY_CHARS = 100
MAX_STEP = 1_000_000


class EventRejected(ValueError):
    """An event failed validation; `reason` is a stable machine code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class Event:
    seq: int
    run_id: str
    step: int
    ts: str
    type: str
    data: dict[str, Any]

    def to_obj(self) -> dict[str, Any]:
        return {"seq": self.seq, "run_id": self.run_id, "step": self.step, "ts": self.ts, "type": self.type,
                "data": self.data}


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def is_valid_type(type_: object) -> bool:
    return isinstance(type_, str) and (type_ in EVENT_TYPES or _DREAM.fullmatch(type_) is not None)


def _check_json(value: Any, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise EventRejected("too_deep", f"nesting deeper than {MAX_DEPTH}")
    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EventRejected("non_finite_number")
        return
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            raise EventRejected("string_too_long")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise EventRejected("invalid_text", "unpaired surrogate") from None
        return
    if isinstance(value, list):
        for item in value:
            _check_json(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > MAX_KEY_CHARS:
                raise EventRejected("bad_key")
            _check_json(item, depth + 1)
        return
    raise EventRejected("not_json", type(value).__name__)


def validate_payload(type_: object, step: object, data: object) -> None:
    """Validate what a producer may send (seq, run_id and ts are assigned by the log)."""
    if not is_valid_type(type_):
        raise EventRejected("unknown_type")
    if isinstance(step, bool) or not isinstance(step, int) or not 0 <= step <= MAX_STEP:
        raise EventRejected("bad_step")
    if not isinstance(data, dict):
        raise EventRejected("data_not_object")
    _check_json(data, 0)
    if len(canonical(data)) > MAX_EVENT_BYTES:
        raise EventRejected("event_too_large")


def parse_line(line: str) -> Event:
    """Parse one stored line, re-checking structure (storage is not blindly trusted)."""
    try:
        obj = json.loads(line)
    except ValueError:
        raise EventRejected("corrupt_line") from None
    if not isinstance(obj, dict) or set(obj) != {"seq", "run_id", "step", "ts", "type", "data"}:
        raise EventRejected("corrupt_line", "wrong keys")
    seq, run_id, ts = obj["seq"], obj["run_id"], obj["ts"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise EventRejected("corrupt_line", "bad seq")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id) or not isinstance(ts, str):
        raise EventRejected("corrupt_line", "bad run_id or ts")
    validate_payload(obj["type"], obj["step"], obj["data"])
    return Event(seq, run_id, obj["step"], ts, obj["type"], obj["data"])
