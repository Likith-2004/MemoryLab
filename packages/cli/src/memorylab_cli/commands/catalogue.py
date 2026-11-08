"""games, presets, models, leaderboard (public reads)."""

import click

from ..app import build, json_option


@click.command()
@json_option
def games(as_json: bool) -> None:
    """Games you can run."""
    app = build(as_json)
    rows = app.api.get("/games", auth=False)
    app.out.data(rows, lambda: app.out.table("games", ["name", "licence", "state map"],
                                             [[g["name"], g["licence"], g["state_map_version"]] for g in rows]))


@click.command()
@json_option
def presets(as_json: bool) -> None:
    """Memory presets (the instruction text a run starts with)."""
    app = build(as_json)
    rows = app.api.get("/presets", auth=False)
    app.out.data(rows, lambda: app.out.table("presets", ["ref", "label", "adaptation"],
                                             [[p["ref"], p["label"], "yes" if p["is_adaptation"] else "no"] for p in rows]))


@click.command()
@json_option
def models(as_json: bool) -> None:
    """Providers and models."""
    app = build(as_json)
    data = app.api.get("/models", auth=False)
    app.out.data(data, lambda: app.out.table("models", ["provider", "models"],
                                             [[p, ", ".join(m)] for p, m in data["providers"].items()]))


@click.command()
@click.option("--limit", type=click.IntRange(1, 100), default=25, show_default=True)
@json_option
def leaderboard(limit: int, as_json: bool) -> None:
    """Verified benchmark runs."""
    app = build(as_json)
    rows = app.api.get("/leaderboard", params={"limit": limit}, auth=False)
    app.out.data(rows, lambda: app.out.table("leaderboard", ["run", "model", "score", "steps"],
                                             [[r["id"], r["model_id"], r["progress_score"], r["steps"]] for r in rows]))
