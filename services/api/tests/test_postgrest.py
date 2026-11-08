"""The Data API (PostgREST on 54321) with a real GoTrue user token: the tables that the API creates rows in
must not be writable directly, and api_private must not be reachable at all."""

import tomllib
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import Env
from test_gotrue import Auth, auth, real, stack  # noqa: F401  (fixtures)
from test_runs_create import body

CONFIG = Path(__file__).resolve().parents[3] / "infra" / "supabase" / "config.toml"


@pytest.fixture
def rest(stack: dict[str, str], auth: Auth) -> Any:  # noqa: F811
    email = auth.email()
    uid = auth.admin_create(email)
    token = auth.login(email).json()["access_token"]
    base = stack["API_URL"] + "/rest/v1"
    anon = {"apikey": stack["PUBLISHABLE_KEY"]}
    user = {**anon, "authorization": f"Bearer {token}", "prefer": "return=representation"}

    class Rest:
        def __init__(self) -> None:
            self.uid, self.token = uid, token

        def post(self, path: str, payload: dict[str, Any], headers: dict[str, str] | None = None,
                 as_user: bool = True) -> httpx.Response:
            return httpx.post(base + path, json=payload, headers={**(user if as_user else anon), **(headers or {})})

        def patch(self, path: str, payload: dict[str, Any]) -> httpx.Response:
            return httpx.patch(base + path, json=payload, headers=user)

    return Rest()


def test_postgrest_exposes_only_public_and_graphql_public() -> None:
    schemas = tomllib.loads(CONFIG.read_text())["api"]["schemas"]
    assert "api_private" not in schemas and set(schemas) <= {"public", "graphql_public"}


def test_direct_writes_to_the_creation_tables_are_refused(rest: Any) -> None:
    rid = str(uuid.uuid4())
    cfg = {"owner_id": rest.uid, "config_json": {"x": 1}, "config_hash": "0" * 64, "mode": "benchmark"}
    run = {"owner_id": rest.uid, "config_id": rid, "visibility": "public", "cap_steps": 2_000_000_000,
           "cap_tokens": 1, "cap_wall_s": 1}
    report = {"reporter_id": rest.uid, "run_id": rid, "reason": "spam"}
    exp = {"owner_id": rest.uid, "title": "t", "variable": "v", "run_ids": []}
    for path, payload in (("/run_configs", cfg), ("/runs", run), ("/reports", report), ("/experiments", exp)):
        assert rest.post(path, payload).status_code in (401, 403), path
        assert rest.post(path, payload, as_user=False).status_code in (401, 403), path
    assert rest.patch("/experiments?owner_id=eq." + rest.uid, {"title": "x"}).status_code in (401, 403)


def test_api_private_functions_are_not_reachable_through_postgrest(rest: Any) -> None:
    args = {"p_config_json": {}, "p_config_hash": "0" * 64, "p_mode": "demo", "p_visibility": "public",
            "p_cap_steps": 1, "p_cap_tokens": 1, "p_cap_wall_s": 1}
    assert rest.post("/rpc/create_run_request", args).status_code == 404            # not in the default schema
    assert rest.post("/rpc/create_run_request", args, {"content-profile": "api_private"}).status_code in (404, 406)
    assert rest.post("/rpc/file_report", {"p_run_id": str(uuid.uuid4()), "p_reason": "x"}).status_code == 404
    assert rest.post("/rpc/abandon_run_request", {"p_run_id": str(uuid.uuid4())}).status_code == 404


def test_the_api_still_creates_a_run_for_a_real_token(real: Env, rest: Any) -> None:  # noqa: F811
    game = real.game()
    r = real.client.post("/api/v1/runs", json=body(real, game), headers={"authorization": "Bearer " + rest.token})
    assert r.status_code == 202, r.text
    row = real.admin.execute("select owner_id, status from public.runs where id = %s", (r.json()["run_id"],)).fetchone()
    assert (str(row["owner_id"]), row["status"]) == (rest.uid, "queued")             # type: ignore[index]


def test_anon_cannot_list_unlisted_runs_but_the_api_serves_them_by_id(real: Env, rest: Any,  # noqa: F811
                                                                      stack: dict[str, str]) -> None:
    owner = real.user()
    unlisted, public, private = real.run(owner, "unlisted"), real.run(owner, "public"), real.run(owner, "private")
    anon = {"apikey": stack["PUBLISHABLE_KEY"]}
    base = stack["API_URL"] + "/rest/v1"
    for headers in (anon, {**anon, "authorization": "Bearer " + rest.token}):
        listed = {r["id"] for r in httpx.get(base + "/runs?select=id", headers=headers).json()}
        assert public in listed and unlisted not in listed and private not in listed
        assert httpx.get(f"{base}/runs?id=eq.{unlisted}&select=id", headers=headers).json() == []
    hidden_cfgs = {str(r["config_id"]) for r in real.admin.execute(
        "select config_id from public.runs where id = any(%s)", ([unlisted, private],)).fetchall()}
    visible_cfgs = {r["id"] for r in httpx.get(f"{base}/run_configs?select=id", headers=anon).json()}
    assert visible_cfgs and not visible_cfgs & hidden_cfgs                              # only the public run's config
    assert real.client.get(f"/api/v1/runs/{unlisted}").status_code == 200              # link-only still works
    assert real.client.get(f"/api/v1/runs/{private}").status_code == 404
    assert httpx.post(base + "/rpc/get_visible_run", json={"p_run_id": unlisted}, headers=anon).status_code == 404
