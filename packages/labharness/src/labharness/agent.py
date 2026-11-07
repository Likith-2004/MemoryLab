"""The agent step loop: observe -> prompt -> provider -> execute -> meter -> emit.

Hard caps (steps, tokens, wall time, frames) are enforced here, by the platform, not by the
prompt. A cap that trips mid-step lets that step finish; the run then moves to `stopping` and
ends `capped`. Token counts come from provider-reported usage, never estimates, so the token
cap can be overshot by at most one model call.
"""

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .actions import action_to_dict
from .constants import canonical_json
from .detectors import DetectorSet, LoopEvent
from .errors import FrameLimitExceeded, HarnessError, InvalidAction
from .memory import TOOL_NAMES, MemoryConfig, MemoryService, MemoryStore
from .memory_paths import Reject, check_path, safe_echo
from .presets import Preset
from .prompt import (
    MAX_SUMMARY_CHARS,
    ContextBuilder,
    ObservationConfig,
    format_observation,
    result_text,
    system_blocks,
)
from .providers.base import (
    Provider,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    ToolCall,
    ToolResultBlock,
)
from .screenshots import ScreenshotPolicy
from .session import Session, Trace
from .telemetry import Snapshot
from .tools import GAME_TOOLS, action_from_call, all_tool_schemas

MAX_ARGS_ECHO = 300


@dataclass(frozen=True)
class Caps:
    max_steps: int = 2000
    max_tokens: int = 4_000_000
    max_wall_s: float = 7200.0
    max_output_tokens: int = 1024
    max_tool_calls_per_step: int = 8
    context_steps: int = 8
    call_timeout_s: float = 120.0

    def __post_init__(self) -> None:
        for name, lo, hi in (("max_steps", 1, 100_000), ("max_tokens", 1, 1_000_000_000), ("max_output_tokens", 1, 64_000),
                             ("max_tool_calls_per_step", 1, 32), ("context_steps", 1, 50)):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"{name} must be an int in {lo}..{hi}")
        if not (0 < self.max_wall_s <= 86_400 and 0 < self.call_timeout_s <= 600):
            raise ValueError("max_wall_s must be in (0, 86400] and call_timeout_s in (0, 600]")


@dataclass(frozen=True)
class RunConfig:
    """Everything that defines a run besides the game bundle and the visitor's key."""

    provider: str
    model_id: str
    preset_ref: str
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    inject_path: str = "global.md"
    inject_cap_chars: int = 2000
    observation: ObservationConfig = field(default_factory=ObservationConfig)
    screenshots: ScreenshotPolicy = field(default_factory=ScreenshotPolicy)
    caps: Caps = field(default_factory=Caps)
    mode: str = "demo"

    def __post_init__(self) -> None:
        if self.mode not in ("demo", "benchmark"):
            raise ValueError("mode must be demo or benchmark")
        if not self.memory.enabled and self.preset_ref != "none@1" and not self.preset_ref.startswith("custom@"):
            raise ValueError("the no-memory arm must use the 'none' preset")
        if isinstance(self.inject_cap_chars, bool) or not 1 <= self.inject_cap_chars <= 8192:
            raise ValueError("inject_cap_chars must be in 1..8192")
        try:
            check_path(self.inject_path)
        except Reject as exc:
            raise ValueError(f"inject_path: {exc.detail}") from None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()


class EventSink(Protocol):
    def emit(self, type_: str, step: int, data: dict[str, Any]) -> None: ...


class ListSink:
    """Collects events in memory (tests, offline runs)."""

    def __init__(self) -> None:
        self.events: list[LoopEvent] = []

    def emit(self, type_: str, step: int, data: dict[str, Any]) -> None:
        self.events.append(LoopEvent(type_, step, data))


@dataclass(frozen=True)
class RunResult:
    state: str      # completed | capped | failed
    reason: str
    steps: int
    tokens: int
    wall_s: float
    trace: Trace


def _core(snap: Snapshot) -> tuple[int, int, int, bool, int]:
    return (snap.map_id, snap.x, snap.y, snap.dialog_active, snap.milestones)


