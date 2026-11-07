"""Key redaction. The plan: before any event is written, replace any occurrence of the visitor's
key (and of known key patterns) with [REDACTED]; the same scan runs again at export.

Two layers: `Redactor` knows the exact key (worker side, memory only) and also its common
encodings; the pattern scan needs no key and runs in ingest, at export and in log scrubbing.
Every replacement increments `hits` (metric: should stay ~0; any hit alerts).
"""

import base64
import json
import logging
import re
import threading
import urllib.parse
from typing import Any

REDACTED = "[REDACTED]"
KEY_PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    "openai_style_key": re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}"),
    "bearer_token": re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/\-]{20,}=*"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
_BYTE_PATTERNS = {name: re.compile(p.pattern.encode(), p.flags & ~re.UNICODE) for name, p in KEY_PATTERNS.items()}


def scan_text(text: str) -> list[str]:
    """Names of key patterns present in `text` (never the matches themselves)."""
    return [name for name, pat in KEY_PATTERNS.items() if pat.search(text)]


def scan_bytes(data: bytes) -> list[str]:
    return [name for name, pat in _BYTE_PATTERNS.items() if pat.search(data)]


MIN_NEEDLE = 8  # same floor as SecretKey: anything shorter is not a key


def _variants(key: str) -> set[str]:
    """Strings to search for: the key and its common encodings. For base64 embedded in other
    data at any byte offset, only the characters fully determined by the key are used (the
    first and last characters mix in neighbouring bytes)."""
    raw = key.encode()
    out = {key, json.dumps(key)[1:-1], urllib.parse.quote(key, safe=""), urllib.parse.quote_plus(key), raw.hex(),
           raw.hex().upper(), key[::-1]}
    for enc in (base64.b64encode, base64.urlsafe_b64encode):
        for shift in range(3):
            body = enc(b"x" * shift + raw).decode().rstrip("=")
            start = (4 * shift + 2) // 3  # first character that depends on key bytes only
            out.add(body[start:-2])       # drop the trailing character(s) that mix in following data
            if shift == 0:
                out.add(body)             # the standalone encoding
                out.add(enc(raw).decode())
    return {v for v in out if len(v) >= MIN_NEEDLE}


class Redactor:
    """Thread-safe. Holds the key only in process memory; its repr never shows it."""

    def __init__(self, keys: list[str] | None = None) -> None:
        self._needles = sorted({v for k in keys or [] for v in _variants(k)}, key=len, reverse=True)
        self._lock = threading.Lock()
        self.hits = 0

    def __repr__(self) -> str:
        return f"Redactor(keys={len(self._needles) > 0}, hits={self.hits})"

    def text(self, value: str) -> str:
        count = 0
        for needle in self._needles:
            if needle in value:
                value = value.replace(needle, REDACTED)
                count += 1
        for pat in KEY_PATTERNS.values():
            value, n = pat.subn(REDACTED, value)
            count += n
        if count:
            with self._lock:
                self.hits += count
        return value

    def obj(self, value: Any) -> Any:
        """Redact every string (and dict key) in a JSON-like structure; returns a new object."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.obj(v) for v in value]
        if isinstance(value, dict):
            return {self.text(k) if isinstance(k, str) else k: self.obj(v) for k, v in value.items()}
        return value


class ScrubFilter(logging.Filter):
    """Log filter: scrubs messages and args so a key can never reach a log sink."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor or Redactor()

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.text(str(record.msg))
        if record.args:
            record.args = tuple(self._redactor.text(str(a)) for a in (record.args if isinstance(record.args, tuple) else (record.args,)))
        return True
