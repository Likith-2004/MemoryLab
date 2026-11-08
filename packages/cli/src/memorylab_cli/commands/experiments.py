"""experiments new | list | show | compare | add | remove."""

from typing import Any

import click

from ..app import build, json_option
from ..render import Out


@click.group()
def experiments() -> None:
    """Group your runs that differ in one declared variable."""


@experiments.command("new")
@click.option("--title", required=True)
@click.option("--variable", required=True, help="The config field the runs differ in, e.g. preset_ref.")
@click.option("--run", "run_ids", multiple=True, required=True, help="A run id (repeat).")
@click.option("--visibility", type=click.Choice(["private", "unlisted", "public"]), default="private")
@json_option
def exp_new(title: str, variable: str, run_ids: tuple[str, ...], visibility: str, as_json: bool) -> None:
    """Create an experiment from your own runs."""
    app = build(as_json)
    out = app.api.post("/experiments", {"title": title, "variable": variable, "run_ids": list(run_ids), "visibility": visibility})
    app.out.data(out, lambda: app.out.line(f"experiment {out['id']}"))


@experiments.command("list")
@json_option
def exp_list(as_json: bool) -> None:
    """Your experiments."""
    app = build(as_json)
    rows = app.api.get("/experiments")
    app.out.data(rows, lambda: app.out.table("experiments", ["id", "title", "variable", "visibility", "runs"],
                                             [[r["id"], r["title"], r["variable"], r["visibility"], r["n_runs"]] for r in rows]))


def _show(out: Out, e: dict[str, Any]) -> None:
    s = e["summary"]
    out.line(f"{e['title']}  variable: {e['variable']}  [{e['visibility']}]")
    out.table("runs", ["run", "status", "model", "steps", "score"],
                  [[r["run_id"], r["status"], r.get("model_id"), r["steps"], r.get("progress_score")] for r in e["runs"]])
    spread = "-" if s["min"] is None else f"{s['min']:g}..{s['max']:g}"
    out.line(f"n={s['n']}  mean={'-' if s['mean'] is None else format(s['mean'], 'g')}  spread={spread}")
    for w in s["warnings"]:
        out.line({"n_below_5": "warning: fewer than 5 scored runs: this is not a finding",
                      "runs_still_running": "warning: some runs are still running"}.get(w, w))


@experiments.command("show")
@click.argument("experiment_id")
@json_option
def exp_show(experiment_id: str, as_json: bool) -> None:
    """An experiment with its summary (you see only the runs you may see)."""
    app = build(as_json)
    e = app.api.get(f"/experiments/{experiment_id}", auth=app.api.store.load() is not None)
    app.out.data(e, lambda: _show(app.out, e))


@experiments.command("compare")
@click.argument("experiment_id")
@click.option("--metric", type=click.Choice(["score", "tokens"]), default="score")
@json_option
def exp_compare(experiment_id: str, metric: str, as_json: bool) -> None:
    """Per-step series for every visible run, aligned on one step axis."""
    app = build(as_json)
    c = app.api.get(f"/experiments/{experiment_id}/compare", params={"metric": metric},
                    auth=app.api.store.load() is not None)

    def human() -> None:
        steps = c["steps"]
        pick = steps[:: max(1, len(steps) // 12)] if steps else []
        idx = [steps.index(s) for s in pick]
        app.out.table(f"{metric} by step", ["run", *[str(s) for s in pick]],
                      [[s["run_id"][:8], *["-" if s["values"][i] is None else f"{s['values'][i]:g}" for i in idx]]
                       for s in c["series"]])

    app.out.data(c, human)


def _edit(experiment_id: str, add: tuple[str, ...], remove: tuple[str, ...], as_json: bool) -> None:
    app = build(as_json)
    e = app.api.patch(f"/experiments/{experiment_id}", {"add_run_ids": list(add), "remove_run_ids": list(remove)})
    app.out.data(e, lambda: _show(app.out, e))


@experiments.command("add")
@click.argument("experiment_id")
@click.argument("run_ids", nargs=-1, required=True)
@json_option
def exp_add(experiment_id: str, run_ids: tuple[str, ...], as_json: bool) -> None:
    """Add your runs to an experiment."""
    _edit(experiment_id, run_ids, (), as_json)


@experiments.command("remove")
@click.argument("experiment_id")
@click.argument("run_ids", nargs=-1, required=True)
@json_option
def exp_remove(experiment_id: str, run_ids: tuple[str, ...], as_json: bool) -> None:
    """Remove runs from an experiment."""
    _edit(experiment_id, (), run_ids, as_json)
