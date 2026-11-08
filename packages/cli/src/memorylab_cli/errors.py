"""The one exception the CLI reports and exits on."""

from .exitcodes import exit_code_for


class CliError(Exception):
    """`code` selects the exit code (exitcodes.EXIT_CODES)."""

    def __init__(self, code: str, title: str, status: int = 0, retry_after: str | None = None) -> None:
        super().__init__(title)
        self.code, self.title, self.status, self.retry_after = code, title, status, retry_after

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.code)
