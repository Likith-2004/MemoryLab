"""CLI units that need no server: exit codes, secrets, session file, terminal safety, the API client's refresh logic."""

import json
import os
import pickle
import stat
import time
from pathlib import Path
from typing import Any

import click
import httpx
import pytest
from click.testing import CliRunner

from memorylab_cli.api import Api, CliError
from memorylab_cli.config import Config, from_env
from memorylab_cli.exitcodes import EXIT_CODES, exit_code_for
from memorylab_cli.main import cli
from memorylab_cli.render import safe
from memorylab_cli.secrets import Secret, read_secret
from memorylab_cli.session import Session, SessionStore

CANARY = "sk-ant-CANARY-0123456789abcdef"


def test_the_exit_code_table_is_pinned() -> None:
    assert EXIT_CODES == {
        "unauthenticated": 3, "not_logged_in": 3, "not_found": 4, "validation_failed": 5, "caps_required": 5,
        "invalid_runs": 5, "variable_mismatch": 5, "rate_limited": 6, "key_rejected": 7, "benchmark_patch_invalid": 8,
        "capacity_full": 9, "breaker_open": 10, "export_not_ready": 11, "export_refused": 11, "runs_still_active": 12,
        "report_closed": 12, "not_verified": 12, "limit_reached": 12, "internal": 13, "network": 14, "sha_mismatch": 15,
        "fork_disabled": 16, "config": 17, "payload_too_large": 5, "run_failed": 20, "run_killed": 21}
    assert exit_code_for("something_new") == 1 and len(set(EXIT_CODES.values())) >= 12


def test_a_secret_never_shows_itself() -> None:
    s = Secret(CANARY)
    assert CANARY not in repr(s) and CANARY not in str(s) and CANARY not in f"{s!r} {s}" and CANARY not in f"{[s]}"
    with pytest.raises(TypeError):
        pickle.dumps(s)
    import copy
    with pytest.raises(TypeError):
        copy.deepcopy(s)
    assert s.reveal() == CANARY


