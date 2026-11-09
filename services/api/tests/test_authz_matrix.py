"""Every run-scoped route x {anon, owner, other user, admin} x {private, unlisted, public}."""

import uuid
from typing import Any

import pytest

from conftest import Env, bearer

VIS = ["private", "unlisted", "public"]
WHO = ["anon", "owner", "other", "admin"]


def seed(env: Env, visibility: str) -> dict[str, str]:
    owner, other, admin = env.user(), env.user(), env.user("admin")
    run = env.run(owner, visibility)
    env.admin.execute("insert into public.run_scores values (%s, 1, 1, 1, 1, 1, 0, 0, false)", (run,))
    env.admin.execute("insert into public.memory_files values (%s, 'a.md', 1, 0, 'one\n', 4, false)", (run,))
    env.admin.execute("insert into public.memory_files values (%s, 'a.md', 2, 1, 'one\ntwo\n', 8, false)", (run,))
    env.admin.execute("insert into public.checkpoints (run_id, step, savestate_key, memory_version, transcript_offset, "
                      "config_hash) values (%s, 0, 'k', 1, 0, %s)", (run, "b" * 64))
    return {"run": run, "owner": owner, "other": other, "admin": admin}


def hdr(ids: dict[str, str], who: str) -> dict[str, str]:
    return {} if who == "anon" else bearer(ids[who])


def can_see(who: str, visibility: str) -> bool:
    return visibility != "private" or who in ("owner", "admin")


READS = [
    "/api/v1/runs/{run}", "/api/v1/runs/{run}/score", "/api/v1/runs/{run}/checkpoints",
    "/api/v1/runs/{run}/memory", "/api/v1/runs/{run}/memory/file?path=a.md",
    "/api/v1/runs/{run}/memory/diff?path=a.md&from=1&to=2", "/api/v1/runs/{run}/events",
]


@pytest.mark.parametrize("visibility", VIS)
@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("route", READS)
def test_read_routes_404_never_403_for_hidden_runs(env: Env, route: str, who: str, visibility: str) -> None:
    ids = seed(env, visibility)
    r = env.client.get(route.format(run=ids["run"]), headers=hdr(ids, who))
    if can_see(who, visibility):
        assert r.status_code == 200, (route, who, visibility, r.text)
    else:
        assert r.status_code == 404 and r.json()["code"] == "not_found", (route, who, visibility)


@pytest.mark.parametrize("visibility", VIS)
@pytest.mark.parametrize("who", WHO)
def test_hidden_and_missing_runs_are_indistinguishable(env: Env, who: str, visibility: str) -> None:
    ids = seed(env, visibility)
    hidden = env.client.get(f"/api/v1/runs/{ids['run']}", headers=hdr(ids, who))
    missing = env.client.get("/api/v1/runs/6f1a2b3c-0000-4000-8000-000000000000", headers=hdr(ids, who))
    if not can_see(who, visibility):
        assert (hidden.status_code, hidden.json(), dict(hidden.headers).keys() - {"content-length"}) == \
               (missing.status_code, missing.json(), dict(missing.headers).keys() - {"content-length"})


MUTATIONS: list[tuple[str, str, dict[str, Any] | None, set[str]]] = [
    # method, path, json body, who may succeed (others 404)
    ("PATCH", "/api/v1/runs/{run}", {"title": "mine"}, {"owner"}),
    ("DELETE", "/api/v1/runs/{run}", None, {"owner"}),
    ("POST", "/api/v1/runs/{run}/stop", None, {"owner", "admin"}),
]


@pytest.mark.parametrize("visibility", VIS)
@pytest.mark.parametrize("who", ["owner", "other", "admin"])
@pytest.mark.parametrize(("method", "path", "body", "allowed"), MUTATIONS)
def test_mutations_are_owner_only(env: Env, method: str, path: str, body: Any, allowed: set[str], who: str,
                                  visibility: str) -> None:
    ids = seed(env, visibility)
    r = env.client.request(method, path.format(run=ids["run"]), json=body, headers=hdr(ids, who))
    if who in allowed and can_see(who, visibility):
        assert r.status_code in (200, 204), (method, who, visibility, r.text)
    else:
        assert r.status_code == 404, (method, who, visibility, r.status_code)


@pytest.mark.parametrize("method", ["PATCH", "DELETE", "POST"])
def test_mutations_without_a_token_are_401(env: Env, method: str) -> None:
    ids = seed(env, "public")
    path = f"/api/v1/runs/{ids['run']}" + ("/stop" if method == "POST" else "")
    assert env.client.request(method, path, json={"title": "x"} if method == "PATCH" else None).status_code == 401


def test_a_non_owner_patch_changes_nothing(env: Env) -> None:
    ids = seed(env, "public")
    env.client.patch(f"/api/v1/runs/{ids['run']}", json={"title": "pwned", "visibility": "private"},
                     headers=hdr(ids, "other"))
    row = env.admin.execute("select title, visibility from public.runs where id = %s", (ids["run"],)).fetchone()
    assert row == {"title": None, "visibility": "public"}


def test_patch_rejects_fields_users_may_not_set(env: Env) -> None:
    ids = seed(env, "private")
    for body in ({"status": "completed"}, {"owner_id": ids["other"]}, {"steps": 99}, {"title": 5}, {}):
        r = env.client.patch(f"/api/v1/runs/{ids['run']}", json=body, headers=hdr(ids, "owner"))
        assert r.status_code == 422 and r.json()["code"] == "validation_failed", body


