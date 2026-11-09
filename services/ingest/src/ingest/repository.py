"""Run records the ingest service reads and updates. The Postgres implementation (M6) shares this
interface; the in-memory one serves tests and local development."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from runlog.eventlog import IndexEntry


@dataclass
class RunRecord:
    run_id: str
    status: str | None = None
    next_batch_no: dict[str, int] = field(default_factory=lambda: {"worker": 1, "orchestrator": 1})
    event_count: int = 0
    bytes_total: int = 0
    frames: int = 0
    tokens: int = 0
    steps: int = 0
    score: int = 0
    closed: bool = False
    manifest_sealed: bool = False
    last_ack: dict[str, dict[str, Any]] = field(default_factory=dict)  # per scope
    bundle_sha256: str | None = None


@dataclass(frozen=True)
class ParentRef:
    """A fork's link to its parent's checkpoint, as the database records it (the API checked visibility when it created the run)."""

    parent_run_id: str
    step: int
    transcript_offset: int      # seq of the parent's `checkpoint` event
    checkpoint_id: str


class RunRepository(Protocol):
    def get(self, run_id: str) -> RunRecord | None: ...
    def lock(self, run_id: str) -> Any: ...
    def save(self, record: RunRecord) -> None: ...
    def add_index(self, run_id: str, entries: list[IndexEntry]) -> None: ...
    def add_memory_rows(self, run_id: str, rows: list[dict[str, Any]]) -> None: ...
    def add_checkpoint(self, run_id: str, row: dict[str, Any]) -> str: ...
    def parent_of(self, run_id: str) -> ParentRef | None: ...


def _copy(record: RunRecord) -> RunRecord:
    clone = RunRecord(**record.__dict__)
    clone.next_batch_no = dict(record.next_batch_no)
    clone.last_ack = {k: dict(v) for k, v in record.last_ack.items()}
    return clone


@dataclass
class InMemoryRunRepository:
    runs: dict[str, RunRecord] = field(default_factory=dict)
    index: dict[str, list[IndexEntry]] = field(default_factory=dict)
    memory_rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    checkpoints: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    parents: dict[str, ParentRef] = field(default_factory=dict)
    _locks: dict[str, threading.RLock] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def register(self, run_id: str, *, parent: ParentRef | None = None) -> RunRecord:
        """The API/orchestrator creates the run row before any token is minted."""
        self.runs[run_id] = RunRecord(run_id)
        if parent is not None:
            self.parents[run_id] = parent
        return self.runs[run_id]

    def get(self, run_id: str) -> RunRecord | None:
        record = self.runs.get(run_id)
        return None if record is None else _copy(record)

    @contextmanager
    def _held(self, run_id: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(run_id, threading.RLock())
        with lock:
            yield

    def lock(self, run_id: str) -> Any:
        return self._held(run_id)

    def save(self, record: RunRecord) -> None:
        self.runs[record.run_id] = _copy(record)

    def add_index(self, run_id: str, entries: list[IndexEntry]) -> None:
        self.index.setdefault(run_id, []).extend(entries)

    def add_memory_rows(self, run_id: str, rows: list[dict[str, Any]]) -> None:
        self.memory_rows.setdefault(run_id, []).extend(rows)

    def add_checkpoint(self, run_id: str, row: dict[str, Any]) -> str:
        cid = f"cp-{run_id[:8]}-{len(self.checkpoints.get(run_id, [])) + 1}"
        self.checkpoints.setdefault(run_id, []).append({"id": cid, **row})
        return cid

    def parent_of(self, run_id: str) -> ParentRef | None:
        return self.parents.get(run_id)
