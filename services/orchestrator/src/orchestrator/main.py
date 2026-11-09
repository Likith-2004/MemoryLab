"""Process entrypoint: `uvicorn --factory orchestrator.main:build`. Values come from the environment; the secrets and the topology
have no default, so a misconfigured deployment fails at start. It starts sandboxes through the Docker CLI on this host, so run it
only on the run-worker host (see docs/LAUNCH.md): the Docker socket it needs is root-equivalent on that host."""

import os

import httpx
from fastapi import FastAPI
from runlog.envfile import secret_env

from .app import create_app
from .config import OrchestratorConfig, SandboxResources
from .core import Orchestrator


def config_from_env(env: dict[str, str] | None = None) -> tuple[OrchestratorConfig, bytes]:
    e = os.environ if env is None else env
    token, secret = secret_env("MEMORYLAB_ORCHESTRATOR_TOKEN", e).encode(), secret_env("MEMORYLAB_TOKEN_SECRET", e).encode()
    if len(token) < 32 or len(secret) < 32:
        raise ValueError("MEMORYLAB_ORCHESTRATOR_TOKEN and MEMORYLAB_TOKEN_SECRET must be at least 32 bytes")
    cfg = OrchestratorConfig(
        image=e["MEMORYLAB_WORKER_IMAGE"], network=e["MEMORYLAB_SANDBOX_NETWORK"], proxy_url=e["MEMORYLAB_PROXY_URL"],
        sandbox_ingest_url=e["MEMORYLAB_SANDBOX_INGEST_URL"], ingest_url=e["MEMORYLAB_INGEST_URL"], games_dir=e["MEMORYLAB_GAMES_DIR"],
        token_secret=secret, max_concurrent=int(e.get("MEMORYLAB_MAX_CONCURRENT", "8")),
        resources=SandboxResources(runtime=e.get("MEMORYLAB_SANDBOX_RUNTIME") or None))
    return cfg, token


def build() -> FastAPI:
    cfg, token = config_from_env()
    orch = Orchestrator(cfg, httpx.Client(base_url=cfg.ingest_url, timeout=15.0))
    orch.start()
    return create_app(orch, token)
