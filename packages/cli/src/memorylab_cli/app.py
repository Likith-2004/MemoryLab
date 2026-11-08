"""The per-invocation context shared by every command."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import click

from .api import Api
from .config import Config, from_env
from .render import Out
from .session import SessionStore


@dataclass
class App:
    cfg: Config
    api: Api
    out: Out


def build(as_json: bool) -> App:
    ctx = click.get_current_context(silent=True)
    if ctx is not None:
        ctx.meta["as_json"] = as_json                  # read by the error handler in main.Cli
    cfg = from_env()                                   # refuses plain http to a remote host before anything is sent
    out = Out(as_json)
    for warning in cfg.warnings:
        out.note(warning)
    return App(cfg, Api(cfg, SessionStore(cfg.session_path)), out)


F = TypeVar("F", bound=Callable[..., Any])


def json_option[F: Callable[..., Any]](f: F) -> F:
    return click.option("--json", "as_json", is_flag=True, help="Print the API's JSON instead of a table.")(f)
