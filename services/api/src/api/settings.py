"""Configuration from the environment. Secrets are SecretStr-like: never logged, never echoed."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    jwks_url: str
    auth_url: str = "http://127.0.0.1:54321/auth/v1"
    auth_apikey: str = field(default="", repr=False)
    audience: str = "authenticated"
    issuer: str | None = None
    db_url: str = "postgresql://memorylab_api:memorylab-api-local@127.0.0.1:54322/postgres"   # local; no superuser
    orchestrator_url: str = "http://127.0.0.1:8300"
    orchestrator_token: bytes = field(default=b"", repr=False)
    accounts_url: str = "http://127.0.0.1:8400"
    accounts_token: bytes = field(default=b"", repr=False)
    allowed_algorithms: tuple[str, ...] = ("ES256", "RS256")   # never HS*/none: no shared secret in the API
    read_limit_per_min: int = 120
    run_create_per_user_per_hour: int = 5
    run_create_per_ip_per_hour: int = 10
    key_failures_per_user_per_hour: int = 5
    signup_per_ip_per_hour: int = 10
    write_limit_per_min: int = 30
    blob_signing_secret: bytes = field(default=b"", repr=False)   # dev-only local signer; production presigns from S3
    public_api_url: str = "http://127.0.0.1:8000"
    trusted_proxy_hops: int = 0       # proxies in front of the API whose X-Forwarded-For entry is trusted

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            jwks_url=os.environ["MEMORYLAB_JWKS_URL"],
            issuer=os.environ["MEMORYLAB_JWT_ISSUER"],       # required: an unset issuer would leave `iss` unchecked
            auth_url=os.environ.get("MEMORYLAB_AUTH_URL", cls.auth_url),
            auth_apikey=os.environ.get("MEMORYLAB_AUTH_APIKEY", ""),
            db_url=os.environ.get("MEMORYLAB_DB_URL", cls.db_url),
            orchestrator_url=os.environ.get("MEMORYLAB_ORCHESTRATOR_URL", cls.orchestrator_url),
            orchestrator_token=os.environ.get("MEMORYLAB_ORCHESTRATOR_TOKEN", "").encode(),
            accounts_url=os.environ.get("MEMORYLAB_ACCOUNTS_URL", cls.accounts_url),
            accounts_token=os.environ.get("MEMORYLAB_ACCOUNTS_TOKEN", "").encode(),
            blob_signing_secret=os.environ.get("MEMORYLAB_BLOB_SIGNING_SECRET", "").encode(),
            public_api_url=os.environ.get("MEMORYLAB_PUBLIC_API_URL", cls.public_api_url),
            trusted_proxy_hops=int(os.environ.get("MEMORYLAB_TRUSTED_PROXY_HOPS", "0")),
        )