def test_delete_is_soft_and_hides_the_run(env: Env) -> None:
    ids = seed(env, "public")
    assert env.client.delete(f"/api/v1/runs/{ids['run']}", headers=hdr(ids, "owner")).status_code == 204
    assert env.client.get(f"/api/v1/runs/{ids['run']}", headers=hdr(ids, "owner")).status_code == 404
    assert env.client.get(f"/api/v1/runs/{ids['run']}").status_code == 404
    assert env.admin.execute("select deleted_at from public.runs where id = %s", (ids["run"],)).fetchone()["deleted_at"]  # type: ignore[index]


def test_memory_file_is_plain_text_nosniff_and_the_diff_is_unified(env: Env) -> None:
    ids = seed(env, "public")
    env.admin.execute("insert into public.memory_files values (%s, 'x.md', 1, 0, %s, 20, false)",
                      (ids["run"], "<script>alert(1)</script>"))
    r = env.client.get(f"/api/v1/runs/{ids['run']}/memory/file", params={"path": "x.md"})
    assert r.headers["content-type"].startswith("text/plain") and r.headers["x-content-type-options"] == "nosniff"
    assert r.text == "<script>alert(1)</script>"
    d = env.client.get(f"/api/v1/runs/{ids['run']}/memory/diff", params={"path": "a.md", "from": 1, "to": 2})
    assert "+two" in d.text and d.headers["content-type"].startswith("text/plain")


def test_memory_tree_at_step_reports_the_version_current_then(env: Env) -> None:
    ids = seed(env, "public")
    at0 = env.client.get(f"/api/v1/runs/{ids['run']}/memory", params={"at_step": 0}).json()
    now = env.client.get(f"/api/v1/runs/{ids['run']}/memory").json()
    assert (at0[0]["version"], now[0]["version"]) == (1, 2)


def test_report_requires_a_visible_run(env: Env) -> None:
    ids = seed(env, "private")
    assert env.client.post(f"/api/v1/runs/{ids['run']}/report", json={"reason": "x"},
                           headers=hdr(ids, "other")).status_code == 404
    pub = seed(env, "public")
    assert env.client.post(f"/api/v1/runs/{pub['run']}/report", json={"reason": "spam"},
                           headers=hdr(pub, "other")).status_code == 202


def test_events_validates_limits(env: Env) -> None:
    ids = seed(env, "public")
    assert env.client.get(f"/api/v1/runs/{ids['run']}/events?limit=1001").status_code == 422
    assert env.client.get(f"/api/v1/runs/{ids['run']}/events?after_seq=-1").status_code == 422


def test_runs_list_is_only_the_callers_and_paginates(env: Env) -> None:
    a, b = env.user(), env.user()
    mine = [env.run(a) for _ in range(3)]
    env.run(b, "public")
    first = env.client.get("/api/v1/runs?limit=2", headers=bearer(a)).json()
    assert len(first["runs"]) == 2 and first["next"]
    second = env.client.get("/api/v1/runs", params={"limit": 2, "before": first["next"]}, headers=bearer(a)).json()
    got = [r["id"] for r in first["runs"] + second["runs"]]
    assert sorted(got) == sorted(mine) and second["next"] is None
    assert env.client.get("/api/v1/runs?before=garbage", headers=bearer(a)).status_code == 422


def test_leaderboard_lists_only_verified_public_runs(env: Env) -> None:
    a = env.user()
    ok, unverified, private = env.run(a, "public"), env.run(a, "public"), env.run(a, "private")
    for run, verified in ((ok, True), (unverified, False), (private, True)):
        env.admin.execute("insert into public.run_scores values (%s, 5, 1, 1, 1, 1, 0, 0, %s)", (run, verified))
    ids = {r["id"] for r in env.client.get("/api/v1/leaderboard").json()}
    assert ok in ids and unverified not in ids and private not in ids


def test_catalogue_shows_only_public_games_and_presets_carry_labels(env: Env) -> None:
    pub, hidden = env.game(public=True), env.game(public=False)
    names = {g["name"] for g in env.client.get("/api/v1/games").json()}
    assert pub in names and hidden not in names
    presets = env.client.get("/api/v1/presets").json()
    assert presets and all({"text", "text_sha256", "label", "is_adaptation"} <= p.keys() for p in presets)
    assert "anthropic" in env.client.get("/api/v1/models").json()["providers"]


@pytest.mark.parametrize("visibility", VIS)
@pytest.mark.parametrize("who", WHO)
def test_run_config_follows_run_visibility(env: Env, who: str, visibility: str) -> None:
    ids = seed(env, visibility)
    r = env.client.get(f"/api/v1/runs/{ids['run']}/config", headers=hdr(ids, who))
    if can_see(who, visibility):
        assert r.status_code == 200 and set(r.json()) == {"config", "config_hash", "mode"}
    else:
        missing = env.client.get(f"/api/v1/runs/{uuid.uuid4()}/config", headers=hdr(ids, who))
        assert (r.status_code, r.json()) == (missing.status_code, missing.json()) == (404, missing.json())


def test_features_reports_what_is_switched_on(make_env: Any) -> None:
    assert make_env(fork_enabled=False).client.get("/api/v1/features").json() == {"fork": False}
    assert make_env(fork_enabled=True).client.get("/api/v1/features").json() == {"fork": True}
