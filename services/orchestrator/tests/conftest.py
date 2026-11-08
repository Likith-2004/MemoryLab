import os
import queue
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ingest.app import create_app as create_ingest_app
from ingest.repository import InMemoryRunRepository
from ingest.service import IngestService
from runlog.store import LocalObjectStore

from orchestrator.config import OrchestratorConfig, SandboxResources
from orchestrator.core import Orchestrator, Submission

SECRET = b"unit-test-secret-unit-test-secret-32b"
GAMES = Path(__file__).resolve().parents[3] / "packages" / "games"
CANARY = "visitor-chosen-secret-value-12345"
RUN_CONFIG = {"provider": "fake", "model_id": "fake-model", "preset_ref": "general@1", "mode": "demo",
              "observation": {"hide_extras": ["input_frames", "frame_counter"]},
              "screenshots": {"mode": "every_k", "k": 2, "frame_scale": 1}, "caps": {"max_steps": 4}}


def make_config(**over: Any) -> OrchestratorConfig:
    base: dict[str, Any] = {
        "image": "memorylab-test-image", "network": "memorylab-test-net", "proxy_url": "http://ml-egress:3128",
        "sandbox_ingest_url": "http://ingest.test:8000", "ingest_url": "http://ingest.test:8000",
        "games_dir": str(GAMES), "token_secret": SECRET, "allow_fake_provider": True,
        "heartbeat_period_s": 0.05, "resources": SandboxResources()}
    base.update(over)
    return OrchestratorConfig(**base)


class FakeProcess:
    """Scripted stand-in for the docker process."""

    def __init__(self, lines: list[bytes] | None = None, exit_code: int = 0, *, run_for: float = 0.0,
                 hang: bool = False) -> None:
        self.stdin = bytearray()
        self.killed = False
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._exit = exit_code
        self._hang = hang
        self._done = threading.Event()
        for line in lines or []:
            self._lines.put(line)
        if not hang:
            threading.Timer(run_for, self._finish).start()

    def _finish(self) -> None:
        self._lines.put(None)
        self._done.set()

    def emit(self, line: bytes) -> None:
        self._lines.put(line)

    def write_stdin(self, data: bytes) -> None:
        if self.killed:
            raise BrokenPipeError
        self.stdin += data

    def lines(self) -> Iterator[bytes]:
        while True:
            item = self._lines.get()
            if item is None:
                return
            yield item

    def poll(self) -> int | None:
        return self._exit if self._done.is_set() else None

    def wait(self, timeout: float | None = None) -> int:
        self._done.wait(timeout)
        return self._exit if not self.killed else -9

    def kill(self) -> None:
        self.killed = True
        self._finish()

    def close_stdin(self) -> None:
        pass


class InProcessWorker:
    """Runs the REAL worker (`worker.main.main`) in a thread behind the same interface as the docker
    process: the orchestrator writes the job to its stdin and reads heartbeats from its stdout."""

    def __init__(self, ingest_client: TestClient) -> None:
        from worker.main import main
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        self._w_in = os.fdopen(w_in, "wb", buffering=0)
        self._r_out = os.fdopen(r_out, "rb")
        stdin_text = os.fdopen(r_in, "r")
        stdout_text = os.fdopen(w_out, "w")
        self.exit_code: int | None = None
        self.stdin_seen = bytearray()

        def target() -> None:
            self.exit_code = main(stdin_text, stdout_text, http_client=ingest_client, games_dir=GAMES)
            stdout_text.close()

        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def write_stdin(self, data: bytes) -> None:
        self.stdin_seen += data
        self._w_in.write(data)

    def lines(self) -> Iterator[bytes]:
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


@dataclass
class FakeDocker:
    network_ok: bool = True
    killed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def check_network(self) -> None:
        if not self.network_ok:
            from orchestrator.dockercmd import DockerError
            raise DockerError("network is not internal")

    def kill(self, run_id: str) -> None:
        self.killed.append(run_id)

    def remove(self, run_id: str) -> None:
        self.removed.append(run_id)


@dataclass
class World:
    repo: InMemoryRunRepository
    store: LocalObjectStore
    ingest: TestClient
    docker: FakeDocker
    launched: list[list[str]]
    procs: list[Any]
    audit: list[dict[str, Any]]
    make: Any

    def orch(self, cfg: OrchestratorConfig | None = None, launcher: Any = None, **kw: Any) -> Orchestrator:
        return self.make(cfg or make_config(), launcher, **kw)

    def register(self) -> str:
        run_id = str(uuid.uuid4())
        self.repo.register(run_id)
        return run_id


def submission(run_id: str, *, user: str = "user-1", ip: str = "ip-1", provider: dict[str, Any] | None = None,
               run_config: dict[str, Any] | None = None, **over: Any) -> Submission:
    script = [{"calls": [["press", {"buttons": ["Right"], "hold_frames": 1}]], "text": "go", "tokens": 100}] * 4
    return Submission(run_id, user, ip, over.get("game", "fixture"), run_config or RUN_CONFIG, over.get("preset_text"),
                      provider or {"name": "fake", "api_key": CANARY, "script": script})


@pytest.fixture()
def world(tmp_path, monkeypatch) -> World:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MEMORYLAB_ALLOW_FAKE_PROVIDER", "1")  # the in-process worker honours it like the container does
    repo = InMemoryRunRepository()
    store = LocalObjectStore(tmp_path / "objects")
    service = IngestService(repo, store, SECRET, clock=time.time)
    ingest = TestClient(create_ingest_app(service), raise_server_exceptions=False, base_url="http://ingest.test:8000")
    docker, launched, procs, audit = FakeDocker(), [], [], []

    def make(cfg: OrchestratorConfig, launcher: Any, **kw: Any) -> Orchestrator:
        def launch(argv: list[str]) -> Any:
            launched.append(argv)
            proc = launcher(argv) if launcher else InProcessWorker(ingest)
            procs.append(proc)
            return proc
        return Orchestrator(cfg, ingest, launcher=launch, docker=docker, audit=audit.append, **kw)

    return World(repo, store, ingest, docker, launched, procs, audit, make)
