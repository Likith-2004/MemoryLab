"""Closed-schema action validation. Unknown tools, keys, buttons and out-of-range values fail."""

from dataclasses import dataclass

from .constants import (
    ACTION_FRAMES_MAX,
    BUTTONS,
    HOLD_FRAMES_MAX,
    HOLD_FRAMES_MIN,
    MAX_BUTTONS_PER_ACTION,
    WAIT_FRAMES_MAX,
)
from .errors import InvalidAction


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Press:
    """Press each button in order: press, hold, release, RELEASE_FRAMES, next."""

    buttons: tuple[str, ...]
    hold_frames: int

    def __post_init__(self) -> None:
        if not isinstance(self.buttons, tuple) or not 1 <= len(self.buttons) <= MAX_BUTTONS_PER_ACTION:
            raise InvalidAction(f"press needs 1..{MAX_BUTTONS_PER_ACTION} buttons")
        for button in self.buttons:
            if not isinstance(button, str) or button not in BUTTONS:
                raise InvalidAction("unknown button")
        if not _is_int(self.hold_frames) or not HOLD_FRAMES_MIN <= self.hold_frames <= HOLD_FRAMES_MAX:
            raise InvalidAction(f"hold_frames must be an int in {HOLD_FRAMES_MIN}..{HOLD_FRAMES_MAX}")

    def cost(self, release_frames: int) -> int:
        return len(self.buttons) * (self.hold_frames + release_frames)


@dataclass(frozen=True)
class Wait:
    frames_to_wait: int

    def __post_init__(self) -> None:
        if not _is_int(self.frames_to_wait) or not 1 <= self.frames_to_wait <= WAIT_FRAMES_MAX:
            raise InvalidAction(f"wait frames must be an int in 1..{WAIT_FRAMES_MAX}")

    def cost(self, release_frames: int) -> int:  # noqa: ARG002 - same signature as Press.cost
        return self.frames_to_wait


Action = Press | Wait


def checked_cost(action: "Action", release_frames: int) -> int:
    """Total frames the action takes in a game with this release-frame count (<= 600)."""
    cost = action.cost(release_frames)
    if cost > ACTION_FRAMES_MAX:
        raise InvalidAction(f"action would take {cost} frames (max {ACTION_FRAMES_MAX})")
    return cost


def parse_action(obj: object) -> Action:
    """Build an Action from untrusted data, e.g. a model tool call."""
    if not isinstance(obj, dict):
        raise InvalidAction("action must be an object")
    tool = obj.get("tool")
    if tool == "press":
        if set(obj) != {"tool", "buttons", "hold_frames"}:
            raise InvalidAction("press takes exactly: tool, buttons, hold_frames")
        buttons = obj["buttons"]
        if not isinstance(buttons, list):
            raise InvalidAction("buttons must be a list")
        return Press(tuple(buttons), obj["hold_frames"])
    if tool == "wait":
        if set(obj) != {"tool", "frames"}:
            raise InvalidAction("wait takes exactly: tool, frames")
        return Wait(obj["frames"])
    raise InvalidAction("unknown tool")


def action_to_dict(action: Action) -> dict[str, object]:
    if isinstance(action, Press):
        return {"tool": "press", "buttons": list(action.buttons), "hold_frames": action.hold_frames}
    return {"tool": "wait", "frames": action.frames_to_wait}
