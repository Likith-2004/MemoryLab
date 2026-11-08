"""login, logout, signup, me, delete-account."""

import os
import sys
import time

import click

from ..api import CliError
from ..app import build, json_option


def _password(prompt: str, from_stdin: bool, confirm: bool = False) -> str:
    if from_stdin:
        line = sys.stdin.readline().rstrip("\n")
        if not line:
            raise click.UsageError("no password on stdin")
        return line
    env = os.environ.pop("MEMORYLAB_PASSWORD", "")
    if env:
        return env
    return str(click.prompt(prompt, hide_input=True, confirmation_prompt=confirm, err=True))


@click.command()
@click.option("--email", prompt=True, help="Your account email.")
@click.option("--password-stdin", is_flag=True, help="Read the password from one line on stdin.")
@click.option("--magic-link", is_flag=True, help="Email a one-time code instead of using a password.")
@click.option("--code", default=None, help="The one-time code, or the sign-in link, from the email (with --magic-link; prompted for after sending the mail if omitted).")
@json_option
def login(email: str, password_stdin: bool, magic_link: bool, code: str | None, as_json: bool) -> None:
    """Sign in and store the session (mode 0600)."""
    app = build(as_json)
    if magic_link:
        if code is None:                                  # with --code you already have the mail: send nothing
            app.api.send_magic_link(email)
            app.out.note("A one-time code was sent if the address has an account.")
            code = click.prompt("code or sign-in link from the email", hide_input=True, err=True).strip()
        session = app.api.verify_code(email, code)
    else:
        session = app.api.sign_in(email, _password("password", password_stdin))
    app.out.data({"user_id": session.user_id, "email": session.email},
                 lambda: app.out.line(f"logged in as {session.email}"))


@click.command()
@click.option("--everywhere", is_flag=True, help="Sign out of every device (revokes all refresh tokens).")
@json_option
def logout(everywhere: bool, as_json: bool) -> None:
    """Sign out and delete the stored session."""
    app = build(as_json)
    app.api.sign_out(everywhere)
    app.out.data({"logged_out": True, "everywhere": everywhere}, lambda: app.out.line("logged out"))


@click.command()
@click.option("--email", prompt=True)
@click.option("--password-stdin", is_flag=True)
@json_option
def signup(email: str, password_stdin: bool, as_json: bool) -> None:
    """Create an account (through the API, which answers the same whether or not the address exists)."""
    app = build(as_json)
    password = _password("password (12+ characters)", password_stdin, confirm=not password_stdin)
    body = app.api.post("/auth/signup", {"email": email, "password": password}, auth=False)
    app.out.data(body, lambda: app.out.line(str(body.get("message", "ok"))))


@click.command()
@json_option
def me(as_json: bool) -> None:
    """Your profile and limits."""
    app = build(as_json)
    data = app.api.get("/me")
    app.out.data(data, lambda: app.out.table("", ["field", "value"], [
        ["id", data["id"]], ["handle", data.get("handle")], ["role", data["role"]], ["active runs", data["active_runs"]],
        ["runs/hour", data["limits"]["runs_per_hour"]], ["reads/min", data["limits"]["reads_per_minute"]]]))


@click.command("delete-account")
@click.option("--confirm", default=None, help="Type DELETE to skip the prompt.")
@json_option
def delete_account(confirm: str | None, as_json: bool) -> None:
    """Erase your account: private runs are deleted, verified public benchmark runs are anonymised."""
    app = build(as_json)
    if confirm is None:
        confirm = click.prompt("This cannot be undone. Type DELETE to confirm", err=True)
    if confirm != "DELETE":
        raise CliError("validation_failed", "not confirmed: nothing was deleted")
    deadline = time.monotonic() + 60
    while True:
        try:
            app.api.delete("/me", {"confirm": "DELETE"})
            break
        except CliError as exc:
            if exc.code != "runs_still_active" or time.monotonic() > deadline:
                raise
            app.out.note("your runs are still stopping; retrying…")
            time.sleep(min(5.0, max(1.0, float(exc.retry_after or 5))))
    app.api.store.clear()
    app.out.data({"deleted": True}, lambda: app.out.line("account deleted"))
