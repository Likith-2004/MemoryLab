"""POST /runs/{id}/fork: visibility of the source, checkpoint ownership (IDOR), benchmark one-field rule, the shared
create quota, and that the server (not the body) decides the parent."""

import json
import uuid
from typing import Any

import pytest

from api.orchestrator_client import UpstreamError
from conftest import CANARY_KEY, Env, bearer

CAPS = {"max_steps": 50, "max_tokens": 100000, "max_wall_s": 600}
CONFIG = {"provider": "anthropic", "model_id": "claude-haiku-5-5", "preset_ref": "general@1", "mode": "demo"}


def source(env: Env, owner: str, game: str, visibility: str = "private", *, mode: str = "demo",
           cfg: dict[str, Any] | None = None, caps: dict[str, Any] | None = None, status: str = "completed",
           with_checkpoint: bool = True) -> tuple[str, str | None]:
    doc = {**CONFIG, "mode": mode, **(cfg or {}), "caps": caps or CAPS}
    cid = env.admin.execute("insert into public.run_configs (owner_id, config_json, config_hash, mode) values "
                            "(%s, %s::jsonb, %s, %s) returning id", (owner, json.dumps(doc), "a" * 64, mode)).fetchone()["id"]  # type: ignore[index]
    run = str(env.admin.execute(
        "insert into public.runs (owner_id, config_id, visibility, game, status, cap_steps, cap_tokens, cap_wall_s) "
        "values (%s, %s, %s, %s, %s, 50, 100000, 600) returning id", (owner, cid, visibility, game, status)).fetchone()["id"])  # type: ignore[index]
    cp = None
    if with_checkpoint:
        cp = str(env.admin.execute(
            "insert into public.checkpoints (run_id, step, savestate_key, memory_version, transcript_offset, config_hash) "
            "values (%s, 7, 'k', 1, 0, %s) returning id", (run, "b" * 64)).fetchone()["id"])  # type: ignore[index]
    return run, cp


def fork_body(cp: str | None, patch: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    b: dict[str, Any] = {"checkpoint_id": cp, "config_patch": {"preset_ref": "none@1"} if patch is None else patch,
                         "caps": CAPS, "api_key": CANARY_KEY}
    b.update(over)
    return b


def fork(env: Env, uid: str | None, run: str, payload: dict[str, Any]) -> Any:
    return env.client.post(f"/api/v1/runs/{run}/fork", json=payload, headers=bearer(uid) if uid else {})


def test_a_fork_is_a_new_run_with_the_server_chosen_parent_and_the_stored_diff(env: Env) -> None:
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game)
    r = fork(env, owner, run, fork_body(cp))
    assert r.status_code == 202 and r.json()["status"] == "queued"
    new = r.json()["run_id"]
    row = env.admin.execute("select owner_id, status, visibility, game, parent_checkpoint_id, fork_patch, cap_steps "
                            "from public.runs where id = %s", (new,)).fetchone()
    assert (str(row["owner_id"]), row["status"], row["visibility"], row["game"]) == (owner, "queued", "private", game)  # type: ignore[index]
    assert (str(row["parent_checkpoint_id"]), row["fork_patch"], row["cap_steps"]) == (cp, {"preset_ref": "none@1"}, 50)  # type: ignore[index]
    cfg = env.admin.execute("select c.config_json from public.runs r join public.run_configs c on c.id = r.config_id "
                            "where r.id = %s", (new,)).fetchone()["config_json"]  # type: ignore[index]
    assert cfg["preset_ref"] == "none@1" and cfg["model_id"] == "claude-haiku-5-5"      # everything else is the parent's
    (sub,) = env.orch.submitted
    assert sub["run_id"] == new and sub["game"] == game and sub["parent"] == {"run_id": run, "checkpoint_id": cp, "step": 7}
    assert sub["provider"]["api_key"] == CANARY_KEY and sub["run_config"]["preset_ref"] == "none@1"
    shown = env.client.get(f"/api/v1/runs/{new}", headers=bearer(owner)).json()
    assert shown["parent_checkpoint_id"] == cp and shown["fork_patch"] == {"preset_ref": "none@1"} and shown["game"] == game


def test_the_parent_checkpoint_cannot_be_set_through_the_body(env: Env) -> None:
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game)
    for extra in ({"parent_checkpoint_id": str(uuid.uuid4())}, {"owner_id": owner}, {"game": "other"}):
        assert fork(env, owner, run, fork_body(cp, **extra)).status_code == 422
    assert env.orch.submitted == []


