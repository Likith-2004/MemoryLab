"""Public API (`/api/v1`). Identity is the verified JWT `sub`, never a body or path field. Reads go
through the caller's own database role so RLS applies; run-scoped routes share one visibility
dependency that answers 404 for both missing and forbidden runs (no existence oracle)."""

import difflib
import hashlib
import ipaddress
import logging
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from ingest.app import BodyLimit, problem
from labharness.agent import RunConfig
from labharness.presets import CUSTOM_LABEL, MAX_CUSTOM_CHARS, PRESETS, custom_preset, get_preset
from labharness.providers.registry import PROVIDER_MODELS, check_model
from psycopg.errors import RaiseException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from runlog.eventlog import EventLogReader
from runlog.redaction import ScrubFilter
from runlog.store import ObjectStore

from .accounts_client import AccountsClient, AccountsError
from .auth import AuthGateway, AuthUnavailable, JwtVerifier, SessionChecker, Unauthenticated, User
from .db import Conn, Database
from .limits import RateLimiter
from .orchestrator_client import KeyValidator, OrchestratorClient, SubmitUnknown, UpstreamError
from .settings import Settings

log = logging.getLogger("api")
log.addFilter(ScrubFilter())
MAX_BODY = 64 * 1024
API = "/api/v1"
_ACTIVE = ("queued", "starting", "running", "session_boundary", "dreaming", "stopping")   # runlog.lifecycle minus FINAL


def _has_nul(value: Any) -> bool:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(_has_nul(v) for v in value)
    return False


class Body(BaseModel):
    """Every request body: unknown fields refused, no coercion, and no NUL byte in any string (Postgres text
    cannot hold one, so it would surface as a 500 DataError instead of a 422)."""
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="after")
    def _no_nul(self) -> Any:
        if _has_nul(dict(self)):
            raise ValueError("NUL byte in input")
        return self


class CapsIn(Body):
    max_steps: int = Field(ge=1, le=100_000)
    max_tokens: int = Field(ge=1, le=1_000_000_000)
    max_wall_s: int = Field(ge=1, le=86_400)           # strict int: 0.5 or "60" is a 422 before any quota is used


class ValidateIn(Body):
    config: dict[str, Any]
    caps: CapsIn | None = None
    preset_text: str | None = Field(default=None, max_length=MAX_CUSTOM_CHARS)


class CreateRunIn(ValidateIn):
    game: str = Field(max_length=32)
    visibility: Literal["private", "unlisted", "public"] = "private"
    api_key: SecretStr = Field(min_length=8, max_length=512)


class ForkIn(Body):
    checkpoint_id: str = Field(min_length=1, max_length=64)
    config_patch: dict[str, Any]
    caps: CapsIn | None = None
    preset_text: str | None = Field(default=None, max_length=MAX_CUSTOM_CHARS)
    visibility: Literal["private", "unlisted", "public"] = "private"
    api_key: SecretStr = Field(min_length=8, max_length=512)


class PatchRunIn(Body):
    title: str | None = Field(default=None, max_length=120)
    visibility: Literal["private", "unlisted", "public"] | None = None


class SignupIn(Body):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: SecretStr = Field(min_length=12, max_length=128)


SIGNUP_MESSAGE = "If an account exists, we've sent an email."


class ReportIn(Body):
    reason: str = Field(min_length=1, max_length=1000)


class BreakerIn(Body):
    open: bool
    reason: str = Field(default="", max_length=200)


class DeleteAccountIn(Body):
    confirm: Literal["DELETE"]


class AdminKillIn(Body):
    reason: str = Field(min_length=1, max_length=200)


_REFUNDED_REFUSALS = ("capacity_full", "breaker_open")


class NotFound(Exception):
    pass


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise NotFound from None


def _client_ip(request: Request, hops: int = 0) -> str:
    """The socket peer, or with `hops` trusted proxies in front the address the outermost trusted proxy saw:
    X-Forwarded-For entries to the left of that were supplied by the client and are never used."""
    peer = request.client.host if request.client else "unknown"
    if hops <= 0:
        return peer
    chain = [p.strip() for line in request.headers.getlist("x-forwarded-for") for p in line.split(",") if p.strip()]
    if len(chain) < hops:
        return peer                      # fewer entries than proxies: the header was not set by our gateway
    try:
        return str(ipaddress.ip_address(chain[-hops]))
    except ValueError:
        return peer


