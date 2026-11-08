"""In-process sliding-window rate limiter (single API replica; the gateway enforces the same limits first)."""

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Hashable


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[Hashable, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: Hashable, window_s: float) -> deque[float]:
        hits = self._hits[key]
        cutoff = self._clock() - window_s
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def allow(self, key: Hashable, limit: int, window_s: float) -> bool:
        """Count one hit if under the limit; refused hits are not counted."""
        with self._lock:
            hits = self._prune(key, window_s)
            if len(hits) >= limit:
                return False
            hits.append(self._clock())
            return True

    def count(self, key: Hashable, window_s: float) -> int:
        with self._lock:
            return len(self._prune(key, window_s))

    def record(self, key: Hashable, window_s: float) -> None:
        with self._lock:
            self._prune(key, window_s).append(self._clock())

    def allow_all(self, checks: list[tuple[Hashable, int, float]]) -> bool:
        """Count a hit on every key, or on none: a refusal by one limit must not use up another's quota."""
        with self._lock:
            if any(len(self._prune(key, window)) >= limit for key, limit, window in checks):
                return False
            for key, _, _ in checks:
                self._hits[key].append(self._clock())
            return True

    def refund(self, key: Hashable) -> None:
        """Take back the most recent hit (a create that was refused downstream and never ran)."""
        with self._lock:
            hits = self._hits.get(key)
            if hits:
                hits.pop()
