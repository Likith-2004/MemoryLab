# ruff: noqa: F811  (imported fixtures are redefined as test parameters, which is how pytest injects them)
"""The memorylab CLI against the real API app, served over HTTP and WebSocket from a thread (uvicorn), with the real
GoTrue for sign-in, refresh and sign-out, the real accounts service for erasure, and the fake orchestrator."""

import json
import re
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import httpx
import pytest
import uvicorn
from click.testing import CliRunner, Result
from memorylab_cli.commands import watch as watch_mod
from memorylab_cli.exitcodes import EXIT_CODES
from memorylab_cli.main import cli
from runlog.eventlog import EventLogWriter

from api.signing import LocalUrlSigner
from conftest import CANARY_KEY, Env
from test_fork import source
from test_gotrue import PASSWORD, Auth, auth, real, real_factory, stack  # noqa: F401  (fixtures)

WEB_ORIGIN = "http://localhost:3000"


@dataclass
class Live:
    env: Env
    url: str
    cfg: Path
    auth: Auth
    stack: dict[str, str]
    monkeypatch: pytest.MonkeyPatch

    def run(self, *args: str, input: str | None = None, env: dict[str, str] | None = None) -> Result:
        return CliRunner().invoke(cli, list(args), input=input, env=env, catch_exceptions=False)

    def user(self, admin: bool = False) -> tuple[str, str]:
        email = self.auth.email()
        uid = self.auth.admin_create(email)
        self.env.users.append(uid)
        if admin:
            self.env.admin.execute("update public.profiles set role = 'admin' where id = %s", (uid,))
        return email, uid

    def login(self, admin: bool = False) -> tuple[str, str]:
        email, uid = self.user(admin)
        r = self.run("login", "--email", email, "--password-stdin", input=PASSWORD + "\n")
        assert r.exit_code == 0, r.output
        return email, uid

    def session_file(self) -> Path:
        return self.cfg / "session.json"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def live(real_factory: Any, stack: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
         auth: Auth) -> Iterator[Live]:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = real_factory(signer=LocalUrlSigner(url, b"s" * 40), allowed_origins=(WEB_ORIGIN,), ws_poll_s=0.05,
                       ws_heartbeat_s=0.4, read_limit_per_min=100_000, write_limit_per_min=10_000, fork_enabled=False)
    server = uvicorn.Server(uvicorn.Config(env.client.app, host="127.0.0.1", port=port, log_level="warning", ws_max_size=1024))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    cfg = tmp_path / "cfg"
    for k, v in {"MEMORYLAB_API_URL": url, "MEMORYLAB_AUTH_URL": stack["API_URL"] + "/auth/v1",
                 "MEMORYLAB_PUBLISHABLE_KEY": stack["PUBLISHABLE_KEY"], "MEMORYLAB_WEB_URL": WEB_ORIGIN,
                 "MEMORYLAB_CONFIG_DIR": str(cfg)}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(watch_mod, "RESUME_GAP_S", 0.0)
    monkeypatch.setattr(watch_mod, "POLL_INTERVAL_S", 0.05)
    yield Live(env, url, cfg, auth, stack, monkeypatch)
    server.should_exit = True
    thread.join(timeout=10)


def run_config(game: str, **over: Any) -> dict[str, Any]:
    return {"game": game, "visibility": "private",
            "config": {"provider": "anthropic", "model_id": "claude-haiku-5-5", "preset_ref": "general@1", "mode": "demo"},
            "caps": {"max_steps": 50, "max_tokens": 100000, "max_wall_s": 600}, **over}


def write_cfg(tmp: Path, doc: dict[str, Any]) -> str:
    p = tmp / f"run-{uuid.uuid4().hex[:6]}.json"
    p.write_text(json.dumps(doc))
    return str(p)


def make_run(live: Live, owner: str, visibility: str = "public", **kw: Any) -> str:
    return live.env.run(owner, visibility, **kw)


def append(live: Live, run: str, items: list[tuple[str, int, dict[str, Any]]]) -> None:
    EventLogWriter(live.env.store, run).append(items)


# ---------------------------------------------------------------- auth and the session file

def test_login_stores_a_private_session_and_me_uses_it(live: Live) -> None:
    email, uid = live.login()
    f = live.session_file()
    assert (f.stat().st_mode & 0o777, f.parent.stat().st_mode & 0o777) == (0o600, 0o700)
    stored = f.read_text()
    assert PASSWORD not in stored and email in stored
    me = json.loads(live.run("me", "--json").output)
    assert me["id"] == uid and me["role"] == "user"
    assert uid in live.run("me").output


