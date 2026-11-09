"""Boots the whole stack from infra/compose.yaml (+ the smoke overlay), then plays one scripted run and a fork of it through the CLI.

    .venv/bin/python scripts/launch_smoke.py            needs `supabase start --workdir infra` and the images (infra/docker/build.sh --all)

Real here: every container (hardened as compose defines them), the sandbox the orchestrator starts per run, the egress proxy, ingest,
Postgres and GoTrue, the web server and the CLI. Scripted: only the model's answers (infra/smoke/*). Prints one line per check and exits
non-zero on the first failure. Creates only objects labelled/named memorylab (project `memorylab-smoke`, network `memorylab-sandbox`)
and removes them; the Docker daemon may host other projects, which are never touched.
"""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "memorylab-smoke"
COMPOSE = ["-p", PROJECT, "-f", str(ROOT / "infra/compose.yaml"), "-f", str(ROOT / "infra/compose.smoke.yaml")]


def free_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


# loopback ports that are free right now (the machine may host other stacks); override with SMOKE_API_PORT / SMOKE_WEB_PORT
API_PORT, WEB_PORT = os.environ.get("SMOKE_API_PORT") or free_port(), os.environ.get("SMOKE_WEB_PORT") or free_port()
SMOKE_KEY = "sk-ant-SMOKE-" + secrets.token_hex(12)
FINAL = {"completed", "capped", "failed", "killed"}
WEB_HEADERS = {"content-security-policy": "frame-ancestors 'none'", "strict-transport-security": "max-age=63072000", "x-content-type-options": "nosniff",
               "referrer-policy": "no-referrer", "permissions-policy": "camera=()", "x-frame-options": "DENY"}
API_HEADERS = {"content-security-policy": "default-src 'none'", "strict-transport-security": "max-age=63072000", "x-content-type-options": "nosniff",
               "referrer-policy": "no-referrer", "permissions-policy": "camera=()", "cache-control": "no-store"}


