"""Internal HTTP API of the orchestrator (called by the public API service, never by browsers).

Production deployments put this behind mutual TLS on an internal network (plan: "internal mTLS
channel"); this module authenticates with a shared bearer token and must not be exposed anywhere
else (DECISIONS M5-6). Request bodies are never logged.
"""

import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from ingest.app import BodyLimit, problem
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from runlog.redaction import ScrubFilter

from .core import Orchestrator, Submission, SubmitError
from .scheduler import AdmissionError

log = logging.getLogger("orchestrator.api")
log.addFilter(ScrubFilter())
MAX_BODY = 512 * 1024


class ProviderIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(max_length=32)
    api_key: SecretStr | None = None
    script: list[dict[str, Any]] | None = None


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str = Field(max_length=40)
    user_id: str = Field(min_length=1, max_length=64)
    ip_hash: str = Field(min_length=1, max_length=128)
    game: str = Field(max_length=32)
    run_config: dict[str, Any]
    preset_text: str | None = Field(default=None, max_length=4000)
    provider: ProviderIn


class KillIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    actor: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=200)


class BreakerIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    open: bool
    actor: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=200)


class StopIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    actor: str = Field(min_length=1, max_length=64)


def create_app(orch: Orchestrator, api_token: bytes) -> FastAPI:
    if len(api_token) < 32:
        raise ValueError("api token must be at least 32 bytes")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="orchestrator")
    app.add_middleware(BodyLimit, max_bytes=MAX_BODY)

    @app.middleware("http")
    async def authenticate_first(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Runs before routing and body parsing, so unauthenticated callers learn nothing about the schema."""
        if request.url.path.startswith("/internal/") and request.url.path != "/internal/health":
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), api_token):
                return problem(401, "unauthenticated", "Unauthenticated")
        return await call_next(request)

    @app.exception_handler(SubmitError)
    async def submit_error(_: Request, exc: SubmitError) -> JSONResponse:
        return problem(422, exc.code, exc.detail[:200])

    @app.exception_handler(AdmissionError)
    async def admission(_: Request, exc: AdmissionError) -> JSONResponse:
        headers = {"retry-after": str(exc.retry_after_s)} if exc.retry_after_s else None
        return problem(exc.status, exc.code, exc.code, headers)

    @app.exception_handler(RequestValidationError)
    async def bad_body(_: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(422, "validation_failed", "Invalid request body")  # never echoes input (it may hold a key)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        incident = secrets.token_hex(6)
        log.error("unhandled error incident=%s type=%s", incident, type(exc).__name__)
        return JSONResponse({"type": "about:blank", "title": "Internal error", "status": 500, "code": "internal",
                             "incident": incident}, status_code=500, media_type="application/problem+json")

    @app.get("/internal/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/runs", status_code=202)
    async def submit(body: RunIn, request: Request) -> dict[str, Any]:
        provider: dict[str, Any] = {"name": body.provider.name}
        if body.provider.api_key is not None:
            provider["api_key"] = body.provider.api_key.get_secret_value()
        if body.provider.script is not None:
            provider["script"] = body.provider.script
        return orch.submit(Submission(body.run_id, body.user_id, body.ip_hash, body.game, body.run_config,
                                      body.preset_text, provider))

    @app.post("/internal/runs/{run_id}/stop")
    async def stop(run_id: str, body: StopIn, request: Request) -> dict[str, str]:
        return {"result": orch.stop(run_id, body.actor)}

    @app.post("/internal/runs/{run_id}/kill")
    async def kill(run_id: str, body: KillIn, request: Request) -> dict[str, str]:
        return {"result": orch.kill(run_id, body.actor, body.reason)}

    @app.get("/internal/runs")
    async def runs(request: Request) -> dict[str, Any]:
        return orch.scheduler.snapshot()

    @app.post("/internal/breaker")
    async def breaker(body: BreakerIn, request: Request) -> dict[str, Any]:
        try:
            return orch.set_breaker(open_=body.open, reason=body.reason, actor=body.actor)
        except ValueError:
            raise SubmitError("validation_failed", "a reason is required to open the breaker") from None

    return app


class _Unauthenticated(Exception):
    pass
