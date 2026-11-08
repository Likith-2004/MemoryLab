"""Public API (`/api/v1`). Identity is the verified JWT `sub`, never a body or path field. Reads go
through the caller's own database role so RLS applies; run-scoped routes share one visibility
dependency that answers 404 for both missing and forbidden runs (no existence oracle)."""

import difflib
import hashlib
import logging
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from ingest.app import BodyLimit, problem
from labharness.agent import RunConfig
from labharness.presets import CUSTOM_LABEL, MAX_CUSTOM_CHARS, PRESETS, custom_preset, get_preset
from labharness.providers.registry import PROVIDER_MODELS, check_model
from psycopg.errors import RaiseException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from runlog.eventlog import EventLogReader
from runlog.redaction import ScrubFilter
from runlog.store import ObjectStore

from .auth import JwtVerifier, Unauthenticated, User
from .db import Conn, Database
from .limits import RateLimiter
from .orchestrator_client import KeyValidator, OrchestratorClient, UpstreamError
from .settings import Settings

log = logging.getLogger("api")
log.addFilter(ScrubFilter())
MAX_BODY = 64 * 1024
API = "/api/v1"
_ACTIVE = ("queued", "running")


class CapsIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    max_steps: int
    max_tokens: int
    max_wall_s: float


class ValidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    config: dict[str, Any]
    caps: CapsIn | None = None
    preset_text: str | None = Field(default=None, max_length=MAX_CUSTOM_CHARS)


class CreateRunIn(ValidateIn):
    game: str = Field(max_length=32)
    visibility: Literal["private", "unlisted", "public"] = "private"
    api_key: SecretStr = Field(min_length=8, max_length=512)


class PatchRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str | None = Field(default=None, max_length=120)
    visibility: Literal["private", "unlisted", "public"] | None = None


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(min_length=1, max_length=1000)


class BreakerIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    open: bool
    reason: str = Field(default="", max_length=200)


class AdminKillIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(min_length=1, max_length=200)


class NotFound(Exception):
    pass


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise NotFound from None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _ip_hash(ip: str, salt: bytes) -> str:
    return hashlib.sha256(salt + ip.encode()).hexdigest()[:32]


RUN_COLS = ("id, owner_id, title, status, visibility, harness_version, model_id, provider, cap_steps, cap_tokens, "
            "cap_wall_s, steps, tokens, started_at, ended_at, end_reason, created_at")


def _resolve_config(body: ValidateIn) -> tuple[RunConfig, str | None, dict[str, Any]]:
    """Validate a run configuration. Caps are mandatory and explicit (`caps_required`)."""
    if body.caps is None:
        raise ApiError(422, "caps_required", "caps are required")
    doc = dict(body.config)
    if "caps" in doc:
        raise ApiError(422, "validation_failed", "give caps in `caps`, not in `config`")
    doc["caps"] = {"max_steps": body.caps.max_steps, "max_tokens": body.caps.max_tokens,
                   "max_wall_s": body.caps.max_wall_s}
    try:
        config = RunConfig.from_dict(doc)
        check_model(config.provider, config.model_id)
    except ValueError as exc:
        raise ApiError(422, "validation_failed", str(exc)[:160]) from None
    preset_text: str | None = None
    if config.preset_ref.startswith("custom@"):
        if not body.preset_text:
            raise ApiError(422, "validation_failed", "preset_text is required for a custom preset")
        preset = custom_preset(body.preset_text)
        if config.preset_ref != preset.ref:
            raise ApiError(422, "validation_failed", "preset_ref does not match preset_text")
        preset_text = body.preset_text
    else:
        try:
            get_preset(config.preset_ref)
        except (KeyError, ValueError):
            raise ApiError(422, "validation_failed", "unknown preset") from None
        if body.preset_text is not None:
            raise ApiError(422, "validation_failed", "preset_text is only for custom presets")
    if config.mode == "benchmark" and preset_text is not None:
        raise ApiError(422, "benchmark_patch_invalid", "benchmark mode uses an approved preset")
    return config, preset_text, doc


