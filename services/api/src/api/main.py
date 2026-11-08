"""Process entrypoint: `uvicorn --factory api.main:build`. Everything comes from the environment; there is
no default for the JWKS URL or the orchestrator token, so a misconfigured deployment fails at start."""

import os

from fastapi import FastAPI
from runlog.store import LocalObjectStore

from .app import create_app
from .auth import GoTrueGateway, GoTrueSessionChecker, JwtVerifier, http_jwks_fetcher
from .db import Database
from .orchestrator_client import HttpOrchestrator
from .settings import Settings


class UnconfiguredKeyValidator:
    """Real provider key validation makes one cheap call to the provider and is NOT RUN until a model key
    exists for testing (BLOCKED.md B-1). Until it is wired, run creation refuses every key."""

    def validate(self, provider: str, api_key: str) -> bool:
        return False


def build() -> FastAPI:
    settings = Settings.from_env()
    verifier = JwtVerifier(http_jwks_fetcher(settings.jwks_url), audience=settings.audience,
                           issuer=settings.issuer, algorithms=settings.allowed_algorithms)
    return create_app(
        settings, verifier=verifier, db=Database(settings.db_url),
        orchestrator=HttpOrchestrator(settings.orchestrator_url, settings.orchestrator_token),
        key_validator=UnconfiguredKeyValidator(),
        session_checker=GoTrueSessionChecker(settings.auth_url, settings.auth_apikey),
        auth_gateway=GoTrueGateway(settings.auth_url, settings.auth_apikey),
        store=LocalObjectStore(os.environ["MEMORYLAB_OBJECT_STORE"]))