@pytest.mark.parametrize("visibility,can", [("private", False), ("unlisted", True), ("public", True)])
def test_anyone_who_can_see_the_run_can_fork_it_into_their_own_run(env: Env, visibility: str, can: bool) -> None:
    owner, other, game = env.user(), env.user(), env.game()
    run, cp = source(env, owner, game, visibility)
    r = fork(env, other, run, fork_body(cp))
    assert r.status_code == (202 if can else 404), r.text
    if can:
        new = env.admin.execute("select owner_id from public.runs where id = %s", (r.json()["run_id"],)).fetchone()
        assert str(new["owner_id"]) == other                                               # type: ignore[index]
    else:
        missing = fork(env, other, str(uuid.uuid4()), fork_body(cp))
        assert (r.status_code, r.json()) == (missing.status_code, missing.json())          # no existence oracle


def test_anonymous_callers_get_401_and_nothing_is_created(env: Env) -> None:
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game, "public")
    assert fork(env, None, run, fork_body(cp)).status_code == 401
    assert env.orch.submitted == [] and env.keys.calls == []


def test_a_deleted_source_run_cannot_be_forked(env: Env) -> None:
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game, "public")
    env.admin.execute("update public.runs set deleted_at = now() where id = %s", (run,))
    assert fork(env, owner, run, fork_body(cp)).status_code == 404


def test_a_checkpoint_of_another_run_is_a_404_identical_to_a_missing_one(env: Env) -> None:
    """IDOR: the checkpoint id is a capability only for its own run."""
    victim, attacker, game = env.user(), env.user(), env.game()
    secret_run, secret_cp = source(env, victim, game, "private")
    mine, my_cp = source(env, attacker, game, "private")
    public_run, public_cp = source(env, victim, game, "public")
    missing = fork(env, attacker, mine, fork_body(str(uuid.uuid4())))
    assert missing.status_code == 404
    for foreign in (secret_cp, public_cp):                      # another user's private run, and a run I can see
        r = fork(env, attacker, mine, fork_body(foreign))
        assert (r.status_code, r.json(), set(r.headers) - {"content-length"}) == (
            missing.status_code, missing.json(), set(missing.headers) - {"content-length"})
    r = fork(env, attacker, public_run, fork_body(secret_cp))   # a visible run with a hidden run's checkpoint
    assert (r.status_code, r.json()) == (missing.status_code, missing.json())
    assert fork(env, attacker, mine, fork_body("not-a-uuid")).json() == missing.json()
    assert fork(env, attacker, mine, fork_body(my_cp)).status_code == 202                 # their own is fine
    assert secret_run and env.orch.submitted[0]["parent"]["checkpoint_id"] == my_cp


def test_a_new_key_is_required_and_is_checked_like_a_create(env: Env) -> None:
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game)
    b = fork_body(cp)
    del b["api_key"]
    assert fork(env, owner, run, b).status_code == 422
    assert fork(env, owner, run, fork_body(cp, api_key="short")).status_code == 422
    assert env.keys.calls == [] and env.orch.submitted == []
    r = fork(env, owner, run, fork_body(cp, api_key="sk-ant-wrong-key-123456"))
    assert (r.status_code, r.json()["code"]) == (422, "key_rejected")
    assert CANARY_KEY not in r.text


def test_forks_share_the_create_quota_with_new_runs(make_env: Any) -> None:
    env: Env = make_env(run_create_per_user_per_hour=3, run_create_per_ip_per_hour=100)
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game)
    from test_runs_create import body
    codes = []
    for kind in ("create", "fork", "fork", "create"):
        if kind == "create":
            codes.append(env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(owner)).status_code)
        else:
            codes.append(fork(env, owner, run, fork_body(cp)).status_code)
        env.admin.execute("update public.runs set status = 'completed' where owner_id = %s", (owner,))
    assert codes == [202, 202, 202, 429]                                     # 3 per hour across both routes


def test_forks_share_the_key_failure_lockout_with_new_runs(make_env: Any) -> None:
    env: Env = make_env(run_create_per_user_per_hour=100, run_create_per_ip_per_hour=100)
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game)
    from test_runs_create import body
    for _ in range(5):
        r = fork(env, owner, run, fork_body(cp, api_key="sk-ant-wrong-key-123456"))
        assert r.status_code == 422
    r = fork(env, owner, run, fork_body(cp))
    assert r.status_code == 429 and r.json()["title"] == "Run creation is locked for an hour"
    r = env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(owner))        # and a plain create too
    assert r.status_code == 429 and r.json()["title"] == "Run creation is locked for an hour"


