from conftest import Env, bearer


def test_admin_routes_do_not_exist_for_non_admins_or_anon(env: Env) -> None:
    user = env.user()
    for method, path, body in (("GET", "/api/v1/admin/runs", None),
                               ("POST", "/api/v1/admin/breaker", {"open": True, "reason": "x"}),
                               ("POST", "/api/v1/admin/runs/6f1a2b3c-0000-4000-8000-000000000000/kill", {"reason": "x"})):
        assert env.client.request(method, path, json=body, headers=bearer(user)).status_code == 404
        assert env.client.request(method, path, json=body).status_code == 401
    assert env.orch.killed == [] and env.orch.breaker_calls == []


def test_admin_role_is_read_from_the_database_not_from_claims(env: Env) -> None:
    user = env.user()
    claims = {"app_metadata": {"role": "admin"}, "user_role": "admin", "role_claim": "admin"}
    assert env.client.get("/api/v1/admin/runs", headers=bearer(user, extra=claims)).status_code == 404


def test_admin_actions_go_through_and_are_attributed(env: Env) -> None:
    admin, run = env.user("admin"), "6f1a2b3c-0000-4000-8000-000000000001"
    assert env.client.get("/api/v1/admin/runs", headers=bearer(admin)).status_code == 200
    assert env.client.post(f"/api/v1/admin/runs/{run}/kill", json={"reason": "abuse"},
                           headers=bearer(admin)).json() == {"result": "killed"}
    assert env.orch.killed == [(run, admin, "abuse")]
    assert env.client.post("/api/v1/admin/breaker", json={"open": True, "reason": "load"},
                           headers=bearer(admin)).status_code == 200
    assert env.orch.breaker_calls == [(True, admin, "load")]
    assert env.client.post("/api/v1/admin/runs/not-a-uuid/kill", json={"reason": "x"},
                           headers=bearer(admin)).status_code == 404


def test_demoting_an_admin_takes_effect_on_the_next_request(env: Env) -> None:
    admin = env.user("admin")
    assert env.client.get("/api/v1/admin/runs", headers=bearer(admin)).status_code == 200
    env.admin.execute("update public.profiles set role = 'user' where id = %s", (admin,))
    assert env.client.get("/api/v1/admin/runs", headers=bearer(admin)).status_code == 404


def test_admin_can_stop_someone_elses_visible_run_and_sees_private_ones(env: Env) -> None:
    admin, owner = env.user("admin"), env.user()
    run = env.run(owner, "private")
    assert env.client.get(f"/api/v1/runs/{run}", headers=bearer(admin)).status_code == 200
    assert env.client.post(f"/api/v1/runs/{run}/stop", headers=bearer(admin)).status_code == 200
    assert env.orch.stopped == [(run, admin)]
