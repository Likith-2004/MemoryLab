"""Fails runs the orchestrator will never start: a `queued` row whose submit never arrived (the API died between
commit and submit) or was never started, and a `starting` row that stopped reporting. Service-role only.

The window is 11 minutes because the orchestrator's own queue TTL is 10; a row older than that is not in any
queue. Rows are claimed with `for update skip locked`, so concurrent reapers (or a run being updated by ingest)
never block each other and every row is failed once."""

import logging
import threading
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("orchestrator.reaper")

REAP_AFTER_S = 11 * 60

_QUEUED = """
update public.runs set status = 'failed', end_reason = 'queue_timeout', ended_at = now()
where id in (select id from public.runs where status = 'queued'
             and created_at < now() - make_interval(secs => %(after)s)
             for update skip locked)
returning id
"""

# `starting`: no event has reached ingest within the window (the sandbox never produced run.start, or stopped)
_STARTING = """
update public.runs set status = 'failed', end_reason = 'startup_timeout', ended_at = now()
where id in (select r.id from public.runs r where r.status = 'starting'
             and r.created_at < now() - make_interval(secs => %(after)s)
             and not exists (select 1 from public.run_events_index e where e.run_id = r.id
                             and e.ts > now() - make_interval(secs => %(after)s))
             for update skip locked)
returning id
"""


class Reaper:
    def __init__(self, url: str, *, after_s: float = REAP_AFTER_S, max_size: int = 2) -> None:
        self._after_s = after_s
        self._pool: ConnectionPool[Connection[dict[str, Any]]] = ConnectionPool(
            url, min_size=1, max_size=max_size, open=True, kwargs={"row_factory": dict_row, "autocommit": False})

    def close(self) -> None:
        self._pool.close()

    def run_once(self) -> list[str]:
        """Fail every stale queued/starting run and return their ids."""
        failed: list[str] = []
        with self._pool.connection() as conn:
            conn.execute("set local role service_role")        # the only role that may write run status
            for sql in (_QUEUED, _STARTING):
                failed += [str(r["id"]) for r in conn.execute(sql, {"after": self._after_s}).fetchall()]
        if failed:
            log.warning("reaped %d stale runs", len(failed))
        return failed

    def run_forever(self, stop: threading.Event, period_s: float = 60.0) -> None:
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:                                  # a database blip must not end the loop
                log.exception("reaper pass failed")
            stop.wait(period_s)


def main() -> None:                                             # pragma: no cover - process entrypoint
    import os
    stop = threading.Event()
    reaper = Reaper(os.environ["MEMORYLAB_SERVICE_DB_URL"])
    try:
        reaper.run_forever(stop, float(os.environ.get("MEMORYLAB_REAPER_PERIOD_S", "60")))
    except KeyboardInterrupt:
        stop.set()
    finally:
        reaper.close()


if __name__ == "__main__":                                      # pragma: no cover
    main()
