"""Fails runs the orchestrator will never finish, so no row stays active forever (an active row blocks its owner
from creating another run). Service-role only. Three cases, each with its own `end_reason`:

- `queue_timeout`: still `queued` after 11 minutes (the orchestrator's queue TTL is 10: the API died between commit
  and submit, or the orchestrator lost the request);
- `startup_timeout`: `starting` for 11 minutes with no event reaching ingest;
- `heartbeat_lost`: `running`/`session_boundary`/`dreaming`/`stopping` with no event for 15 minutes (a model call
  may take up to 600 s and the worker's heartbeat does not reach ingest, so this is longer than both).

A reaped run that has an event log is ended through ingest under the orchestrator scope (`run.state failed` with the
reason, then close), so replay and verification agree with the row; a run with no events is updated directly.
Non-queued runs are also killed through the orchestrator so no sandbox keeps running on the user's key.

Concurrency: a reaper claims a run with a session advisory lock (the ingest service takes the row lock itself, so we
cannot hold it across the calls), and re-checks staleness once it holds the claim; the direct update uses
`for update skip locked`, so a row someone else is writing is left for the next pass."""

import logging
import threading
from collections.abc import Callable
from typing import Any, Protocol

import httpx
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .ingest_api import IngestApi, IngestApiError

log = logging.getLogger("orchestrator.reaper")

REAP_AFTER_S = 11 * 60              # queued / starting
HEARTBEAT_LOST_AFTER_S = 15 * 60    # running and the states between runs

_CLASSIFY = """
select r.id, r.status, coalesce(s.event_count, 0) as events, case
  when r.status = 'queued' and r.created_at < now() - make_interval(secs => %(queue)s) then 'queue_timeout'
  when r.status = 'starting' and r.created_at < now() - make_interval(secs => %(queue)s)
       and not exists (select 1 from public.run_events_index e where e.run_id = r.id
                       and e.ts > now() - make_interval(secs => %(queue)s)) then 'startup_timeout'
  when r.status in ('running', 'session_boundary', 'dreaming', 'stopping')
       and coalesce((select max(e.ts) from public.run_events_index e where e.run_id = r.id), r.started_at, r.created_at)
           < now() - make_interval(secs => %(beat)s) then 'heartbeat_lost'
end as reason
from public.runs r left join public.run_ingest_state s on s.run_id = r.id
where r.status in ('queued', 'starting', 'running', 'session_boundary', 'dreaming', 'stopping')
"""

_DIRECT = """
update public.runs set status = 'failed', end_reason = %(reason)s, ended_at = now()
where id in (select id from public.runs where id = %(id)s and status = %(status)s for update skip locked)
returning id
"""


class RunKiller(Protocol):
    def kill(self, run_id: str, actor: str, reason: str) -> str: ...


class IngestHandle(Protocol):
    def state(self, state: str, reason: str | None = None) -> None: ...
    def close(self) -> dict[str, Any]: ...


class HttpKiller:
    def __init__(self, base_url: str, token: bytes, timeout_s: float = 10.0) -> None:
        if len(token) < 32:
            raise ValueError("orchestrator token must be at least 32 bytes")
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s, follow_redirects=False,
                                  headers={"authorization": "Bearer " + token.decode()})

    def kill(self, run_id: str, actor: str, reason: str) -> str:
        resp = self._http.post(f"/internal/runs/{run_id}/kill", json={"actor": actor, "reason": reason})
        resp.raise_for_status()
        return str(resp.json().get("result", ""))


def http_ingest(client: httpx.Client, secret: bytes) -> Callable[[str], IngestHandle]:
    return lambda run_id: IngestApi(client, run_id, secret)


