import io
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from runlog.store import LocalObjectStore

from ingest.app import create_app
from ingest.repository import InMemoryRunRepository
from ingest.service import IngestService, Limits
from ingest.tokens import issue_token

SECRET = b"unit-test-secret-unit-test-secret-32b"


def png(size: tuple[int, int] = (160, 144), mode: str = "RGB", color: object = (10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")  # type: ignore[arg-type]
    return buf.getvalue()


@dataclass
class Env:
    repo: InMemoryRunRepository
    store: LocalObjectStore
    service: IngestService
    client: TestClient
    now: list[float]

    def run(self) -> str:
        run_id = str(uuid.uuid4())
        self.repo.register(run_id)
        return run_id

    def token(self, run_id: str, scope: str = "worker", ttl: int = 600) -> str:
        return issue_token(SECRET, run_id, scope, ttl, self.now[0])

    def headers(self, run_id: str, scope: str = "worker") -> dict[str, str]:
        return {"authorization": f"Bearer {self.token(run_id, scope)}"}

    def post(self, run_id: str, batch_no: int, events: list[dict[str, Any]], scope: str = "worker"):  # type: ignore[no-untyped-def]
        return self.client.post(f"/v1/runs/{run_id}/events", json={"batch_no": batch_no, "events": events},
                                headers=self.headers(run_id, scope))

    def start(self, run_id: str, *, worker: bool = True):  # type: ignore[no-untyped-def]
        """orchestrator: starting (batch 1), worker: running (batch 2)."""
        r1 = self.post(run_id, 1, [ev("run.state", state="starting")], "orchestrator")
        assert r1.status_code == 200, r1.text
        if worker:
            r2 = self.post(run_id, 2, [ev("run.state", state="running")])
            assert r2.status_code == 200, r2.text


def ev(type_: str, step: int = 0, **data: Any) -> dict[str, Any]:
    return {"type": type_, "step": step, "data": data}


def make_env(tmp_path, limits: Limits | None = None) -> Env:  # type: ignore[no-untyped-def]
    repo = InMemoryRunRepository()
    store = LocalObjectStore(tmp_path / "objects")
    now = [1_000_000.0]
    service = IngestService(repo, store, SECRET, limits=limits, clock=lambda: now[0])
    return Env(repo, store, service, TestClient(create_app(service), raise_server_exceptions=False), now)


@pytest.fixture()
def env(tmp_path) -> Env:  # type: ignore[no-untyped-def]
    return make_env(tmp_path)