def test_a_wrong_password_exits_3_and_stores_nothing(live: Live) -> None:
    email, _ = live.user()
    r = live.run("login", "--email", email, "--password-stdin", input="wrong-password-123\n")
    assert r.exit_code == 3 and not live.session_file().exists() and "wrong-password-123" not in r.output


def test_magic_link_login_with_the_emailed_link(live: Live) -> None:
    email, uid = live.user()
    assert live.run("login", "--email", email, "--magic-link", "--code", "000000").exit_code == 3   # a wrong code is refused
    assert not live.session_file().exists()
    seen = len(live.auth.mails_to(email))
    r = live.run("login", "--email", email, "--magic-link", input="000000\n")           # sends the mail, then prompts
    assert r.exit_code == 3 and "one-time code was sent" in r.stderr
    for _ in range(40):
        mails = live.auth.mails_to(email)
        if len(mails) > seen:
            break
        time.sleep(0.25)
    text = live.auth.http.get(f"{live.auth.mail}/api/v1/message/{mails[0]['ID']}").json()["Text"]
    link = re.search(r"https?://\S+verify\?\S+", text).group(0)         # type: ignore[union-attr]
    r = live.run("login", "--email", email, "--magic-link", "--code", link)
    assert r.exit_code == 0, r.output
    assert json.loads(live.run("me", "--json").output)["id"] == uid
    assert (live.session_file().stat().st_mode & 0o777) == 0o600


def test_logout_deletes_the_session_and_everywhere_revokes_the_refresh_token(live: Live) -> None:
    live.login()
    stored = json.loads(live.session_file().read_text())
    assert live.run("logout").exit_code == 0 and not live.session_file().exists()
    assert live.run("me").exit_code == 3
    # --everywhere: another device's refresh token dies too
    email, _ = live.login()
    other = live.auth.login(email).json()["refresh_token"]
    assert live.auth.refresh(other).status_code == 200
    other = live.auth.login(email).json()["refresh_token"]
    assert live.run("logout", "--everywhere").exit_code == 0
    assert live.auth.refresh(other).status_code in (400, 401) and stored["refresh_token"]


def test_an_expired_token_is_refreshed_and_the_command_works(live: Live) -> None:
    live.login()
    before = json.loads(live.session_file().read_text())
    data = dict(before, expires_at=time.time() - 5)
    live.session_file().write_text(json.dumps(data))
    assert live.run("me").exit_code == 0
    after = json.loads(live.session_file().read_text())
    assert after["access_token"] != before["access_token"] and after["refresh_token"] != before["refresh_token"]
    assert after["expires_at"] > time.time() + 600 and after["email"] == before["email"]
    assert (live.session_file().stat().st_mode & 0o777) == 0o600


def test_a_rejected_access_token_triggers_one_refresh_and_a_retry(live: Live) -> None:
    live.login()
    before = json.loads(live.session_file().read_text())
    live.session_file().write_text(json.dumps(dict(before, access_token="not.a.jwt")))   # looks unexpired, is rejected
    r = live.run("me", "--json")
    assert r.exit_code == 0 and json.loads(r.output)["id"] == before["user_id"]
    assert json.loads(live.session_file().read_text())["access_token"] != "not.a.jwt"


def test_a_revoked_refresh_token_asks_to_log_in_again_exit_3(live: Live) -> None:
    live.login()
    live.run("logout", "--everywhere")
    stored = {"access_token": "x.y.z", "refresh_token": "revoked-refresh-token", "expires_at": 0, "user_id": "u", "email": "e"}
    live.session_file().parent.mkdir(parents=True, exist_ok=True)
    live.session_file().write_text(json.dumps(stored))
    r = live.run("me")
    assert r.exit_code == 3 and "login" in r.output


def test_signup_goes_through_the_api_with_one_answer_for_new_and_existing_addresses(live: Live) -> None:
    existing, _ = live.user()
    new = live.auth.email()
    a = live.run("signup", "--email", new, "--password-stdin", input=PASSWORD + "\n", env={})
    b = live.run("signup", "--email", existing, "--password-stdin", input=PASSWORD + "\n")
    assert a.exit_code == b.exit_code == 0 and a.output == b.output
    live.env.admin.execute("delete from auth.users where email = %s", (new,))
    bad = live.run("signup", "--email", "x@example.test", "--password-stdin", input="short\n")
    assert bad.exit_code == 5


def test_public_commands_need_no_login(live: Live) -> None:
    live.env.game()
    for cmd in ("games", "presets", "models", "leaderboard"):
        r = live.run(cmd, "--json")
        assert r.exit_code == 0, (cmd, r.output)
        json.loads(r.output)
    assert live.run("me").exit_code == 3 and not live.session_file().exists()


