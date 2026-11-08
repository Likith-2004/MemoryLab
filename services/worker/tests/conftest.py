import io
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ingest.app import create_app
from ingest.repository import InMemoryRunRepository
from ingest.service import IngestService
from ingest.tokens import issue_token
from runlog.store import LocalObjectStore

SECRET = b"unit-test-secret-unit-test-secret-32b"
GAMES = Path(__file__).resolve().parents[3] / "packages" / "games"
CANARY = "visitor-chosen-secret-value-12345"
RUN_CONFIG = {"provider": "fake", "model_id": "fake-model", "preset_ref": "general@1", "mode": "demo",
              "observation": {"hide_extras": ["input_frames", "frame_counter"]},
              "screenshots": {"mode": "every_k", "k": 2, "frame_scale": 1}, "caps": {"max_steps": 4}}


@dataclass
class Env:
    repo: InMemoryRunRepository
    store: LocalObjectStore
    client: TestClient
    now: float = 1_000_000.0

    def run(self) -> str:
        run_id = str(uuid.uuid4())
        self.repo.register(run_id)
        orch = {"authorization": f"Bearer {issue_token(SECRET, run_id, 'orchestrator', 600, self.now)}"}
        r = self.client.post(f"/v1/runs/{run_id}/events", headers=orch, json={
            "batch_no": 1, "events": [{"type": "run.state", "step": 0, "data": {"state": "starting"}}]})
        assert r.status_code == 200
        return run_id

    def spec(self, run_id: str, **over: Any) -> dict[str, Any]:
        token = issue_token(SECRET, run_id, "worker", 600, self.now)
        script = [{"calls": [["press", {"buttons": ["Right"], "hold_frames": 1}]], "text": "go", "tokens": 100}] * 4
        doc: dict[str, Any] = {"v": 1, "run_id": run_id, "game": "fixture", "run_config": RUN_CONFIG,
                               "ingest": {"url": "http://ingest.test:8000", "token": token},
                               "provider": {"name": "fake", "api_key": CANARY, "script": script}, "heartbeat_s": 0.05}
        doc.update(over)
        return doc

    def line(self, run_id: str, extra_lines: str = "", **over: Any) -> io.StringIO:
        return io.StringIO(json.dumps(self.spec(run_id, **over)) + "\n" + extra_lines)


@pytest.fixture()
def env(tmp_path, monkeypatch) -> Env:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MEMORYLAB_ALLOW_FAKE_PROVIDER", "1")
    repo = InMemoryRunRepository()
    store = LocalObjectStore(tmp_path / "objects")
    service = IngestService(repo, store, SECRET, clock=lambda: 1_000_000.0)
    return Env(repo, store, TestClient(create_app(service), raise_server_exceptions=False))
