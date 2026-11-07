"""Memory path and content rules. Pure functions, no I/O.

Paths are accepted only in canonical form (never rewritten): `[a-z0-9_\\-/.]{1,120}`, 1..4
segments, no empty / "." / ".." segments, no leading or trailing slash. That removes path
traversal by construction: a path is only ever a key into the run's row set, never a
filesystem path.
"""

import re

MAX_PATH_CHARS = 120
MAX_DEPTH = 4
_PATH = re.compile(r"[a-z0-9_\-/.]{1,120}", re.ASCII)
# C0 controls except tab/newline/carriage return, plus DEL.
_FORBIDDEN_CONTROLS = frozenset(chr(c) for c in range(0x20) if chr(c) not in "\t\n\r") | {"\x7f"}


class Reject(Exception):
    """Why an input was refused. `reason` is a stable machine code."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(reason, detail)
        self.reason = reason
        self.detail = detail


def check_path(path: object) -> str:
    """Return `path` unchanged if canonical, else raise Reject."""
    if not isinstance(path, str):
        raise Reject("invalid_path", "path must be a string")
    if not path.isascii() or _PATH.fullmatch(path) is None:
        raise Reject("invalid_path", "path must match [a-z0-9_-/.] and be 1-120 chars")
    segments = path.split("/")
    if len(segments) > MAX_DEPTH:
        raise Reject("invalid_path", f"path depth must be at most {MAX_DEPTH}")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise Reject("invalid_path", "empty, '.' and '..' segments are not allowed")
    return path


def check_content(content: object, max_chars: int) -> str:
    """Content is opaque text: stored and returned verbatim, never interpreted."""
    if not isinstance(content, str):
        raise Reject("invalid_content", "content must be a string")
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        raise Reject("invalid_content", "content contains unpaired surrogates") from None
    if any(ch in _FORBIDDEN_CONTROLS for ch in content):
        raise Reject("invalid_content", "content contains control characters")
    if len(content) > max_chars:
        raise Reject("file_cap", f"file would be {len(content)} chars (max {max_chars})")
    return content


def conflicts(path: str, existing: "set[str] | frozenset[str]") -> str | None:
    """A file cannot also be a folder: return the clashing existing path, if any."""
    prefix = path + "/"
    for other in existing:
        if other.startswith(prefix) or path.startswith(other + "/"):
            return other
    return None


def safe_echo(value: object, limit: int = 60) -> str:
    """Bounded, printable rendering of untrusted input for error messages and events."""
    text = value if isinstance(value, str) else f"<{type(value).__name__}>"
    text = "".join(ch if ch.isprintable() else "?" for ch in text[:limit])
    return text + ("..." if isinstance(value, str) and len(value) > limit else "")