def test_a_user_with_an_active_run_cannot_fork(env: Env) -> None:
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game)
    env.run(owner, status="running")
    r = fork(env, owner, run, fork_body(cp))
    assert (r.status_code, r.json()["code"]) == (409, "rate_limited") and env.keys.calls == []


def test_a_refused_fork_submit_fails_the_row_and_refunds(make_env: Any) -> None:
    env: Env = make_env(run_create_per_user_per_hour=1)
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game)
    env.orch.fail_with = UpstreamError(503, "capacity_full", 30)
    assert fork(env, owner, run, fork_body(cp)).status_code == 503
    assert env.admin.execute("select status, end_reason from public.runs where owner_id = %s and id <> %s",
                             (owner, run)).fetchone() == {"status": "failed", "end_reason": "submit_refused"}
    env.orch.fail_with = None
    assert fork(env, owner, run, fork_body(cp)).status_code == 202


# ---------------------------------------------------------------- benchmark: one field only

def bench(env: Env, owner: str, game: str) -> tuple[str, str | None]:
    return source(env, owner, game, "public", mode="benchmark")


@pytest.mark.parametrize("patch,caps,code", [
    ({"preset_ref": "none@1"}, CAPS, 202),                                       # one field
    ({"preset_ref": "none@1", "model_id": "claude-opus-5-5"}, CAPS, "benchmark_patch_invalid"),
    ({"preset_ref": "none@1"}, {**CAPS, "max_steps": 60}, "benchmark_patch_invalid"),   # a field and the caps
    ({"mode": "demo", "preset_ref": "none@1"}, CAPS, "benchmark_patch_invalid"),         # leaving benchmark counts too
    ({}, {**CAPS, "max_steps": 60}, 202),                                        # caps alone count as one field
    ({"preset_ref": "general@1", "model_id": "claude-opus-5-5"}, CAPS, 202),     # one real change, one no-op
    ({"observation": {"hide_extras": ["input_frames", "frame_counter"]},
      "screenshots": {"mode": "every_k", "k": 2, "frame_scale": 1}}, CAPS, "benchmark_patch_invalid"),
    ({"screenshots": {"mode": "every_k", "k": 2, "frame_scale": 1}}, CAPS, 202),                # one nested field
])
def test_benchmark_forks_may_change_one_field_only(env: Env, patch: dict[str, Any], caps: dict[str, int],
                                                    code: Any) -> None:
    owner, game = env.user(), env.game()
    run, cp = bench(env, owner, game)
    r = fork(env, owner, run, fork_body(cp, patch, caps=caps))
    if code == 202:
        assert r.status_code == 202, r.text
    else:
        assert (r.status_code, r.json()["code"]) == (422, code)
        assert env.orch.submitted == [] and env.keys.calls == []                  # refused before any key call or quota


def test_demo_forks_may_change_several_fields_but_a_benchmark_switch_is_judged_on_the_result(env: Env) -> None:
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game)
    r = fork(env, owner, run, fork_body(cp, {"preset_ref": "none@1", "model_id": "claude-opus-5-5"}))
    assert r.status_code == 202
    env.admin.execute("update public.runs set status = 'completed' where owner_id = %s", (owner,))
    r = fork(env, owner, run, fork_body(cp, {"mode": "benchmark", "preset_ref": "none@1"}))   # a demo -> benchmark fork
    assert (r.status_code, r.json()["code"]) == (422, "benchmark_patch_invalid")
    assert fork(env, owner, run, fork_body(cp, {"mode": "benchmark"})).status_code == 202


# ---------------------------------------------------------------- patch validation

@pytest.mark.parametrize("patch", [
    {"caps": {"max_steps": 1}},                               # caps go in `caps`
    {"base_url": "http://evil"},                              # unknown field
    {"model_id": "nope"},
    {"preset_ref": "evil@9"},
    {"provider": "openai"},
    {"model_id": "a\x00b"},
    {"preset_ref": "custom@1"},                               # custom needs its text
])
def test_a_bad_patch_is_a_422_before_anything_is_used(env: Env, patch: dict[str, Any]) -> None:
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game)
    r = fork(env, owner, run, fork_body(cp, patch))
    assert r.status_code == 422, r.text
    assert env.keys.calls == [] and env.orch.submitted == []
    assert env.admin.execute("select count(*) n from public.runs where owner_id = %s", (owner,)).fetchone()["n"] == 1  # type: ignore[index]


