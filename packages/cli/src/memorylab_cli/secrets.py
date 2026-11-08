"""The provider key. It is never a command-line argument (shell history, `ps`): only a hidden prompt, one line on stdin,
or MEMORYLAB_PROVIDER_KEY. It lives in a `Secret` whose repr and str are redacted, is read once, and goes straight into
the request body; it is never logged, printed, written to the session file, or put in exception text."""

import os
import sys
from typing import TextIO

import click


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __reduce__(self) -> tuple[type, tuple[str]]:           # pickling/copying must not expose it either
        raise TypeError("a Secret cannot be serialised")


def read_secret(prompt: str, *, from_stdin: bool, env_name: str = "MEMORYLAB_PROVIDER_KEY",
                stdin: TextIO | None = None) -> Secret:
    """stdin (one line) > environment > hidden prompt. The environment variable is removed once read, so child
    processes do not inherit it."""
    if from_stdin:
        line = (stdin or sys.stdin).readline().strip()
        if not line:
            raise click.UsageError("no key on stdin")
        return Secret(line)
    value = os.environ.pop(env_name, "")
    if value:
        return Secret(value.strip())
    got = click.prompt(prompt, hide_input=True, default="", show_default=False, err=True).strip()
    if not got:
        raise click.UsageError("a key is required")
    return Secret(got)
