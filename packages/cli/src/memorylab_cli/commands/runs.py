"""runs new | list | show | stop | delete | patch | events | checkpoints | score | frame | export | memory ... | fork."""

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any

import click

from ..app import App, F, build, json_option
from ..errors import CliError
from ..render import safe
from ..secrets import Secret, read_secret
from .watch import watch_run

VISIBILITIES = click.Choice(["private", "unlisted", "public"])


def _key_options(f: F) -> F:
    """The only ways to give a provider key. There is deliberately no --api-key option."""
    return click.option("--api-key-stdin", is_flag=True,
                        help="Read the provider key from one line on stdin (else MEMORYLAB_PROVIDER_KEY, else a hidden prompt).")(f)


def _load_config_file(path: str) -> dict[str, Any]:
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise CliError("config", f"cannot read {path} as JSON") from None
    if not isinstance(doc, dict):
        raise CliError("config", "the config file must hold one JSON object")
    if _has_key_field(doc):
        raise CliError("config", "a key field in the config file was refused: give the provider key on stdin, in "
                       "MEMORYLAB_PROVIDER_KEY, or at the prompt")
    return doc


def _has_key_field(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(k).lower() in ("api_key", "apikey", "provider_key", "key") or _has_key_field(v)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(_has_key_field(v) for v in value)
    return False


def _choose(prompt: str, options: list[str], default: str | None = None) -> str:
    return str(click.prompt(prompt, type=click.Choice(options), default=default, err=True))


def _wizard(app: App, game: str | None, visibility: str | None) -> dict[str, Any]:
    games = [g["name"] for g in app.api.get("/games", auth=False)]
    models = app.api.get("/models", auth=False)["providers"]
    presets = [p["ref"] for p in app.api.get("/presets", auth=False)]
    chosen_game = game or _choose("game", games, games[0] if len(games) == 1 else None)
    chosen_visibility = visibility or _choose("visibility", ["private", "unlisted", "public"], "private")
    provider = _choose("provider", sorted(models), min(models) if len(models) == 1 else None)
    model_id = _choose("model", list(models[provider]))
    preset_ref = _choose("preset", presets, "general@1" if "general@1" in presets else None)
    mode = _choose("mode", ["demo", "benchmark"], "demo")
    caps = {"max_steps": click.prompt("max steps", type=click.IntRange(1, 100_000), default=500, err=True),
            "max_tokens": click.prompt("max tokens", type=click.IntRange(1, 1_000_000_000), default=500_000, err=True),
            "max_wall_s": click.prompt("max wall seconds", type=click.IntRange(1, 86_400), default=1800, err=True)}
    return {"game": chosen_game, "visibility": chosen_visibility, "caps": caps,
            "config": {"provider": provider, "model_id": model_id, "preset_ref": preset_ref, "mode": mode}}


def _send_key(body: dict[str, Any], key: Secret) -> dict[str, Any]:
    return {**body, "api_key": key.reveal()}


@click.group()
def runs() -> None:
    """Create, inspect and control runs."""


@runs.command("new")
@click.option("--config", "config_file", type=click.Path(), default=None, help="A JSON file: game, config, caps, visibility, preset_text.")
@click.option("--game", default=None)
@click.option("--visibility", type=VISIBILITIES, default=None)
@click.option("--preset-text-file", type=click.Path(exists=True, dir_okay=False), default=None, help="Custom instructions (Demo mode).")
@click.option("--validate-only", is_flag=True, help="Check the configuration and print its hash; start nothing, ask for no key.")
@click.option("--watch", "then_watch", is_flag=True, help="Follow the run after starting it.")
@_key_options
@json_option
def runs_new(config_file: str | None, game: str | None, visibility: str | None, preset_text_file: str | None,
             validate_only: bool, then_watch: bool, api_key_stdin: bool, as_json: bool) -> None:
    """Start a run. The provider key is read from stdin, MEMORYLAB_PROVIDER_KEY, or a hidden prompt, never from an argument."""
    app = build(as_json)
    body = _load_config_file(config_file) if config_file else _wizard(app, game, visibility)
    if config_file:
        if game:
            body["game"] = game
        if visibility:
            body["visibility"] = visibility
    if preset_text_file:
        body["preset_text"] = Path(preset_text_file).read_text()
    if validate_only:
        probe = {k: v for k, v in body.items() if k in ("config", "caps", "preset_text")}
        out = app.api.post("/runs/validate", probe)
        app.out.data(out, lambda: app.out.table("", ["field", "value"], [["config hash", out["config_hash"]],
                                                                        ["mode", out["mode"]],
                                                                        ["benchmark eligible", out["benchmark_eligible"]]]))
        return
    key = read_secret("provider API key", from_stdin=api_key_stdin)
    created = app.api.post("/runs", _send_key(body, key))
    del key
    app.out.data(created, lambda: app.out.line(f"run {created['run_id']} {created['status']}"))
    if then_watch:
        raise SystemExit(watch_run(app, created["run_id"]))


@runs.command("list")
@click.option("--limit", type=click.IntRange(1, 100), default=25, show_default=True)
@click.option("--all", "all_pages", is_flag=True, help="Follow the cursor to the end.")
@json_option
def runs_list(limit: int, all_pages: bool, as_json: bool) -> None:
    """Your runs, newest first."""
    app = build(as_json)
    out: list[dict[str, Any]] = []
    before = None
    while True:
        page = app.api.get("/runs", params={"limit": limit, **({"before": before} if before else {})})
        out += page["runs"]
        before = page["next"]
        if not all_pages or not before:
            break
    app.out.data(out, lambda: app.out.table("runs", ["id", "status", "visibility", "model", "steps", "title"],
                                            [[r["id"], r["status"], r["visibility"], r.get("model_id"), r["steps"], r.get("title")]
                                             for r in out]))


@runs.command("show")
@click.argument("run_id")
@json_option
def runs_show(run_id: str, as_json: bool) -> None:
    """One run."""
    app = build(as_json)
    r = app.api.get(f"/runs/{run_id}", auth=_has_session(app))
    app.out.data(r, lambda: app.out.table("", ["field", "value"], [[k, json.dumps(v) if isinstance(v, dict | list) else v]
                                                                   for k, v in r.items()]))


def _has_session(app: App) -> bool:
    return app.api.store.load() is not None


@runs.command("stop")
@click.argument("run_id")
@json_option
def runs_stop(run_id: str, as_json: bool) -> None:
    """Stop a run after its current step."""
    app = build(as_json)
    out = app.api.post(f"/runs/{run_id}/stop")
    app.out.data(out, lambda: app.out.line(f"stop: {out['result']}"))


@runs.command("delete")
@click.argument("run_id")
@click.option("--yes", is_flag=True)
@json_option
def runs_delete(run_id: str, yes: bool, as_json: bool) -> None:
    """Delete a run (artifacts are purged after 7 days)."""
    app = build(as_json)
    if not yes:
        click.confirm(f"Delete run {run_id}?", abort=True, err=True)
    app.api.delete(f"/runs/{run_id}")
    app.out.data({"deleted": run_id}, lambda: app.out.line("deleted"))


@runs.command("patch")
@click.argument("run_id")
@click.option("--title", default=None)
@click.option("--visibility", type=VISIBILITIES, default=None)
@json_option
def runs_patch(run_id: str, title: str | None, visibility: str | None, as_json: bool) -> None:
    """Change a run's title or visibility."""
    app = build(as_json)
    body = {k: v for k, v in (("title", title), ("visibility", visibility)) if v is not None}
    if not body:
        raise click.UsageError("give --title and/or --visibility")
    r = app.api.patch(f"/runs/{run_id}", body)
    app.out.data(r, lambda: app.out.line(f"{r['id']}: {safe(r.get('title'))} [{r['visibility']}]"))


@runs.command("watch")
@click.argument("run_id")
@click.option("--poll", is_flag=True, help="Poll /events instead of opening a WebSocket.")
@click.option("--from-start/--from-now", default=True, help="Replay the log first (default) or only show new events.")
@json_option
def runs_watch(run_id: str, poll: bool, from_start: bool, as_json: bool) -> None:
    """Follow a run live until it ends. Exit 0 if it completed or hit a cap, 20 if it failed, 21 if it was killed."""
    app = build(as_json)
    raise SystemExit(watch_run(app, run_id, poll=poll, from_start=from_start))


@runs.command("events")
@click.argument("run_id")
@click.option("--after-seq", type=click.IntRange(min=0), default=0)
@click.option("--limit", type=click.IntRange(1, 1000), default=1000)
@click.option("--all", "all_pages", is_flag=True)
@json_option
def runs_events(run_id: str, after_seq: int, limit: int, all_pages: bool, as_json: bool) -> None:
    """The run's event log."""
    app = build(as_json)
    events: list[dict[str, Any]] = []
    while True:
        page = app.api.get(f"/runs/{run_id}/events", params={"after_seq": after_seq, "limit": limit}, auth=_has_session(app))
        events += page["events"]
        if not all_pages or not page["events"]:
            break
        after_seq = page["next_after_seq"]
    def human() -> None:
        for e in events:
            app.out.line(f"{e['seq']:>6} step {e['step']:>5} {e['type']:<14} {json.dumps(e['data'], sort_keys=True)[:200]}")

    app.out.data(events, human)


@runs.command("checkpoints")
@click.argument("run_id")
@json_option
def runs_checkpoints(run_id: str, as_json: bool) -> None:
    """Checkpoints you can fork from."""
    app = build(as_json)
    rows = app.api.get(f"/runs/{run_id}/checkpoints", auth=_has_session(app))
    app.out.data(rows, lambda: app.out.table("checkpoints", ["id", "step", "memory version"],
                                             [[c["id"], c["step"], c["memory_version"]] for c in rows]))


@runs.command("score")
@click.argument("run_id")
@json_option
def runs_score(run_id: str, as_json: bool) -> None:
    """The server-computed score."""
    app = build(as_json)
    s = app.api.get(f"/runs/{run_id}/score", auth=_has_session(app))
    app.out.data(s, lambda: app.out.table("", ["field", "value"], [[k, v] for k, v in s.items()]))


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


@runs.command("frame")
@click.argument("run_id")
@click.argument("step", type=click.IntRange(0, 1_000_000))
@click.option("-o", "--output", type=click.Path(dir_okay=False), default=None)
@json_option
def runs_frame(run_id: str, step: int, output: str | None, as_json: bool) -> None:
    """Download the nearest sampled frame at or before STEP."""
    app = build(as_json)
    resp = app.api.get(f"/runs/{run_id}/frames/{step}", auth=_has_session(app), raw=True)
    location = resp.headers.get("location")
    if resp.status_code != 302 or not location:
        raise CliError("internal", "unexpected answer for a frame")
    data = app.api.download(location)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CliError("internal", "the download is not a PNG")
    out = Path(output or f"frame-{resp.headers.get('x-frame-step', step)}.png")
    _write_atomic(out, data)
    app.out.data({"file": str(out), "bytes": len(data), "frame_step": resp.headers.get("x-frame-step")},
                 lambda: app.out.line(f"wrote {out} ({len(data)} bytes)"))


@runs.command("export")
@click.argument("run_id")
@click.option("-o", "--output", type=click.Path(dir_okay=False), default=None)
@json_option
def runs_export(run_id: str, output: str | None, as_json: bool) -> None:
    """Download the run bundle and verify its sha256 before keeping it."""
    app = build(as_json)
    info = app.api.get(f"/runs/{run_id}/export", auth=_has_session(app))
    data = app.api.download(info["url"])
    digest = hashlib.sha256(data).hexdigest()
    if digest != info["sha256"] or not zipfile.is_zipfile(io.BytesIO(data)):
        raise CliError("sha_mismatch", "the downloaded bundle does not match its sha256: it was discarded")
    out = Path(output or f"run-{run_id}.zip")
    _write_atomic(out, data)
    app.out.data({"file": str(out), "bytes": len(data), "sha256": digest, "verified": True},
                 lambda: app.out.line(f"wrote {out} ({len(data)} bytes), sha256 {digest} verified"))


@runs.group("memory")
def memory() -> None:
    """A run's memory files."""


@memory.command("ls")
@click.argument("run_id")
@click.option("--at-step", type=click.IntRange(min=0), default=None)
@json_option
def memory_ls(run_id: str, at_step: int | None, as_json: bool) -> None:
    """The memory tree (optionally as of a step)."""
    app = build(as_json)
    rows = app.api.get(f"/runs/{run_id}/memory", params={} if at_step is None else {"at_step": at_step}, auth=_has_session(app))
    app.out.data(rows, lambda: app.out.table("memory", ["path", "chars", "version"], [[r["path"], r["chars"], r["version"]] for r in rows]))


@memory.command("cat")
@click.argument("run_id")
@click.argument("path")
@click.option("--version", type=click.IntRange(min=1), default=None)
def memory_cat(run_id: str, path: str, version: int | None) -> None:
    """Print a memory file (control characters are escaped)."""
    app = build(False)
    resp = app.api.get(f"/runs/{run_id}/memory/file", params={"path": path, **({} if version is None else {"version": version})},
                       auth=_has_session(app), raw=True)
    app.out.console.print(safe(resp.text), end="")


@memory.command("diff")
@click.argument("run_id")
@click.argument("path")
@click.option("--from", "from_", type=click.IntRange(min=0), required=True)
@click.option("--to", type=click.IntRange(min=1), required=True)
def memory_diff(run_id: str, path: str, from_: int, to: int) -> None:
    """A unified diff between two versions of a memory file."""
    app = build(False)
    resp = app.api.get(f"/runs/{run_id}/memory/diff", params={"path": path, "from": from_, "to": to},
                       auth=_has_session(app), raw=True)
    app.out.console.print(safe(resp.text), end="")


@click.command("fork")
@click.argument("run_id")
@click.option("--checkpoint", "checkpoint_id", required=True, help="A checkpoint id of that run (see `runs checkpoints`).")
@click.option("--set", "sets", multiple=True, metavar="FIELD=JSON", help="Change a config field, e.g. --set preset_ref='\"none@1\"'.")
@click.option("--max-steps", type=click.IntRange(1, 100_000), required=True)
@click.option("--max-tokens", type=click.IntRange(1, 1_000_000_000), required=True)
@click.option("--max-wall-s", type=click.IntRange(1, 86_400), required=True)
@click.option("--visibility", type=VISIBILITIES, default="private")
@click.option("--preset-text-file", type=click.Path(exists=True, dir_okay=False), default=None)
@_key_options
@json_option
def fork(run_id: str, checkpoint_id: str, sets: tuple[str, ...], max_steps: int, max_tokens: int, max_wall_s: int,
         visibility: str, preset_text_file: str | None, api_key_stdin: bool, as_json: bool) -> None:
    """Fork a run from a checkpoint with a changed config (the server may have forking switched off)."""
    app = build(as_json)
    patch: dict[str, Any] = {}
    for item in sets:
        name, sep, raw = item.partition("=")
        if not sep or not name:
            raise click.UsageError(f"--set wants FIELD=JSON, got {item!r}")
        try:
            patch[name] = json.loads(raw)
        except ValueError:
            patch[name] = raw
    body: dict[str, Any] = {"checkpoint_id": checkpoint_id, "config_patch": patch, "visibility": visibility,
                            "caps": {"max_steps": max_steps, "max_tokens": max_tokens, "max_wall_s": max_wall_s}}
    if preset_text_file:
        body["preset_text"] = Path(preset_text_file).read_text()
    key = read_secret("provider API key", from_stdin=api_key_stdin)
    created = app.api.post(f"/runs/{run_id}/fork", _send_key(body, key))
    del key
    app.out.data(created, lambda: app.out.line(f"run {created['run_id']} {created['status']}"))
