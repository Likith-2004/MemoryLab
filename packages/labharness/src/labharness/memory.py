"""Per-run virtual memory filesystem stored as versioned rows.

Nothing here touches the host disk: files are rows in a `MemoryStore`. Every mutation appends
an immutable row with a global revision (`rev`) and the harness step, so a snapshot is just a
`rev` pointer (copy-on-write: rows are shared, never copied or modified) and a fork is a new
store seeded with the parent's rows up to a rev.

Tool calls go through `MemoryService.dispatch`, which validates the closed argument schema,
enforces the caps, and returns both the result the model sees and the events to log
(`mem.read`, `mem.write` with full diff, `mem.delete`, `mem.reject`).
"""

import difflib
import hashlib
import threading
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from .memory_paths import Reject, check_content, check_path, conflicts, safe_echo

MAX_READ_CHARS = 8192  # the plan's "<= 8 KB returned" limit for mem_read
MAX_FILE_CHARS_LIMIT = MAX_READ_CHARS  # so a file always fits in one mem_read
SEARCH_MAX_RESULTS = 20
SEARCH_SNIPPET_CHARS = 200
SEARCH_QUERY_MAX = 200
DIFF_LINE_PRODUCT_LIMIT = 4_000_000  # bound difflib's quadratic worst case
TOOL_NAMES = ("mem_list", "mem_read", "mem_write", "mem_delete", "mem_search")


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    max_files: int = 10
    max_file_chars: int = 4000
    max_total_chars: int = 20000
    # History budget: every write keeps its version, so bound the rows and characters kept.
    max_history_rows: int = 5000
    max_history_chars: int = 8_000_000

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a bool")  # noqa: TRY004
        for name, lo, hi in (("max_files", 1, 200), ("max_file_chars", 1, MAX_FILE_CHARS_LIMIT),
                             ("max_total_chars", 1, 1_000_000), ("max_history_rows", 1, 100_000),
                             ("max_history_chars", 1, 100_000_000)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{name} must be an int in {lo}..{hi}")


@dataclass(frozen=True, slots=True)
class MemoryRow:
    run_id: str
    path: str
    version: int      # per-path, starts at 1, counts tombstones
    step: int         # harness step that wrote it
    rev: int          # global, strictly increasing within a store
    content: str
    chars: int
    deleted: bool

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any]


@dataclass(frozen=True)
class ToolOutcome:
    ok: bool
    result: dict[str, Any]   # exactly what the model sees
    events: tuple[Event, ...]


@dataclass(frozen=True)
class MemorySnapshot:
    """A pointer, not a copy: the store state as of `rev`."""

    run_id: str
    rev: int
    step: int


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class MemoryStore:
    """Append-only row store with per-path indexes. Thread-safe."""

    run_id: str
    _rows: list[MemoryRow] = field(default_factory=list)
    _by_path: dict[str, list[MemoryRow]] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _history_chars: int = 0

    @property
    def rev(self) -> int:
        return self._rows[-1].rev if self._rows else 0

    @property
    def last_step(self) -> int:
        return self._rows[-1].step if self._rows else 0

    @property
    def history_rows(self) -> int:
        return len(self._rows)

    @property
    def history_chars(self) -> int:
        return self._history_chars

    def append(self, path: str, step: int, content: str, deleted: bool) -> MemoryRow:
        with self._lock:
            versions = self._by_path.setdefault(path, [])
            row = MemoryRow(self.run_id, path, len(versions) + 1, step, self.rev + 1, content,
                            len(content), deleted)
            self._rows.append(row)
            versions.append(row)
            self._history_chars += row.chars
            return row

    def latest(self, path: str) -> MemoryRow | None:
        with self._lock:
            versions = self._by_path.get(path)
            return versions[-1] if versions and not versions[-1].deleted else None

    def live(self) -> dict[str, MemoryRow]:
        with self._lock:
            return {p: v[-1] for p, v in self._by_path.items() if not v[-1].deleted}

    def view_at(self, rev: int) -> dict[str, MemoryRow]:
        """Live files as of `rev` (rows are immutable, so this never changes for a given rev)."""
        with self._lock:
            out: dict[str, MemoryRow] = {}
            for path, versions in self._by_path.items():
                i = bisect_right([r.rev for r in versions], rev)
                if i and not versions[i - 1].deleted:
                    out[path] = versions[i - 1]
            return out

    def rev_at_step(self, step: int) -> int:
        """Highest rev written at or before `step` (0 if none)."""
        with self._lock:
            best = 0
            for row in self._rows:
                if row.step <= step:
                    best = row.rev
                else:
                    break
            return best

    def versions(self, path: str) -> list[MemoryRow]:
        with self._lock:
            return list(self._by_path.get(path, []))

    def fork(self, rev: int, new_run_id: str) -> MemoryStore:
        """New store seeded with this store's rows up to `rev` (rows are shared, not copied)."""
        with self._lock:
            child = MemoryStore(new_run_id)
            for row in self._rows:
                if row.rev > rev:
                    break
                moved = replace(row, run_id=new_run_id)
                child._rows.append(moved)
                child._by_path.setdefault(moved.path, []).append(moved)
                child._history_chars += moved.chars
            return child


