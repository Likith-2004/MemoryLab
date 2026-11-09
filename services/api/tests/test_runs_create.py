import json
from typing import Any

from api.orchestrator_client import UpstreamError
from conftest import CANARY_KEY, Env, bearer


def body(env: Env, g: str, **over: Any) -> dict[str, Any]:
    b: dict[str, Any] = {
        "game": g, "visibility": "private", "api_key": CANARY_KEY,
        "config": {"provider": "anthropic", "model_id": "claude-haiku-5-5", "preset_ref": "general@1", "mode": "demo"},
        "caps": {"max_steps": 50, "max_tokens": 100000, "max_wall_s": 600}}
    b.update(over)
    return b


def test_validate_returns_the_resolved_config_hash_and_takes_no_key(env: Env) -> None:
    uid = env.user()
    b = body(env, "x")
    del b["api_key"], b["game"], b["visibility"]
    r = env.client.post("/api/v1/runs/validate", json=b, headers=bearer(uid))
    assert r.status_code == 200 and len(r.json()["config_hash"]) == 64 and r.json()["mode"] == "demo"
    b["api_key"] = CANARY_KEY                                               # a key here is a client bug: refuse it
    assert env.client.post("/api/v1/runs/validate", json=b, headers=bearer(uid)).status_code == 422


def test_create_run_happy_path(env: Env) -> None:
    uid, game = env.user(), env.game()
    r = env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(uid))
    assert r.status_code == 202 and r.json()["status"] == "queued"
    run_id = r.json()["run_id"]
    row = env.admin.execute("select owner_id, status, cap_steps, visibility from public.runs where id = %s",
                            (run_id,)).fetchone()
    assert (str(row["owner_id"]), row["status"], row["cap_steps"], row["visibility"]) == (uid, "queued", 50, "private")  # type: ignore[index]
    (sub,) = env.orch.submitted
    assert sub["user_id"] == uid and sub["run_id"] == run_id and sub["game"] == game
    assert sub["provider"]["api_key"] == CANARY_KEY and sub["run_config"]["caps"]["max_steps"] == 50
    assert sub["ip_hash"] != "testclient" and len(sub["ip_hash"]) == 32     # the raw IP never travels


def test_the_key_is_never_stored_returned_or_echoed(env: Env) -> None:
    uid, game = env.user(), env.game()
    ok = env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(uid))
    bad_shape = env.client.post("/api/v1/runs", json=body(env, game, extra=1), headers=bearer(uid))
    bad_caps = env.client.post("/api/v1/runs", json=body(env, game, caps=None), headers=bearer(uid))
    for resp in (ok, bad_shape, bad_caps):
        assert CANARY_KEY not in resp.text and CANARY_KEY not in json.dumps(dict(resp.headers))
    # every row of every public table, as text
    tables = [r["relname"] for r in env.admin.execute(
        "select relname from pg_class where relnamespace = 'public'::regnamespace and relkind = 'r'").fetchall()]
    for t in tables:
        dump = env.admin.execute(f"select coalesce(string_agg(x::text, ' '), '') s from public.{t} x").fetchone()["s"]  # type: ignore[index]
        assert CANARY_KEY not in dump, t


def test_caps_are_required_and_explicit(env: Env) -> None:
    uid, game = env.user(), env.game()
    r = env.client.post("/api/v1/runs", json=body(env, game, caps=None), headers=bearer(uid))
    assert r.status_code == 422 and r.json()["code"] == "caps_required"
    b = body(env, game)
    b["config"]["caps"] = {"max_steps": 999}                                # smuggled around the required block
    assert env.client.post("/api/v1/runs", json=b, headers=bearer(uid)).json()["code"] == "validation_failed"
    assert env.orch.submitted == []


def test_hostile_config_values_are_rejected_before_anything_is_created(env: Env) -> None:
    uid, game = env.user(), env.game()
    cases = [
        {"config": {"provider": "openai", "model_id": "gpt", "preset_ref": "general@1", "mode": "demo"}},
        {"config": {"provider": "anthropic", "model_id": "nope", "preset_ref": "general@1", "mode": "demo"}},
        {"config": {"provider": "anthropic", "model_id": "claude-haiku-5-5", "preset_ref": "evil@9", "mode": "demo"}},
        {"config": {"provider": "anthropic", "model_id": "claude-haiku-5-5", "preset_ref": "general@1", "mode": "demo",
                    "base_url": "http://evil"}},
        {"caps": {"max_steps": 0, "max_tokens": 1, "max_wall_s": 1}},
        {"caps": {"max_steps": True, "max_tokens": 1, "max_wall_s": 1}},
        {"game": "no-such-game"},
        {"visibility": "everyone"},
        {"preset_text": "stuff"},                                            # text for a built-in preset
    ]
    for over in cases:
        r = env.client.post("/api/v1/runs", json=body(env, game, **over), headers=bearer(uid))
        assert r.status_code == 422, (over, r.status_code, r.text)
    assert env.orch.submitted == []
    assert env.admin.execute("select count(*) n from public.runs where owner_id = %s", (uid,)).fetchone()["n"] == 0  # type: ignore[index]