def test_every_command_takes_json_and_errors_become_json_on_stderr(live: Live) -> None:
    for cmd in (["me"], ["games"], ["presets"], ["models"], ["leaderboard"], ["runs", "list"], ["experiments", "list"],
                ["admin", "runs"], ["admin", "reports"], ["logout"]):
        assert any(p.name == "as_json" for p in _leaf(cmd).params), cmd
    r = live.run("runs", "show", str(uuid.uuid4()), "--json")
    assert r.exit_code == 4
    err = json.loads(r.stderr)["error"]
    assert err["code"] == "not_found" and err["status"] == 404 and r.stdout == ""


def _leaf(path: list[str]) -> Any:
    cmd: Any = cli
    for part in path:
        cmd = cmd.commands[part]
    return cmd


# ---------------------------------------------------------------- creating runs: the key rule

def test_a_run_started_with_the_key_on_stdin_keeps_the_key_out_of_everything_the_cli_writes(live: Live, tmp_path: Path) -> None:
    live.login()
    game = live.env.game()
    r = live.run("runs", "new", "--config", write_cfg(tmp_path, run_config(game)), "--api-key-stdin", "--json", input=CANARY_KEY + "\n")
    assert r.exit_code == 0, r.output
    run_id = json.loads(r.stdout)["run_id"]
    for blob in (r.stdout, r.stderr, r.output, live.session_file().read_text()):
        assert CANARY_KEY not in blob
    assert live.env.orch.submitted[-1]["provider"]["api_key"] == CANARY_KEY and live.env.orch.submitted[-1]["run_id"] == run_id
    for table in ("runs", "run_configs", "audit_log"):
        assert CANARY_KEY not in live.env.admin.execute(f"select coalesce(string_agg(x::text, ' '), '') s from public.{table} x").fetchone()["s"]  # type: ignore[index]


def test_the_key_from_the_environment_or_the_hidden_prompt_works_and_stays_out_of_the_output(live: Live, tmp_path: Path) -> None:
    live.login()
    game = live.env.game()
    cfgfile = write_cfg(tmp_path, run_config(game))
    r = live.run("runs", "new", "--config", cfgfile, env={"MEMORYLAB_PROVIDER_KEY": CANARY_KEY})
    assert r.exit_code == 0 and CANARY_KEY not in r.output
    live.env.admin.execute("update public.runs set status = 'completed' where owner_id = %s", (json.loads(live.session_file().read_text())["user_id"],))
    r = live.run("runs", "new", "--config", cfgfile, input=CANARY_KEY + "\n")        # the hidden prompt
    assert r.exit_code == 0 and CANARY_KEY not in r.output
    assert len(live.env.orch.submitted) == 2


def test_a_rejected_key_exits_7_and_is_never_echoed(live: Live, tmp_path: Path) -> None:
    live.login()
    cfgfile = write_cfg(tmp_path, run_config(live.env.game()))
    wrong = "sk-ant-wrong-key-123456"
    r = live.run("runs", "new", "--config", cfgfile, "--api-key-stdin", input=wrong + "\n")
    assert r.exit_code == EXIT_CODES["key_rejected"] and wrong not in r.output
    rj = live.run("runs", "new", "--config", cfgfile, "--api-key-stdin", "--json", input=wrong + "\n")
    assert rj.exit_code == 7 and wrong not in rj.stdout + rj.stderr


def test_an_internal_server_error_never_leaks_the_key_through_the_cli(live: Live, tmp_path: Path) -> None:
    live.login()

    def boom(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"secret detail {CANARY_KEY}")

    live.env.orch.submit = boom  # type: ignore[method-assign]
    r = live.run("runs", "new", "--config", write_cfg(tmp_path, run_config(live.env.game())), "--api-key-stdin", input=CANARY_KEY + "\n")
    assert r.exit_code == EXIT_CODES["internal"] and CANARY_KEY not in r.output and "secret detail" not in r.output


def test_validate_only_asks_for_no_key_and_starts_nothing(live: Live, tmp_path: Path) -> None:
    live.login()
    r = live.run("runs", "new", "--config", write_cfg(tmp_path, run_config(live.env.game())), "--validate-only", "--json")
    assert r.exit_code == 0 and len(json.loads(r.output)["config_hash"]) == 64 and live.env.orch.submitted == []


def test_the_wizard_prompts_for_each_choice(live: Live) -> None:
    live.login()
    game = live.env.game()
    answers = f"{game}\nprivate\nanthropic\nclaude-haiku-5-5\ngeneral@1\ndemo\n40\n90000\n300\n{CANARY_KEY}" + "\n"
    r = live.run("runs", "new", input=answers)
    assert r.exit_code == 0, r.output
    sub = live.env.orch.submitted[-1]
    assert sub["game"] == game and sub["run_config"]["caps"]["max_steps"] == 40 and CANARY_KEY not in r.output


