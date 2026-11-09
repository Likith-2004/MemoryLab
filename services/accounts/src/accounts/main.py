"""Process entrypoint: `uvicorn --factory accounts.main:build`. Every value comes from the environment and none has
a default, so a misconfigured deployment fails at start."""

import os

from fastapi import FastAPI
from runlog.envfile import secret_env

from .app import create_app
from .eraser import AccountEraser, GoTrueAdmin, HttpRunKiller


def build() -> FastAPI:
    eraser = AccountEraser(
        secret_env("MEMORYLAB_SERVICE_DB_URL"),
        HttpRunKiller(os.environ["MEMORYLAB_ORCHESTRATOR_URL"], secret_env("MEMORYLAB_ORCHESTRATOR_TOKEN").encode()),
        GoTrueAdmin(os.environ["MEMORYLAB_AUTH_URL"], secret_env("MEMORYLAB_AUTH_SERVICE_KEY")))
    return create_app(eraser, secret_env("MEMORYLAB_ACCOUNTS_TOKEN").encode())