class ApiError(Exception):
    def __init__(self, status: int, code: str, title: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.title, self.headers = status, code, title, headers


def create_app(settings: Settings, *, verifier: JwtVerifier, db: Database, orchestrator: OrchestratorClient,
               key_validator: KeyValidator, store: ObjectStore | None = None,
               clock: Callable[[], float] | None = None, ip_salt: bytes | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="memorylab-api")
    app.add_middleware(BodyLimit, max_bytes=MAX_BODY)
    salt = ip_salt or secrets.token_bytes(16)
    limiter = RateLimiter(*([clock] if clock else []))

    @app.middleware("http")
    async def headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        resp = await call_next(request)
        resp.headers.setdefault("x-content-type-options", "nosniff")
        resp.headers.setdefault("cache-control", "no-store")
        resp.headers.setdefault("content-security-policy", "default-src 'none'; frame-ancestors 'none'")
        return resp

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return problem(exc.status, exc.code, exc.title, exc.headers)

    @app.exception_handler(UpstreamError)
    async def upstream(_: Request, exc: UpstreamError) -> JSONResponse:
        headers = {"retry-after": str(exc.retry_after_s)} if exc.retry_after_s else None
        return problem(exc.status, exc.code, exc.code, headers)

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return problem(404, "not_found", "Not found")

    @app.exception_handler(Unauthenticated)
    async def unauth(_: Request, exc: Unauthenticated) -> JSONResponse:
        return problem(401, "unauthenticated", "Unauthenticated", {"www-authenticate": "Bearer"})

    @app.exception_handler(RequestValidationError)
    async def bad_input(_: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(422, "validation_failed", "Invalid request")      # never echoes input: it may hold a key

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        incident = secrets.token_hex(6)
        log.error("unhandled error incident=%s type=%s", incident, type(exc).__name__)
        return JSONResponse({"type": "about:blank", "title": "Internal error", "status": 500, "code": "internal",
                             "incident": incident}, status_code=500, media_type="application/problem+json")

    # ------------------------------------------------------------ dependencies

    def bearer(request: Request) -> User | None:
        header = request.headers.get("authorization")
        if header is None:
            return None
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Unauthenticated
        return verifier.verify(token)       # a bad token is 401, never silently anonymous

    def require_user(user: Annotated[User | None, Depends(bearer)]) -> User:
        if user is None:
            raise Unauthenticated
        return user

    def read_guard(request: Request, user: Annotated[User | None, Depends(bearer)]) -> None:
        who = user.id if user else f"ip:{_ip_hash(_client_ip(request), salt)}"
        if not limiter.allow(("read", who), settings.read_limit_per_min, 60):
            raise ApiError(429, "rate_limited", "Too many requests", {"retry-after": "60"})

    def session(user: Annotated[User | None, Depends(bearer)]) -> Any:
        with db.session(user) as conn:
            yield conn

    Authed = Annotated[User, Depends(require_user)]
    Sess = Annotated[Conn, Depends(session)]
    Guard = Depends(read_guard)

    def load_visible_run(run_id: str, conn: Sess) -> dict[str, Any]:
        """The one place a run is looked up for a caller. RLS decides visibility; absent and hidden are
        indistinguishable (404)."""
        row = conn.execute(f"select {RUN_COLS} from public.runs where id = %s and deleted_at is null",
                           (_uuid(run_id),)).fetchone()
        if row is None:
            raise NotFound
        return _public_run(row)

    Run = Annotated[dict[str, Any], Depends(load_visible_run)]

    def owner_run(run: Run, user: Authed) -> dict[str, Any]:
        if run["owner_id"] != user.id:
            raise NotFound               # visible but not theirs: same answer as hidden
        return run

    OwnerRun = Annotated[dict[str, Any], Depends(owner_run)]

    def admin_user(user: Authed, conn: Sess) -> User:
        if not Database.is_admin(conn):
            raise NotFound               # admin routes do not exist for non-admins
        return user

    Admin = Annotated[User, Depends(admin_user)]

    # ------------------------------------------------------------ catalogue and account

    @app.get(f"{API}/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(f"{API}/me", dependencies=[Guard])
    def me(user: Authed, conn: Sess) -> dict[str, Any]:
        prof = conn.execute("select id, handle, role, created_at from public.profiles where id = %s",
                            (user.id,)).fetchone()
        if prof is None:
            raise NotFound
        active = conn.execute("select count(*) n from public.runs where owner_id = %s and status = any(%s) "
                              "and deleted_at is null", (user.id, list(_ACTIVE))).fetchone()["n"]  # type: ignore[index]
        return {**_jsonable(prof), "active_runs": active, "limits": {
            "concurrent_runs": 1, "runs_per_hour": settings.run_create_per_user_per_hour,
            "reads_per_minute": settings.read_limit_per_min}}

    @app.get(f"{API}/games", dependencies=[Guard])
    def games(conn: Sess) -> list[dict[str, Any]]:
        rows = conn.execute("select id, name, licence, state_map_version from public.games order by name").fetchall()
        return [_jsonable(r) for r in rows]

    @app.get(API + "/games/{game_id}/start-states", dependencies=[Guard])
    def start_states(game_id: str, conn: Sess) -> list[dict[str, Any]]:
        rows = conn.execute("select id, label, savestate_sha256 from public.start_states where game_id = %s",
                            (_uuid(game_id),)).fetchall()
        return [_jsonable(r) for r in rows]

    @app.get(f"{API}/presets", dependencies=[Guard])
    def presets() -> list[dict[str, Any]]:
        return [{"id": p.id, "version": p.version, "ref": p.ref, "text": p.text, "text_sha256": p.text_sha256,
                 "label": p.label, "is_adaptation": p.is_adaptation} for p in PRESETS.values()]

    @app.get(f"{API}/models", dependencies=[Guard])
    def models() -> dict[str, Any]:
        return {"providers": {k: list(v) for k, v in PROVIDER_MODELS.items()},
                "price_table": {"version": None, "as_of": None, "note": "no price table loaded; no cost estimate shown"}}

    # ------------------------------------------------------------ runs

    @app.post(f"{API}/runs/validate", dependencies=[Guard])
    def validate_run(body: ValidateIn, user: Authed) -> dict[str, Any]:
        config, _, doc = _resolve_config(body)
        return {"config": doc, "config_hash": config.config_hash, "mode": config.mode,
                "benchmark_eligible": config.mode == "benchmark" and config.preset_ref in PRESETS}

    @app.post(f"{API}/runs", status_code=202, dependencies=[Guard])
    def create_run(body: CreateRunIn, request: Request, user: Authed, conn: Sess) -> dict[str, Any]:
        ip = _ip_hash(_client_ip(request), salt)
        config, preset_text, doc = _resolve_config(body)          # invalid requests cost nothing but the read guard
        if not conn.execute("select 1 from public.games where name = %s", (body.game,)).fetchone():
            raise ApiError(422, "validation_failed", "unknown game")
        if limiter.count(("keyfail", user.id), 3600) >= settings.key_failures_per_user_per_hour:
            raise ApiError(429, "rate_limited", "Run creation is locked for an hour", {"retry-after": "3600"})
        if not limiter.allow(("create", user.id), settings.run_create_per_user_per_hour, 3600) or \
                not limiter.allow(("create-ip", ip), settings.run_create_per_ip_per_hour, 3600):
            raise ApiError(429, "rate_limited", "Too many runs", {"retry-after": "3600"})
        if not key_validator.validate(config.provider, body.api_key.get_secret_value()):
            limiter.record(("keyfail", user.id), 3600)
            raise ApiError(422, "key_rejected", "The key was not accepted")   # one generic answer, always
        run_id = _insert_run(conn, user, body, config, doc)
        conn.commit()     # the run row must be visible to ingest before the orchestrator mints a token
        Database.apply_identity(conn, user)
        orchestrator.submit({
            "run_id": run_id, "user_id": user.id, "ip_hash": ip, "game": body.game, "run_config": config.to_dict(),
            "preset_text": preset_text,
            "provider": {"name": config.provider, "api_key": body.api_key.get_secret_value()}})
        return {"run_id": run_id, "status": "queued"}

    @app.get(f"{API}/runs", dependencies=[Guard])
    def list_runs(user: Authed, conn: Sess, limit: Annotated[int, Query(ge=1, le=100)] = 25,
                  before: str | None = None) -> dict[str, Any]:
        cursor = None if before is None else _cursor(before)
        rows = conn.execute(
            f"select {RUN_COLS} from public.runs where owner_id = %s and deleted_at is null "
            "and (%s::timestamptz is null or (created_at, id) < (%s::timestamptz, %s::uuid)) "
            "order by created_at desc, id desc limit %s",
            (user.id, cursor and cursor[0], cursor and cursor[0], cursor and cursor[1], limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        nxt = f"{rows[-1]['created_at'].isoformat()}|{rows[-1]['id']}" if more else None
        return {"runs": [_public_run(r) for r in rows], "next": nxt}

    @app.get(API + "/runs/{run_id}", dependencies=[Guard])
    def get_run(run: Run) -> dict[str, Any]:
        return run

    @app.patch(API + "/runs/{run_id}")
    def patch_run(body: PatchRunIn, run: OwnerRun, conn: Sess) -> dict[str, Any]:
        if body.title is None and body.visibility is None:
            raise ApiError(422, "validation_failed", "nothing to change")
        try:
            row = conn.execute(
                f"update public.runs set title = coalesce(%s, title), visibility = coalesce(%s, visibility) "
                f"where id = %s returning {RUN_COLS}", (body.title, body.visibility, run["id"])).fetchone()
        except RaiseException as exc:
            if "benchmark_visibility_locked" in str(exc):
                raise ApiError(422, "benchmark_patch_invalid", "a leaderboard run cannot be made private") from None
            raise
        if row is None:
            raise NotFound
        return _public_run(row)

    @app.post(API + "/runs/{run_id}/stop")
    def stop_run(run_id: str, user: Authed, conn: Sess) -> dict[str, Any]:
        run = load_visible_run(run_id, conn)
        if run["owner_id"] != user.id and not Database.is_admin(conn):
            raise NotFound
        return {"result": orchestrator.stop(run["id"], user.id)}      # idempotent upstream

    @app.delete(API + "/runs/{run_id}", status_code=204)
    def delete_run(run: OwnerRun, conn: Sess) -> Response:
        conn.execute("select public.soft_delete_run(%s)", (run["id"],))
        return Response(status_code=204)

    @app.get(API + "/runs/{run_id}/events", dependencies=[Guard])
    def events(run: Run, after_seq: Annotated[int, Query(ge=0)] = 0,
               limit: Annotated[int, Query(ge=1, le=1000)] = 1000) -> dict[str, Any]:
        if store is None:
            raise ApiError(503, "internal", "event store unavailable")
        page = EventLogReader(store, run["id"]).read_page(after_seq, limit)
        return {"events": [e.to_obj() for e in page], "next_after_seq": page[-1].seq if page else after_seq}

    @app.get(API + "/runs/{run_id}/score", dependencies=[Guard])
    def score(run: Run, conn: Sess) -> dict[str, Any]:
        row = conn.execute("select * from public.run_scores where run_id = %s", (run["id"],)).fetchone()
        if row is None:
            raise NotFound
        return _jsonable(row)

    @app.get(API + "/runs/{run_id}/checkpoints", dependencies=[Guard])
    def checkpoints(run: Run, conn: Sess) -> list[dict[str, Any]]:
        rows = conn.execute("select id, step, memory_version, config_hash from public.checkpoints "
                            "where run_id = %s order by step", (run["id"],)).fetchall()
        return [_jsonable(r) for r in rows]

    @app.get(API + "/runs/{run_id}/memory", dependencies=[Guard])
    def memory_tree(run: Run, conn: Sess, at_step: Annotated[int, Query(ge=0)] = 1_000_000) -> list[dict[str, Any]]:
        rows = conn.execute(
            "select distinct on (path) path, chars, version, deleted from public.memory_files "
            "where run_id = %s and step <= %s order by path, version desc", (run["id"], at_step)).fetchall()
        return [{"path": r["path"], "chars": r["chars"], "version": r["version"]} for r in rows if not r["deleted"]]

    @app.get(API + "/runs/{run_id}/memory/file", dependencies=[Guard])
    def memory_file(run: Run, conn: Sess, path: Annotated[str, Query(max_length=256)],
                    version: Annotated[int | None, Query(ge=1)] = None) -> Response:
        row = _memory_version(conn, run["id"], path, version)
        if row is None or row["deleted"]:
            raise NotFound
        return PlainTextResponse(row["content"], headers={"x-content-type-options": "nosniff"})

    @app.get(API + "/runs/{run_id}/memory/diff", dependencies=[Guard])
    def memory_diff(run: Run, conn: Sess, path: Annotated[str, Query(max_length=256)],
                    from_: Annotated[int, Query(alias="from", ge=0)], to: Annotated[int, Query(ge=1)]) -> Response:
        old = "" if from_ == 0 else (_memory_version(conn, run["id"], path, from_) or {}).get("content")
        new = _memory_version(conn, run["id"], path, to)
        if old is None or new is None:
            raise NotFound
        diff = difflib.unified_diff(old.splitlines(keepends=True), new["content"].splitlines(keepends=True),
                                    f"v{from_}", f"v{to}")
        return PlainTextResponse("".join(diff))

    @app.post(API + "/runs/{run_id}/report", status_code=202)
    def report(body: ReportIn, run: Run, user: Authed, conn: Sess) -> dict[str, str]:
        conn.execute("insert into public.reports (reporter_id, run_id, reason) values (%s, %s, %s)",
                     (user.id, run["id"], body.reason))
        return {"status": "received"}

    # ------------------------------------------------------------ leaderboard

    @app.get(f"{API}/leaderboard", dependencies=[Guard])
    def leaderboard(conn: Sess, limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[dict[str, Any]]:
        rows = conn.execute(
            "select r.id, r.model_id, r.provider, s.progress_score, s.steps, s.tokens_in, s.tokens_out "
            "from public.run_scores s join public.runs r on r.id = s.run_id "
            "where s.verified and r.visibility = 'public' and r.deleted_at is null "
            "order by s.progress_score desc, s.steps asc limit %s", (limit,)).fetchall()
        return [_jsonable(r) for r in rows]

    # ------------------------------------------------------------ admin

    @app.get(f"{API}/admin/runs")
    def admin_runs(_: Admin) -> dict[str, Any]:
        return orchestrator.snapshot()

    @app.post(API + "/admin/runs/{run_id}/kill")
    def admin_kill(run_id: str, body: AdminKillIn, admin: Admin) -> dict[str, Any]:
        return {"result": orchestrator.kill(_uuid(run_id), admin.id, body.reason)}

    @app.post(f"{API}/admin/breaker")
    def admin_breaker(body: BreakerIn, admin: Admin) -> dict[str, Any]:
        return orchestrator.breaker(body.open, admin.id, body.reason)

    return app


# ---------------------------------------------------------------- helpers

_CURSOR = re.compile(r"^(\d{4}-\d\d-\d\dT[0-9:.+\-Z]+)\|([0-9a-f-]{36})$")


def _cursor(value: str) -> tuple[str, str]:
    m = _CURSOR.match(value)
    if not m:
        raise ApiError(422, "validation_failed", "bad cursor")
    return m.group(1), _uuid(m.group(2))


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else str(v) if isinstance(v, uuid.UUID) else (
            float(v) if v.__class__.__name__ == "Decimal" else v)
    return out


def _public_run(row: dict[str, Any]) -> dict[str, Any]:
    run = _jsonable(row)
    run["caps"] = {"max_steps": run.pop("cap_steps"), "max_tokens": run.pop("cap_tokens"),
                   "max_wall_s": run.pop("cap_wall_s")}
    return run


def _memory_version(conn: Conn, run_id: str, path: str, version: int | None) -> dict[str, Any] | None:
    if version is None:
        return conn.execute("select content, deleted from public.memory_files where run_id = %s and path = %s "
                            "order by version desc limit 1", (run_id, path)).fetchone()
    return conn.execute("select content, deleted from public.memory_files where run_id = %s and path = %s "
                        "and version = %s", (run_id, path, version)).fetchone()


def _insert_run(conn: Conn, user: User, body: CreateRunIn, config: RunConfig, doc: dict[str, Any]) -> str:
    from psycopg.types.json import Jsonb
    cfg_id = conn.execute(
        "insert into public.run_configs (owner_id, config_json, config_hash, mode) values (%s, %s, %s, %s) "
        "returning id", (user.id, Jsonb(doc), config.config_hash, config.mode)).fetchone()["id"]  # type: ignore[index]
    caps = config.caps
    row = conn.execute(
        "insert into public.runs (owner_id, config_id, visibility, cap_steps, cap_tokens, cap_wall_s) "
        "values (%s, %s, %s, %s, %s, %s) returning id",
        (user.id, cfg_id, body.visibility, caps.max_steps, caps.max_tokens, int(caps.max_wall_s))).fetchone()
    return str(row["id"])  # type: ignore[index]


__all__ = ["CUSTOM_LABEL", "create_app"]