class AgentLoop:
    def __init__(
        self,
        session: Session,
        provider: Provider,
        config: RunConfig,
        preset: Preset,
        sink: EventSink,
        *,
        run_id: str,
        stop: threading.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
        content_filter: Callable[[str], str] | None = None,
        frame_sink: Callable[[int, bytes], None] | None = None,
    ) -> None:
        if preset.ref != config.preset_ref:
            raise ValueError("preset does not match the run config")
        self.session, self.provider, self.config, self.preset, self.sink = session, provider, config, preset, sink
        self.stop = stop or threading.Event()
        self.clock = clock
        self.memory = MemoryService(MemoryStore(run_id), config.memory, content_filter)
        self.context = ContextBuilder(config.caps.context_steps)
        self.detectors = DetectorSet()
        self.tools = all_tool_schemas(config.memory)
        self.tokens = 0
        self._frame_cap_hit = False
        self._last_snapshot: Snapshot | None = None
        self._last_inject_sha: str | None = None
        self._entries: list[dict[str, Any]] = []
        self._frame_sink = frame_sink  # (step, PNG bytes) for frames the screenshot policy captured

    # ---- events ---------------------------------------------------------------------------
    def _emit(self, type_: str, step: int, data: dict[str, Any]) -> None:
        self.sink.emit(type_, step, data)
        for signal in self.detectors.observe(LoopEvent(type_, step, data)):
            self.sink.emit(signal.type, signal.step, signal.data)

    # ---- run --------------------------------------------------------------------------------
    def run(self) -> RunResult:
        started = self.clock()
        self._emit("run.state", 0, {"state": "running"})
        try:
            state, reason = self._run_steps(started)
        except ProviderError as exc:
            self._emit("error", self.session.step, {"code": exc.code, "message": safe_echo(str(exc), 200)})
            state, reason = "failed", exc.code
        except HarnessError as exc:
            self._emit("error", self.session.step, {"code": "emulator_failure", "message": safe_echo(str(exc), 200)})
            state, reason = "failed", "emulator_failure"
        except BaseException:
            self._emit("error", self.session.step, {"code": "internal", "message": "internal error"})
            self._emit("run.state", self.session.step, {"state": "failed", "reason": "internal"})
            raise
        self._emit("run.state", self.session.step, {"state": "stopping", "reason": reason})
        self._emit("run.state", self.session.step, {"state": state, "reason": reason})
        wall = self.clock() - started
        trace = Trace(self.session.manifest_sha256(), tuple(self._entries), self.session.tracker.score,
                      self.session.tracker.tiebreak_steps)
        return RunResult(state, reason, self.session.step, self.tokens, wall, trace)

    def _run_steps(self, started: float) -> tuple[str, str]:
        rec = self.session.open()
        self._record_obs(rec)
        while True:
            cap = self._check_stop(started)
            if cap is not None:
                return cap
            step_no = self.session.step + 1
            request = self._build_request(step_no, started)
            result = self.provider.step(request)
            self._meter(step_no, result)
            results, executed = self._execute(result, step_no)
            rec = self.session.finish_step(executed)
            self.context.finish(result.raw_assistant, results)
            self._record_obs(rec)

    def _check_stop(self, started: float) -> tuple[str, str] | None:
        caps = self.config.caps
        if self.stop.is_set():
            return "completed", "user_stop"
        checks = (
            ("steps", self.session.step, caps.max_steps),
            ("tokens", self.tokens, caps.max_tokens),
            ("wall", self.clock() - started, caps.max_wall_s),
            ("frames", self.session.emulator.frames, self.session.limits.max_total_frames),
        )
        for name, value, limit in checks:
            if value >= limit or (name == "frames" and self._frame_cap_hit):
                self._emit("cap.hit", self.session.step, {"cap": name, "limit": limit, "value": value})
                return "capped", name
        return None

    def _build_request(self, step_no: int, started: float) -> ProviderRequest:
        caps = self.config.caps
        remaining_wall = caps.max_wall_s - (self.clock() - started)
        injected = self._injected(step_no)
        return ProviderRequest(
            system=system_blocks(self.preset, injected),
            messages=self.context.messages(),
            tools=self.tools,
            max_output_tokens=min(caps.max_output_tokens, max(1, caps.max_tokens - self.tokens)),
            timeout_s=max(1.0, min(caps.call_timeout_s, remaining_wall)),
        )

    def _injected(self, step_no: int) -> str | None:
        if not self.config.memory.enabled:
            return None
        row = self.memory.store.latest(self.config.inject_path)
        text = row.content[: self.config.inject_cap_chars] if row else None
        sha = hashlib.sha256(text.encode()).hexdigest() if text is not None else None
        if sha != self._last_inject_sha:  # log only when what is injected changes
            self._last_inject_sha = sha
            self._emit("mem.read", step_no, {"op": "inject", "path": self.config.inject_path, "found": text is not None,
                                             "chars": len(text) if text else 0})
        return text

    def _meter(self, step_no: int, result: ProviderResult) -> None:
        u = result.usage
        self.tokens += u.total
        self._emit("model.call", step_no, {
            "provider": self.provider.name, "model_id": self.provider.model_id,
            "usage": {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                      "cache_creation_input_tokens": u.cache_creation_input_tokens,
                      "cache_read_input_tokens": u.cache_read_input_tokens},
            "tokens_total": self.tokens, "latency_s": round(result.latency_s, 3),
            "request_id": result.request_id, "stop_reason": result.stop_reason,
        })
        if result.summary:
            self._emit("model.summary", step_no, {"text": result.summary[:MAX_SUMMARY_CHARS]})

    # ---- tool execution ---------------------------------------------------------------------------
    def _execute(self, result: ProviderResult, step_no: int) -> tuple[list[ToolResultBlock], list[dict[str, Any]]]:
        results: list[ToolResultBlock] = []
        executed: list[dict[str, Any]] = []
        limit = self.config.caps.max_tool_calls_per_step
        for index, call in enumerate(result.actions):
            if index >= limit:
                results.append(self._skip(call, "too many tool calls in one step"))
            elif self._frame_cap_hit:
                results.append(self._skip(call, "frame budget exhausted"))
            elif call.name in GAME_TOOLS:
                results.append(self._game_call(call, step_no, executed))
            elif call.name in TOOL_NAMES:
                outcome = self.memory.dispatch(call.name, call.input, step_no)
                for event in outcome.events:
                    self._emit(event.type, step_no, event.data)
                results.append(ToolResultBlock(call.id, result_text(outcome.result), not outcome.ok))
            else:
                self._emit("error", step_no, {"code": "unknown_tool", "tool": safe_echo(call.name),
                                              "arg_keys": sorted(map(str, call.input))[:10]})
                results.append(ToolResultBlock(call.id, result_text({"ok": False, "error": "unknown_tool"}), True))
        return results, executed

    @staticmethod
    def _skip(call: ToolCall, why: str) -> ToolResultBlock:
        return ToolResultBlock(call.id, result_text({"ok": False, "error": "not_executed", "message": why}), True)

    def _game_call(self, call: ToolCall, step_no: int, executed: list[dict[str, Any]]) -> ToolResultBlock:
        args_echo = safe_echo(str(call.input), MAX_ARGS_ECHO)
        base = {"call_id": call.id, "tool": call.name, "args": args_echo}
        try:
            action = action_from_call(call.name, call.input)
            frames = self.session.advance(action)
        except InvalidAction as exc:
            self._emit("action", step_no, {**base, "ok": False, "error": "invalid_action", "frames": 0})
            return ToolResultBlock(call.id, result_text({"ok": False, "error": "invalid_action", "message": str(exc)}), True)
        except FrameLimitExceeded:
            self._frame_cap_hit = True
            self._emit("action", step_no, {**base, "ok": False, "error": "frame_cap", "frames": 0})
            return ToolResultBlock(call.id, result_text({"ok": False, "error": "frame_cap"}), True)
        executed.append({**action_to_dict(action), "frames": frames})
        self._emit("action", step_no, {**base, "ok": True, "frames": frames, "action": action_to_dict(action)})
        return ToolResultBlock(call.id, result_text({"ok": True, "frames": frames}), False)

    # ---- observation -----------------------------------------------------------------------------------
    def _record_obs(self, rec: Any) -> None:
        snap: Snapshot = rec.snapshot
        prev = self._last_snapshot
        changed = prev is None or _core(prev) != _core(snap)
        self._last_snapshot = snap
        self._entries.append(rec.trace_entry())
        self._emit("obs", rec.step, {
            "snapshot": snap.to_dict(), "map_id": snap.map_id, "x": snap.x, "y": snap.y,
            "dialog_active": snap.dialog_active, "changed": changed, "score": rec.score,
            "new_milestones": list(rec.new_milestones), "frame_sha256": rec.frame_sha256,
            "ram_sha256": rec.ram_sha256, "frames": rec.frames,
        })
        if rec.frame_png is not None and self._frame_sink is not None:
            self._frame_sink(rec.step, rec.frame_png)
        self.context.begin(format_observation(rec.step, snap, self.config.observation), rec.frame_png)


__all__ = ["AgentLoop", "Caps", "EventSink", "ListSink", "RunConfig", "RunResult"]
