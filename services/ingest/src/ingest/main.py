"""Process entrypoint: `uvicorn --factory ingest.main:build`. Every value comes from the environment and none has a default,
so a misconfigured deployment fails at start. Internal service: only the orchestrator and the per-run sandboxes reach it."""

import os

from fastapi import FastAPI
from runlog.envfile import secret_env
from runlog.store import LocalObjectStore

from .app import create_app
from .pg_repository import PostgresRunRepository
from .service import IngestService


def build() -> FastAPI:
    secret = secret_env("MEMORYLAB_TOKEN_SECRET").encode()
    if len(secret) < 32:
        raise ValueError("MEMORYLAB_TOKEN_SECRET must be at least 32 bytes")
    repo = PostgresRunRepository(secret_env("MEMORYLAB_SERVICE_DB_URL"))
    return create_app(IngestService(repo, LocalObjectStore(os.environ["MEMORYLAB_OBJECT_STORE"]), secret))
