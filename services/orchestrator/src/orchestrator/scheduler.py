"""Queue and admission control: breaker, per-user / per-IP / global limits, queue TTL."""

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import OrchestratorConfig
from .keybox import KeyBox


class AdmissionError(Exception):
    code = "admission_error"
    status = 400
    retry_after_s: int | None = None


class BreakerOpen(AdmissionError):
    code, status = "breaker_open", 503


class CapacityFull(AdmissionError):
    code, status = "capacity_full", 429

    def __init__(self, retry_after_s: int = 60) -> None:
        super().__init__("capacity_full")
        self.retry_after_s = retry_after_s


class UserBusy(AdmissionError):
    code, status = "user_busy", 429


class IpBusy(AdmissionError):
    code, status = "ip_busy", 429


class Duplicate(AdmissionError):
    code, status = "duplicate_run", 409


@dataclass
class RunRequest:
    run_id: str
    user_id: str
    ip_hash: str
    game: str
    run_config: Any            # labharness RunConfig
    preset_text: str | None
    provider_name: str
    key_box: KeyBox
    fake_script: list[dict[str, Any]] | None
    submitted_at: float


@dataclass(frozen=True)
class Breaker:
    open: bool = False
    reason: str = ""
    actor: str = ""
    since: float = 0.0


class Scheduler:
    def __init__(self, cfg: OrchestratorConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg, self.clock = cfg, clock
        self._queue: deque[RunRequest] = deque()
        self._active: dict[str, RunRequest] = {}
        self._known: set[str] = set()
        self._lock = threading.RLock()
        self.breaker = Breaker()

    # ---- breaker -----------------------------------------------------------------------------
    def set_breaker(self, *, open_: bool, reason: str, actor: str) -> Breaker:
        if open_ and not reason.strip():
            raise ValueError("a reason is required to open the breaker")
        with self._lock:
            self.breaker = Breaker(open_, reason.strip() if open_ else "", actor, self.clock())
            return self.breaker

    # ---- admission -------------------------------------------------------------------------------
    def submit(self, req: RunRequest) -> int:
        """Queue a run; returns its queue position (0 = next). Raises AdmissionError subclasses."""
        with self._lock:
            if self.breaker.open:
                raise BreakerOpen(self.breaker.reason)
            if req.run_id in self._known:
                raise Duplicate(req.run_id)
            mine = [r for r in (*self._queue, *self._active.values())]
            if sum(r.user_id == req.user_id for r in mine) >= self.cfg.per_user:
                raise UserBusy("a run is already queued or active for this user")
            if sum(r.ip_hash == req.ip_hash for r in mine) >= self.cfg.per_ip:
                raise IpBusy("too many runs from this address")
            if len(self._queue) >= self.cfg.max_queue:
                raise CapacityFull(retry_after_s=60)
            self._queue.append(req)
            self._known.add(req.run_id)
            return len(self._queue) - 1

    def expire(self) -> list[RunRequest]:
        """Drop queued runs older than the TTL and destroy their keys."""
        with self._lock:
            now = self.clock()
            gone = [r for r in self._queue if now - r.submitted_at >= self.cfg.queue_ttl_s]
            for r in gone:
                self._queue.remove(r)
                r.key_box.discard()
            return gone

    def next_ready(self) -> RunRequest | None:
        with self._lock:
            if self.breaker.open or not self._queue or len(self._active) >= self.cfg.max_concurrent:
                return None
            req = self._queue.popleft()
            self._active[req.run_id] = req
            return req

    def finished(self, run_id: str) -> None:
        with self._lock:
            self._active.pop(run_id, None)

    def cancel_queued(self, run_id: str) -> RunRequest | None:
        with self._lock:
            for r in self._queue:
                if r.run_id == run_id:
                    self._queue.remove(r)
                    r.key_box.discard()
                    return r
            return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"queued": [r.run_id for r in self._queue], "active": list(self._active),
                    "breaker": {"open": self.breaker.open, "reason": self.breaker.reason}}
