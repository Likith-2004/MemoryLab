"""Configuration from the environment. Secrets are SecretStr-like: never logged, never echoed."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    jwks_url: str
    audience: str = "authenticated"
    issuer: str | None = None
    db_url: str = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
    orchestrator_url: str = "http://127.0.0.1:8300"
    orchestrator_token: bytes = field(default=b"", repr=False)
    allowed_algorithms: tuple[str, ...] = ("ES256", "RS256")   # never HS*/none: no shared secret in the API
    read_limit_per_min: int = 120
    run_create_per_user_per_hour: int = 5
    run_create_per_ip_per_hour: int = 10
    key_failures_per_user_per_hour: int = 5

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            jwks_url=os.environ["MEMORYLAB_JWKS_URL"],
            issuer=os.environ.get("MEMORYLAB_JWT_ISSUER"),
            db_url=os.environ.get("MEMORYLAB_DB_URL", cls.db_url),
            orchestrator_url=os.environ.get("MEMORYLAB_ORCHESTRATOR_URL", cls.orchestrator_url),
            orchestrator_token=os.environ.get("MEMORYLAB_ORCHESTRATOR_TOKEN", "").encode(),
        )