class Reaper:
    def __init__(self, url: str, *, killer: RunKiller, ingest: Callable[[str], IngestHandle],
                 queue_after_s: float = REAP_AFTER_S, heartbeat_after_s: float = HEARTBEAT_LOST_AFTER_S,
                 max_size: int = 4) -> None:
        self._killer, self._ingest = killer, ingest
        self._params = {"queue": queue_after_s, "beat": heartbeat_after_s}
        self._pool: ConnectionPool[Connection[dict[str, Any]]] = ConnectionPool(
            url, min_size=1, max_size=max_size, open=True, kwargs={"row_factory": dict_row, "autocommit": True})

    def close(self) -> None:
        self._pool.close()

    # ---- one pass -----------------------------------------------------------------------------------
    def run_once(self) -> list[str]:
        """Fail every stale run and return the ids this pass reaped."""
        with self._pool.connection() as conn:
            conn.execute("set role service_role")           # the only role that may write run status
            try:
                candidates = conn.execute(f"select * from ({_CLASSIFY}) t where reason is not null order by id",
                                          self._params).fetchall()
            finally:
                conn.execute("reset role")
        reaped = [str(c["id"]) for c in candidates if self._reap(str(c["id"]))]
        if reaped:
            log.warning("reaped %d stale runs", len(reaped))
        return reaped

    def _reap(self, run_id: str) -> bool:
        with self._pool.connection() as conn:
            conn.execute("set role service_role")
            try:
                got = conn.execute("select pg_try_advisory_lock(hashtextextended('reap:' || %s::text, 0)) as ok",
                                   (run_id,)).fetchone()
                if not got or not got["ok"]:
                    return False                              # another reaper has it
                try:
                    return self._reap_claimed(conn, run_id)
                finally:
                    conn.execute("select pg_advisory_unlock(hashtextextended('reap:' || %s::text, 0))", (run_id,))
            finally:
                conn.execute("reset role")

    def _classify(self, conn: Connection[dict[str, Any]], run_id: str) -> dict[str, Any] | None:
        row = conn.execute(f"select * from ({_CLASSIFY} and r.id = %(id)s) t where reason is not null",
                           {**self._params, "id": run_id}).fetchone()
        return row

    def _reap_claimed(self, conn: Connection[dict[str, Any]], run_id: str) -> bool:
        row = self._classify(conn, run_id)
        if row is None:
            return False                                      # moved on (or reaped by the other pass) since listing
        reason, status, events = str(row["reason"]), str(row["status"]), int(row["events"])
        if status != "queued":                                # a queued run older than the TTL is in no queue
            try:
                self._killer.kill(run_id, "system", reason)
            except Exception:  # noqa: BLE001 - the orchestrator may be the thing that died; the row must still end
                log.warning("could not kill run %s (%s)", run_id, reason)
        if events > 0 and self._end_through_ingest(conn, run_id, reason):
            return True
        return self._end_directly(conn, run_id, status, reason)

    def _end_through_ingest(self, conn: Connection[dict[str, Any]], run_id: str, reason: str) -> bool:
        """Append `run.state failed` (orchestrator scope) and close the log; True if the run now ends in `failed`."""
        handle = self._ingest(run_id)
        try:
            handle.state("failed", reason)
        except Exception:  # noqa: BLE001 - conflict (already final/closed), unreachable ingest, rejected transition
            log.warning("could not end run %s through ingest", run_id)
            return False
        try:
            handle.close()
        except (IngestApiError, httpx.HTTPError):
            # no sealed manifest or an export that cannot be built: the log still ends in a final state, so close
            # the run to further appends
            conn.execute("update public.run_ingest_state set closed = true where run_id = %s", (run_id,))
        conn.execute("update public.runs set end_reason = %s where id = %s and status = 'failed'", (reason, run_id))
        return True

    def _end_directly(self, conn: Connection[dict[str, Any]], run_id: str, status: str, reason: str) -> bool:
        done = conn.execute(_DIRECT, {"id": run_id, "status": status, "reason": reason}).fetchone()
        return done is not None

    def run_forever(self, stop: threading.Event, period_s: float = 60.0) -> None:
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("reaper pass failed")
            stop.wait(period_s)


def main() -> None:                                             # pragma: no cover - process entrypoint
    import os

    from runlog.envfile import secret_env
    stop = threading.Event()
    ingest_client = httpx.Client(base_url=os.environ["MEMORYLAB_INGEST_URL"], timeout=15.0)
    reaper = Reaper(secret_env("MEMORYLAB_SERVICE_DB_URL"),
                    killer=HttpKiller(os.environ["MEMORYLAB_ORCHESTRATOR_URL"],
                                      secret_env("MEMORYLAB_ORCHESTRATOR_TOKEN").encode()),
                    ingest=http_ingest(ingest_client, secret_env("MEMORYLAB_TOKEN_SECRET").encode()))
    try:
        reaper.run_forever(stop, float(os.environ.get("MEMORYLAB_REAPER_PERIOD_S", "60")))
    except KeyboardInterrupt:
        stop.set()
    finally:
        reaper.close()


if __name__ == "__main__":                                      # pragma: no cover
    main()
