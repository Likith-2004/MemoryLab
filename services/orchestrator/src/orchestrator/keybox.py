"""Holds a visitor key in memory for as long as a run is queued or starting, then destroys the
reference. (CPython cannot overwrite a string's bytes; dropping every reference and keeping the
key out of logs, files, env and argv is the achievable guarantee: see SECURITY.md.)"""

import threading

from labharness.providers.secret import SecretKey


class KeyBox:
    __slots__ = ("_key", "_lock")

    def __init__(self, key: SecretKey) -> None:
        self._key: SecretKey | None = key
        self._lock = threading.Lock()

    def take(self) -> SecretKey:
        """Hand the key over exactly once; afterwards the box is empty."""
        with self._lock:
            key, self._key = self._key, None
        if key is None:
            raise RuntimeError("key already taken or discarded")
        return key

    def discard(self) -> None:
        with self._lock:
            self._key = None

    @property
    def empty(self) -> bool:
        return self._key is None

    def __repr__(self) -> str:
        return f"KeyBox(empty={self.empty})"