def _ip_hash(ip: str, salt: bytes) -> str:
    return hashlib.sha256(salt + ip.encode()).hexdigest()[:32]


RUN_COLS = ("id, owner_id, title, status, visibility, game, parent_checkpoint_id, fork_patch, harness_version, "
            "model_id, provider, cap_steps, cap_tokens, cap_wall_s, steps, tokens, started_at, ended_at, end_reason, "
            "created_at")


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
               key_validator: KeyValidator, session_checker: SessionChecker, auth_gateway: AuthGateway,
               accounts: AccountsClient, store: ObjectStore | None = None,
               clock: Callable[[], float] | None = None, ip_salt: bytes | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="memorylab-api")
    app.add_middleware(BodyLimit, max_bytes=MAX_BODY)
    salt = ip_salt or secrets.token_bytes(16)
    limiter = RateLimiter(*([clock] if clock else []))

    def client_ip(request: Request) -> str:
        return _client_ip(request, settings.trusted_proxy_hops)

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
        who = user.id if user else f"ip:{_ip_hash(client_ip(request), salt)}"
        if not limiter.allow(("read", who), settings.read_limit_per_min, 60):
            raise ApiError(429, "rate_limited", "Too many requests", {"retry-after": "60"})

    def write_guard(user: Annotated[User, Depends(require_user)]) -> None:
        if not limiter.allow(("write", user.id), settings.write_limit_per_min, 60):
            raise ApiError(429, "rate_limited", "Too many requests", {"retry-after": "60"})

    def session(user: Annotated[User | None, Depends(bearer)]) -> Any:
        with db.session(user) as conn:
            yield conn

    Authed = Annotated[User, Depends(require_user)]
    Sess = Annotated[Conn, Depends(session)]
    Guard = Depends(read_guard)
    WriteGuard = Depends(write_guard)

    def load_visible_run(run_id: str, conn: Sess) -> dict[str, Any]:
        """The one place a run is looked up for a caller. The database decides visibility (owner, admin, or
        link-only unlisted/public); absent and hidden are indistinguishable (404)."""
        row = conn.execute(f"select {RUN_COLS} from api_private.get_visible_run(%s) where deleted_at is null",
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
        if not session_checker.is_active(user.token):
            raise Unauthenticated        # revoked/signed-out sessions lose admin access now, not at expiry
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

    @app.delete(f"{API}/me", status_code=204, dependencies=[WriteGuard])
    def delete_me(body: DeleteAccountIn, user: Authed) -> Response:
        """Erase the caller's account. The session is re-checked first, so a token that outlived a sign-out (or a
        previous erase) cannot do this; the service-role work happens in the accounts service."""
        if not session_checker.is_active(user.token):
            raise Unauthenticated
        if not limiter.allow(("erase", user.id), 3, 3600):
            raise ApiError(429, "rate_limited", "Too many attempts", {"retry-after": "3600"})
        try:
            accounts.erase(user.id)
        except AccountsError as exc:
            headers = {"retry-after": str(exc.retry_after_s)} if exc.retry_after_s else None
            if exc.code == "runs_still_active":
                raise ApiError(409, "runs_still_active", "Your runs are still stopping; try again shortly", headers) from None
            raise ApiError(exc.status, "internal", "Try again later", headers) from None
        return Response(status_code=204)

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

    @app.post(f"{API}/auth/signup", status_code=202)
    def signup(body: SignupIn, request: Request) -> dict[str, str]:
        """One answer for new and existing emails. An existing account gets a recovery email instead of an error."""
        ip = _ip_hash(client_ip(request), salt)
        if not limiter.allow(("signup-ip", ip), settings.signup_per_ip_per_hour, 3600):
            raise ApiError(429, "rate_limited", "Too many attempts", {"retry-after": "3600"})
        try:
            if not auth_gateway.signup(body.email.lower(), body.password.get_secret_value()):
                auth_gateway.recover(body.email.lower())
        except AuthUnavailable:
            raise ApiError(503, "internal", "Try again later", {"retry-after": "60"}) from None
        return {"message": SIGNUP_MESSAGE}

    # ------------------------------------------------------------ runs

    @app.post(f"{API}/runs/validate", dependencies=[Guard])
    def validate_run(body: ValidateIn, user: Authed) -> dict[str, Any]:
        config, _, doc = _resolve_config(body)
        return {"config": doc, "config_hash": config.config_hash, "mode": config.mode,
                "benchmark_eligible": config.mode == "benchmark" and config.preset_ref in PRESETS}

    def launch(conn: Conn, user: User, ip: str, *, game: str, config: RunConfig, doc: dict[str, Any],
               preset_text: str | None, visibility: str, api_key: SecretStr,
               parent: dict[str, Any] | None = None, fork_patch: dict[str, Any] | None = None) -> dict[str, Any]:
        """The one create path (new run and fork): lockout, one-active-run, quota, key check, the api_private
        function, commit, submit. Everything before the function call costs nothing but the read guard."""
        if limiter.count(("keyfail", user.id), 3600) >= settings.key_failures_per_user_per_hour:
            raise ApiError(429, "rate_limited", "Run creation is locked for an hour", {"retry-after": "3600"})
        if _has_active_run(conn, user):                           # cheap early answer; the DB function is the authority
            raise _active_run()
        create_keys = (("create", user.id), ("create-ip", ip))
        if not limiter.allow_all([(create_keys[0], settings.run_create_per_user_per_hour, 3600),
                                  (create_keys[1], settings.run_create_per_ip_per_hour, 3600)]):
            raise ApiError(429, "rate_limited", "Too many runs", {"retry-after": "3600"})
        refund = True                                             # every exit below refunds unless the run was accepted
        try:
            if not key_validator.validate(config.provider, api_key.get_secret_value()):
                refund = False
                limiter.record(("keyfail", user.id), 3600)
                raise ApiError(422, "key_rejected", "The key was not accepted")   # one generic answer, always
            try:
                run_id = _create_run_request(conn, game, visibility, config, doc,
                                             str(parent["checkpoint_id"]) if parent else None, fork_patch)
            except RaiseException as exc:
                if "active_run_exists" in str(exc):
                    raise _active_run() from None
                if "account_erased" in str(exc):
                    raise Unauthenticated from None
                if "caps_out_of_range" in str(exc):
                    raise ApiError(422, "validation_failed", "caps out of range") from None
                if "parent_checkpoint_invalid" in str(exc):
                    raise NotFound from None
                raise
            conn.commit()     # the run row must be visible to ingest before the orchestrator mints a token
            Database.apply_identity(conn, user)
            refund = False
            payload: dict[str, Any] = {
                "run_id": run_id, "user_id": user.id, "ip_hash": ip, "game": game,
                "run_config": config.to_dict(), "preset_text": preset_text,
                "provider": {"name": config.provider, "api_key": api_key.get_secret_value()}}
            if parent is not None:
                payload["parent"] = parent
            try:
                orchestrator.submit(payload)
            except SubmitUnknown:
                # the orchestrator may have accepted it: do not fail the row or refund the quota (a sandbox may be
                # running on this key); ask for a kill and leave the row to the reaper
                try:
                    orchestrator.kill(run_id, "system", "submit_unknown")
                except Exception:  # noqa: BLE001 - best effort; the reaper is the backstop
                    log.warning("could not kill an ambiguous submit run=%s", run_id)
                raise ApiError(503, "internal", "Try again later", {"retry-after": "30"}) from None
            except Exception as exc:
                # a refused submit must not leave a queued row that blocks the user until the reaper runs
                conn.execute("select api_private.abandon_run_request(%s)", (run_id,))
                conn.commit()
                Database.apply_identity(conn, user)
                refund = isinstance(exc, UpstreamError) and exc.code in _REFUNDED_REFUSALS
                raise
            return {"run_id": run_id, "status": "queued"}
        finally:
            if refund:
                for key in create_keys:
                    limiter.refund(key)

    @app.post(f"{API}/runs", status_code=202, dependencies=[Guard])
    def create_run(body: CreateRunIn, request: Request, user: Authed, conn: Sess) -> dict[str, Any]:
        ip = _ip_hash(client_ip(request), salt)
        config, preset_text, doc = _resolve_config(body)          # invalid requests cost nothing but the read guard
        if not conn.execute("select 1 from public.games where name = %s", (body.game,)).fetchone():
            raise ApiError(422, "validation_failed", "unknown game")
        return launch(conn, user, ip, game=body.game, config=config, doc=doc, preset_text=preset_text,
                      visibility=body.visibility, api_key=body.api_key)

    @app.post(API + "/runs/{run_id}/fork", status_code=202, dependencies=[Guard])
    def fork_run(user: Authed, run: Run, body: ForkIn, request: Request, conn: Sess) -> dict[str, Any]:
        """A new run that starts from one of this run's checkpoints, with its config changed by `config_patch`.
        The caller must be able to see the run; the checkpoint must belong to it (same 404 as a missing one); the
        parent checkpoint is set here, never taken from the body."""
        ip = _ip_hash(client_ip(request), salt)
        if body.caps is None:
            raise ApiError(422, "caps_required", "caps are required")
        if "caps" in body.config_patch:
            raise ApiError(422, "validation_failed", "give caps in `caps`, not in `config_patch`")
        if not run.get("game"):
            raise ApiError(422, "validation_failed", "this run has no recorded game to fork")
        checkpoint = conn.execute("select id, step from api_private.run_checkpoint(%s, %s)",
                                  (run["id"], _uuid(body.checkpoint_id))).fetchone()
        source = conn.execute("select config_json, mode from api_private.run_config(%s)", (run["id"],)).fetchone()
        if checkpoint is None or source is None:
            raise NotFound
        old: dict[str, Any] = dict(source["config_json"])
        changed = {k: v for k, v in body.config_patch.items() if old.get(k) != v}
        new_caps = {"max_steps": body.caps.max_steps, "max_tokens": body.caps.max_tokens,
                    "max_wall_s": body.caps.max_wall_s}
        if any(old.get("caps", {}).get(k) != v for k, v in new_caps.items()):
            changed["caps"] = new_caps
        merged = {k: v for k, v in {**old, **body.config_patch}.items() if k != "caps"}
        config, preset_text, doc = _resolve_config(
            ValidateIn(config=merged, caps=body.caps, preset_text=body.preset_text))
        if (source["mode"] == "benchmark" or config.mode == "benchmark") and len(changed) > 1:
            raise ApiError(422, "benchmark_patch_invalid", "a benchmark fork may change one field only")
        return launch(conn, user, ip, game=run["game"], config=config, doc=doc, preset_text=preset_text,
                      visibility=body.visibility, api_key=body.api_key, fork_patch=changed,
                      parent={"run_id": run["id"], "checkpoint_id": str(checkpoint["id"]), "step": checkpoint["step"]})

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

    @app.patch(API + "/runs/{run_id}", dependencies=[WriteGuard])
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

    @app.post(API + "/runs/{run_id}/stop", dependencies=[WriteGuard])
    def stop_run(run_id: str, user: Authed, conn: Sess) -> dict[str, Any]:
        run = load_visible_run(run_id, conn)
        if run["owner_id"] != user.id and not Database.is_admin(conn):
            raise NotFound
        return {"result": orchestrator.stop(run["id"], user.id)}      # idempotent upstream

    @app.delete(API + "/runs/{run_id}", status_code=204, dependencies=[WriteGuard])
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
        row = conn.execute("select * from api_private.run_score(%s)", (run["id"],)).fetchone()
        if row is None:
            raise NotFound
        return _jsonable(row)

    @app.get(API + "/runs/{run_id}/checkpoints", dependencies=[Guard])
    def checkpoints(run: Run, conn: Sess) -> list[dict[str, Any]]:
        rows = conn.execute("select id, step, memory_version, config_hash from api_private.run_checkpoints(%s)",
                            (run["id"],)).fetchall()
        return [_jsonable(r) for r in rows]

    @app.get(API + "/runs/{run_id}/memory", dependencies=[Guard])
    def memory_tree(run: Run, conn: Sess, at_step: Annotated[int, Query(ge=0)] = 1_000_000) -> list[dict[str, Any]]:
        rows = conn.execute("select path, chars, version, deleted from api_private.run_memory_tree(%s, %s)",
                            (run["id"], at_step)).fetchall()
        return [{"path": r["path"], "chars": r["chars"], "version": r["version"]} for r in rows if not r["deleted"]]

    @app.get(API + "/runs/{run_id}/memory/file", dependencies=[Guard])
    def memory_file(run: Run, conn: Sess, path: Annotated[str, Query(max_length=256)],
                    version: Annotated[int | None, Query(ge=1)] = None) -> Response:
        if "\x00" in path:
            raise NotFound
        row = _memory_version(conn, run["id"], path, version)
        if row is None or row["deleted"]:
            raise NotFound
        return PlainTextResponse(row["content"], headers={"x-content-type-options": "nosniff"})

    @app.get(API + "/runs/{run_id}/memory/diff", dependencies=[Guard])
    def memory_diff(run: Run, conn: Sess, path: Annotated[str, Query(max_length=256)],
                    from_: Annotated[int, Query(alias="from", ge=0)], to: Annotated[int, Query(ge=1)]) -> Response:
        if "\x00" in path:
            raise NotFound
        old = "" if from_ == 0 else (_memory_version(conn, run["id"], path, from_) or {}).get("content")
        new = _memory_version(conn, run["id"], path, to)
        if old is None or new is None:
            raise NotFound
        diff = difflib.unified_diff(old.splitlines(keepends=True), new["content"].splitlines(keepends=True),
                                    f"v{from_}", f"v{to}")
        return PlainTextResponse("".join(diff))

    @app.post(API + "/runs/{run_id}/report", status_code=202, dependencies=[WriteGuard])
    def report(body: ReportIn, run: Run, user: Authed, conn: Sess) -> dict[str, str]:
        if run["owner_id"] == user.id:
            raise ApiError(422, "validation_failed", "you cannot report your own run")
        try:
            conn.execute("select api_private.file_report(%s, %s)", (run["id"], body.reason))
        except RaiseException as exc:
            if "report_rate_limited" in str(exc):
                raise ApiError(429, "rate_limited", "Too many reports", {"retry-after": "3600"}) from None
            if "run_not_found" in str(exc):
                raise NotFound from None
            if "cannot_report_own_run" in str(exc):
                raise ApiError(422, "validation_failed", "you cannot report your own run") from None
            raise
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

_CURSOR = re.compile(r"^([0-9T:.+\-Z]{16,40})\|([0-9a-f-]{36})$")


def _cursor(value: str) -> tuple[datetime, str]:
    m = _CURSOR.match(value) if len(value) <= 80 else None
    if not m:
        raise ApiError(422, "validation_failed", "bad cursor")
    try:
        created = datetime.fromisoformat(m.group(1))
        if created.tzinfo is None:
            raise ValueError("no timezone")
        created.astimezone()           # out-of-range offsets overflow here, not in Postgres
    except (ValueError, OverflowError):
        raise ApiError(422, "validation_failed", "bad cursor") from None
    try:
        return created, str(uuid.UUID(m.group(2)))
    except ValueError:
        raise ApiError(422, "validation_failed", "bad cursor") from None


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
    return conn.execute("select content, deleted from api_private.run_memory_version(%s, %s, %s)",
                        (run_id, path, version)).fetchone()


def _active_run() -> ApiError:
    return ApiError(409, "rate_limited", "You already have an active run", {"retry-after": "60"})


def _has_active_run(conn: Conn, user: User) -> bool:
    return conn.execute("select 1 from public.runs where owner_id = %s and status = any(%s) limit 1",
                        (user.id, list(_ACTIVE))).fetchone() is not None


def _create_run_request(conn: Conn, game: str, visibility: str, config: RunConfig, doc: dict[str, Any],
                        parent_checkpoint_id: str | None, fork_patch: dict[str, Any] | None) -> str:
    """The only write path for a new run: the owner is auth.uid() inside the function, never a parameter."""
    from psycopg.types.json import Jsonb
    caps = config.caps
    row = conn.execute(
        "select api_private.create_run_request(p_config_json => %s, p_config_hash => %s, p_mode => %s, "
        "p_visibility => %s, p_cap_steps => %s, p_cap_tokens => %s, p_cap_wall_s => %s, p_game => %s, "
        "p_parent_checkpoint_id => %s, p_fork_patch => %s) as id",
        (Jsonb(doc), config.config_hash, config.mode, visibility, caps.max_steps, caps.max_tokens,
         int(caps.max_wall_s), game, parent_checkpoint_id, None if fork_patch is None else Jsonb(fork_patch))).fetchone()
    return str(row["id"])  # type: ignore[index]


__all__ = ["CUSTOM_LABEL", "create_app"]
