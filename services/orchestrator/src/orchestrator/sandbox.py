"""Supervision of one sandbox: send the job, read heartbeats, enforce deadlines, kill on silence."""

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import OrchestratorConfig
from .process import ProcessLike


@dataclass
class SandboxResult:
    exit_code: int | None
    done: dict[str, Any] | None = None
    fatal: str | None = None
    killed_reason: str | None = None   # heartbeat_timeout | wall_clock | admin_kill | startup_timeout
    heartbeats: int = 0
    garbage_lines: int = 0
    lines_seen: list[str] = field(default_factory=list)


class Sandbox:
    """One supervised process. `run()` blocks until the process ends or is killed."""

    def __init__(self, proc: ProcessLike, cfg: OrchestratorConfig, *, wall_limit_s: float,
                 kill_container: Callable[[], None], clock: Callable[[], float] = time.monotonic,
                 poll_s: float = 0.25) -> None:
        self.proc, self.cfg, self._kill_container, self.clock, self._poll_s = proc, cfg, kill_container, clock, poll_s
        self.result = SandboxResult(None)
        self._deadline = clock() + wall_limit_s + cfg.wall_grace_s
        self._last_beat = clock()
        self._started = clock()
        self._beating = False
        self._kill_reason: str | None = None
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)

    def send_job(self, job_line: bytes) -> None:
        """Write the job spec (contains the key) to stdin and keep stdin open for control commands."""
        self.proc.write_stdin(job_line + b"\n")

    def stop(self) -> None:
        try:
            self.proc.write_stdin(b'{"cmd":"stop"}\n')
        except (BrokenPipeError, OSError, ValueError):
            pass

    def kill(self, reason: str) -> None:
        with self._lock:
            self._kill_reason = self._kill_reason or reason
        self._kill_container()
        self.proc.kill()

    def _read(self) -> None:
        for raw in self.proc.lines():
            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict) or len(msg) != 1:
                    raise ValueError
                (kind, value), = msg.items()
            except ValueError:
                self.result.garbage_lines += 1
                continue
            if kind == "hb" and isinstance(value, int):
                self._last_beat = self.clock()
                self._beating = True
                self.result.heartbeats += 1
            elif kind == "done" and isinstance(value, dict):
                self.result.done = {k: v for k, v in value.items() if k in {"state", "reason", "steps", "tokens", "redaction_hits"}}
            elif kind == "fatal" and isinstance(value, str):
                self.result.fatal = value[:60]
            else:
                self.result.garbage_lines += 1  # unknown shapes are dropped, never trusted

    def run(self) -> SandboxResult:
        self._reader.start()
        while self.proc.poll() is None:
            now = self.clock()
            silent_for = now - self._last_beat
            if now > self._deadline:
                self.kill("wall_clock")
            elif self._beating and silent_for > self.cfg.heartbeat_timeout_s:
                self.kill("heartbeat_timeout")
            elif not self._beating and now - self._started > self.cfg.startup_grace_s:
                self.kill("startup_timeout")
            if self._kill_reason:
                break
            time.sleep(self._poll_s)
        try:
            self.result.exit_code = self.proc.wait(10)
        except Exception:  # noqa: BLE001 - last resort: make sure it is dead
            self.proc.kill()
            self.result.exit_code = -9
        self._reader.join(5)
        self.proc.close_stdin()
        self.result.killed_reason = self._kill_reason
        return self.result
