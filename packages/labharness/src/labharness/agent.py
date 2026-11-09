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
    Message,
    Provider,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    SystemBlock,
    TextBlock,
    ToolCall,
    ToolResultBlock,
)
from .screenshots import ScreenshotPolicy
from .session import Checkpoint, Session, Trace
from .sessions import (
    DREAM_PROMPT,
    DREAM_PROMPT_SHA256,
    ResumeState,
    SessionsConfig,
    classify_change,
    context_has_image,
    dream_input,
    restore_context,
    serialize_context,
    transcript_line,
)
from .telemetry import Snapshot
from .tools import GAME_TOOLS, action_from_call, all_tool_schemas, memory_only_schemas

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
    sessions: SessionsConfig = field(default_factory=SessionsConfig)

    def __post_init__(self) -> None:
        if self.mode not in ("demo", "benchmark"):
            raise ValueError("mode must be demo or benchmark")
        if not self.memory.enabled and self.preset_ref != "none@1" and not self.preset_ref.startswith("custom@"):
            raise ValueError("the no-memory arm must use the 'none' preset")
        if self.sessions.dream and not self.memory.enabled:
            raise ValueError("the dream job needs memory to be enabled")
        if isinstance(self.inject_cap_chars, bool) or not 1 <= self.inject_cap_chars <= 8192:
            raise ValueError("inject_cap_chars must be in 1..8192")
        try:
            check_path(self.inject_path)
        except Reject as exc:
            raise ValueError(f"inject_path: {exc.detail}") from None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.sessions.is_default:
            del d["sessions"]       # an absent block and an explicit default are the same config (and the same hash)
        return d

    @classmethod
    def from_dict(cls, doc: object) -> RunConfig:
        """Strict inverse of `to_dict` for untrusted input: closed key sets, exact types."""
        d = _closed(doc, "run_config", {"provider", "model_id", "preset_ref", "mode"},
                    {"memory", "inject_path", "inject_cap_chars", "observation", "screenshots", "caps", "sessions"})
        for key in ("provider", "model_id", "preset_ref", "mode"):
            if not isinstance(d[key], str):
                raise ValueError(f"run_config.{key} must be a string")  # noqa: TRY004
        kwargs: dict[str, Any] = {k: d[k] for k in ("provider", "model_id", "preset_ref", "mode")}
        if "memory" in d:
            kwargs["memory"] = MemoryConfig(**_closed(d["memory"], "memory", set(), {
                "enabled", "max_files", "max_file_chars", "max_total_chars", "max_history_rows", "max_history_chars"}))
        if "observation" in d:
            obs = _closed(d["observation"], "observation", set(), {"hide_extras", "show_milestones"})
            hide = obs.get("hide_extras", [])
            if not isinstance(hide, list) or not all(isinstance(x, str) for x in hide):
                raise ValueError("observation.hide_extras must be a list of strings")
            kwargs["observation"] = ObservationConfig(tuple(hide), obs.get("show_milestones", False))
        if "screenshots" in d:
            kwargs["screenshots"] = ScreenshotPolicy(**_closed(d["screenshots"], "screenshots", set(), {"mode", "k", "frame_scale"}))
        if "caps" in d:
            kwargs["caps"] = Caps(**_closed(d["caps"], "caps", set(), {
                "max_steps", "max_tokens", "max_wall_s", "max_output_tokens", "max_tool_calls_per_step",
                "context_steps", "call_timeout_s"}))
        if "sessions" in d:
            kwargs["sessions"] = SessionsConfig(**_closed(d["sessions"], "sessions", set(), {
                "session_len", "dream", "dream_max_tokens", "dream_max_turns", "checkpoint_every"}))
        for key in ("inject_path", "inject_cap_chars"):
            if key in d:
                kwargs[key] = d[key]
        return cls(**kwargs)

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()


