"""The API's fork route end to end: real API app -> real orchestrator (in process, through its HTTP app) -> real worker and emulator ->
real ingest on Postgres. The provider is the scripted fake (the API only accepts real provider names, so a thin adapter swaps it in at
the orchestrator boundary); everything else is the production code path."""

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from ingest.app import create_app as create_ingest_app
from ingest.pg_repository import PostgresRunRepository
from ingest.service import IngestService
from orchestrator.app import create_app as create_orch_app
from orchestrator.config import OrchestratorConfig, SandboxResources
from orchestrator.core import Orchestrator, Submission
from runlog.eventlog import EventLogReader
from runlog.replay import verify_log

from conftest import CANARY_KEY, DB_URL, Env, bearer

GAMES = Path(__file__).resolve().parents[3] / "packages" / "games"
SECRET = b"fork-e2e-secret-fork-e2e-secret-32b"
ORCH_TOKEN = b"o" * 40
CAPS = {"max_steps": 4, "max_tokens": 100_000, "max_wall_s": 600}
CONFIG = {"provider": "anthropic", "model_id": "claude-haiku-5-5", "preset_ref": "general@1", "mode": "demo", "sessions": {"session_len": 2}}
STEPS = [{"calls": [["press", {"buttons": ["Right"], "hold_frames": 1}]] + ([["mem_write", {"path": "notes/a.md", "content": "door at 3,3\n"}]] if i == 0 else []),
          "text": f"step {i + 1}", "tokens": 100} for i in range(4)]


class InProcessWorker:
    """The real worker (`worker.main.main`) in a thread behind the process interface the orchestrator drives."""

    def __init__(self, ingest: TestClient) -> None:
        from worker.main import main
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        self._w_in, self._r_out = os.fdopen(w_in, "wb", buffering=0), os.fdopen(r_out, "rb")
        stdin_text, stdout_text = os.fdopen(r_in, "r"), os.fdopen(w_out, "w")
        self.exit_code: int | None = None

        def target() -> None:
            self.exit_code = main(stdin_text, stdout_text, http_client=ingest, games_dir=GAMES)
            stdout_text.close()

        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def write_stdin(self, data: bytes) -> None:
        self._w_in.write(data)

    def lines(self):  # type: ignore[no-untyped-def]
        while line := self._r_out.readline():
            yield line

    def poll(self) -> int | None:
        return self.exit_code if not self._thread.is_alive() else None

    def wait(self, timeout: float | None = None) -> int:
        self._thread.join(timeout)
        return self.exit_code if self.exit_code is not None else -9

    def kill(self) -> None:
        pass

    def close_stdin(self) -> None:
        self._w_in.close()


class FakeDocker:
    def check_network(self) -> None: ...
    def kill(self, run_id: str) -> None: ...
    def remove(self, run_id: str) -> None: ...


class RecordingClient(TestClient):
    """The worker's and orchestrator's view of ingest, recording which endpoints were called."""

    calls: list[str]

    def request(self, method, url, **kw):  # type: ignore[no-untyped-def,override]
        self.calls.append(f"{method} {url}")
        return super().request(method, url, **kw)


