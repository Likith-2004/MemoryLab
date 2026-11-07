"""Object store abstraction (S3-compatible shape) and a local-directory implementation.

Keys are validated names, never paths supplied by users; the local store additionally refuses
anything that resolves outside its root.
"""

import os
import re
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

_SEGMENT = re.compile(r"[a-z0-9_][a-z0-9_.\-]{0,99}")
MAX_KEY_CHARS = 300
MAX_SEGMENTS = 8
DEFAULT_MAX_OBJECT_BYTES = 64 * 1024 * 1024


class StoreError(Exception):
    pass


class AlreadyExists(StoreError):
    pass


class NotFound(StoreError):
    pass


class TooLarge(StoreError):
    pass


class BadKey(StoreError):
    pass


def check_key(key: object) -> str:
    if not isinstance(key, str) or not 1 <= len(key) <= MAX_KEY_CHARS or not key.isascii():
        raise BadKey("invalid key")
    parts = key.split("/")
    if len(parts) > MAX_SEGMENTS or any(_SEGMENT.fullmatch(p) is None or p.endswith(".") or ".." in p for p in parts):
        raise BadKey("invalid key")
    return key


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, *, overwrite: bool = False) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...
    def append(self, key: str, data: bytes) -> int: ...


class LocalObjectStore:
    """Directory-backed store. Writes are atomic (temp file + link/rename); `append` is
    serialised per key within the process."""

    def __init__(self, root: str | Path, max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_object_bytes = max_object_bytes
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _path(self, key: str) -> Path:
        check_key(key)
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise BadKey("invalid key")
        return path

    @contextmanager
    def _lock(self, key: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            yield

    def put(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
        if len(data) > self.max_object_bytes:
            raise TooLarge(key)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".tmp-{secrets.token_hex(8)}"
        tmp.write_bytes(data)
        try:
            if overwrite:
                os.replace(tmp, path)
            else:
                try:
                    os.link(tmp, path)  # fails if the key exists: immutable objects stay immutable
                except FileExistsError:
                    raise AlreadyExists(key) from None
        finally:
            tmp.unlink(missing_ok=True)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise NotFound(key) from None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str) -> list[str]:
        base = self._path(prefix.rstrip("/")) if prefix else self.root
        if not base.exists():
            return []
        if base.is_file():
            return [prefix]
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*")
                      if p.is_file() and not p.name.startswith(".tmp-"))

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def append(self, key: str, data: bytes) -> int:
        """Append and return the byte offset where `data` starts."""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock(key), open(path, "ab") as fh:
            offset = fh.tell()
            if offset + len(data) > self.max_object_bytes:
                raise TooLarge(key)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            return offset
