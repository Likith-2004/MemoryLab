"""The full local stack for the browser tests: the real API app (real GoTrue for identity and sessions, real Postgres, the real accounts
service) on 127.0.0.1:8010, with a scripted orchestrator that plays a short run into the real event log, tables and object store.
Run with the repo venv:  .venv/bin/python apps/web/e2e/stack.py     (needs `supabase start`). SIGTERM cleans up what it created."""

import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import httpx
import psycopg
import uvicorn
from accounts.app import create_app as create_accounts_app
from accounts.eraser import AccountEraser, GoTrueAdmin
from api.accounts_client import AccountsError
from api.app import create_app
from api.auth import GoTrueGateway, GoTrueSessionChecker, JwtVerifier, http_jwks_fetcher
from api.mailjobs import MailJobs
from api.orchestrator_client import UpstreamError
from api.settings import Settings
from api.signing import LocalUrlSigner
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from runlog.eventlog import EventLogWriter
from runlog.store import LocalObjectStore

HERE = Path(__file__).resolve().parent
CORPUS: list[str] = json.loads((HERE / "xss-corpus.json").read_text())
API_PORT = int(os.environ.get("E2E_API_PORT", "8010"))
WEB_ORIGIN = os.environ.get("E2E_WEB_ORIGIN", "http://localhost:3010")
PASSWORD = "e2e-password-" + uuid.uuid4().hex[:10]
DOMAIN = "e2e.test"
DELAY = [0.35]              # seconds per step; a test can slow the run down to observe every state
CANARY_KEY = os.environ.get("E2E_CANARY_KEY", "sk-ant-CANARY-0123456789abcdef")


def supabase_env() -> dict[str, str]:
    out = subprocess.run([str(Path.home() / ".local/bin/supabase"), "status", "--workdir", str(HERE.parents[2] / "infra"), "-o", "env"],
                         capture_output=True, text=True, check=True).stdout
    return {k: v.strip('"') for k, _, v in (line.partition("=") for line in out.splitlines())}


ENV = supabase_env()
DB_URL = os.environ.get("MEMORYLAB_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
API_DB_URL = os.environ.get("MEMORYLAB_API_DB_URL", "postgresql://memorylab_api:memorylab-api-local@127.0.0.1:54322/postgres")
RUN_DIR = Path(os.environ.get("E2E_DIR") or __import__("tempfile").mkdtemp(prefix="memorylab-e2e-"))
SECRET = {"apikey": ENV["SECRET_KEY"], "authorization": "Bearer " + ENV["SECRET_KEY"]}
AUTH = ENV["API_URL"] + "/auth/v1"
admin = psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True)
store = LocalObjectStore(RUN_DIR / "store")
created_users: list[str] = []


def png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    im = Image.new("RGB", (160, 144), (30, 40, 50))
    for x in range(20, 140):
        for y in range(60, 70):
            im.putpixel((x, y), (200, 220, 90))
    im.save(buf, format="PNG")
    return buf.getvalue()


FRAME = png()


def make_user(email: str, role: str = "user") -> str:
    r = httpx.post(f"{AUTH}/admin/users", headers=SECRET, json={"email": email, "password": PASSWORD, "email_confirm": True})
    r.raise_for_status()
    uid = str(r.json()["id"])
    created_users.append(uid)
    if role == "admin":
        admin.execute("update public.profiles set role = 'admin' where id = %s", (uid,))
    return uid


def bundle(run_id: str) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bundle.json", json.dumps({"run_id": run_id}))
        zf.writestr("events.jsonl", "{}\n")
    blob = buf.getvalue()
    store.put(f"runs/{run_id}/bundle.zip", blob, overwrite=True)
    admin.execute("insert into public.run_ingest_state (run_id, bundle_sha256) values (%s, %s) on conflict (run_id) do update set bundle_sha256 = excluded.bundle_sha256",
                  (run_id, hashlib.sha256(blob).hexdigest()))