def test_caps_are_required_and_bounded(env: Env) -> None:
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game)
    b = fork_body(cp)
    del b["caps"]
    r = fork(env, owner, run, b)
    assert (r.status_code, r.json()["code"]) == (422, "caps_required")
    for bad in ({"max_steps": 100_001, "max_tokens": 1, "max_wall_s": 1}, {"max_steps": 1, "max_tokens": 1, "max_wall_s": 0.5}):
        assert fork(env, owner, run, fork_body(cp, caps=bad)).status_code == 422


def test_visibility_is_the_callers_choice_and_defaults_to_private(env: Env) -> None:
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game, "public")
    new = fork(env, owner, run, fork_body(cp, visibility="unlisted")).json()["run_id"]
    assert env.admin.execute("select visibility from public.runs where id = %s", (new,)).fetchone()["visibility"] == "unlisted"  # type: ignore[index]


def test_a_run_without_a_recorded_game_cannot_be_forked(env: Env) -> None:
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game)
    env.admin.execute("update public.runs set game = null where id = %s", (run,))
    assert fork(env, owner, run, fork_body(cp)).status_code == 422


def test_a_custom_preset_source_needs_its_text_again(env: Env) -> None:
    from labharness.presets import custom_preset
    text = "Write short notes about what failed."
    ref = custom_preset(text).ref
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game, cfg={"preset_ref": ref})
    assert fork(env, owner, run, fork_body(cp, {"model_id": "claude-opus-5-5"})).status_code == 422       # no text
    r = fork(env, owner, run, fork_body(cp, {"model_id": "claude-opus-5-5"}, preset_text=text))
    assert r.status_code == 202 and env.orch.submitted[-1]["preset_text"] == text


def test_the_key_is_not_stored_or_echoed_by_a_fork(env: Env) -> None:
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game)
    ok = fork(env, owner, run, fork_body(cp))
    bad = fork(env, owner, run, fork_body(cp, extra=1))
    assert CANARY_KEY not in ok.text + bad.text
    tables = [r["relname"] for r in env.admin.execute(
        "select relname from pg_class where relnamespace = 'public'::regnamespace and relkind = 'r'").fetchall()]
    for t in tables:
        dump = env.admin.execute(f"select coalesce(string_agg(x::text, ' '), '') s from public.{t} x").fetchone()["s"]  # type: ignore[index]
        assert CANARY_KEY not in dump, t


def test_the_function_refuses_a_parent_checkpoint_the_caller_cannot_see(env: Env) -> None:
    """Defence in depth: even the DB function (the only write path) rejects a hidden run's checkpoint."""
    import psycopg
    victim, attacker, game = env.user(), env.user(), env.game()
    _, secret_cp = source(env, victim, game, "private")
    from conftest import API_DB_URL
    with psycopg.connect(API_DB_URL) as c:
        c.execute("select set_config('request.jwt.claims', %s, true)",
                  (json.dumps({"sub": attacker, "role": "authenticated"}),))
        c.execute("set local role authenticated")
        with pytest.raises(psycopg.errors.RaiseException, match="parent_checkpoint_invalid"):
            c.execute("select api_private.create_run_request('{}', %s, 'demo', 'private', 5, 100, 30, 'g', %s)",
                      ("c" * 64, secret_cp))


def test_fork_is_disabled_by_default_and_costs_nothing(make_env: Any) -> None:
    from api.settings import Settings
    assert Settings(jwks_url="x").fork_enabled is False
    env: Env = make_env(fork_enabled=False)
    owner, game = env.user(), env.game()
    run, cp = source(env, owner, game)
    r = fork(env, owner, run, fork_body(cp))
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed") and "not available" in r.json()["title"]
    assert env.keys.calls == [] and env.orch.submitted == []
    assert env.admin.execute("select count(*) n from public.runs where owner_id = %s and id <> %s", (owner, run)).fetchone()["n"] == 0  # type: ignore[index]
    from test_runs_create import body
    for _ in range(5):                                            # no create quota was used by the refused forks
        fork(env, owner, run, fork_body(cp))
    assert env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(owner)).status_code == 202