def test_a_custom_preset_must_match_its_text(env: Env) -> None:
    from labharness.presets import custom_preset
    uid, game = env.user(), env.game()
    text = "Write short notes about what failed."
    b = body(env, game, preset_text=text)
    b["config"]["preset_ref"] = custom_preset(text).ref
    assert env.client.post("/api/v1/runs", json=b, headers=bearer(uid)).status_code == 202
    assert env.orch.submitted[-1]["preset_text"] == text
    b["config"]["preset_ref"] = "custom@7"
    assert env.client.post("/api/v1/runs", json=b, headers=bearer(uid)).status_code == 422
    b["preset_text"] = "x" * 4001                                           # over the custom-preset length cap
    b["config"]["preset_ref"] = custom_preset(text).ref
    assert env.client.post("/api/v1/runs", json=b, headers=bearer(uid)).status_code == 422
    b["preset_text"] = text
    b["config"]["mode"] = "benchmark"
    b["config"]["preset_ref"] = custom_preset(text).ref
    assert env.client.post("/api/v1/runs", json=b, headers=bearer(uid)).json()["code"] == "benchmark_patch_invalid"


def test_a_rejected_key_gets_one_generic_answer_then_a_lockout(env: Env) -> None:
    uid, game = env.user(), env.game()
    for _ in range(5):
        r = env.client.post("/api/v1/runs", json=body(env, game, api_key="sk-ant-wrong-key-123456"),
                            headers=bearer(uid))
        assert (r.status_code, r.json()["code"]) == (422, "key_rejected")
    r = env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(uid))   # even a good key is locked out
    assert r.status_code == 429 and r.headers["retry-after"] == "3600"
    assert env.orch.submitted == []


def finish(env: Env, uid: str) -> None:
    env.admin.execute("update public.runs set status = 'completed' where owner_id = %s", (uid,))


def test_run_creation_is_rate_limited_per_user_and_per_ip(env: Env) -> None:
    uid, game = env.user(), env.game()
    codes = []
    for _ in range(6):
        codes.append(env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(uid)).status_code)
        finish(env, uid)                                                    # one active run at a time
    assert codes == [202] * 5 + [429]
    other = env.user()                                                      # same IP, different user: IP limit is 10/h
    codes = []
    for _ in range(6):
        codes.append(env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(other)).status_code)
        finish(env, other)
    assert codes == [202] * 5 + [429]


def test_orchestrator_refusals_are_mapped_without_upstream_detail(env: Env) -> None:
    uid, game = env.user(), env.game()
    env.orch.fail_with = UpstreamError(503, "capacity_full", 30)
    r = env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(uid))
    assert (r.status_code, r.json()["code"], r.headers["retry-after"]) == (503, "capacity_full", "30")
    env.orch.fail_with = UpstreamError(409, "breaker_open")
    assert env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(uid)).json()["code"] == "breaker_open"


def test_an_internal_failure_is_an_opaque_incident_id(env: Env) -> None:
    uid, game = env.user(), env.game()

    def boom(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"secret detail {CANARY_KEY}")

    env.orch.submit = boom  # type: ignore[method-assign]
    r = env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(uid))
    assert r.status_code == 500 and set(r.json()) == {"type", "title", "status", "code", "incident"}
    assert "secret detail" not in r.text and CANARY_KEY not in r.text


def test_the_creating_user_owns_the_row_even_if_the_body_names_another_owner(env: Env) -> None:
    a, b_, game = env.user(), env.user(), env.game()
    r = env.client.post("/api/v1/runs", json=body(env, game, owner_id=b_), headers=bearer(a))
    assert r.status_code == 422
    assert env.admin.execute("select count(*) n from public.runs where owner_id in (%s, %s)", (a, b_)).fetchone()["n"] == 0  # type: ignore[index]


def test_read_endpoints_are_rate_limited(make_env: Any) -> None:
    env: Env = make_env(read_limit_per_min=3)
    codes = [env.client.get("/api/v1/games").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    uid = env.user()
    assert env.client.get("/api/v1/me", headers=bearer(uid)).status_code == 200      # separate bucket per identity


def test_me_reports_active_runs(env: Env) -> None:
    uid = env.user()
    env.run(uid, status="running")
    env.run(uid, status="completed")
    r = env.client.get("/api/v1/me", headers=bearer(uid)).json()
    assert r["active_runs"] == 1 and r["limits"]["concurrent_runs"] == 1


def test_security_headers_on_every_response(env: Env) -> None:
    for r in (env.client.get("/api/v1/health"), env.client.get("/api/v1/me"), env.client.get("/nope")):
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_oversized_bodies_are_refused(env: Env) -> None:
    uid = env.user()
    r = env.client.post("/api/v1/runs/validate", content=b"{" + b" " * 70_000 + b"}",
                        headers={**bearer(uid), "content-type": "application/json"})
    assert r.status_code == 413


def test_an_explicit_default_sessions_block_has_the_same_config_hash_as_none_on_the_validate_path(env: Env) -> None:
    uid = env.user()
    base = body(env, "x")
    for k in ("api_key", "game", "visibility"):
        del base[k]
    plain = env.client.post("/api/v1/runs/validate", json=base, headers=bearer(uid)).json()
    for block in ({}, {"session_len": None, "dream": False}):
        cfg = {**base, "config": {**base["config"], "sessions": block}}
        got = env.client.post("/api/v1/runs/validate", json=cfg, headers=bearer(uid))
        assert got.status_code == 200 and got.json()["config_hash"] == plain["config_hash"]
    on = env.client.post("/api/v1/runs/validate", json={**base, "config": {**base["config"], "sessions": {"session_len": 100, "dream": True}}}, headers=bearer(uid))
    assert on.status_code == 200 and on.json()["config_hash"] != plain["config_hash"]
    bad = env.client.post("/api/v1/runs/validate", json={**base, "config": {**base["config"], "sessions": {"dream": True}}}, headers=bearer(uid))
    assert bad.status_code == 422