def play(run_id: str, *, steps: int = 8, delay: float | None = None, stop: threading.Event | None = None) -> None:
    """A run as the real system would write it: lifecycle events, observations with scores and frames, memory writes (with the XSS corpus as
    their content), a session boundary and a checkpoint, then the final state and a sealed bundle."""
    delay = DELAY[0] if delay is None else delay
    db = psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True)
    w = EventLogWriter(store, run_id)
    w.append([("run.state", 0, {"state": "queued"})])
    time.sleep(delay)
    db.execute("update public.runs set status = 'starting' where id = %s", (run_id,))
    w.append([("run.state", 0, {"state": "starting"})])
    time.sleep(delay)
    db.execute("update public.runs set status = 'running', started_at = now() where id = %s", (run_id,))
    w.append([("run.state", 0, {"state": "running"})])
    version = 0
    score = 0
    for step in range(1, steps + 1):
        if stop is not None and stop.is_set():
            break
        time.sleep(delay)
        score += step
        store.put(f"runs/{run_id}/frames/{step:07d}.png", FRAME, overwrite=True)
        w.append([("model.call", step, {"tokens_total": step * 120, "provider": "fake", "model_id": "fake-model"}),
                  ("model.summary", step, {"text": CORPUS[step % len(CORPUS)]}),
                  ("action", step, {"ok": True, "frames": 1, "action": {"press": ["Right"]}}),
                  ("obs", step, {"score": score, "changed": True, "frames": 1, "snapshot": {}, "new_milestones": []})])
        def note(kind: str, extra: dict[str, Any], step: int = step) -> None:
            nonlocal version
            version += 1
            content = f"notes v{version}\n" + CORPUS[(step * 3) % len(CORPUS)] + "\n"
            db.execute("insert into public.memory_files (run_id, path, version, step, content, chars, deleted) values (%s, 'notes/a.md', %s, %s, %s, %s, false)",
                       (run_id, version, step, content, len(content)))
            w.append([(kind, step, {"path": "notes/a.md", "version": version, "step": step, "rev": version, "chars": len(content),
                                    "created": version == 1, "diff": "", "content": content, **extra})])

        if step in (2, 5):
            note("mem.write", {})
        if step == 4:
            w.append([("run.state", step, {"state": "session_boundary"}), ("session.boundary", step, {"session": 1})])
            db.execute("update public.runs set status = 'session_boundary' where id = %s", (run_id,))
            time.sleep(delay)
            w.append([("run.state", step, {"state": "dreaming"})])
            db.execute("update public.runs set status = 'dreaming' where id = %s", (run_id,))
            w.append([("dream.start", step, {"session": 1, "budget_tokens": 60000, "max_turns": 6, "memory_rev": version, "files": 1})])
            time.sleep(delay)
            note("dream.write", {"kind": "correct"})                         # the dream job rewrites the note between sessions
            w.append([("dream.end", step, {"session": 1, "turns": 1, "dream_tokens": 80, "changes": 1, "cut_short": None})])
            w.append([("run.state", step, {"state": "running"})])
            db.execute("update public.runs set status = 'running' where id = %s", (run_id,))
            db.execute("insert into public.checkpoints (run_id, step, savestate_key, memory_version, transcript_offset, config_hash) values (%s, %s, 'k', %s, 0, %s)",
                       (run_id, step, version, "c" * 64))
        db.execute("update public.runs set steps = %s, tokens = %s where id = %s", (step, step * 120, run_id))
    final = "completed"
    w.append([("run.state", steps, {"state": "stopping", "reason": "stopped" if stop and stop.is_set() else "step_cap"}),
              ("run.state", steps, {"state": final, "reason": "stopped" if stop and stop.is_set() else "step_cap"})])
    db.execute("insert into public.run_scores values (%s, %s, %s, 1, 1, 1, 0, 0, false) on conflict (run_id) do update set progress_score = excluded.progress_score",
               (run_id, score, steps))
    db.execute("update public.runs set status = %s, ended_at = now(), end_reason = 'step_cap' where id = %s", (final, run_id))
    w.close()
    bundle(run_id)
    db.close()


