"""Append-only event log: gapless `seq`, JSONL segments sealed (zstd) every 1000 events.

Layout (plan): runs/{run_id}/events/{segment:06d}.jsonl.zst for sealed segments; the unsealed
tail is runs/{run_id}/events/{segment:06d}.jsonl; runs/{run_id}/events/segments.json lists sealed
segments with their sha256 so readers detect tampering or corruption.
"""

import hashlib
import json
import threading
from collections.abc import Callable, Iterator, Sequence
from compression import zstd
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .events import RUN_ID, Event, EventRejected, canonical, parse_line, validate_payload
from .store import NotFound, ObjectStore

SEGMENT_SIZE = 1000
MAX_SEGMENT_BYTES = 80 * 1024 * 1024


class LogCorrupt(Exception):
    pass


class LogClosed(Exception):
    pass


@dataclass(frozen=True)
class IndexEntry:
    seq: int
    type: str
    step: int
    ts: str
    segment: int
    offset: int  # byte offset of the line within the (uncompressed) segment


def now_ms() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _prefix(run_id: str) -> str:
    if RUN_ID.fullmatch(run_id) is None:
        raise ValueError("bad run id")
    return f"runs/{run_id}/events/"


def _name(segment: int) -> str:
    return f"{segment:06d}"


def _decompress(data: bytes) -> bytes:
    d = zstd.ZstdDecompressor()
    out = d.decompress(data, MAX_SEGMENT_BYTES)
    if not d.eof:
        raise LogCorrupt("segment decompresses past the size limit or is truncated")
    return out


def load_index(store: ObjectStore, run_id: str) -> dict[str, Any]:
    key = _prefix(run_id) + "segments.json"
    try:
        doc = json.loads(store.get(key))
    except NotFound:
        return {"run_id": run_id, "segment_size": SEGMENT_SIZE, "segments": [], "closed": False}
    except ValueError:
        raise LogCorrupt("segments.json is not valid JSON") from None
    if not isinstance(doc, dict) or doc.get("run_id") != run_id or not isinstance(doc.get("segments"), list):
        raise LogCorrupt("segments.json does not belong to this run")
    return doc


class EventLogWriter:
    """Single writer per run (callers serialise per run). Resumable after a crash."""

    def __init__(self, store: ObjectStore, run_id: str, *, clock: Callable[[], str] = now_ms,
                 segment_size: int = SEGMENT_SIZE) -> None:
        self.store, self.run_id, self._clock, self.segment_size = store, run_id, clock, segment_size
        self._prefix = _prefix(run_id)
        self._lock = threading.RLock()
        self._index = load_index(store, run_id)
        self.closed = bool(self._index.get("closed"))
        sealed = self._index["segments"]
        self._segment = (sealed[-1]["segment"] + 1) if sealed else 1
        self.next_seq = (sealed[-1]["last_seq"] + 1) if sealed else 1
        self._tail_count = 0
        self._reconcile_tail()

    # ---- recovery ---------------------------------------------------------------------
    def _tail_key(self) -> str:
        return f"{self._prefix}{_name(self._segment)}.jsonl"

    def _reconcile_tail(self) -> None:
        key = self._tail_key()
        if not self.store.exists(key):
            return
        raw = self.store.get(key)
        events = [parse_line(line) for line in raw.decode().splitlines()]
        for i, e in enumerate(events):
            if e.seq != self.next_seq + i or e.run_id != self.run_id:
                raise LogCorrupt("tail segment is not contiguous")
        self._tail_count = len(events)
        self.next_seq += len(events)
        if self._tail_count >= self.segment_size:  # crashed after the last append, before sealing finished
            self._seal_tail()

    # ---- appending --------------------------------------------------------------------------
    def append(self, items: Sequence[tuple[str, int, dict[str, Any]]]) -> tuple[list[Event], list[IndexEntry]]:
        with self._lock:
            if self.closed:
                raise LogClosed(self.run_id)
            for type_, step, data in items:
                validate_payload(type_, step, data)
            events: list[Event] = []
            index: list[IndexEntry] = []
            for type_, step, data in items:
                event = Event(self.next_seq, self.run_id, step, self._clock(), type_, data)
                line = (canonical(event.to_obj()) + "\n").encode()
                offset = self.store.append(self._tail_key(), line)
                events.append(event)
                index.append(IndexEntry(event.seq, type_, step, event.ts, self._segment, offset))
                self.next_seq += 1
                self._tail_count += 1
                if self._tail_count >= self.segment_size:
                    self._seal_tail()
            return events, index

    def _seal_tail(self) -> None:
        key = self._tail_key()
        raw = self.store.get(key)
        events = [parse_line(line) for line in raw.decode().splitlines()]
        sealed_key = f"{self._prefix}{_name(self._segment)}.jsonl.zst"
        compressed = zstd.compress(raw)
        if not self.store.exists(sealed_key):
            self.store.put(sealed_key, compressed)
        elif _decompress(self.store.get(sealed_key)) != raw:
            raise LogCorrupt("sealed segment differs from its tail")
        stored = self.store.get(sealed_key)
        self._index["segments"].append({"segment": self._segment, "first_seq": events[0].seq,
                                         "last_seq": events[-1].seq, "count": len(events),
                                         "sha256": hashlib.sha256(stored).hexdigest(), "bytes": len(stored)})
        self.store.put(self._prefix + "segments.json", canonical(self._index).encode(), overwrite=True)
        self.store.delete(key)
        self._segment += 1
        self._tail_count = 0

    def close(self) -> None:
        """Seal the partial tail and refuse further appends."""
        with self._lock:
            if self.closed:
                return
            if self._tail_count:
                self._seal_tail()
            self._index["closed"] = True
            self.store.put(self._prefix + "segments.json", canonical(self._index).encode(), overwrite=True)
            self.closed = True


