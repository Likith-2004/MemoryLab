"""Output. Human output goes through rich with markup and highlighting OFF and every server-supplied string stripped
of terminal control sequences (a memory file or a title must not be able to move the cursor, retitle the window or
inject escape codes). `--json` prints the API's JSON unchanged, one document on stdout."""

import json
import re
import sys
from collections.abc import Callable
from typing import Any

from rich.console import Console
from rich.table import Table

# C0 (except \t and \n), DEL, C1, and the unicode line/paragraph separators and bidi overrides
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def safe(value: Any) -> str:
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}" if ord(m.group()) < 256 else f"\\u{ord(m.group()):04x}", text)


class Out:
    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json
        self.console = Console(markup=False, highlight=False, emoji=False, soft_wrap=True)
        self.err = Console(stderr=True, markup=False, highlight=False, emoji=False, soft_wrap=True)

    def data(self, payload: Any, human: Callable[[], None] | None = None) -> None:
        if self.as_json:
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        elif human is not None:
            human()

    def line(self, text: str) -> None:
        self.console.print(safe(text))

    def note(self, text: str) -> None:
        """Diagnostics go to stderr in both modes, so stdout stays one clean document."""
        self.err.print(safe(text))

    def table(self, title: str, columns: list[str], rows: list[list[Any]]) -> None:
        t = Table(title=title or None, show_lines=False)
        for c in columns:
            t.add_column(c, overflow="fold")
        for r in rows:
            t.add_row(*(safe(c) for c in r))
        self.console.print(t)


def error_out(code: str, title: str, status: int, as_json: bool, retry_after: str | None = None) -> None:
    if as_json:
        body: dict[str, Any] = {"error": {"code": code, "title": title, "status": status}}
        if retry_after:
            body["error"]["retry_after"] = retry_after
        sys.stderr.write(json.dumps(body) + "\n")
    else:
        extra = f" (retry after {retry_after}s)" if retry_after else ""
        sys.stderr.write(f"error [{code}]: {safe(title)}{extra}\n")