def say(msg: str) -> None:
    print(f"smoke: {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"smoke: FAIL {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, **kw)  # type: ignore[call-overload,no-any-return]


def supabase_env() -> dict[str, str]:
    out = run([str(Path.home() / ".local/bin/supabase"), "status", "--workdir", str(ROOT / "infra"), "-o", "env"], check=True).stdout
    return {k: v.strip('"') for k, _, v in (line.partition("=") for line in out.splitlines()) if _}


def main() -> None:
    sb = supabase_env()
    tmp = Path(tempfile.mkdtemp(prefix="memorylab-smoke-"))
    gid = str(Path("/var/run/docker.sock").stat().st_gid)
    env = {
        "MEMORYLAB_TOKEN_SECRET": secrets.token_hex(32), "MEMORYLAB_ORCHESTRATOR_TOKEN": secrets.token_hex(32),
        "MEMORYLAB_ACCOUNTS_TOKEN": secrets.token_hex(32), "MEMORYLAB_BLOB_SIGNING_SECRET": secrets.token_hex(32),
        "MEMORYLAB_LOG_PEPPER": secrets.token_hex(16),
        "MEMORYLAB_SERVICE_DB_URL": "postgresql://postgres:postgres@supabase_db_memorylab:5432/postgres",
        "MEMORYLAB_API_DB_URL": "postgresql://memorylab_api:memorylab-api-local@supabase_db_memorylab:5432/postgres",
        "MEMORYLAB_AUTH_URL_INTERNAL": "http://supabase_kong_memorylab:8000/auth/v1",
        "MEMORYLAB_JWKS_URL_INTERNAL": "http://supabase_kong_memorylab:8000/auth/v1/.well-known/jwks.json",
        "MEMORYLAB_JWT_ISSUER": sb["API_URL"] + "/auth/v1", "MEMORYLAB_AUTH_SERVICE_KEY": sb["SECRET_KEY"],
        "MEMORYLAB_PUBLISHABLE_KEY": sb["PUBLISHABLE_KEY"],
        "MEMORYLAB_PUBLIC_API_URL": f"http://127.0.0.1:{API_PORT}", "MEMORYLAB_SITE_URL": f"http://localhost:{WEB_PORT}",
        "MEMORYLAB_API_BIND": f"127.0.0.1:{API_PORT}", "MEMORYLAB_WEB_BIND": f"127.0.0.1:{WEB_PORT}",
        "DOCKER_GID": gid, "SMOKE_PROVIDER_KEY": SMOKE_KEY,
    }
    env_file = tmp / "smoke.env"
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in env.items()))
    compose = ["docker", "compose", "--env-file", str(env_file), *COMPOSE]
    admin = psycopg.connect("postgresql://postgres:postgres@127.0.0.1:54322/postgres", autocommit=True)
    secret_hdr = {"apikey": sb["SECRET_KEY"], "authorization": "Bearer " + sb["SECRET_KEY"]}
    auth = sb["API_URL"] + "/auth/v1"
    email, password = f"smoke-{uuid.uuid4().hex[:8]}@smoke.test", "smoke-" + secrets.token_hex(8)
    user_id = ""
    added_game = False
    try:
        for img in ("ingest", "egress", "orchestrator", "reaper", "accounts", "api", "web", "worker"):
            if run(["docker", "image", "inspect", f"memorylab-{img}:dev"]).returncode != 0:
                fail(f"image memorylab-{img}:dev is missing: run infra/docker/build.sh --all")
        say("booting the stack (docker compose up --wait)")
        up = run([*compose, "up", "-d", "--wait", "--wait-timeout", "300"])
        if up.returncode != 0:
            print(run([*compose, "ps", "-a"]).stdout, run([*compose, "logs", "--tail", "40"]).stdout[-4000:])
            fail("compose up: " + up.stderr[-1500:])
        say("all services healthy")

        # the published surface is the API and the web app on loopback, nothing else
        rows = [json.loads(line) for line in run([*compose, "ps", "--format", "json"]).stdout.splitlines() if line.strip()]
        published = sorted(r["Service"] for r in rows if any(p.get("PublishedPort") for p in r.get("Publishers") or []))
        if published != ["api", "web"]:
            fail(f"published services {published}, expected api and web only")
        say("only api and web publish a port")

        # every container: non-root, read-only root filesystem, no capabilities, no-new-privileges
        for row in rows:
            info = json.loads(run(["docker", "inspect", row["ID"]]).stdout)[0]
            host, user = info["HostConfig"], info["Config"].get("User", "")
            problems = [name for name, bad in (
                ("root user", user in ("", "0", "root", "0:0")), ("writable rootfs", not host["ReadonlyRootfs"]),
                ("capabilities not dropped", host["CapDrop"] != ["ALL"]),
                ("no-new-privileges missing", "no-new-privileges:true" not in (host["SecurityOpt"] or [])),
                ("privileged", host["Privileged"])) if bad]
            if problems:
                fail(f"{row['Service']}: {', '.join(problems)}")
        say(f"{len(rows)} containers are non-root, read-only, capability-free")

        if admin.execute("select 1 from public.games where name = 'fixture'").fetchone() is None:
            admin.execute("insert into public.games (name, rom_sha256, licence, state_map_version, public) values ('fixture', %s, 'CC0-1.0', '1', true)", ("f" * 64,))
            added_game = True
        r = httpx.post(f"{auth}/admin/users", headers=secret_hdr, json={"email": email, "password": password, "email_confirm": True})
        r.raise_for_status()
        user_id = r.json()["id"]

        cli_env = {**os.environ, "MEMORYLAB_API_URL": f"http://127.0.0.1:{API_PORT}", "MEMORYLAB_AUTH_URL": auth,
                   "MEMORYLAB_PUBLISHABLE_KEY": sb["PUBLISHABLE_KEY"], "MEMORYLAB_WEB_URL": f"http://localhost:{WEB_PORT}",
                   "MEMORYLAB_CONFIG_DIR": str(tmp / "cli")}
        cli_env.pop("MEMORYLAB_PROVIDER_KEY", None)

        def cli(*args: str, stdin: str | None = None, key: bool = False, retry_busy: bool = False) -> object:
            e = dict(cli_env, **({"MEMORYLAB_PROVIDER_KEY": SMOKE_KEY} if key else {}))
            p = run([str(ROOT / ".venv/bin/memorylab"), *args, "--json"], input=stdin, env=e, timeout=120)
            for _ in range(6 if retry_busy else 0):
                # the orchestrator frees a finished run's slot a moment after the row turns final: a client retries on this answer (Retry-After 5)
                if p.returncode == 0 or "rate_limited" not in p.stdout + p.stderr:
                    break
                time.sleep(5)
                p = run([str(ROOT / ".venv/bin/memorylab"), *args, "--json"], input=stdin, env=e, timeout=120)
            if p.returncode != 0:
                fail(f"memorylab {' '.join(args)} exited {p.returncode}: {p.stderr[-400:]}")
            return json.loads(p.stdout)

        cli("login", "--email", email, "--password-stdin", stdin=password + "\n")
        say("CLI login ok")
        doc = {"config": {"provider": "anthropic", "model_id": "claude-haiku-5-5", "preset_ref": "general@1", "mode": "demo",
                          "sessions": {"session_len": 2}},
               "caps": {"max_steps": 4, "max_tokens": 100000, "max_wall_s": 600}, "game": "fixture", "visibility": "private"}
        cfg_file = tmp / "run.json"
        cfg_file.write_text(json.dumps(doc))

        def wait_final(run_id: str, what: str) -> dict[str, object]:
            deadline = time.time() + 240
            while time.time() < deadline:
                shown = cli("runs", "show", run_id)
                assert isinstance(shown, dict)
                if shown.get("status") in FINAL:
                    return shown
                time.sleep(2)
            fail(f"{what} did not finish in 240 s (last status {shown.get('status')})")
            raise AssertionError

        created = cli("runs", "new", "--config", str(cfg_file), key=True)
        assert isinstance(created, dict)
        parent = str(created["run_id"])
        shown = wait_final(parent, "the scripted run")
        if shown["status"] != "capped" or shown.get("steps") != 4:
            fail(f"the run ended {shown['status']} after {shown.get('steps')} steps, expected capped after 4")
        say(f"scripted run {parent} played 4 steps in a real sandbox: {shown['status']}")

        cps = cli("runs", "checkpoints", parent)
        assert isinstance(cps, list) and cps, "no checkpoint"
        cp = next((c for c in cps if c["step"] == 2), cps[0])
        child_created = cli("fork", parent, "--checkpoint", str(cp["id"]), "--max-steps", "4", "--max-tokens", "100000",
                            "--max-wall-s", "600", key=True, retry_busy=True)
        assert isinstance(child_created, dict)
        child = str(child_created["run_id"])
        child_row = wait_final(child, "the fork")
        if child_row["status"] != "capped" or str(child_row.get("parent_checkpoint_id")) != str(cp["id"]):
            fail(f"the fork ended {child_row['status']} with parent checkpoint {child_row.get('parent_checkpoint_id')}")
        def events(run_id: str) -> list[dict[str, object]]:
            out = cli("runs", "events", run_id, "--all", "--limit", "100")
            assert isinstance(out, list)
            return out

        parent_events, child_events = events(parent), events(child)

        def last_obs(evs: list[dict[str, object]]) -> dict[str, object]:
            return [e for e in evs if e["type"] == "obs"][-1]["data"]  # type: ignore[return-value]

        start = next((e["data"] for e in child_events if e["type"] == "fork.start"), None)
        if not isinstance(start, dict) or start.get("parent_run_id") != parent or start.get("parent_step") != 2 or start.get("context_restored") is not True:
            fail(f"the fork did not record a resume from the parent's step 2: {start}")
        po, co = last_obs(parent_events), last_obs(child_events)
        if po["ram_sha256"] != co["ram_sha256"] or po["score"] != co["score"]:
            fail("an empty-patch fork must end in the parent's emulator state and score")
        say(f"fork {child} resumed checkpoint {cp['id']} and finished {child_row['status']} in the parent's exact emulator state")

        # the provider key must be nowhere: not in a container log, not in the object store
        logs = run([*compose, "logs", "--no-color"]).stdout
        if SMOKE_KEY in logs:
            fail("the provider key appears in a container log")
        vol = run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}", "--filter", "name=store"]).stdout.split()
        if not vol:
            fail("the object store volume was not found")
        hit = run(["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "-v", f"{vol[0]}:/d:ro",
                   "--label", "memorylab.owner=memorylab", "--entrypoint", "grep", "memorylab-ingest:dev", "-rl", SMOKE_KEY, "/d"])
        if hit.returncode == 0:
            fail("the provider key appears in the object store: " + hit.stdout[:200])
        say("the provider key is in no container log and not in the object store")

        left = run(["docker", "ps", "-aq", "--filter", "label=memorylab.owner=memorylab", "--filter", "name=memorylab-run"]).stdout.split()
        if left:
            fail("sandbox containers were left behind")
        say("no sandbox container left behind")

        # security headers on the real production containers: web pages (incl. the 404) and API answers (incl. a 404 and a 401)
        web, api = f"http://localhost:{WEB_PORT}", f"http://127.0.0.1:{API_PORT}"
        for url, status, want in (
                (web + "/", 200, WEB_HEADERS), (web + "/privacy", 200, WEB_HEADERS), (web + "/no-such-page", 404, WEB_HEADERS),
                (api + "/api/v1/health", 200, API_HEADERS), (api + "/api/v1/no-such-route", 404, API_HEADERS), (api + "/api/v1/me", 401, API_HEADERS)):
            resp = httpx.get(url, follow_redirects=False, timeout=15)
            if resp.status_code != status:
                fail(f"{url} answered {resp.status_code}, expected {status}")
            missing = [f"{k}={v!r}" for k, v in want.items() if v not in resp.headers.get(k, "")]
            if missing or "x-powered-by" in resp.headers or "server" in resp.headers:
                fail(f"{url}: header problems {missing}, x-powered-by/server present: {'x-powered-by' in resp.headers or 'server' in resp.headers}")
            for leak in ("Traceback", "site-packages", "node_modules", "at Object."):
                if leak in resp.text:
                    fail(f"{url}: the body contains {leak!r}")
        say("security headers present on web pages (incl. 404) and API answers (incl. 404 and 401); no stack traces")
        robots = httpx.get(web + "/robots.txt", timeout=15).text
        if "Disallow: /admin" not in robots or "Sitemap:" not in robots:
            fail("robots.txt is missing its rules")
        if "noindex" not in httpx.get(web + "/auth/signin", timeout=15).text:
            fail("the sign-in page is indexable")
        say("robots.txt present, auth pages noindex")
        say("PASS")
    except BaseException:
        print(run([*compose, "logs", "--no-color", "--tail", "25", "api", "orchestrator", "ingest"]).stdout[-6000:], file=sys.stderr)
        raise
    finally:
        down = run([*compose, "down", "-v", "--remove-orphans", "--timeout", "10"])
        stale = run(["docker", "ps", "-aq", "--filter", "label=memorylab.owner=memorylab", "--filter", "name=memorylab-run"]).stdout.split()
        if stale:
            run(["docker", "rm", "-f", *stale])
        try:
            if user_id:
                admin.execute("delete from public.runs where owner_id = %s", (user_id,))
                admin.execute("delete from public.run_configs where owner_id = %s", (user_id,))
                httpx.delete(f"{auth}/admin/users/{user_id}", headers=secret_hdr)
            if added_game:
                admin.execute("delete from public.games where name = 'fixture'")
        except Exception as exc:  # noqa: BLE001 - cleanup is best effort
            print("smoke: cleanup incomplete:", type(exc).__name__, file=sys.stderr)
        if down.returncode != 0:
            print("smoke: compose down:", down.stderr[-300:], file=sys.stderr)
        for p in tmp.rglob("*"):
            if p.is_file():
                p.unlink()


if __name__ == "__main__":
    main()