class ScriptedOrchestrator:
    """Accepts a submit and plays the run on a thread. The key it is handed is held only in the payload and dropped."""

    def __init__(self) -> None:
        self.stops: dict[str, threading.Event] = {}
        self.breaker_open = False
        self.reason = ""
        self.mode = "ok"            # a test can set: capacity_full

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.mode == "capacity_full":
            raise UpstreamError(503, "capacity_full", 30)
        run_id = payload["run_id"]
        payload.pop("provider", None)
        self.stops[run_id] = threading.Event()
        threading.Thread(target=play, args=(run_id,), kwargs={"stop": self.stops[run_id]}, daemon=True).start()
        return {"run_id": run_id, "status": "queued"}

    def stop(self, run_id: str, actor: str) -> str:
        ev = self.stops.get(run_id)
        if ev:
            ev.set()
        return "stopping"

    def kill(self, run_id: str, actor: str, reason: str) -> str:
        self.stop(run_id, actor)
        return "killed"

    def snapshot(self) -> dict[str, Any]:
        return {"queued": [], "active": [r for r, e in self.stops.items() if not e.is_set()], "breaker": {"open": self.breaker_open, "reason": self.reason}}

    def breaker(self, open_: bool, actor: str, reason: str) -> dict[str, Any]:
        self.breaker_open, self.reason = open_, reason
        return {"open": open_}


def seed() -> dict[str, Any]:
    game = "e2e" + uuid.uuid4().hex[:8]
    admin.execute("insert into public.games (name, rom_sha256, licence, state_map_version, public) values (%s, %s, 'CC0-1.0', '1', true)", (game, "d" * 64))
    owner_email, admin_email, other_email = f"owner-{uuid.uuid4().hex[:8]}@{DOMAIN}", f"admin-{uuid.uuid4().hex[:8]}@{DOMAIN}", f"other-{uuid.uuid4().hex[:8]}@{DOMAIN}"
    owner, boss, other = make_user(owner_email), make_user(admin_email, "admin"), make_user(other_email)
    # a finished, verified, public run to replay (the landing page and leaderboard show it)
    cfg = admin.execute("insert into public.run_configs (owner_id, config_json, config_hash, mode) values (%s, %s::jsonb, %s, 'benchmark') returning id",
                        (owner, json.dumps({"provider": "anthropic", "model_id": "claude-haiku-5-5", "preset_ref": "general@1", "mode": "benchmark",
                                            "caps": {"max_steps": 50, "max_tokens": 100000, "max_wall_s": 600}}), "a" * 64)).fetchone()["id"]  # type: ignore[index]
    run = str(admin.execute("insert into public.runs (owner_id, config_id, visibility, game, status, title, model_id, provider, cap_steps, cap_tokens, cap_wall_s) "
                            "values (%s, %s, 'public', %s, 'queued', %s, 'claude-haiku-5-5', 'anthropic', 50, 100000, 600) returning id",
                            (owner, cfg, game, "Seeded replay " + CORPUS[0])).fetchone()["id"])  # type: ignore[index]
    play(run, steps=8, delay=0.01)
    admin.execute("update public.run_scores set verified = true where run_id = %s", (run,))
    return {"game": game, "owner": owner_email, "admin": admin_email, "other": other_email, "password": PASSWORD, "run": run,
            "owner_id": owner, "admin_id": boss, "other_id": other, "api": f"http://127.0.0.1:{API_PORT}"}


