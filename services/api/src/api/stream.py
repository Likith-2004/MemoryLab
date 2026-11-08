"""Live-stream plumbing: ticket format and the socket admission counters.

A ticket is 32 random bytes (43 URL-safe characters); only its sha256 is ever stored. Sockets are limited to 3 per
viewer per run and 200 per run. The counters are in-process (one API replica; the gateway applies the same limits)."""

import hashlib
import re
import secrets
import threading
from collections import defaultdict

TICKET_BYTES = 32
TICKET_TTL_S = 60
MAX_CLIENT_MESSAGE_BYTES = 1024
SOCKETS_PER_VIEWER = 3
SOCKETS_PER_RUN = 200
_TICKET = re.compile(r"^[A-Za-z0-9_-]{43}$")


def new_ticket() -> str:
    return secrets.token_urlsafe(TICKET_BYTES)


def ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


def well_formed(ticket: str) -> bool:
    return _TICKET.fullmatch(ticket) is not None


class SocketCounter:
    def __init__(self, per_viewer: int | None = None, per_run: int | None = None) -> None:
        self._per_viewer = SOCKETS_PER_VIEWER if per_viewer is None else per_viewer
        self._per_run = SOCKETS_PER_RUN if per_run is None else per_run
        self._viewer: dict[tuple[str, str], int] = defaultdict(int)
        self._run: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def acquire(self, run_id: str, viewer: str) -> bool:
        with self._lock:
            if self._viewer[(run_id, viewer)] >= self._per_viewer or self._run[run_id] >= self._per_run:
                return False
            self._viewer[(run_id, viewer)] += 1
            self._run[run_id] += 1
            return True

    def release(self, run_id: str, viewer: str) -> None:
        with self._lock:
            if self._viewer[(run_id, viewer)] > 0:
                self._viewer[(run_id, viewer)] -= 1
                self._run[run_id] -= 1
            if self._viewer[(run_id, viewer)] == 0:
                del self._viewer[(run_id, viewer)]
            if self._run[run_id] <= 0:
                del self._run[run_id]

    def open(self, run_id: str) -> int:
        with self._lock:
            return self._run.get(run_id, 0)
