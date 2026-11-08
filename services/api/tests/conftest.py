"""Fixtures for tests against the local Supabase stack (`supabase start --workdir infra`).

Set MEMORYLAB_REQUIRE_DB=1 (the gate does) to make an unreachable database a failure, not a skip.
"""

import json
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jwt import PyJWT
from jwt.algorithms import ECAlgorithm
from psycopg.rows import dict_row

from api.app import create_app
from api.auth import JwtVerifier
from api.db import Database
from api.settings import Settings

DB_URL = os.environ.get("MEMORYLAB_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")   # admin: seeding
API_DB_URL = os.environ.get("MEMORYLAB_API_DB_URL", Settings.db_url)                                       # what the API uses


@pytest.fixture(scope="session")
def admin_conn() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    try:
        conn = psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("MEMORYLAB_REQUIRE_DB") == "1":
            pytest.fail(f"local Supabase is required but unreachable: {exc}")
        pytest.skip("local Supabase not running (supabase start --workdir infra)")
    yield conn
    conn.close()


class Db:
    """A transaction rolled back at the end of each test; `as_` switches role and JWT claims."""

    def __init__(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        self.conn = conn
        self.conn.execute("begin")

    def new_user(self, role: str = "user") -> str:
        uid = str(uuid.uuid4())
        self.conn.execute(
            "insert into auth.users (id, email, instance_id, aud, role, created_at, updated_at, confirmation_token, recovery_token, email_change_token_new, email_change) "
            "values (%s, %s, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', now(), now(), '', '', '', '')",
            (uid, f"{uid}@example.test"))
        if role == "admin":
            self.conn.execute("update public.profiles set role = 'admin' where id = %s", (uid,))
        return uid

    def as_(self, user: str | None) -> None:
        """None = anon. The claims are what Supabase's PostgREST sets for a verified JWT."""
        self.conn.execute("reset role")
        if user is None:
            claims = {"role": "anon"}
            role = "anon"
        else:
            claims = {"sub": user, "role": "authenticated"}
            role = "authenticated"
        self.conn.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
        self.conn.execute(f"set local role {role}")

    def as_service(self) -> None:
        self.conn.execute("reset role")
        self.conn.execute("select set_config('request.jwt.claims', %s, true)",
                          (json.dumps({"role": "service_role"}),))
        self.conn.execute("set local role service_role")

    def q(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self.conn.execute(sql, args).fetchall()  # type: ignore[arg-type]

    def x(self, sql: str, *args: Any) -> None:
        self.conn.execute(sql, args)  # type: ignore[arg-type]

    def make_run(self, owner: str, visibility: str = "private", **cols: Any) -> str:
        """Seed a run as the service role (RLS bypass), then return to the previous caller-less state."""
        self.as_service()
        cfg = self.q("insert into public.run_configs (owner_id, config_json, config_hash, mode) "
                     "values (%s, '{}', %s, 'demo') returning id", owner, "a" * 64)[0]["id"]
        row = self.q("insert into public.runs (owner_id, config_id, visibility, cap_steps, cap_tokens, cap_wall_s) "
                     "values (%s, %s, %s, 10, 1000, 60) returning id", owner, cfg, visibility)[0]
        run_id = str(row["id"])
        for k, v in cols.items():
            self.x(f"update public.runs set {k} = %s where id = %s", v, run_id)
        self.conn.execute("reset role")
        return run_id

    def close(self) -> None:
        self.conn.execute("rollback")


@pytest.fixture
def db(admin_conn: psycopg.Connection[dict[str, Any]]) -> Iterator[Db]:
    d = Db(admin_conn)
    yield d
    d.close()


# ---------------------------------------------------------------- app-level fixtures



KID = "test-key-1"
AUD = "authenticated"
PRIVATE = ec.generate_private_key(ec.SECP256R1())
JWK = {**__import__("json").loads(ECAlgorithm.to_jwk(PRIVATE.public_key())), "kid": KID, "alg": "ES256", "use": "sig"}
CANARY_KEY = "sk-ant-CANARY-0123456789abcdef"


def mint(sub: str | None, *, aud: str = AUD, exp_in: int = 600, alg: str = "ES256", kid: str | None = KID,
         role: str = "authenticated", key: Any = PRIVATE, extra: dict[str, Any] | None = None) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {"aud": aud, "exp": now + exp_in, "iat": now, "role": role, **(extra or {})}
    if sub is not None:
        claims["sub"] = sub
    headers = {"kid": kid} if kid else {}
    return PyJWT().encode(claims, key, algorithm=alg, headers=headers)


def bearer(sub: str, **kw: Any) -> dict[str, str]:
    return {"authorization": "Bearer " + mint(sub, **kw)}


@dataclass
class FakeOrchestrator:
    submitted: list[dict[str, Any]] = field(default_factory=list)
    stopped: list[tuple[str, str]] = field(default_factory=list)
    killed: list[tuple[str, str, str]] = field(default_factory=list)
    breaker_calls: list[tuple[bool, str, str]] = field(default_factory=list)
    fail_with: Exception | None = None
    kill_fail: Exception | None = None
    delegate: Any = None                 # a real HttpOrchestrator pointed at a test server, for transport-level cases

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail_with:
            raise self.fail_with
        if self.delegate is not None:
            return self.delegate.submit(payload)  # type: ignore[no-any-return]
        self.submitted.append(payload)
        return {"run_id": payload["run_id"], "status": "queued"}

    def stop(self, run_id: str, actor: str) -> str:
        self.stopped.append((run_id, actor))
        return "stopping"

    def kill(self, run_id: str, actor: str, reason: str) -> str:
        if self.kill_fail:
            raise self.kill_fail
        self.killed.append((run_id, actor, reason))
        if self.delegate is not None:
            return self.delegate.kill(run_id, actor, reason)  # type: ignore[no-any-return]
        return "killed"

    def snapshot(self) -> dict[str, Any]:
        return {"running": [], "queued": []}

    def breaker(self, open_: bool, actor: str, reason: str) -> dict[str, Any]:
        self.breaker_calls.append((open_, actor, reason))
        return {"open": open_}


class FakeAccounts:
    def __init__(self) -> None:
        self.erased: list[str] = []
        self.fail: Any = None

    def erase(self, user_id: str) -> None:
        if self.fail:
            raise self.fail
        self.erased.append(user_id)


class FakeSessions:
    def __init__(self) -> None:
        self.revoked: set[str] = set()

    def is_active(self, token: str) -> bool:
        return token not in self.revoked


class FakeGateway:
    def __init__(self) -> None:
        self.existing: set[str] = set()
        self.signups: list[str] = []
        self.recovered: list[str] = []
        self.down = False

    def signup(self, email: str, password: str) -> bool:
        from api.auth import AuthUnavailable
        if self.down:
            raise AuthUnavailable
        self.signups.append(email)
        return email not in self.existing

    def recover(self, email: str) -> None:
        self.recovered.append(email)


class FakeKeys:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.accept = True

    def validate(self, provider: str, api_key: str) -> bool:
        self.calls.append((provider, api_key))
        return self.accept and api_key == CANARY_KEY


@dataclass
class Env:
    admin: psycopg.Connection[dict[str, Any]]
    client: TestClient
    orch: FakeOrchestrator
    keys: FakeKeys
    sessions: FakeSessions
    gateway: FakeGateway
    accounts: FakeAccounts
    store: Any
    signer: Any
    clock: list[float]
    settings: Settings
    users: list[str]
    games: list[str]

    def user(self, role: str = "user") -> str:
        uid = str(uuid.uuid4())
        self.admin.execute(
            "insert into auth.users (id, email, instance_id, aud, role, created_at, updated_at, confirmation_token, recovery_token, email_change_token_new, email_change) "
            "values (%s, %s, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', now(), now(), '', '', '', '')",
            (uid, f"{uid}@example.test"))
        if role == "admin":
            self.admin.execute("update public.profiles set role = 'admin' where id = %s", (uid,))
        self.users.append(uid)
        return uid

    def game(self, name: str | None = None, public: bool = True) -> str:
        name = name or "g" + uuid.uuid4().hex[:10]
        self.admin.execute("insert into public.games (name, rom_sha256, licence, state_map_version, public) "
                           "values (%s, %s, 'CC0', '1', %s)", (name, "d" * 64, public))
        self.games.append(name)
        return name

    def run(self, owner: str, visibility: str = "private", **cols: Any) -> str:
        cfg = self.admin.execute("insert into public.run_configs (owner_id, config_json, config_hash, mode) "
                                 "values (%s, '{}', %s, 'demo') returning id", (owner, "a" * 64)).fetchone()["id"]  # type: ignore[index]
        rid = str(self.admin.execute(
            "insert into public.runs (owner_id, config_id, visibility, cap_steps, cap_tokens, cap_wall_s) "
            "values (%s, %s, %s, 10, 1000, 60) returning id", (owner, cfg, visibility)).fetchone()["id"])  # type: ignore[index]
        for k, v in cols.items():
            self.admin.execute(f"update public.runs set {k} = %s where id = %s", (v, rid))
        return rid


def make_settings(**kw: Any) -> Settings:
    kw.setdefault("fork_enabled", True)           # the product default is off; the fork tests exercise the route
    return Settings(jwks_url="http://unused.invalid/jwks", db_url=API_DB_URL, issuer=None, **kw)


@pytest.fixture
def make_env(admin_conn: psycopg.Connection[dict[str, Any]], tmp_path: Any) -> Iterator[Any]:
    created: list[tuple[Env, Database]] = []

    def build(signer: bool = True, **settings_kw: Any) -> Env:
        with_signer = signer
        settings = make_settings(**settings_kw)
        db = Database(API_DB_URL, max_size=4)
        verifier = JwtVerifier(lambda: {"keys": [JWK]}, audience=AUD, issuer=None,
                               algorithms=settings.allowed_algorithms)
        orch, keys, sessions, gateway = FakeOrchestrator(), FakeKeys(), FakeSessions(), FakeGateway()
        accounts = FakeAccounts()
        from runlog.store import LocalObjectStore

        from api.signing import LocalUrlSigner
        store = LocalObjectStore(tmp_path / "store")
        clock = [1_000_000.0]                                        # the signer's clock, for expiry tests
        signer = LocalUrlSigner("http://api.test", b"s" * 40, clock=lambda: clock[0])
        app = create_app(settings, verifier=verifier, db=db, orchestrator=orch, key_validator=keys, session_checker=sessions, auth_gateway=gateway,
                         accounts=accounts, store=store, signer=signer if with_signer else None,
                         ip_salt=b"test-salt")
        env = Env(admin_conn, TestClient(app, raise_server_exceptions=False, base_url="http://api.test"), orch, keys,
                  sessions, gateway, accounts, store, signer, clock, settings, [], [])
        created.append((env, db))
        return env

    yield build
    for env, db in created:
        db.close()
        for uid in env.users:
            admin_conn.execute("delete from auth.users where id = %s", (uid,))   # cascades to profiles, runs, ...
        for name in env.games:
            admin_conn.execute("delete from public.games where name = %s", (name,))


@pytest.fixture
def env(make_env: Any) -> Env:
    e: Env = make_env()
    return e
