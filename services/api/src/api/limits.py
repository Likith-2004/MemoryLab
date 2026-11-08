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
