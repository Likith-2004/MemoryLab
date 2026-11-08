"""Process abstraction so supervision logic is testable without Docker."""

import subprocess
import threading
from collections.abc import Iterator
from typing import IO, Protocol

MAX_LINE_BYTES = 4096


class ProcessLike(Protocol):
    def write_stdin(self, data: bytes) -> None: ...
    def lines(self) -> Iterator[bytes]: ...
    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...
    def close_stdin(self) -> None: ...


class PopenProcess:
    """A real subprocess with piped stdin/stdout. stderr is discarded (never captured: it could
    carry secrets, and the worker writes none)."""

    def __init__(self, argv: list[str]) -> None:
        self._p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        self._lock = threading.Lock()

    def write_stdin(self, data: bytes) -> None:
        stdin: IO[bytes] = self._p.stdin  # type: ignore[assignment]
        with self._lock:
            stdin.write(data)
            stdin.flush()

    def close_stdin(self) -> None:
        try:
            self._p.stdin.close()  # type: ignore[union-attr]
        except OSError:
            pass

    def lines(self) -> Iterator[bytes]:
        stdout: IO[bytes] = self._p.stdout  # type: ignore[assignment]
        while True:
            line = stdout.readline(MAX_LINE_BYTES + 1)  # bounded: a hostile worker cannot grow our memory
            if not line:
                return
            if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                while stdout.read(MAX_LINE_BYTES):  # drain the rest of an oversized line
                    pass
                yield b""
                return
            yield line

    def poll(self) -> int | None:
        return self._p.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self._p.wait(timeout)

    def kill(self) -> None:
        try:
            self._p.kill()
        except ProcessLookupError:
            pass
