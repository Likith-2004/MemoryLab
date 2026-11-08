"""Entry point. Errors become one line on stderr (or one JSON document with --json) and a stable exit code; an
unexpected exception prints only its type, because its text could carry request data."""

from typing import Any

import click

from .api import CliError
from .commands import account, admin, catalogue, experiments, runs
from .exitcodes import EXIT_CODES, GENERIC
from .render import error_out


class Cli(click.Group):
    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except CliError as exc:
            error_out(exc.code, exc.title, exc.status, bool(ctx.meta.get("as_json")), exc.retry_after)
            ctx.exit(exc.exit_code)
        except (click.ClickException, click.exceptions.Exit, click.Abort, SystemExit):
            raise
        except KeyboardInterrupt:
            ctx.exit(130)
        except Exception as exc:  # noqa: BLE001 - never print its text
            error_out("internal", f"unexpected {type(exc).__name__}", 0, bool(ctx.meta.get("as_json")))
            ctx.exit(GENERIC)


@click.group(cls=Cli, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="memorylab-cli")
def cli() -> None:
    """memorylab: launch, watch, fork and compare agent runs from the terminal.

    Configure with MEMORYLAB_API_URL, MEMORYLAB_AUTH_URL and MEMORYLAB_PUBLISHABLE_KEY. The provider key is never an
    argument: use a hidden prompt, --api-key-stdin, or MEMORYLAB_PROVIDER_KEY."""


for _command in (account.login, account.logout, account.signup, account.me, account.delete_account, catalogue.games,
                 catalogue.presets, catalogue.models, catalogue.leaderboard, runs.runs, runs.fork,
                 experiments.experiments, admin.admin):
    cli.add_command(_command)


def main() -> None:
    cli()


__all__ = ["EXIT_CODES", "cli", "main"]