def build() -> tuple[Any, ScriptedOrchestrator]:
    settings = Settings(
        jwks_url=AUTH + "/.well-known/jwks.json", db_url=API_DB_URL, issuer=None, auth_url=AUTH, auth_apikey=ENV["PUBLISHABLE_KEY"],
        allowed_origins=(WEB_ORIGIN,), ws_poll_s=0.1, ws_heartbeat_s=2.0, read_limit_per_min=100_000, write_limit_per_min=100_000,
        signup_per_ip_per_hour=1000, run_create_per_user_per_hour=1000, run_create_per_ip_per_hour=1000, trusted_proxy_hops=1,
        fork_enabled=False)
    from api.db import Database
    verifier = JwtVerifier(http_jwks_fetcher(AUTH + "/.well-known/jwks.json"), audience="authenticated", issuer=None,
                           algorithms=settings.allowed_algorithms)
    killer = ScriptedOrchestrator()

    class Keys:
        def validate(self, provider: str, api_key: str) -> bool:
            return api_key == CANARY_KEY

    class Kill:
        def kill(self, run_id: str, actor: str, reason: str) -> str:
            admin.execute("update public.runs set status = 'killed' where id = %s and status not in ('completed','capped','failed','killed')", (run_id,))
            return "killed"

    accounts_token = b"a" * 40
    eraser = AccountEraser(DB_URL, Kill(), GoTrueAdmin(AUTH, ENV["SECRET_KEY"]), wait_s=2, poll_s=0.2)
    accounts_http = TestClient(create_accounts_app(eraser, accounts_token), raise_server_exceptions=False)

    class Accounts:
        def erase(self, user_id: str) -> None:
            r = accounts_http.post("/internal/accounts/erase", json={"user_id": user_id}, headers={"authorization": "Bearer " + accounts_token.decode()})
            if r.status_code == 409:
                raise AccountsError(409, "runs_still_active", 5)
            if r.status_code >= 400:
                raise AccountsError(502, "internal")

    app = create_app(settings, verifier=verifier, db=Database(API_DB_URL, max_size=8), orchestrator=killer, key_validator=Keys(),
                     session_checker=GoTrueSessionChecker(AUTH, ENV["PUBLISHABLE_KEY"]), auth_gateway=GoTrueGateway(AUTH, ENV["PUBLISHABLE_KEY"]),
                     accounts=Accounts(), store=store, signer=LocalUrlSigner(f"http://127.0.0.1:{API_PORT}", b"s" * 40),
                     mail_jobs=MailJobs(inline=False), ip_salt=b"e2e-salt")
    # a control endpoint for the tests (loopback only, e2e harness only): flip the orchestrator's mode
    @app.post("/__e2e/delay/{ms}")
    def set_delay(ms: int) -> dict[str, int]:
        DELAY[0] = max(ms, 1) / 1000
        return {"ms": ms}

    @app.post("/__e2e/mode/{mode}")
    def set_mode(mode: str) -> dict[str, str]:
        killer.mode = mode
        return {"mode": mode}

    return app, killer


def cleanup() -> None:
    try:
        admin.execute("delete from public.runs where owner_id = any(%s) or owner_id = '00000000-0000-0000-0000-00000000dead'", (created_users,))
        admin.execute("delete from public.run_configs where owner_id = any(%s) or owner_id = '00000000-0000-0000-0000-00000000dead'", (created_users,))
        admin.execute("delete from public.games where name like 'e2e%'")
        for uid in created_users:
            httpx.delete(f"{AUTH}/admin/users/{uid}", headers=SECRET)
        emails = httpx.get(f"{AUTH}/admin/users?per_page=1000", headers=SECRET).json()["users"]
        for u in emails:
            if str(u.get("email", "")).endswith("@" + DOMAIN):
                httpx.delete(f"{AUTH}/admin/users/{u['id']}", headers=SECRET)
    except Exception as exc:  # noqa: BLE001 - best effort
        print("cleanup failed:", type(exc).__name__, file=sys.stderr)


def main() -> None:
    cleanup()                      # leftovers of an earlier run that was killed
    seeded = seed()
    (RUN_DIR / "seed.json").write_text(json.dumps(seeded))
    app, _ = build()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=API_PORT, log_level="warning", ws_max_size=1024))

    def stop(*_: object) -> None:
        server.should_exit = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.run()
    finally:
        cleanup()


if __name__ == "__main__":
    main()