def test_a_wrong_choice_in_the_wizard_is_reprompted_not_crashed(live: Live) -> None:
    live.login()
    game = live.env.game()
    answers = f"no-such-game\n{game}\nprivate\nanthropic\nclaude-haiku-5-5\ngeneral@1\ndemo\n40\n90000\n300\n{CANARY_KEY}" + "\n"
    assert live.run("runs", "new", input=answers).exit_code == 0


# ---------------------------------------------------------------- run management

def test_list_show_patch_stop_delete(live: Live) -> None:
    _, uid = live.login()
    runs = [make_run(live, uid, "private") for _ in range(3)]
    listed = json.loads(live.run("runs", "list", "--limit", "2", "--all", "--json").output)
    assert {r["id"] for r in listed} == set(runs)
    shown = json.loads(live.run("runs", "show", runs[0], "--json").output)
    assert shown["id"] == runs[0] and shown["visibility"] == "private"
    r = json.loads(live.run("runs", "patch", runs[0], "--title", "my run", "--visibility", "unlisted", "--json").output)
    assert (r["title"], r["visibility"]) == ("my run", "unlisted")
    assert live.run("runs", "patch", runs[0]).exit_code == 2
    assert json.loads(live.run("runs", "stop", runs[1], "--json").output)["result"] == "stopping"
    assert live.run("runs", "delete", runs[2], "--yes").exit_code == 0
    assert live.run("runs", "show", runs[2]).exit_code == 4
    assert live.run("runs", "delete", runs[1], input="n\n").exit_code != 0            # declined: nothing deleted
    assert live.run("runs", "show", runs[1]).exit_code == 0


def test_someone_elses_private_run_is_not_found_exit_4(live: Live) -> None:
    live.login()
    owner = live.env.user()
    run = make_run(live, owner, "private")
    for args in (["runs", "show", run], ["runs", "stop", run], ["runs", "delete", run, "--yes"], ["runs", "events", run]):
        assert live.run(*args).exit_code == 4, args


def test_a_public_run_can_be_read_without_logging_in(live: Live) -> None:
    owner = live.env.user()
    run = make_run(live, owner, "public")
    assert live.run("runs", "show", run).exit_code == 0 and live.run("runs", "events", run).exit_code == 0


# ---------------------------------------------------------------- reading a run

def test_events_memory_checkpoints_score(live: Live) -> None:
    _, uid = live.login()
    run = make_run(live, uid, "private")
    append(live, run, [("run.state", 0, {"state": "queued"}), ("obs", 1, {"score": 3})])
    live.env.admin.execute("insert into public.memory_files values (%s, 'a.md', 1, 0, 'one\n', 4, false)", (run,))
    live.env.admin.execute("insert into public.memory_files values (%s, 'a.md', 2, 1, 'one\ntwo\n', 8, false)", (run,))
    live.env.admin.execute("insert into public.checkpoints (run_id, step, savestate_key, memory_version, transcript_offset, config_hash) "
                           "values (%s, 1, 'k', 2, 0, %s)", (run, "b" * 64))
    live.env.admin.execute("insert into public.run_scores values (%s, 3, 1, 1, 1, 1, 0, 0, false)", (run,))
    ev = json.loads(live.run("runs", "events", run, "--json").output)
    assert [e["seq"] for e in ev] == [1, 2]
    assert json.loads(live.run("runs", "events", run, "--after-seq", "1", "--json").output)[0]["seq"] == 2
    assert json.loads(live.run("runs", "memory", "ls", run, "--json").output)[0]["version"] == 2
    assert json.loads(live.run("runs", "memory", "ls", run, "--at-step", "0", "--json").output)[0]["version"] == 1
    assert live.run("runs", "memory", "cat", run, "a.md").output == "one\ntwo\n"
    assert live.run("runs", "memory", "cat", run, "a.md", "--version", "1").output == "one\n"
    d = live.run("runs", "memory", "diff", run, "a.md", "--from", "1", "--to", "2").output
    assert "+two" in d
    assert len(json.loads(live.run("runs", "checkpoints", run, "--json").output)) == 1
    assert json.loads(live.run("runs", "score", run, "--json").output)["progress_score"] == 3
    assert live.run("runs", "memory", "cat", run, "missing.md").exit_code == 4


