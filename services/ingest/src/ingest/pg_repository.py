"""Postgres implementation of `RunRepository` (service-role connection; never reachable from user JWTs).

`lock(run_id)` opens one transaction holding a row lock on the run, and `get`/`save`/`add_*` made inside
it use that same transaction, so the service's read-modify-write is atomic across processes. Outside
a lock each call is its own transaction. Raising inside the lock rolls everything back."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from runlog.eventlog import IndexEntry
from runlog.lifecycle import FINAL

from .repository import ParentRef, RunRecord

Conn = Connection[dict[str, Any]]


class PostgresRunRepository:
    def __init__(self, url: str, *, max_size: int = 8) -> None:
        self.pool: ConnectionPool[Conn] = ConnectionPool(
            url, min_size=1, max_size=max_size, open=True, kwargs={"row_factory": dict_row, "autocommit": False})
        self._local = threading.local()

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def _conn(self) -> Iterator[Conn]:
        held: Conn | None = getattr(self._local, "conn", None)
        if held is not None:
            yield held
            return
        with self.pool.connection() as conn:       # commits on exit, rolls back on error
            yield conn

    @contextmanager
    def lock(self, run_id: str) -> Iterator[None]:
        if getattr(self._local, "conn", None) is not None:
            yield                                   # re-entrant, like the in-memory RLock
            return
        with self.pool.connection() as conn:
            conn.execute("select 1 from public.runs where id = %s for update", (run_id,))
            self._local.conn = conn
            try:
                yield
            finally:
                self._local.conn = None

    def get(self, run_id: str) -> RunRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "select r.status, r.steps, r.tokens, s.* from public.runs r "
                "left join public.run_ingest_state s on s.run_id = r.id where r.id = %s", (run_id,)).fetchone()
        if row is None:
            return None
        events = row["event_count"] or 0
        # A freshly created run is `queued` in the table before any lifecycle event exists; ingest's
        # lifecycle starts from None (first event may be `queued`/`starting`).
        status = None if row["status"] == "queued" and events == 0 else row["status"]
        return RunRecord(
            run_id=run_id, status=status,
            next_batch_no=dict(row["next_batch_no"] or {"worker": 1, "orchestrator": 1}),
            event_count=events, bytes_total=row["bytes_total"] or 0, frames=row["frames"] or 0, checkpoints=row["checkpoints"] or 0,
            tokens=row["tokens"], steps=row["steps"], score=row["score"] or 0,
            closed=bool(row["closed"]), manifest_sealed=bool(row["manifest_sealed"]),
            last_ack=dict(row["last_ack"] or {}), bundle_sha256=row["bundle_sha256"])

    def save(self, record: RunRecord) -> None:
        final = record.status in FINAL
        with self._conn() as conn:
            conn.execute(
                "update public.runs set status = coalesce(%s, status), steps = %s, tokens = %s, "
                "started_at = case when started_at is null and %s then now() else started_at end, "
                "ended_at = case when %s and ended_at is null then now() else ended_at end, "
                "end_reason = case when %s then %s else end_reason end where id = %s",
                (record.status, record.steps, record.tokens,
                 record.status not in (None, "queued", "starting"), final, final, record.status, record.run_id))
            conn.execute(
                "insert into public.run_ingest_state (run_id, next_batch_no, event_count, bytes_total, frames, "
                "checkpoints, score, closed, manifest_sealed, last_ack, bundle_sha256) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (run_id) do update set next_batch_no = excluded.next_batch_no, "
                "event_count = excluded.event_count, bytes_total = excluded.bytes_total, frames = excluded.frames, "
                "checkpoints = excluded.checkpoints, score = excluded.score, closed = excluded.closed, manifest_sealed = excluded.manifest_sealed, "
                "last_ack = excluded.last_ack, bundle_sha256 = excluded.bundle_sha256",
                (record.run_id, Jsonb(record.next_batch_no), record.event_count, record.bytes_total, record.frames,
                 record.checkpoints, record.score, record.closed, record.manifest_sealed, Jsonb(record.last_ack), record.bundle_sha256))

    def add_index(self, run_id: str, entries: list[IndexEntry]) -> None:
        if not entries:
            return
        with self._conn() as conn, conn.cursor() as cur:
            cur.executemany(
                "insert into public.run_events_index (run_id, seq, type, step, ts, blob_offset, segment) "
                "values (%s, %s, %s, %s, %s, %s, %s)",
                [(run_id, e.seq, e.type, e.step, _ts(e.ts), e.offset, e.segment) for e in entries])

    def add_memory_rows(self, run_id: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self._conn() as conn, conn.cursor() as cur:
            cur.executemany(
                "insert into public.memory_files (run_id, path, version, step, rev, content, chars, deleted) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s)",
                [(run_id, r["path"], r["version"], r["step"], r["rev"], r["content"], r["chars"], r["deleted"])
                 for r in rows])


    def add_checkpoint(self, run_id: str, row: dict[str, Any]) -> str:
        with self._conn() as conn:
            got = conn.execute(
                "insert into public.checkpoints (run_id, step, savestate_key, memory_version, transcript_offset, config_hash) "
                "values (%s, %s, %s, %s, %s, %s) returning id",
                (run_id, row["step"], row["savestate_key"], row["memory_version"], row["transcript_offset"], row["config_hash"])).fetchone()
        return str(got["id"])  # type: ignore[index]

    def parent_of(self, run_id: str) -> ParentRef | None:
        with self._conn() as conn:
            row = conn.execute(
                "select c.id, c.run_id, c.step, c.transcript_offset from public.runs r "
                "join public.checkpoints c on c.id = r.parent_checkpoint_id where r.id = %s", (run_id,)).fetchone()
        return None if row is None else ParentRef(str(row["run_id"]), int(row["step"]), int(row["transcript_offset"]), str(row["id"]))


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


__all__ = ["PostgresRunRepository"]
