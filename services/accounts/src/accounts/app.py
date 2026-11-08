"""Internal HTTP API of the accounts service. Called only by the public API (which has just verified the caller's
JWT and session); authenticated with a shared bearer token on an internal network, like the orchestrator's."""

import hmac
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from ingest.app import BodyLimit, problem
from pydantic import BaseModel, ConfigDict

from .eraser import TOMBSTONE_ID, AccountEraser, RunsStillActive, UpstreamUnavailable

log = logging.getLogger("accounts.api")


class BadId(Exception):
    pass


class EraseIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_id: str


def create_app(eraser: AccountEraser, token: bytes) -> FastAPI:
    if len(token) < 32:
        raise ValueError("accounts token must be at least 32 bytes")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="accounts")
    app.add_middleware(BodyLimit, max_bytes=4096)

    @app.middleware("http")
    async def authenticate_first(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path.startswith("/internal/") and request.url.path != "/internal/health":
            scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), token):
                return problem(401, "unauthenticated", "Unauthenticated")
        return await call_next(request)

    @app.exception_handler(BadId)
    async def bad_id(_: Request, exc: BadId) -> JSONResponse:
        return problem(422, "validation_failed", "Invalid request body")

    @app.exception_handler(RunsStillActive)
    async def active(_: Request, exc: RunsStillActive) -> JSONResponse:
        return problem(409, "runs_still_active", "Runs are still stopping", {"retry-after": "5"})

    @app.exception_handler(UpstreamUnavailable)
    async def upstream(_: Request, exc: UpstreamUnavailable) -> JSONResponse:
        return problem(503, "upstream_unavailable", "Try again later", {"retry-after": "30"})

    @app.exception_handler(RequestValidationError)
    async def bad_body(_: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(422, "validation_failed", "Invalid request body")

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        incident = secrets.token_hex(6)
        log.error("unhandled error incident=%s type=%s", incident, type(exc).__name__)
        return JSONResponse({"type": "about:blank", "title": "Internal error", "status": 500, "code": "internal",
                             "incident": incident}, status_code=500, media_type="application/problem+json")

    @app.get("/internal/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/accounts/erase")
    def erase(body: EraseIn) -> dict[str, int]:
        try:
            user_id = str(uuid.UUID(body.user_id))
        except ValueError:
            raise BadId from None
        if user_id == TOMBSTONE_ID:
            raise BadId
        result = eraser.erase(user_id)
        return {"kept": result.kept, "removed": result.removed}

    return app
