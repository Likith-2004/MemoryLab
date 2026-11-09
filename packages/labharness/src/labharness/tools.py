"""Game tools exposed to the model (closed schemas) and routing of tool calls."""

from typing import Any

from .actions import Action, Press, Wait
from .constants import (
    BUTTONS,
    HOLD_FRAMES_MAX,
    HOLD_FRAMES_MIN,
    MAX_BUTTONS_PER_ACTION,
    WAIT_FRAMES_MAX,
)
from .errors import InvalidAction
from .memory import MemoryConfig
from .memory import tool_schemas as memory_tool_schemas
from .providers.base import ToolSchema

GAME_TOOLS = ("press", "wait")


def game_tool_schemas() -> list[ToolSchema]:
    return [
        ToolSchema(
            "press",
            "Press game buttons one after another (each is pressed, held for hold_frames, released). "
            f"At most {MAX_BUTTONS_PER_ACTION} buttons per call.",
            {"type": "object", "additionalProperties": False, "required": ["buttons", "hold_frames"],
             "properties": {
                 "buttons": {"type": "array", "minItems": 1, "maxItems": MAX_BUTTONS_PER_ACTION,
                             "items": {"type": "string", "enum": list(BUTTONS)}},
                 "hold_frames": {"type": "integer", "minimum": HOLD_FRAMES_MIN, "maximum": HOLD_FRAMES_MAX}}},
        ),
        ToolSchema(
            "wait", "Let game frames pass without pressing anything.",
            {"type": "object", "additionalProperties": False, "required": ["frames"],
             "properties": {"frames": {"type": "integer", "minimum": 1, "maximum": WAIT_FRAMES_MAX}}},
        ),
    ]


def all_tool_schemas(memory: MemoryConfig) -> tuple[ToolSchema, ...]:
    """The exact tool list the model sees. With memory disabled the memory tools are absent."""
    mem = [ToolSchema(t["name"], t["description"], t["input_schema"]) for t in memory_tool_schemas(memory)]
    return tuple(game_tool_schemas() + mem)


def memory_only_schemas(memory: MemoryConfig) -> tuple[ToolSchema, ...]:
    """What the dream job sees: the memory tools and nothing else (no game tool exists in its schema)."""
    return tuple(ToolSchema(t["name"], t["description"], t["input_schema"]) for t in memory_tool_schemas(memory))


def action_from_call(name: str, args: dict[str, Any]) -> Action:
    """Build a validated game action from a model tool call (closed schema; extra keys fail)."""
    if "tool" in args:
        raise InvalidAction("unexpected argument 'tool'")
    if name == "press":
        if set(args) != {"buttons", "hold_frames"}:
            raise InvalidAction("press takes exactly: buttons, hold_frames")
        buttons = args["buttons"]
        if not isinstance(buttons, list):
            raise InvalidAction("buttons must be a list")
        return Press(tuple(buttons), args["hold_frames"])
    if name == "wait":
        if set(args) != {"frames"}:
            raise InvalidAction("wait takes exactly: frames")
        return Wait(args["frames"])
    raise InvalidAction("unknown tool")