class EventLogReader:
    def __init__(self, store: ObjectStore, run_id: str) -> None:
        self.store, self.run_id = store, run_id
        self._prefix = _prefix(run_id)

    def _segment_events(self, segment: int, raw: bytes) -> list[Event]:
        try:
            events = [parse_line(line) for line in raw.decode().splitlines()]
        except (UnicodeDecodeError, EventRejected) as exc:
            raise LogCorrupt(f"segment {segment}: {exc}") from None
        if any(e.run_id != self.run_id for e in events):
            raise LogCorrupt(f"segment {segment} contains another run's events")
        return events

    def iter_events(self, after_seq: int = 0) -> Iterator[Event]:
        index = load_index(self.store, self.run_id)
        expected: int | None = None
        for meta in index["segments"]:
            if meta["last_seq"] <= after_seq:
                expected = meta["last_seq"] + 1
                continue
            blob = self.store.get(f"{self._prefix}{_name(meta['segment'])}.jsonl.zst")
            if hashlib.sha256(blob).hexdigest() != meta["sha256"]:
                raise LogCorrupt(f"segment {meta['segment']} failed its checksum")
            for e in self._segment_events(meta["segment"], _decompress(blob)):
                if expected is not None and e.seq != expected:
                    raise LogCorrupt(f"sequence gap at {e.seq}")
                expected = e.seq + 1
                if e.seq > after_seq:
                    yield e
        tail_segment = (index["segments"][-1]["segment"] + 1) if index["segments"] else 1
        try:
            tail = self.store.get(f"{self._prefix}{_name(tail_segment)}.jsonl")
        except NotFound:
            return
        for e in self._segment_events(tail_segment, tail):
            if expected is not None and e.seq != expected:
                raise LogCorrupt(f"sequence gap at {e.seq}")
            if expected is None and e.seq != 1:
                raise LogCorrupt("log does not start at seq 1")
            expected = e.seq + 1
            if e.seq > after_seq:
                yield e

    def read_page(self, after_seq: int = 0, limit: int = 1000) -> list[Event]:
        if not 1 <= limit <= 1000 or after_seq < 0:
            raise ValueError("limit must be 1..1000 and after_seq >= 0")
        page: list[Event] = []
        for e in self.iter_events(after_seq):
            page.append(e)
            if len(page) >= limit:
                break
        return page
