"""FastAPI application for the ingest service (internal: only the orchestrator and per-run
sandboxes reach it)."""

import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from runlog.events import RUN_ID
from runlog.redaction import ScrubFilter
from starlette.types import ASGIApp, Receive, Scope, Send

from .service import IngestError, IngestService, Invalid, Unauthenticated, UnknownRun
from .tokens import Claims

log = logging.getLogger("ingest")
log.addFilter(ScrubFilter())


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: str = Field(max_length=40)
    step: int
    data: dict[str, Any]


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    batch_no: int = Field(ge=1)
    events: list[EventIn] = Field(max_length=100)


def problem(status: int, code: str, title: str, headers: dict[str, str] | None = None) -> JSONResponse:
    """RFC 9457 problem+json with a stable `code`."""
    return JSONResponse({"type": "about:blank", "title": title, "status": status, "code": code}, status_code=status,
                        media_type="application/problem+json", headers={"cache-control": "no-store", **(headers or {})})


class BodyLimit:
    """Bound request bodies: reject on Content-Length up front, and buffer chunked bodies up to the
    limit before the app sees them, answering 413 as soon as the limit is crossed."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        too_big = problem(413, "payload_too_large", "Payload too large")
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await too_big(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            size += len(body)
            if size > self.max_bytes:
                await too_big(scope, receive, send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break
        replay = iter([{"type": "http.request", "body": b"".join(chunks), "more_body": False}])

        async def buffered() -> Any:
            return next(replay, {"type": "http.disconnect"})

        await self.app(scope, buffered, send)


def create_app(service: IngestService) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="ingest")
    app.add_middleware(BodyLimit, max_bytes=service.limits.max_body_bytes)

    def claims_for(request: Request, run_id: str) -> Claims:
        if not RUN_ID.fullmatch(run_id):
            raise UnknownRun("no such run")
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Unauthenticated("missing bearer token")
        return service.authenticate(token, run_id)

    def authed(request: Request) -> Claims:
        claims: Claims = request.state.claims  # set by the auth middleware below
        return claims

    @app.middleware("http")
    async def authenticate_first(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Authentication runs before routing and body parsing, so an unauthenticated caller can learn
        nothing from validation errors (FastAPI parses bodies before it resolves dependencies)."""
        parts = request.url.path.split("/")
        if len(parts) >= 5 and parts[1] == "v1" and parts[2] == "runs":
            try:
                request.state.claims = claims_for(request, parts[3])
            except IngestError as exc:
                return problem(exc.status, exc.code, exc.detail[:200])
        return await call_next(request)

    @app.exception_handler(IngestError)
    async def ingest_error(_: Request, exc: IngestError) -> JSONResponse:
        return problem(exc.status, exc.code, exc.detail[:200])

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        incident = secrets.token_hex(6)
        log.error("unhandled error incident=%s type=%s", incident, type(exc).__name__)  # no message, no body
        return JSONResponse({"type": "about:blank", "title": "Internal error", "status": 500, "code": "internal",
                             "incident": incident}, status_code=500, media_type="application/problem+json")

    @app.exception_handler(RequestValidationError)
    async def bad_body(_: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(422, "validation_failed", "Invalid request body")  # never echo the offending input

    @app.middleware("http")
    async def no_store(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers.setdefault("cache-control", "no-store")
        response.headers.setdefault("x-content-type-options", "nosniff")
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/runs/{run_id}/events")
    async def post_events(run_id: str, body: BatchIn, claims: Claims = Depends(authed)) -> dict[str, Any]:  # noqa: B008
        return service.append(claims, run_id, body.batch_no, [e.model_dump() for e in body.events])

    @app.put("/v1/runs/{run_id}/frames/{step}")
    async def put_frame(run_id: str, step: int, request: Request, claims: Claims = Depends(authed)) -> dict[str, bool]:  # noqa: B008
        if request.headers.get("content-type", "").split(";")[0] != "image/png":
            raise Invalid("content-type must be image/png")
        service.put_frame(claims, run_id, step, await request.body())
        return {"ok": True}

    @app.put("/v1/runs/{run_id}/manifest")
    async def put_manifest(run_id: str, request: Request, claims: Claims = Depends(authed)) -> dict[str, bool]:  # noqa: B008
        try:
            manifest = await request.json()
        except ValueError:
            raise Invalid("manifest must be JSON") from None
        if not isinstance(manifest, dict):
            raise Invalid("manifest must be an object")
        service.put_manifest(claims, run_id, manifest)
        return {"sealed": True}

    @app.get("/v1/runs/{run_id}/status")
    async def status(run_id: str, claims: Claims = Depends(authed)) -> dict[str, Any]:  # noqa: B008
        return service.run_status(claims, run_id)

    @app.post("/v1/runs/{run_id}/close")
    async def close(run_id: str, claims: Claims = Depends(authed)) -> dict[str, Any]:  # noqa: B008
        return service.close_run(claims, run_id)

    return app