def test_server_text_cannot_inject_terminal_escape_sequences(live: Live) -> None:
    _, uid = live.login()
    evil = "\x1b[2J\x1b]0;pwned\x07\x9b31mred"
    run = make_run(live, uid, "private", title=evil[:100])
    append(live, run, [("run.state", 0, {"state": "queued"}), ("model.summary", 1, {"text": evil})])
    live.env.admin.execute("insert into public.memory_files values (%s, 'a.md', 1, 0, %s, 4, false)", (run, evil))
    for args in (["runs", "memory", "cat", run, "a.md"], ["runs", "events", run], ["runs", "list"], ["runs", "show", run]):
        out = live.run(*args).output
        assert "\x1b" not in out and "\x07" not in out and "\x9b" not in out, args
    assert "\\x1b" in live.run("runs", "memory", "cat", run, "a.md").output


def test_the_frame_is_downloaded_and_checked_to_be_a_png(live: Live, tmp_path: Path) -> None:
    _, uid = live.login()
    run = make_run(live, uid, "private")
    png = b"\x89PNG\r\n\x1a\nframe-bytes"
    live.env.store.put(f"runs/{run}/frames/0000010.png", png)
    out = tmp_path / "f.png"
    r = live.run("runs", "frame", run, "15", "-o", str(out), "--json")
    assert r.exit_code == 0 and out.read_bytes() == png and json.loads(r.output)["frame_step"] == "10"
    assert live.run("runs", "frame", run, "3").exit_code == 4                          # nothing at or before step 3
    live.env.store.put(f"runs/{run}/frames/0000020.png", b"not a png at all", overwrite=True)
    bad = tmp_path / "bad.png"
    assert live.run("runs", "frame", run, "25", "-o", str(bad)).exit_code == EXIT_CODES["internal"] and not bad.exists()


def seal_bundle(live: Live, run: str, status: str = "completed") -> bytes:
    from test_blobs import make_bundle, seal
    return seal(live.env, run, make_bundle(), status=status)


def test_export_downloads_and_verifies_the_sha256(live: Live, tmp_path: Path) -> None:
    _, uid = live.login()
    run = make_run(live, uid, "private")
    blob = seal_bundle(live, run)
    out = tmp_path / "bundle.zip"
    r = live.run("runs", "export", run, "-o", str(out), "--json")
    assert r.exit_code == 0 and out.read_bytes() == blob and json.loads(r.output)["verified"] is True