def _closed(doc: object, what: str, required: set[str], optional: set[str]) -> dict[str, Any]:
    if not isinstance(doc, dict) or not required <= set(doc) or not set(doc) <= required | optional:
        raise ValueError(f"{what}: expected an object with keys {sorted(required)} (+ optional {sorted(optional)})")
    return doc


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
        checkpoint_sink: Callable[[Checkpoint, dict[str, Any]], None] | None = None,
        resume: ResumeState | None = None,
    ) -> None:
        if preset.ref != config.preset_ref:
            raise ValueError("preset does not match the run config")
        self.session, self.provider, self.config, self.preset, self.sink = session, provider, config, preset, sink
        self.stop = stop or threading.Event()
        self.clock = clock
        self.memory = MemoryService(MemoryStore(run_id), config.memory, content_filter)
        self.resume = resume
        self.run_id = run_id
        if resume is not None:
            self._replay_parent_memory(resume)
        self.context = ContextBuilder(config.caps.context_steps)
        self.detectors = DetectorSet()
        self.tools = all_tool_schemas(config.memory)
        self.tokens = 0
        self._frame_cap_hit = False
        self._last_snapshot: Snapshot | None = None
        self._last_inject_sha: str | None = None
        self._entries: list[dict[str, Any]] = []
        self._frame_sink = frame_sink  # (step, PNG bytes) for frames the screenshot policy captured
        self._checkpoint_sink = checkpoint_sink  # (Checkpoint, context document): stores the state blob and context for a later fork
        self._session_no = 1
        self._last_boundary_step = -1
        self._last_checkpoint_step = -1
        self._transcript: list[str] = []
        self._last_obs_text = ""
        self._last_obs_png: bytes | None = None
        self.dream_tokens = 0

    # ---- events ---------------------------------------------------------------------------
    def _emit(self, type_: str, step: int, data: dict[str, Any]) -> None:
        self.sink.emit(type_, step, data)
        for signal in self.detectors.observe(LoopEvent(type_, step, data)):
            self.sink.emit(signal.type, signal.step, signal.data)

    # ---- run --------------------------------------------------------------------------------
    def run(self) -> RunResult:
        started = self.clock()
        self._emit("run.state", 0, {"state": "running"})
        if self.resume is not None:
            self._announce_resume(self.resume)
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
        if self.resume is None:
            rec = self.session.open()
            self._record_obs(rec)
        else:
            has_image = context_has_image(self.resume.context)
            rec = self.session.open_from(self.resume.checkpoint, force_frame=has_image)
            self._record_obs(rec, begin_context=False)
            self._transcript = restore_context(self.resume.context, self.context, rec.frame_png)
            self._session_no = int(self.resume.context.get("session_no", 1))
            self._last_checkpoint_step = rec.step
            if self.resume.context.get("boundary"):
                self._last_boundary_step = rec.step      # the checkpoint IS the post-boundary state: do not run the boundary again
        while True:
            cap = self._check_stop(started)
            if cap is not None:
                return cap
            if self._boundary_due():
                self._boundary(started)
                cap = self._check_stop(started)          # the dream's spend or time may have crossed a cap
                if cap is not None:
                    return cap
            step_no = self.session.step + 1
            request = self._build_request(step_no, started)
            result = self.provider.step(request)
            self._meter(step_no, result)
            results, executed = self._execute(result, step_no)
            rec = self.session.finish_step(executed)
            self.context.finish(result.raw_assistant, results)
            self._note_step(step_no, result, results, rec)
            self._record_obs(rec)
            self._maybe_checkpoint()

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
    def _record_obs(self, rec: Any, begin_context: bool = True) -> None:
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
        self._last_obs_text = format_observation(rec.step, snap, self.config.observation)
        self._last_obs_png = rec.frame_png
        if begin_context:
            self.context.begin(self._last_obs_text, rec.frame_png)

    # ---- sessions, the dream job, checkpoints -----------------------------------------------------------------
    def _note_step(self, step_no: int, result: ProviderResult, results: list[ToolResultBlock], rec: Any) -> None:
        snap: Snapshot = rec.snapshot
        calls = [(c.name, c.input, r.content) for c, r in zip(result.actions, results, strict=False)]
        self._transcript.append(transcript_line(step_no, result.summary, calls, f"map {snap.map_id} ({snap.x},{snap.y})"))
        del self._transcript[:-400]            # bounded; the dream sees the tail that fits its input

    def _boundary_due(self) -> bool:
        n = self.config.sessions.session_len
        step = self.session.step
        return n is not None and step > 0 and step % n == 0 and step != self._last_boundary_step

    def _boundary(self, started: float) -> None:
        """End a session: context dropped, emulator and memory kept, optional dream, then a checkpoint of exactly that state."""
        step = self.session.step
        self._last_boundary_step = step
        number = self._session_no
        self._emit("run.state", step, {"state": "session_boundary"})
        self._emit("session.boundary", step, {
            "session": number, "next_session": number + 1, "context_steps_dropped": len(self.context.steps),
            "inject_path": self.config.inject_path, "memory_rev": self.memory.store.rev})
        if self.config.sessions.dream and self.config.memory.enabled and self.tokens < self.config.caps.max_tokens:
            self._emit("run.state", step, {"state": "dreaming"})
            self._dream(number, started)
        self.context = ContextBuilder(self.config.caps.context_steps)      # the model forgets; only the pinned file survives
        self.context.begin(self._last_obs_text, self._last_obs_png)
        self._last_inject_sha = None                                       # the new session's injected notes are logged afresh
        self._session_no += 1
        self._transcript.clear()                                           # the next session starts with an empty transcript
        self._take_checkpoint("boundary")
        self._emit("run.state", step, {"state": "running"})

    def _dream(self, session_no: int, started: float) -> None:
        """One dream job: a few provider calls that may only use the memory tools. Own token cap; its spend counts against the run's."""
        cfg, caps = self.config.sessions, self.config.caps
        step = self.session.step
        rev_before = self.memory.store.rev
        live = {p: r.content for p, r in self.memory.store.live().items()}
        self._emit("dream.start", step, {
            "session": session_no, "budget_tokens": cfg.dream_max_tokens, "max_turns": cfg.dream_max_turns, "memory_rev": rev_before,
            "files": len(live), "prompt_version": "dream@1", "prompt_sha256": DREAM_PROMPT_SHA256})
        messages: list[Message] = [Message("user", (TextBlock(dream_input(
            self._transcript, live, self.config.inject_path, self.config.inject_cap_chars, rev_before)),))]
        spent = turns = changes = 0
        stopped = ""
        while turns < cfg.dream_max_turns:
            remaining_run = caps.max_tokens - self.tokens
            remaining_dream = cfg.dream_max_tokens - spent
            if self.stop.is_set():
                stopped = "stop"
            elif remaining_dream <= 0:
                stopped = "dream_tokens"
            elif remaining_run <= 0:
                stopped = "run_tokens"
            elif self.clock() - started >= caps.max_wall_s:
                stopped = "wall"
            if stopped:
                break
            request = ProviderRequest(
                system=(SystemBlock(DREAM_PROMPT, cache=True),), messages=tuple(messages), tools=memory_only_schemas(self.config.memory),
                # a call may not be allowed to overrun what is left of either budget (the run cap is hard, the dream cap is the dream's own)
                max_output_tokens=min(caps.max_output_tokens, remaining_dream, remaining_run),
                timeout_s=max(1.0, min(caps.call_timeout_s, caps.max_wall_s - (self.clock() - started))))
            result = self.provider.step(request)
            turns += 1
            used = result.usage.total
            spent += used
            self.tokens += used
            self.dream_tokens += used
            self._emit("dream.call", step, {
                "turn": turns, "usage": {"input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens},
                "dream_tokens": spent, "tokens_total": self.tokens, "request_id": result.request_id, "stop_reason": result.stop_reason})
            if result.summary:
                self._emit("dream.summary", step, {"text": result.summary[:MAX_SUMMARY_CHARS]})
            if not result.actions:
                break
            answers: list[ToolResultBlock] = []
            for index, call in enumerate(result.actions):
                if index >= caps.max_tool_calls_per_step:
                    answers.append(self._skip(call, "too many tool calls in one turn"))
                elif call.name in TOOL_NAMES:
                    outcome = self.memory.dispatch(call.name, call.input, step)
                    for event in outcome.events:
                        changes += self._emit_dream_event(event.type, event.data, step)
                    answers.append(ToolResultBlock(call.id, result_text(outcome.result), not outcome.ok))
                else:
                    # a dream can reach memory and nothing else: a game tool (or anything unknown) is refused and the emulator is not touched
                    self._emit("dream.reject", step, {"op": "unknown", "reason": "not_a_memory_tool", "tool": safe_echo(call.name)})
                    answers.append(ToolResultBlock(call.id, result_text({"ok": False, "error": "not_a_memory_tool"}), True))
            messages += [Message("assistant", result.raw_assistant), Message("user", tuple(answers))]
        if stopped in ("dream_tokens", "run_tokens"):
            # the dream was cut short rather than allowed to overrun: recorded as a cap hit with reason `dream`
            self._emit("cap.hit", step, {"cap": "dream", "limit": cfg.dream_max_tokens if stopped == "dream_tokens" else caps.max_tokens,
                                         "value": spent if stopped == "dream_tokens" else self.tokens, "reason": stopped})
        self._emit("dream.end", step, {"session": session_no, "turns": turns, "dream_tokens": spent, "changes": changes,
                                       "cut_short": stopped or None, "memory_rev_before": rev_before, "memory_rev_after": self.memory.store.rev})

    def _emit_dream_event(self, mem_type: str, data: dict[str, Any], step: int) -> int:
        """Re-badge a memory event as the dream's own. Returns 1 if it changed memory."""
        name = {"mem.write": "dream.write", "mem.delete": "dream.delete", "mem.reject": "dream.reject", "mem.read": "dream.read"}[mem_type]
        extra: dict[str, Any] = {}
        if name in ("dream.write", "dream.delete"):
            extra["kind"] = classify_change("delete" if name == "dream.delete" else "write", data["path"], self.config.inject_path,
                                            bool(data.get("created")))
        self._emit(name, step, {**data, **extra})
        return 1 if name in ("dream.write", "dream.delete") else 0

    def _maybe_checkpoint(self) -> None:
        n = self.config.sessions.checkpoint_every
        step = self.session.step
        s = self.config.sessions.session_len
        if n is None or step % n != 0 or step == self._last_checkpoint_step or (s is not None and step % s == 0):
            return                          # a boundary step is checkpointed by the boundary (after the dream), not here
        self._take_checkpoint("interval")

    def _take_checkpoint(self, reason: str) -> None:
        if self.session.step == self._last_checkpoint_step:
            return
        cp = self.session.checkpoint()
        self._last_checkpoint_step = cp.step
        context = serialize_context(self.context.steps, self.config.caps.context_steps, transcript=self._transcript,
                                    session_no=self._session_no, boundary=reason == "boundary")
        if self._checkpoint_sink is not None:
            self._checkpoint_sink(cp, context)          # state blob and context are stored BEFORE the event that announces them
        self._emit("checkpoint", cp.step, {
            "step": cp.step, "reason": reason, "session": self._session_no, "memory_rev": self.memory.store.rev,
            "state_sha256": cp.state_sha256, "frames": cp.frames, "tracker": cp.tracker, "snapshot_sha256": cp.snapshot_sha256,
            "ram_sha256": cp.ram_sha256, "rom_sha256": cp.rom_sha256, "semantics_sha256": cp.semantics_sha256,
            "pyboy_version": cp.pyboy_version, "config_hash": self.config.config_hash, "trace_len": len(self._entries)})

    # ---- resuming from a parent's checkpoint ---------------------------------------------------------------------
    def _replay_parent_memory(self, resume: ResumeState) -> None:
        """Seed the store by replaying the parent's memory mutations in order: revs and versions come out identical."""
        for _type, _step, data in resume.memory_events:
            deleted = _type in ("mem.delete", "dream.delete")
            row = self.memory.store.append(str(data["path"]), int(data["step"]), "" if deleted else str(data["content"]), deleted)
            if row.rev != data["rev"] or row.version != data["version"]:
                raise ValueError("the parent's memory log is inconsistent")

    def _announce_resume(self, resume: ResumeState) -> None:
        step = resume.checkpoint.step
        self._emit("fork.start", step, {"parent_run_id": resume.parent_run_id, "parent_step": step, "session": resume.context.get("session_no", 1),
                                        "memory_rev": self.memory.store.rev, "context_restored": True})
        for type_, _step, data in resume.memory_events:   # the child's own log carries the history, so replay and verification stand alone
            self._emit(type_, int(data["step"]), {**data, "seeded_from": resume.parent_run_id})


__all__ = ["AgentLoop", "Caps", "EventSink", "ListSink", "ResumeState", "RunConfig", "RunResult", "SessionsConfig"]
