"""Background delivery for the unauthenticated mail endpoints (signup, magic link, recover).

GoTrue sends mail synchronously only for a real account, so calling it inline made the response time depend on whether
the address exists. The endpoints validate and charge the limits, answer 202 at once, and hand the GoTrue call to this
bounded pool. Failures cannot reach the caller; they are logged (address as a sha256 prefix only) and counted."""

import hashlib
import hmac
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("api.mail")


def address_tag(email: str, pepper: bytes = b"") -> str:
    """A log-safe tag for an address: HMAC-SHA256 under a per-deployment secret, so holding the logs is not enough to test
    candidate addresses against them. (An empty pepper is only for tests; production refuses to start without one.)"""
    return hmac.new(pepper, email.encode(), hashlib.sha256).hexdigest()[:10]


class MailJobs:
    def __init__(self, workers: int = 4, max_pending: int = 200, *, inline: bool = False,
                 pepper: bytes = b"") -> None:
        self._pepper = pepper
        self.inline = inline     # tests only: run on the caller's thread so assertions need no waiting
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mail")
        self._max, self._pending, self._lock = max_pending, 0, threading.Lock()
        self.failed = 0          # metric: calls that raised
        self.dropped = 0         # metric: calls refused because the queue was full

    def submit(self, kind: str, email: str, job: Callable[[], None]) -> None:
        with self._lock:
            if self._pending >= self._max:
                self.dropped += 1
                log.error("mail job dropped (queue full) kind=%s addr=%s", kind, address_tag(email, self._pepper))
                return
            self._pending += 1
        if self.inline:
            self._run(kind, email, job)
        else:
            self._pool.submit(self._run, kind, email, job)

    def drain(self, timeout_s: float = 5.0) -> bool:
        import time
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            with self._lock:
                if self._pending == 0:
                    return True
            time.sleep(0.01)
        return False

    def _run(self, kind: str, email: str, job: Callable[[], None]) -> None:
        try:
            job()
        except Exception as exc:  # noqa: BLE001 - nobody is waiting for the answer
            self.failed += 1
            log.error("mail job failed kind=%s addr=%s error=%s", kind, address_tag(email, self._pepper), type(exc).__name__)
        finally:
            with self._lock:
                self._pending -= 1

    def close(self) -> None:
        self._pool.shutdown(wait=True)