@pytest.fixture
def stack(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("MEMORYLAB_ALLOW_FAKE_PROVIDER", "1")
    repo = PostgresRunRepository(DB_URL, max_size=6)
    service = IngestService(repo, env.store, SECRET, clock=time.time)
    ingest = RecordingClient(create_ingest_app(service), raise_server_exceptions=False, base_url="http://ingest.test:8000")
    ingest.calls = []
    cfg = OrchestratorConfig(image="img", network="net", proxy_url="http://ml-egress:3128", sandbox_ingest_url="http://ingest.test:8000",
                             ingest_url="http://ingest.test:8000", games_dir=str(GAMES), token_secret=SECRET, allow_fake_provider=True,
                             heartbeat_period_s=0.05, resources=SandboxResources())
    orch = Orchestrator(cfg, ingest, launcher=lambda argv: InProcessWorker(ingest), docker=FakeDocker())
    orch_http = TestClient(create_orch_app(orch, ORCH_TOKEN), raise_server_exceptions=False)

    class Adapter:
        """The API only knows real provider names; at the orchestrator boundary the run is switched to the scripted fake."""

        def __init__(self) -> None:
            self.payloads: list[dict[str, Any]] = []

        def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.payloads.append(json.loads(json.dumps(payload)))
            payload["run_config"]["provider"] = "fake"
            payload["provider"] = {"name": "fake", "api_key": payload["provider"]["api_key"], "script": STEPS[2:]}
            r = orch_http.post("/internal/runs", json=payload, headers={"authorization": "Bearer " + ORCH_TOKEN.decode()})
            assert r.status_code == 202, r.text
            orch.tick()
            return r.json()  # type: ignore[no-any-return]

        def kill(self, run_id: str, actor: str, reason: str) -> str:
            return "unknown"

    adapter = Adapter()
    env.orch.delegate = adapter
    yield env, orch, ingest, adapter, repo
    repo.close()


def make_parent(env: Env, orch: Orchestrator, owner: str, game: str) -> tuple[str, str]:
    """A finished PUBLIC run of `owner`, played by the real stack, with a checkpoint at its session boundary (step 2)."""
    doc = {**CONFIG, "caps": CAPS}
    cfg = env.admin.execute("insert into public.run_configs (owner_id, config_json, config_hash, mode) values (%s, %s::jsonb, %s, 'demo') returning id",
                            (owner, json.dumps(doc), "a" * 64)).fetchone()["id"]  # type: ignore[index]
    run = str(env.admin.execute(
        "insert into public.runs (owner_id, config_id, visibility, game, cap_steps, cap_tokens, cap_wall_s, title) "
        "values (%s, %s, 'public', %s, 4, 100000, 600, 'parent') returning id", (owner, cfg, game)).fetchone()["id"])  # type: ignore[index]
    run_config = {**doc, "provider": "fake"}
    orch.submit(Submission(run, owner, "ip-a", game, run_config, None, {"name": "fake", "api_key": CANARY_KEY, "script": STEPS}))
    orch.tick()
    assert orch.wait_idle(120)
    cp = env.admin.execute("select id from public.checkpoints where run_id = %s and step = 2", (run,)).fetchone()
    assert cp is not None, "the parent run recorded no checkpoint row"
    return run, str(cp["id"])


def fork_body(cp: str, **over: Any) -> dict[str, Any]:
    return {"checkpoint_id": cp, "config_patch": {}, "caps": CAPS, "api_key": CANARY_KEY, **over}


def last_obs(events: list[Any]) -> dict[str, Any]:
    return [e for e in events if e.type == "obs"][-1].data  # type: ignore[no-any-return]


def snapshot(env: Env, run: str) -> dict[str, Any]:
    row = env.admin.execute("select owner_id::text o, title, visibility, status, steps, tokens, parent_checkpoint_id, fork_patch, deleted_at "
                            "from public.runs where id = %s", (run,)).fetchone()
    return dict(row)  # type: ignore[arg-type]


def test_user_b_forks_user_as_public_run_through_the_api_orchestrator_worker_and_ingest(stack: Any) -> None:
    env, orch, ingest, adapter, _ = stack
    a, b = env.user(), env.user()
    env.game("fixture")
    parent, cp = make_parent(env, orch, a, "fixture")
    parent_events = list(EventLogReader(env.store, parent).iter_events())
    before_row = snapshot(env, parent)
    parent_event_count = env.admin.execute("select count(*) n from public.run_events_index where run_id = %s", (parent,)).fetchone()["n"]  # type: ignore[index]
    assert before_row["status"] == "capped" and before_row["o"] == a

    r = env.client.post(f"/api/v1/runs/{parent}/fork", json=fork_body(cp), headers=bearer(b))
    assert r.status_code == 202, r.text
    child = r.json()["run_id"]
    assert orch.wait_idle(120)

    row = snapshot(env, child)
    assert row["o"] == b and str(row["parent_checkpoint_id"]) == cp and row["fork_patch"] == {} and row["status"] == "capped"
    # the API told the orchestrator exactly which parent: the checkpoint it looked up itself, never anything from the body
    sent = adapter.payloads[-1]
    assert sent["parent"] == {"run_id": parent, "checkpoint_id": cp, "step": 2} and sent["user_id"] == b
    # the worker fetched the parent through ingest, which found it only through the database link
    assert any(c.endswith(f"/v1/runs/{child}/parent") for c in ingest.calls) and any(c.endswith(f"/v1/runs/{child}/parent/state") for c in ingest.calls)
    child_events = list(EventLogReader(env.store, child).iter_events())
    assert verify_log(child_events) == []
    start = next(e for e in child_events if e.type == "fork.start")
    assert start.data["parent_run_id"] == parent and start.data["parent_step"] == 2 and start.data["context_restored"] is True
    # an empty patch: the fork's continuation is the parent's (same emulator state, same score)
    assert last_obs(child_events)["ram_sha256"] == last_obs(parent_events)["ram_sha256"] and last_obs(child_events)["score"] == last_obs(parent_events)["score"]
    # user A's run is untouched
    assert snapshot(env, parent) == before_row
    assert env.admin.execute("select count(*) n from public.run_events_index where run_id = %s", (parent,)).fetchone()["n"] == parent_event_count  # type: ignore[index]
    assert list(EventLogReader(env.store, parent).iter_events()) == parent_events
    # the provider key is nowhere: not in any table, event, blob or file the run touched
    tables = [x["relname"] for x in env.admin.execute("select relname from pg_class where relnamespace = 'public'::regnamespace and relkind = 'r'").fetchall()]
    for t in tables:
        assert CANARY_KEY not in env.admin.execute(f"select coalesce(string_agg(x::text, ' '), '') s from public.{t} x").fetchone()["s"], t  # type: ignore[index]
    blob = b"".join(p.read_bytes() for p in env.store.root.rglob("*") if p.is_file())
    assert CANARY_KEY.encode() not in blob


def test_once_the_owner_makes_the_run_private_the_fork_is_a_404_and_creates_nothing(stack: Any) -> None:
    env, orch, _, adapter, _ = stack
    a, b = env.user(), env.user()
    env.game("fixture")
    parent, cp = make_parent(env, orch, a, "fixture")
    assert env.client.patch(f"/api/v1/runs/{parent}", json={"visibility": "private"}, headers=bearer(a)).status_code == 200
    r = env.client.post(f"/api/v1/runs/{parent}/fork", json=fork_body(cp), headers=bearer(b))
    missing = env.client.post(f"/api/v1/runs/{uuid.uuid4()}/fork", json=fork_body(cp), headers=bearer(b))
    assert (r.status_code, r.json()) == (missing.status_code, missing.json()) == (404, missing.json())
    assert env.admin.execute("select count(*) n from public.runs where owner_id = %s", (b,)).fetchone()["n"] == 0  # type: ignore[index]
    assert adapter.payloads == [] and env.keys.calls == []                                 # nothing reached the orchestrator, no key was even checked
    # the owner can still fork their own private run
    assert env.client.post(f"/api/v1/runs/{parent}/fork", json=fork_body(cp), headers=bearer(a)).status_code == 202
    assert orch.wait_idle(120)


def test_the_unrelated_user_cannot_use_a_checkpoint_of_a_run_they_cannot_see(stack: Any) -> None:
    env, orch, _, adapter, _ = stack
    a, b = env.user(), env.user()
    env.game("fixture")
    secret_parent, secret_cp = make_parent(env, orch, a, "fixture")
    env.admin.execute("update public.runs set visibility = 'private' where id = %s", (secret_parent,))
    mine, _ = make_parent(env, orch, b, "fixture")
    r = env.client.post(f"/api/v1/runs/{mine}/fork", json=fork_body(secret_cp), headers=bearer(b))      # my run, someone else's checkpoint
    assert r.status_code == 404 and adapter.payloads == []
    _ = httpx