def test_the_key_comes_from_stdin_the_environment_or_a_hidden_prompt_in_that_order(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    env_value, stdin_value = "from-env-" + "123456", "from-stdin-" + "123456"
    monkeypatch.setenv("MEMORYLAB_PROVIDER_KEY", env_value)
    assert read_secret("key", from_stdin=True, stdin=io.StringIO(stdin_value + "\n")).reveal() == stdin_value
    assert read_secret("key", from_stdin=False).reveal() == env_value
    assert "MEMORYLAB_PROVIDER_KEY" not in os.environ                    # consumed: child processes do not inherit it
    with pytest.raises(click.UsageError):
        read_secret("key", from_stdin=True, stdin=io.StringIO("\n"))


def test_there_is_no_way_to_pass_the_key_as_an_argument() -> None:
    r = CliRunner().invoke(cli, ["runs", "new", "--api-key", CANARY, "--game", "x"])
    assert r.exit_code == 2 and CANARY not in r.output
    for group in (["runs", "new"], ["fork", "r", "--checkpoint", "c", "--max-steps", "1", "--max-tokens", "1", "--max-wall-s", "1"]):
        r = CliRunner().invoke(cli, [*group, f"--api-key={CANARY}"])
        assert r.exit_code == 2 and CANARY not in r.output
    for name, cmd in (("runs new", cli.commands["runs"].commands["new"]), ("fork", cli.commands["fork"])):  # type: ignore[attr-defined]
        assert not any("api-key" == o.lstrip("-") or o in ("--key", "--provider-key") for p in cmd.params for o in p.opts), name


def test_a_config_file_with_a_key_in_it_is_refused_without_echoing_it(tmp_path: Path) -> None:
    for doc in ({"api_key": CANARY}, {"config": {"apiKey": CANARY}}, {"provider": {"key": CANARY}}, {"x": [{"API_KEY": CANARY}]}):
        f = tmp_path / "c.json"
        f.write_text(json.dumps({"game": "g", **doc}))
        r = CliRunner().invoke(cli, ["runs", "new", "--config", str(f)])
        assert r.exit_code == EXIT_CODES["config"] and CANARY not in r.output and "key field" in r.output
    bad = tmp_path / "bad.json"
    bad.write_text("not json " + CANARY)
    r = CliRunner().invoke(cli, ["runs", "new", "--config", str(bad)])
    assert r.exit_code == EXIT_CODES["config"] and CANARY not in r.output


def test_the_session_file_is_private_and_atomic(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "cfg" / "session.json")
    store.save(Session("a", "r", time.time() + 900, "u", "e@x.test"))
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600 and stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    loose = tmp_path / "cfg"
    os.chmod(loose, 0o755)
    store.save(Session("a2", "r2", time.time() + 900, "u", "e@x.test"))        # tightened again on every write
    assert stat.S_IMODE(loose.stat().st_mode) == 0o700 and store.load().access_token == "a2"  # type: ignore[union-attr]
    assert [p.name for p in loose.iterdir()] == ["session.json"]                # no temp files left behind
    store.path.write_text("{broken")
    assert store.load() is None
    store.clear()
    store.clear()


@pytest.mark.parametrize("text", ["\x1b[2J\x1b[Hhacked", "\x1b]0;title\x07", "a\x9bb", "x\u202ey", "a\x00b", "\r\x08\x08ok", "\x7f"])
def test_terminal_control_sequences_are_neutralised(text: str) -> None:
    out = safe(text)
    assert not any(ord(c) < 32 and c not in "\n\t" or 0x7F <= ord(c) <= 0x9F or c in "\u202a\u202b\u202c\u202d\u202e" for c in out)
    assert safe("plain text\nline two\ttab") == "plain text\nline two\ttab" and safe(None) == "" and safe(7) == "7"


def test_config_defaults_and_overrides(tmp_path: Path) -> None:
    c = from_env({"MEMORYLAB_API_URL": "https://api.example.org/", "MEMORYLAB_CONFIG_DIR": str(tmp_path)})
    assert c.api_base == "https://api.example.org/api/v1" and c.ws_base == "wss://api.example.org"
    assert from_env({}).ws_base == "ws://127.0.0.1:8000" and from_env({}).session_path.name == "session.json"


# ---------------------------------------------------------------- the client: refresh logic against a stub server

def make_api(tmp_path: Path, handler: Any, now: list[float]) -> tuple[Api, SessionStore]:
    cfg = Config("http://api.test", "http://auth.test/auth/v1", "pk-public", "http://web.test", tmp_path)
    store = SessionStore(cfg.session_path)
    return Api(cfg, store, httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: now[0]), store


def test_an_expiring_token_is_refreshed_before_the_request_and_the_new_refresh_token_is_kept(tmp_path: Path) -> None:
    now, seen = [1000.0], []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers.get("authorization")))
        if req.url.path.endswith("/token"):
            assert json.loads(req.content) == {"refresh_token": "r1"} and req.headers["apikey"] == "pk-public"
            return httpx.Response(200, json={"access_token": "a2", "refresh_token": "r2", "expires_in": 900, "user": {"id": "u"}})
        return httpx.Response(200, json={"ok": True})

    api, store = make_api(tmp_path, handler, now)
    store.save(Session("a1", "r1", 1030.0, "u", "e@x.test"))                 # 30 s left: inside the 60 s skew
    assert api.get("/me") == {"ok": True}
    assert seen[-1] == ("/api/v1/me", "Bearer a2")
    saved = store.load()
    assert saved is not None and (saved.access_token, saved.refresh_token, saved.email) == ("a2", "r2", "e@x.test")


def test_a_401_triggers_one_refresh_and_one_retry(tmp_path: Path) -> None:
    now, calls = [1000.0], []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "good", "refresh_token": "r2", "expires_in": 900})
        if req.headers["authorization"] == "Bearer good":
            return httpx.Response(200, json={"v": 1})
        return httpx.Response(401, json={"code": "unauthenticated", "title": "Unauthenticated"})

    api, store = make_api(tmp_path, handler, now)
    store.save(Session("stale", "r1", 99999.0, "u", "e"))
    assert api.get("/me") == {"v": 1}
    assert calls == ["/api/v1/me", "/auth/v1/token", "/api/v1/me"]


def test_a_401_after_the_refresh_is_reported_not_retried_forever(tmp_path: Path) -> None:
    now, calls = [1000.0], []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "still-bad", "refresh_token": "r2", "expires_in": 900})
        return httpx.Response(401, json={"code": "unauthenticated", "title": "Unauthenticated"})

    api, store = make_api(tmp_path, handler, now)
    store.save(Session("x", "r1", 99999.0, "u", "e"))
    with pytest.raises(CliError) as info:
        api.get("/me")
    assert info.value.code == "unauthenticated" and calls.count("/auth/v1/token") == 1 and len(calls) == 3