def _unified_diff(path: str, before: str, after: str) -> str:
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    if len(a) * len(b) > DIFF_LINE_PRODUCT_LIMIT:
        return "(diff omitted: file too large to diff; see before/after hashes)"
    return "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=2))


class MemoryService:
    def __init__(self, store: MemoryStore, config: MemoryConfig,
                 content_filter: Callable[[str], str] | None = None) -> None:
        self.store = store
        self.config = config
        # Applied to every written file before validation (the run's key redactor): a credential
        # must never be stored in memory, even if the model somehow produced one.
        self.content_filter = content_filter

    # ---- snapshots ------------------------------------------------------------------
    def snapshot(self) -> MemorySnapshot:
        return MemorySnapshot(self.store.run_id, self.store.rev, self.store.last_step)

    def tree_at_step(self, step: int) -> list[dict[str, Any]]:
        view = self.store.view_at(self.store.rev_at_step(step))
        return [{"path": p, "chars": r.chars, "version": r.version} for p, r in sorted(view.items())]

    def read_version(self, path: str, version: int) -> MemoryRow | None:
        versions = self.store.versions(check_path(path))
        return versions[version - 1] if 1 <= version <= len(versions) else None

    def diff_versions(self, path: str, from_version: int, to_version: int) -> str | None:
        a, b = self.read_version(path, from_version), self.read_version(path, to_version)
        if a is None or b is None:
            return None
        return _unified_diff(path, a.content, b.content)

    @classmethod
    def forked(cls, parent: MemoryService, snapshot: MemorySnapshot, new_run_id: str,
               config: MemoryConfig) -> MemoryService:
        return cls(parent.store.fork(snapshot.rev, new_run_id), config, parent.content_filter)

    # ---- tools ----------------------------------------------------------------------
    def dispatch(self, tool: str, args: object, step: int) -> ToolOutcome:
        """Run one memory tool call. Never raises for bad model input; bad input becomes a
        `mem.reject` outcome the model can see."""
        if isinstance(step, bool) or not isinstance(step, int) or step < 0 or step < self.store.last_step:
            raise ValueError("step must be a non-negative int that never goes backwards")
        handlers = {"mem_list": self._list, "mem_read": self._read, "mem_write": self._write,
                    "mem_delete": self._delete, "mem_search": self._search}
        op = tool[4:] if tool in handlers else "unknown"
        try:
            if tool not in handlers:
                raise Reject("unknown_tool", "not a memory tool")
            if not self.config.enabled:
                raise Reject("memory_disabled", "memory is not available in this run")
            if not isinstance(args, dict):
                raise Reject("invalid_args", "arguments must be an object")
            with self.store._lock:
                return handlers[tool](args, step)
        except Reject as rej:
            path = args.get("path") if isinstance(args, dict) else None
            data = {"op": op, "reason": rej.reason, "detail": rej.detail}
            if path is not None:
                data["path"] = safe_echo(path, 120)
            return ToolOutcome(False, {"ok": False, "error": rej.reason, "message": rej.detail},
                               (Event("mem.reject", data),))

    @staticmethod
    def _exact_args(args: dict[str, Any], required: set[str], optional: frozenset[str] = frozenset()) -> None:
        keys = set(args)
        if not required <= keys or not keys <= required | optional:
            raise Reject("invalid_args", f"expected arguments: {sorted(required | optional)}")

    def _list(self, args: dict[str, Any], step: int) -> ToolOutcome:
        self._exact_args(args, set(), frozenset({"path"}))
        prefix = args.get("path")
        if prefix is not None:
            check_path(prefix)
        files = [{"path": p, "chars": r.chars} for p, r in sorted(self.store.live().items())
                 if prefix is None or p == prefix or p.startswith(prefix + "/")]
        return ToolOutcome(True, {"ok": True, "files": files},
                           (Event("mem.read", {"op": "list", "path": prefix, "count": len(files)}),))

    def _read(self, args: dict[str, Any], step: int) -> ToolOutcome:
        self._exact_args(args, {"path"})
        path = check_path(args["path"])
        row = self.store.latest(path)
        if row is None:
            return ToolOutcome(False, {"ok": False, "error": "not_found", "message": "no such file"},
                               (Event("mem.read", {"op": "read", "path": path, "found": False}),))
        content = row.content[:MAX_READ_CHARS]  # unreachable truncation: max_file_chars <= MAX_READ_CHARS
        return ToolOutcome(True, {"ok": True, "path": path, "content": content, "version": row.version},
                           (Event("mem.read", {"op": "read", "path": path, "found": True, "chars": row.chars,
                                               "version": row.version}),))

    def _search(self, args: dict[str, Any], step: int) -> ToolOutcome:
        self._exact_args(args, {"query"})
        query = args["query"]
        if not isinstance(query, str) or not 1 <= len(query) <= SEARCH_QUERY_MAX:
            raise Reject("invalid_args", f"query must be a string of 1-{SEARCH_QUERY_MAX} chars")
        needle = query.casefold()
        hits: list[tuple[int, str, int, str]] = []
        for path, row in self.store.live().items():
            lines = row.content.splitlines()
            count = 0
            first: tuple[int, str] | None = None
            for number, line in enumerate(lines, 1):
                if needle in line.casefold():
                    count += 1
                    first = first or (number, line[:SEARCH_SNIPPET_CHARS])
            if first:
                hits.append((count, path, first[0], first[1]))
        hits.sort(key=lambda h: (-h[0], h[1]))
        results = [{"path": p, "matches": c, "line": n, "snippet": s} for c, p, n, s in hits[:SEARCH_MAX_RESULTS]]
        return ToolOutcome(True, {"ok": True, "results": results},
                           (Event("mem.read", {"op": "search", "query": safe_echo(query, SEARCH_QUERY_MAX),
                                               "count": len(results)}),))

    def _write(self, args: dict[str, Any], step: int) -> ToolOutcome:
        self._exact_args(args, {"path", "content"})
        path = check_path(args["path"])
        raw = args["content"]
        content = self.content_filter(raw) if self.content_filter is not None and isinstance(raw, str) else raw
        redacted = content != raw
        content = check_content(content, self.config.max_file_chars)
        live = self.store.live()
        clash = conflicts(path, frozenset(live))
        if clash is not None:
            raise Reject("path_conflict", f"'{clash}' already exists as a file or folder that conflicts")
        before = live.get(path)
        if before is None and len(live) >= self.config.max_files:
            raise Reject("files_cap", f"at most {self.config.max_files} files allowed")
        total_after = sum(r.chars for r in live.values()) - (before.chars if before else 0) + len(content)
        if total_after > self.config.max_total_chars:
            raise Reject("total_cap", f"total would be {total_after} chars (max {self.config.max_total_chars})")
        self._check_history(len(content))
        row = self.store.append(path, step, content, deleted=False)
        before_text = before.content if before else ""
        event = Event("mem.write", {
            "path": path, "version": row.version, "step": step, "rev": row.rev,
            "before_sha256": _sha(before_text) if before else None, "after_sha256": row.sha256,
            "chars": row.chars, "created": before is None,
            "diff": _unified_diff(path, before_text, content), "content": content,
            **({"redacted": True} if redacted else {}),
        })
        return ToolOutcome(True, {"ok": True, "path": path, "version": row.version, "chars": row.chars},
                           (event,))

    def _delete(self, args: dict[str, Any], step: int) -> ToolOutcome:
        self._exact_args(args, {"path"})
        path = check_path(args["path"])
        before = self.store.latest(path)
        if before is None:
            raise Reject("not_found", "no such file")
        self._check_history(0)
        row = self.store.append(path, step, "", deleted=True)
        return ToolOutcome(True, {"ok": True, "path": path},
                           (Event("mem.delete", {"path": path, "version": row.version, "step": step, "rev": row.rev,
                                                 "before_sha256": before.sha256, "chars": before.chars}),))

    def _check_history(self, new_chars: int) -> None:
        if self.store.history_rows + 1 > self.config.max_history_rows:
            raise Reject("history_cap", "this run has used its memory-history budget")
        if self.store.history_chars + new_chars > self.config.max_history_chars:
            raise Reject("history_cap", "this run has used its memory-history budget")


def tool_schemas(config: MemoryConfig) -> list[dict[str, Any]]:
    """Closed JSON schemas for the memory tools. With memory disabled (the no-memory arm) the
    list is empty: the tools are absent from what the model sees, not merely discouraged."""
    if not config.enabled:
        return []

    def schema(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {"name": name, "description": description,
                "input_schema": {"type": "object", "properties": props, "required": required,
                                 "additionalProperties": False}}

    path = {"type": "string", "pattern": "^[a-z0-9_./-]{1,120}$", "maxLength": 120,
            "description": "lowercase path, at most 4 folders deep, no '..'"}
    return [
        schema("mem_list", "List your memory files with sizes.", {"path": path}, []),
        schema("mem_read", "Read one memory file.", {"path": path}, ["path"]),
        schema("mem_write", "Create or replace a memory file.",
               {"path": path, "content": {"type": "string", "maxLength": config.max_file_chars}},
               ["path", "content"]),
        schema("mem_delete", "Delete a memory file.", {"path": path}, ["path"]),
        schema("mem_search", "Case-insensitive substring search over your memory files.",
               {"query": {"type": "string", "minLength": 1, "maxLength": SEARCH_QUERY_MAX}}, ["query"]),
    ]