def test_a_tampered_download_is_discarded_with_a_non_zero_exit(live: Live, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from memorylab_cli.api import Api
    _, uid = live.login()
    run = make_run(live, uid, "private")
    blob = seal_bundle(live, run)
    monkeypatch.setattr(Api, "download", lambda self, url: blob + b"tampered")
    out = tmp_path / "bundle.zip"
    r = live.run("runs", "export", run, "-o", str(out))
    assert r.exit_code == EXIT_CODES["sha_mismatch"] and not out.exists() and not list(tmp_path.glob(".*part"))
    monkeypatch.setattr(Api, "download", lambda self, url: b"definitely not a zip")
    assert live.run("runs", "export", run, "-o", str(out)).exit_code == 15


def test_an_unfinished_run_cannot_be_exported_exit_11(live: Live) -> None:
    _, uid = live.login()
    run = make_run(live, uid, "private", status="running")
    assert live.run("runs", "export", run).exit_code == EXIT_CODES["export_not_ready"]


def test_the_server_refusing_a_dirty_bundle_exits_11(live: Live) -> None:
    from test_blobs import make_bundle, seal
    _, uid = live.login()
    run = make_run(live, uid, "private")
    seal(live.env, run, make_bundle({"events.jsonl": b"sk-ant-api03-" + b"A" * 40}))
    assert live.run("runs", "export", run).exit_code == EXIT_CODES["export_refused"]


# ---------------------------------------------------------------- watching

def final(state: str, reason: str | None = None) -> tuple[str, int, dict[str, Any]]:
    return ("run.state", 0, {"state": state, **({"reason": reason} if reason else {})})


def test_watch_replays_the_log_over_the_websocket_and_exits_0_when_the_run_completes(live: Live) -> None:
    _, uid = live.login()
    run = make_run(live, uid, "private", status="running")
    append(live, run, [("run.state", 0, {"state": "queued"}), ("obs", 1, {"score": 1}), ("obs", 2, {"score": 2}), final("completed")])
    r = live.run("runs", "watch", run, "--json")
    assert r.exit_code == 0, r.output + r.stderr
    events = [json.loads(line) for line in r.stdout.splitlines()]
    assert [e["seq"] for e in events] == [1, 2, 3, 4] and events[-1]["data"]["state"] == "completed"


@pytest.mark.parametrize("state,code", [("completed", 0), ("capped", 0), ("failed", 20), ("killed", 21)])
def test_watch_exit_codes_follow_the_final_state(live: Live, state: str, code: int) -> None:
    _, uid = live.login()
    run = make_run(live, uid, "private")
    append(live, run, [("run.state", 0, {"state": "queued"}), final(state, "why")])
    assert live.run("runs", "watch", run).exit_code == code


def test_watch_follows_a_live_run_as_events_arrive(live: Live) -> None:
    _, uid = live.login()
    run = make_run(live, uid, "private", status="running")
    append(live, run, [("run.state", 0, {"state": "queued"})])

    def feed() -> None:
        for i in range(1, 4):
            time.sleep(0.4)
            append(live, run, [("obs", i, {"score": i})])
        time.sleep(0.4)
        append(live, run, [final("completed")])

    t = threading.Thread(target=feed)
    t.start()
    r = live.run("runs", "watch", run, "--json")
    t.join()
    assert r.exit_code == 0 and [json.loads(x)["seq"] for x in r.stdout.splitlines()] == [1, 2, 3, 4, 5]


def test_a_dropped_socket_resumes_with_a_new_ticket_from_the_last_seq_without_repeats(live: Live, monkeypatch: pytest.MonkeyPatch) -> None:
    from websockets.exceptions import ConnectionClosedError
    _, uid = live.login()
    run = make_run(live, uid, "private", status="running")
    append(live, run, [("run.state", 0, {"state": "queued"}), ("obs", 1, {"score": 1}), ("obs", 2, {"score": 2}),
                       ("obs", 3, {"score": 3}), final("completed")])
    real_connect = watch_mod.connect
    connects: list[str] = []

    class Flaky:
        def __init__(self, inner: Any, drop_after: int | None) -> None:
            self.inner, self.drop_after, self.n = inner, drop_after, 0

        def __enter__(self) -> Self:
            self.inner.__enter__()
            return self

        def __exit__(self, *a: object) -> Any:
            return self.inner.__exit__(*a)

        def send(self, data: str) -> None:
            connects.append("resume:" + data)
            self.inner.send(data)

        def recv(self, timeout: float | None = None) -> Any:
            msg = self.inner.recv(timeout=timeout)
            if json.loads(msg).get("t") == "event":
                self.n += 1
                if self.drop_after is not None and self.n > self.drop_after:
                    raise ConnectionClosedError(None, None)
            return msg

    def connect(url: str, **kw: Any) -> Flaky:
        connects.append("connect")
        return Flaky(real_connect(url, **kw), 2 if connects.count("connect") == 1 else None)

    monkeypatch.setattr(watch_mod, "connect", connect)
    r = live.run("runs", "watch", run, "--json")
    assert r.exit_code == 0, r.output
    assert [json.loads(x)["seq"] for x in r.stdout.splitlines()] == [1, 2, 3, 4, 5]          # nothing missing, nothing twice
    assert connects.count("connect") == 2 and any(c.startswith("resume:") and '"after_seq": 2' in c for c in connects)
    tickets = live.env.admin.execute("select count(*) n from public.stream_tickets where run_id = %s", (run,)).fetchone()["n"]
    assert tickets == 0                                                                        # both tickets were consumed


def test_after_repeated_failures_watch_falls_back_to_polling_events(live: Live, monkeypatch: pytest.MonkeyPatch) -> None:
    _, uid = live.login()
    run = make_run(live, uid, "private", status="running")
    append(live, run, [("run.state", 0, {"state": "queued"}), ("obs", 1, {"score": 1})])
    attempts: list[int] = []

    def refuse(url: str, **kw: Any) -> Any:
        attempts.append(1)
        raise OSError("websocket blocked by a proxy")

    monkeypatch.setattr(watch_mod, "connect", refuse)

    def feed() -> None:
        time.sleep(0.5)
        append(live, run, [("obs", 2, {"score": 2}), final("failed", "worker_exit_1")])

    t = threading.Thread(target=feed)
    t.start()
    r = live.run("runs", "watch", run, "--json")
    t.join()
    assert r.exit_code == EXIT_CODES["run_failed"] and len(attempts) == watch_mod.MAX_WS_FAILURES
    assert [json.loads(x)["seq"] for x in r.stdout.splitlines()] == [1, 2, 3, 4]
    assert "falling back to polling" in r.stderr


def test_poll_mode_never_opens_a_websocket_and_from_now_skips_the_backlog(live: Live, monkeypatch: pytest.MonkeyPatch) -> None:
    _, uid = live.login()
    run = make_run(live, uid, "private", status="running")
    append(live, run, [("run.state", 0, {"state": "queued"}), ("obs", 1, {"score": 1})])
    monkeypatch.setattr(watch_mod, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no websocket in poll mode")))
    t = threading.Timer(0.4, lambda: append(live, run, [final("completed")]))
    t.start()
    r = live.run("runs", "watch", run, "--poll", "--from-now", "--json")
    t.join()
    assert r.exit_code == 0 and [json.loads(x)["seq"] for x in r.stdout.splitlines()] == [3]


def test_watching_a_run_you_cannot_see_is_not_found_exit_4(live: Live) -> None:
    live.login()
    run = make_run(live, live.env.user(), "private")
    assert live.run("runs", "watch", run).exit_code == 4


def test_watching_an_anonymous_public_run_works_without_a_login(live: Live) -> None:
    run = make_run(live, live.env.user(), "public")
    append(live, run, [("run.state", 0, {"state": "queued"}), final("completed")])
    assert live.run("runs", "watch", run).exit_code == 0


# ---------------------------------------------------------------- fork

def test_fork_respects_fork_enabled_with_its_own_exit_code(live: Live) -> None:
    _, uid = live.login()
    run, cp = source(live.env, uid, live.env.game())
    r = live.run("fork", run, "--checkpoint", cp or "", "--set", 'preset_ref="none@1"', "--max-steps", "50", "--max-tokens",
                 "100000", "--max-wall-s", "600", "--api-key-stdin", input=CANARY_KEY + "\n")
    assert r.exit_code == EXIT_CODES["fork_disabled"] and "not available" in r.output and CANARY_KEY not in r.output


def test_fork_creates_a_run_when_the_server_allows_it(real_factory: Any, stack: dict[str, str], tmp_path: Path, auth: Auth,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = real_factory(signer=LocalUrlSigner(url, b"s" * 40), fork_enabled=True, read_limit_per_min=100_000)
    server = uvicorn.Server(uvicorn.Config(env.client.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    try:
        for k, v in {"MEMORYLAB_API_URL": url, "MEMORYLAB_AUTH_URL": stack["API_URL"] + "/auth/v1",
                     "MEMORYLAB_PUBLISHABLE_KEY": stack["PUBLISHABLE_KEY"], "MEMORYLAB_CONFIG_DIR": str(tmp_path / "c")}.items():
            monkeypatch.setenv(k, v)
        live = Live(env, url, tmp_path / "c", auth, stack, monkeypatch)
        _, uid = live.login()
        run, cp = source(env, uid, env.game())
        r = live.run("fork", run, "--checkpoint", cp or "", "--set", 'preset_ref="none@1"', "--max-steps", "50", "--max-tokens",
                     "100000", "--max-wall-s", "600", "--api-key-stdin", "--json", input=CANARY_KEY + "\n")
        assert r.exit_code == 0, r.output
        assert env.orch.submitted[-1]["parent"]["checkpoint_id"] == cp and CANARY_KEY not in r.output
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ---------------------------------------------------------------- experiments, admin, account

def test_experiments_new_show_compare_add_remove(live: Live) -> None:
    from test_experiments import arm
    _, uid = live.login()
    game = live.env.game()
    a, b, c = (arm(live.env, uid, game, p, score=s) for p, s in (("general@1", 10), ("none@1", 20), ("failures_only@1", 30)))
    append(live, a, [("obs", 0, {"score": 1}), ("obs", 2, {"score": 3})])
    new = json.loads(live.run("experiments", "new", "--title", "arms", "--variable", "preset_ref", "--run", a, "--run", b, "--json").output)
    eid = new["id"]
    shown = live.run("experiments", "show", eid)
    assert shown.exit_code == 0 and "fewer than 5 scored runs: this is not a finding" in shown.output
    j = json.loads(live.run("experiments", "show", eid, "--json").output)
    assert j["summary"]["n"] == 2 and j["summary"]["mean"] == 15.0
    cmp = json.loads(live.run("experiments", "compare", eid, "--json").output)
    assert cmp["steps"] == [0, 2] and live.run("experiments", "compare", eid).exit_code == 0
    assert len(json.loads(live.run("experiments", "add", eid, c, "--json").output)["runs"]) == 3
    assert len(json.loads(live.run("experiments", "remove", eid, a, "--json").output)["runs"]) == 2
    assert [e["id"] for e in json.loads(live.run("experiments", "list", "--json").output)] == [eid]
    foreign = arm(live.env, live.env.user(), game, "failures_only@1")
    assert live.run("experiments", "add", eid, foreign).exit_code == EXIT_CODES["invalid_runs"]
    assert live.run("experiments", "new", "--title", "x", "--variable", "banana", "--run", a).exit_code == 5


def test_admin_commands_are_404_for_users_and_work_for_admins(live: Live) -> None:
    live.login()
    for args in (["admin", "runs"], ["admin", "reports"], ["admin", "breaker", "--open", "--reason", "x"]):
        assert live.run(*args).exit_code == 4, args
    live.run("logout")
    _, admin = live.login(admin=True)
    owner = live.env.user()
    run = make_run(live, owner, "public")
    rid = str(live.env.admin.execute("insert into public.reports (reporter_id, run_id, reason) values (%s, %s, 'spam') returning id", (owner, run)).fetchone()["id"])  # type: ignore[index]
    assert live.run("admin", "runs", "--json").exit_code == 0
    reports = json.loads(live.run("admin", "reports", "--json").output)["reports"]
    assert rid in {r["id"] for r in reports}
    assert live.run("admin", "resolve", rid, "--status", "actioned").exit_code == 2                     # --reason is required
    assert live.run("admin", "resolve", rid, "--status", "actioned", "--reason", "removed").exit_code == 0
    assert live.run("admin", "resolve", rid, "--status", "dismissed", "--reason", "again").exit_code == EXIT_CODES["report_closed"]
    live.env.admin.execute("insert into public.run_scores values (%s, 5, 1, 1, 1, 1, 0, 0, true)", (run,))
    assert live.run("admin", "unverify", run, "--reason", "cheating").exit_code == 0
    assert live.run("admin", "unverify", run, "--reason", "again").exit_code == EXIT_CODES["not_verified"]
    assert live.run("admin", "kill", run, "--reason", "abuse").exit_code == 0
    assert live.run("admin", "breaker", "--open", "--reason", "outage").exit_code == 0
    assert live.run("admin", "breaker", "--close", "--reason", "back").exit_code == 0
    actions = [r["action"] for r in live.env.admin.execute("select action from public.audit_log where actor_id = %s order by id", (admin,)).fetchall()]
    assert actions == ["report.actioned", "leaderboard.unverify", "run.kill", "breaker.open", "breaker.close"]


def test_delete_account_needs_the_confirmation_and_clears_the_session(live: Live) -> None:
    email, uid = live.login()
    run = make_run(live, uid, "private")
    r = live.run("delete-account", "--confirm", "nope")
    assert r.exit_code == 5 and live.env.admin.execute("select count(*) n from public.profiles where id = %s", (uid,)).fetchone()["n"] == 1  # type: ignore[index]
    assert live.run("delete-account", input="nope\n").exit_code == 5
    r = live.run("delete-account", "--confirm", "DELETE")
    assert r.exit_code == 0, r.output
    assert not live.session_file().exists()
    assert live.env.admin.execute("select count(*) n from public.profiles where id = %s", (uid,)).fetchone()["n"] == 0  # type: ignore[index]
    assert live.auth.login(email).status_code == 400
    assert live.run("me").exit_code == 3 and run
    live.env.admin.execute("delete from public.runs where id = %s", (run,))


def test_a_dead_server_is_exit_14(live: Live, monkeypatch: pytest.MonkeyPatch) -> None:
    live.login()
    monkeypatch.setenv("MEMORYLAB_API_URL", "http://127.0.0.1:1")
    r = live.run("me")
    assert r.exit_code == EXIT_CODES["network"]
    assert json.loads(live.run("me", "--json").stderr)["error"]["code"] == "network"


def test_the_cli_never_talks_to_the_database_directly(live: Live) -> None:
    """Whatever the CLI does, it does as the signed-in user over HTTP: another user's data stays out of reach."""
    live.login()
    other = live.env.user()
    run = make_run(live, other, "private")
    assert live.run("runs", "list", "--json", "--all").output.count(run) == 0
    assert live.run("runs", "memory", "ls", run).exit_code == 4
    assert httpx.get(live.url + "/api/v1/health").status_code == 200


def test_a_write_is_committed_before_the_response_reaches_the_client(live: Live, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI that asks for a stream ticket and connects at once must find it: the transaction commits before the response
    is sent, not after. A slow commit makes the race deterministic."""
    from contextlib import contextmanager

    from api.db import Database
    email, uid = live.login()
    run = make_run(live, uid, "private")
    original = Database.session

    @contextmanager
    def slow_commit(self: Any, user: Any) -> Iterator[Any]:
        with original(self, user) as conn:
            yield conn
            time.sleep(0.6)                                  # the work after the handler, before the commit

    monkeypatch.setattr(Database, "session", slow_commit)
    token = json.loads(live.session_file().read_text())["access_token"]
    r = httpx.post(f"{live.url}/api/v1/runs/{run}/stream-ticket", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200
    seen = live.env.admin.execute("select count(*) n from public.stream_tickets where run_id = %s", (run,)).fetchone()["n"]  # type: ignore[index]
    assert seen == 1 and email
    p = httpx.patch(f"{live.url}/api/v1/runs/{run}", json={"title": "fresh"}, headers={"authorization": f"Bearer {token}"})
    assert p.status_code == 200
    assert live.env.admin.execute("select title from public.runs where id = %s", (run,)).fetchone()["title"] == "fresh"  # type: ignore[index]