def test_a_dead_refresh_token_says_to_log_in_again(tmp_path: Path) -> None:
    api, store = make_api(tmp_path, lambda r: httpx.Response(400, json={"error_code": "refresh_token_not_found"}), [1000.0])
    store.save(Session("x", "dead", 0.0, "u", "e"))
    with pytest.raises(CliError) as info:
        api.get("/me")
    assert info.value.code == "not_logged_in" and info.value.exit_code == 3 and "login" in info.value.title


def test_no_session_means_not_logged_in_and_public_calls_send_no_credentials(tmp_path: Path) -> None:
    seen: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("authorization"))
        return httpx.Response(200, json=[])

    api, _ = make_api(tmp_path, handler, [1.0])
    with pytest.raises(CliError) as info:
        api.get("/me")
    assert info.value.code == "not_logged_in" and seen == []
    assert api.get("/games", auth=False) == [] and seen == [None]


def test_error_bodies_map_to_stable_codes_without_echoing_the_request(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else {}
        assert "api_key" in body
        return httpx.Response(422, json={"code": "key_rejected", "title": "The key was not accepted", "status": 422},
                              headers={"retry-after": "30"})

    api, store = make_api(tmp_path, handler, [1.0])
    store.save(Session("a", "r", 99999.0, "u", "e"))
    with pytest.raises(CliError) as info:
        api.post("/runs", {"api_key": CANARY})
    e = info.value
    assert (e.code, e.exit_code, e.status, e.retry_after) == ("key_rejected", 7, 422, "30") and CANARY not in str(e) + repr(e)


def test_a_non_json_error_and_a_dead_server_are_reported_cleanly(tmp_path: Path) -> None:
    api, store = make_api(tmp_path, lambda r: httpx.Response(502, text="<html>bad gateway</html>"), [1.0])
    store.save(Session("a", "r", 99999.0, "u", "e"))
    with pytest.raises(CliError) as info:
        api.get("/me")
    assert info.value.code == "internal" and "<html>" not in info.value.title

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {CANARY}")

    api2, store2 = make_api(tmp_path, boom, [1.0])
    store2.save(Session("a", "r", 99999.0, "u", "e"))
    with pytest.raises(CliError) as info2:
        api2.get("/me")
    assert info2.value.code == "network" and CANARY not in info2.value.title


def test_downloads_carry_no_credentials(tmp_path: Path) -> None:
    seen: list[dict[str, str]] = []
    api, store = make_api(tmp_path, lambda r: (seen.append(dict(r.headers)), httpx.Response(200, content=b"x"))[1], [1.0])
    store.save(Session("secret-token", "r", 99999.0, "u", "e"))
    assert api.download("http://files.test/x?sig=1") == b"x"
    assert "authorization" not in seen[0] and "apikey" not in seen[0]


def test_an_unexpected_exception_prints_only_its_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from memorylab_cli.commands import catalogue

    def explode(*a: Any, **k: Any) -> Any:
        raise RuntimeError(f"internal detail {CANARY}")

    monkeypatch.setattr(catalogue, "build", explode)
    r = CliRunner().invoke(cli, ["games"])
    assert r.exit_code == 1 and CANARY not in r.output and "RuntimeError" in r.output


def test_the_cli_has_no_database_or_service_key_code() -> None:
    import memorylab_cli
    root = Path(memorylab_cli.__file__).parent
    for path in root.rglob("*.py"):
        text = path.read_text().lower()
        assert "psycopg" not in text and "service_role" not in text and "secret_key" not in text and "service_key" not in text, path.name


def test_verify_accepts_a_six_digit_code_or_the_pasted_link(tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 900, "user": {"id": "u", "email": "e@x.test"}})

    api, store = make_api(tmp_path, handler, [1000.0])
    api.verify_code("e@x.test", " 123456 ")
    api.verify_code("e@x.test", "http://auth/auth/v1/verify?token=abc123&type=magiclink&redirect_to=http://x")
    assert sent == [{"type": "email", "email": "e@x.test", "token": "123456"},
                    {"type": "magiclink", "token_hash": "abc123"}]
    assert store.load() is not None
