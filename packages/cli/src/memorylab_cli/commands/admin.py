"""admin runs | kill | breaker | reports | resolve | unverify (the API answers 404 to anyone who is not an admin)."""

import click

from ..app import build, json_option


@click.group()
def admin() -> None:
    """Moderation and operations (admins only)."""


@admin.command("runs")
@json_option
def admin_runs(as_json: bool) -> None:
    """Queued and active runs."""
    app = build(as_json)
    snap = app.api.get("/admin/runs")
    app.out.data(snap, lambda: app.out.table("", ["queued", "active", "breaker"],
                                             [[len(snap.get("queued", [])), len(snap.get("active", [])),
                                               "OPEN: " + str(snap["breaker"].get("reason")) if snap.get("breaker", {}).get("open") else "closed"]]))


@admin.command("kill")
@click.argument("run_id")
@click.option("--reason", required=True)
@json_option
def admin_kill(run_id: str, reason: str, as_json: bool) -> None:
    """Kill a run now."""
    app = build(as_json)
    out = app.api.post(f"/admin/runs/{run_id}/kill", {"reason": reason})
    app.out.data(out, lambda: app.out.line(f"kill: {out['result']}"))


@admin.command("breaker")
@click.option("--open/--close", "open_", required=True)
@click.option("--reason", required=True)
@json_option
def admin_breaker(open_: bool, reason: str, as_json: bool) -> None:
    """Open or close the global stop-new-runs switch."""
    app = build(as_json)
    out = app.api.post("/admin/breaker", {"open": open_, "reason": reason})
    app.out.data(out, lambda: app.out.line("breaker " + ("open" if out.get("open") else "closed")))


@admin.command("reports")
@click.option("--status", type=click.Choice(["open", "actioned", "dismissed", "all"]), default="open")
@click.option("--limit", type=click.IntRange(1, 100), default=50)
@json_option
def admin_reports(status: str, limit: int, as_json: bool) -> None:
    """The moderation queue."""
    app = build(as_json)
    page = app.api.get("/admin/reports", params={"status": status, "limit": limit})
    app.out.data(page, lambda: app.out.table("reports", ["id", "run", "status", "reason"],
                                             [[r["id"], r["run_id"], r["status"], r["reason"]] for r in page["reports"]]))


@admin.command("resolve")
@click.argument("report_id")
@click.option("--status", "new_status", type=click.Choice(["actioned", "dismissed"]), required=True)
@click.option("--reason", required=True)
@json_option
def admin_resolve(report_id: str, new_status: str, reason: str, as_json: bool) -> None:
    """Close an open report."""
    app = build(as_json)
    out = app.api.patch(f"/admin/reports/{report_id}", {"status": new_status, "reason": reason})
    app.out.data(out, lambda: app.out.line(f"{out['id']}: {out['status']}"))


@admin.command("unverify")
@click.argument("run_id")
@click.option("--reason", required=True)
@json_option
def admin_unverify(run_id: str, reason: str, as_json: bool) -> None:
    """Take a run off the leaderboard."""
    app = build(as_json)
    out = app.api.post(f"/admin/leaderboard/{run_id}/unverify", {"reason": reason})
    app.out.data(out, lambda: app.out.line(f"{out['run_id']}: {out['status']}"))
