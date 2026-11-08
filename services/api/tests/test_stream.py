"""Stream tickets and the live WebSocket."""

import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from runlog.eventlog import EventLogWriter
from starlette.websockets import WebSocketDisconnect

from conftest import Env, bearer
from test_authz_matrix import hdr, seed

ORIGIN = "https://app.test"


def env_ws(make_env: Any, **kw: Any) -> Env:
    env: Env = make_env(allowed_origins=(ORIGIN,), ws_poll_s=0.03, ws_heartbeat_s=0.15, read_limit_per_min=10_000, **kw)
    return env


def ticket(env: Env, run: str, who: str | None = None) -> str:
    r = env.client.post(f"/api/v1/runs/{run}/stream-ticket", headers=bearer(who) if who else {})
    assert r.status_code == 200, r.text
    return str(r.json()["ticket"])


def connect(env: Env, run: str, tkt: str, origin: str | None = ORIGIN) -> Any:
    headers = {} if origin is None else {"origin": origin}
    return env.client.websocket_connect(f"/ws/runs/{run}?ticket={tkt}", headers=headers)


def rejected(env: Env, run: str, tkt: str, origin: str | None = ORIGIN) -> int:
    """Close code of a connection refused at the handshake. A socket that was accepted and closed afterwards does not
    count: admission itself has to refuse."""
    cm = connect(env, run, tkt, origin)
    try:
        cm.__enter__()
    except WebSocketDisconnect as exc:
        return int(exc.code)
    cm.__exit__(None, None, None)
    raise AssertionError("the socket was accepted")


def events(env: Env, run: str, n: int, start: int = 1) -> None:
    w = EventLogWriter(env.store, run)
    w.append([("run.state", 0, {"state": "queued"})] if start == 1 else [])
    for i in range(start, start + n - (1 if start == 1 else 0)):
        w.append([("obs", i, {"score": i})])


def next_event(ws: Any, limit: int = 100) -> dict[str, Any]:
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["t"] in ("event", "frame"):
            return dict(msg)
    raise AssertionError("no event")


# ---------------------------------------------------------------- tickets

def test_a_ticket_is_32_random_bytes_stored_only_as_a_sha256(env: Env) -> None:
    ids = seed(env, "public")
    r = env.client.post(f"/api/v1/runs/{ids['run']}/stream-ticket", headers=hdr(ids, "other"))
    assert r.status_code == 200 and r.json()["expires_in"] == 60 and r.json()["path"] == f"/ws/runs/{ids['run']}"
    tkt = r.json()["ticket"]
    assert len(tkt) == 43 and tkt.replace("-", "").replace("_", "").isalnum()          # 32 bytes, urlsafe base64
    rows = env.admin.execute("select token_sha256, run_id, user_id, expires_at - now() as ttl "
                             "from public.stream_tickets where run_id = %s", (ids["run"],)).fetchall()
    (row,) = rows
    assert row["token_sha256"] == hashlib.sha256(tkt.encode()).hexdigest() and str(row["user_id"]) == ids["other"]
    assert 55 <= row["ttl"].total_seconds() <= 60
    dump = env.admin.execute("select string_agg(x::text, ' ') s from public.stream_tickets x").fetchone()["s"]  # type: ignore[index]
    assert tkt not in dump                                                                # the plaintext is nowhere


def test_two_tickets_differ_and_an_anonymous_viewer_gets_a_null_user(env: Env) -> None:
    ids = seed(env, "public")
    a, b = ticket(env, ids["run"]), ticket(env, ids["run"])
    assert a != b
    users = env.admin.execute("select user_id from public.stream_tickets where run_id = %s", (ids["run"],)).fetchall()
    assert [u["user_id"] for u in users] == [None, None]


@pytest.mark.parametrize("visibility", ["private", "unlisted", "public"])
@pytest.mark.parametrize("who", ["anon", "owner", "other", "admin"])
def test_tickets_follow_run_visibility_with_the_same_404(env: Env, who: str, visibility: str) -> None:
    ids = seed(env, visibility)
    r = env.client.post(f"/api/v1/runs/{ids['run']}/stream-ticket", headers=hdr(ids, who))
    if visibility != "private" or who in ("owner", "admin"):
        assert r.status_code == 200
    else:
        missing = env.client.post(f"/api/v1/runs/{uuid.uuid4()}/stream-ticket", headers=hdr(ids, who))
        assert (r.status_code, r.json()) == (missing.status_code, missing.json())
        assert env.admin.execute("select count(*) n from public.stream_tickets").fetchone()["n"] >= 0


def test_ticket_issuance_is_limited_per_identity(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    codes = [env.client.post(f"/api/v1/runs/{ids['run']}/stream-ticket").status_code for _ in range(22)]
    assert codes == [200] * 20 + [429] * 2
    assert env.client.post(f"/api/v1/runs/{ids['run']}/stream-ticket", headers=hdr(ids, "other")).status_code == 200


def test_redeeming_is_atomic_exactly_one_of_many_concurrent_redeems_wins(env: Env) -> None:
    from api.db import Database
    from api.stream import ticket_hash
    from conftest import API_DB_URL
    ids = seed(env, "public")
    tkt = ticket(env, ids["run"])
    db = Database(API_DB_URL, max_size=8)
    barrier = threading.Barrier(8)

    def redeem(_: int) -> int:
        with db.session(None) as conn:
            barrier.wait(timeout=10)
            return len(conn.execute("select * from api_private.redeem_stream_ticket(%s)", (ticket_hash(tkt),)).fetchall())

    try:
        with ThreadPoolExecutor(8) as pool:
            wins = sum(pool.map(redeem, range(8)))
    finally:
        db.close()
    assert wins == 1


# ---------------------------------------------------------------- admission

def test_a_valid_ticket_connects_and_streams_the_log_then_new_events(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    events(env, ids["run"], 3)
    with connect(env, ids["run"], ticket(env, ids["run"])) as ws:
        got = [next_event(ws) for _ in range(3)]
        assert [m["seq"] for m in got] == [1, 2, 3] and got[0]["type"] == "run.state" and got[2]["data"] == {"score": 2}
        EventLogWriter(env.store, ids["run"]).append([("obs", 9, {"score": 9})])
        assert next_event(ws)["seq"] == 4                                              # a live append arrives


def test_a_ticket_works_once(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    tkt = ticket(env, ids["run"])
    with connect(env, ids["run"], tkt) as ws:
        assert ws.receive_json()["t"] == "hb"
    assert rejected(env, ids["run"], tkt) == 1008                                      # replayed
    assert env.admin.execute("select count(*) n from public.stream_tickets where run_id = %s", (ids["run"],)).fetchone()["n"] == 0  # type: ignore[index]


def test_an_expired_ticket_is_refused_and_consumed(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    tkt = ticket(env, ids["run"])
    env.admin.execute("update public.stream_tickets set expires_at = now() - interval '1 second' where run_id = %s", (ids["run"],))
    assert rejected(env, ids["run"], tkt) == 1008
    env.admin.execute("select 1")
    assert env.admin.execute("select count(*) n from public.stream_tickets where run_id = %s", (ids["run"],)).fetchone()["n"] == 0  # type: ignore[index]


def test_a_ticket_for_another_run_is_refused_and_burned(make_env: Any) -> None:
    env = env_ws(make_env)
    a, b = seed(env, "public"), seed(env, "public")
    tkt = ticket(env, a["run"])
    assert rejected(env, b["run"], tkt) == 1008
    assert rejected(env, a["run"], tkt) == 1008                                        # presenting it anywhere consumed it


def test_a_ticket_used_after_the_owner_makes_the_run_private_is_refused(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    anon_ticket, other_ticket, owner_ticket = (ticket(env, ids["run"]), ticket(env, ids["run"], ids["other"]),
                                               ticket(env, ids["run"], ids["owner"]))
    r = env.client.patch(f"/api/v1/runs/{ids['run']}", json={"visibility": "private"}, headers=bearer(ids["owner"]))
    assert r.status_code == 200
    assert rejected(env, ids["run"], anon_ticket) == 1008
    assert rejected(env, ids["run"], other_ticket) == 1008
    with connect(env, ids["run"], owner_ticket) as ws:                                  # the owner still sees it
        assert ws.receive_json()["t"] == "hb"


def test_a_ticket_for_a_deleted_run_is_refused(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    tkt = ticket(env, ids["run"], ids["owner"])
    env.admin.execute("update public.runs set deleted_at = now() where id = %s", (ids["run"],))
    assert rejected(env, ids["run"], tkt) == 1008


@pytest.mark.parametrize("origin", [None, "", "null", "https://evil.test", "https://app.test.evil.test", "http://app.test",
                                    "https://APP.test", "https://app.test/"])
def test_a_bad_origin_is_refused(make_env: Any, origin: str | None) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    assert rejected(env, ids["run"], ticket(env, ids["run"]), origin) == 1008


def test_a_refused_origin_does_not_burn_the_ticket(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    tkt = ticket(env, ids["run"])
    assert rejected(env, ids["run"], tkt, "https://evil.test") == 1008                  # a hostile page cannot burn it
    with connect(env, ids["run"], tkt) as ws:
        assert ws.receive_json()["t"] == "hb"


def test_with_no_allowed_origins_configured_nothing_connects(make_env: Any) -> None:
    env: Env = make_env(ws_poll_s=0.03, ws_heartbeat_s=0.15)
    ids = seed(env, "public")
    assert rejected(env, ids["run"], ticket(env, ids["run"])) == 1008


@pytest.mark.parametrize("tkt", ["", "short", "x" * 43 + "!", "a" * 44, "../../etc", "%00" * 14])
def test_a_malformed_ticket_is_refused_without_a_database_call(make_env: Any, tkt: str) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    assert rejected(env, ids["run"], tkt) == 1008


def test_a_malformed_ticket_never_reaches_the_database(make_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db import Database
    env = env_ws(make_env)
    ids = seed(env, "public")

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("the database was used for a malformed ticket")

    monkeypatch.setattr(Database, "session", boom)
    for tkt in ("", "short", "x" * 43 + "!", "a" * 44):
        assert rejected(env, ids["run"], tkt) == 1008


def test_a_bad_run_id_is_refused(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    tkt = ticket(env, ids["run"])
    assert rejected(env, "not-a-uuid", tkt) == 1008 and rejected(env, str(uuid.uuid4()), tkt) == 1008


# ---------------------------------------------------------------- the client protocol

def test_resume_replays_from_after_seq(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    events(env, ids["run"], 5)
    with connect(env, ids["run"], ticket(env, ids["run"])) as ws:
        assert [next_event(ws)["seq"] for _ in range(5)] == [1, 2, 3, 4, 5]
        ws.send_text(json.dumps({"t": "resume", "after_seq": 2}))
        assert [next_event(ws)["seq"] for _ in range(3)] == [3, 4, 5]


def test_an_oversized_client_message_closes_the_socket(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    with connect(env, ids["run"], ticket(env, ids["run"])) as ws:
        ws.send_text(json.dumps({"t": "resume", "after_seq": 1, "pad": "x" * 2000}))
        with pytest.raises(WebSocketDisconnect) as info:
            for _ in range(50):
                ws.receive_json()
        assert info.value.code == 1009


def test_a_message_of_exactly_one_kilobyte_is_still_read(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    events(env, ids["run"], 2)
    msg = json.dumps({"t": "resume", "after_seq": 0})
    padded = msg[:-1] + " " * (1024 - len(msg)) + "}"
    assert len(padded.encode()) == 1024
    with connect(env, ids["run"], ticket(env, ids["run"])) as ws:
        ws.send_text(padded)
        assert next_event(ws)["seq"] in (1, 2)


@pytest.mark.parametrize("message", [
    {"t": "hello"}, {"t": "subscribe", "after_seq": 1}, {"t": "resume"}, {"t": "resume", "after_seq": -1},
    {"t": "resume", "after_seq": "1"}, {"t": "resume", "after_seq": True}, {"t": "resume", "after_seq": 1.5},
    {"t": "resume", "after_seq": 1, "extra": 1}, {"after_seq": 1}, {"t": "RESUME", "after_seq": 1},
    {"t": "resume", "after_seq": 2**60}, [1, 2], "resume", 7, None,
])
def test_anything_but_resume_closes_with_1008(make_env: Any, message: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    with connect(env, ids["run"], ticket(env, ids["run"])) as ws:
        ws.send_text(json.dumps(message))
        with pytest.raises(WebSocketDisconnect) as info:
            for _ in range(50):
                ws.receive_json()
        assert info.value.code == 1008


def test_non_json_and_binary_messages_close_with_1008(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    for send in (lambda ws: ws.send_text("not json"), lambda ws: ws.send_bytes(b"\x00\x01"),
                 lambda ws: ws.send_text("")):
        with connect(env, ids["run"], ticket(env, ids["run"])) as ws:
            send(ws)
            with pytest.raises(WebSocketDisconnect) as info:
                for _ in range(50):
                    ws.receive_json()
            assert info.value.code == 1008


def test_the_server_sends_heartbeats(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    with connect(env, ids["run"], ticket(env, ids["run"])) as ws:
        assert [ws.receive_json()["t"] for _ in range(3)] == ["hb"] * 3


def test_a_frame_message_follows_an_obs_event_with_a_signed_url(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    env.store.put(f"runs/{ids['run']}/frames/0000000.png", b"\x89PNG-frame")
    events(env, ids["run"], 2)
    with connect(env, ids["run"], ticket(env, ids["run"])) as ws:
        msgs = [ws.receive_json() for _ in range(6)]
    frames = [m for m in msgs if m["t"] == "frame"]
    assert frames and frames[0]["step"] == 1 and frames[0]["url"].startswith("http://api.test/_blob/")
    assert env.client.get(frames[0]["url"].replace("http://api.test", "")).content == b"\x89PNG-frame"


# ---------------------------------------------------------------- limits and visibility changes

def test_three_sockets_per_viewer_per_run(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    sockets = []
    try:
        for _ in range(3):
            cm = connect(env, ids["run"], ticket(env, ids["run"], ids["other"]))
            sockets.append((cm, cm.__enter__()))
        assert rejected(env, ids["run"], ticket(env, ids["run"], ids["other"])) == 1008           # the fourth
        other_viewer = connect(env, ids["run"], ticket(env, ids["run"], ids["owner"]))
        with other_viewer as ws:                                                                  # another viewer is fine
            assert ws.receive_json()["t"] == "hb"
        cm, ws = sockets.pop()
        cm.__exit__(None, None, None)                                                              # closing frees a slot
        with connect(env, ids["run"], ticket(env, ids["run"], ids["other"])) as again:
            assert again.receive_json()["t"] == "hb"
    finally:
        for cm, _ in sockets:
            cm.__exit__(None, None, None)


def test_anonymous_viewers_are_limited_per_address(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    sockets = []
    try:
        for _ in range(3):
            cm = connect(env, ids["run"], ticket(env, ids["run"]))
            sockets.append(cm)
            cm.__enter__()
        assert rejected(env, ids["run"], ticket(env, ids["run"])) == 1008
    finally:
        for cm in sockets:
            cm.__exit__(None, None, None)


def test_anonymous_viewers_at_different_addresses_do_not_share_a_limit(make_env: Any) -> None:
    from starlette.testclient import TestClient
    env = env_ws(make_env)
    ids = seed(env, "public")
    elsewhere = TestClient(env.client.app, client=("203.0.113.77", 5555), base_url="http://api.test",
                           raise_server_exceptions=False)
    sockets = []
    try:
        for _ in range(3):
            cm = connect(env, ids["run"], ticket(env, ids["run"]))
            sockets.append(cm)
            cm.__enter__()
        tkt = ticket(env, ids["run"])
        with elsewhere.websocket_connect(f"/ws/runs/{ids['run']}?ticket={tkt}", headers={"origin": ORIGIN}) as ws:
            assert ws.receive_json()["t"] == "hb"                         # a different address has its own three
    finally:
        for cm in sockets:
            cm.__exit__(None, None, None)


def test_two_hundred_viewers_per_run(make_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from api import stream
    monkeypatch.setattr(stream, "SOCKETS_PER_RUN", 2)
    env = env_ws(make_env)
    ids = seed(env, "public")
    third = env.user()
    a = connect(env, ids["run"], ticket(env, ids["run"], ids["owner"]))
    b = connect(env, ids["run"], ticket(env, ids["run"], ids["other"]))
    a.__enter__()
    b.__enter__()
    try:
        assert rejected(env, ids["run"], ticket(env, ids["run"], third)) == 1008
    finally:
        a.__exit__(None, None, None)
        b.__exit__(None, None, None)


def test_the_socket_counter_unit() -> None:
    from api.stream import SocketCounter
    c = SocketCounter(per_viewer=2, per_run=3)
    assert [c.acquire("r", "a"), c.acquire("r", "a"), c.acquire("r", "a")] == [True, True, False]
    assert c.acquire("r", "b") and not c.acquire("r", "c") and c.open("r") == 3
    c.release("r", "a")
    assert c.acquire("r", "c") and c.open("r") == 3
    for v in ("a", "b", "c"):
        c.release("r", v)
        c.release("r", v)
    assert c.open("r") == 0 and c.acquire("r", "z")


def test_a_run_made_private_while_connected_closes_the_socket(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    with connect(env, ids["run"], ticket(env, ids["run"])) as ws:
        assert ws.receive_json()["t"] == "hb"
        env.admin.execute("update public.runs set visibility = 'private' where id = %s", (ids["run"],))
        with pytest.raises(WebSocketDisconnect) as info:
            for _ in range(60):
                ws.receive_json()
        assert info.value.code == 1008


def test_a_run_deleted_while_connected_closes_the_socket(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    with connect(env, ids["run"], ticket(env, ids["run"], ids["other"])) as ws:
        assert ws.receive_json()["t"] == "hb"
        env.admin.execute("update public.runs set deleted_at = now() where id = %s", (ids["run"],))
        with pytest.raises(WebSocketDisconnect) as info:
            for _ in range(60):
                ws.receive_json()
        assert info.value.code == 1008


def test_the_owner_keeps_the_socket_when_the_run_goes_private(make_env: Any) -> None:
    env = env_ws(make_env)
    ids = seed(env, "public")
    with connect(env, ids["run"], ticket(env, ids["run"], ids["owner"])) as ws:
        env.admin.execute("update public.runs set visibility = 'private' where id = %s", (ids["run"],))
        assert [ws.receive_json()["t"] for _ in range(8)] == ["hb"] * 8                         # still open, still beating


def test_the_ticket_value_is_never_logged(make_env: Any, caplog: pytest.LogCaptureFixture) -> None:
    import logging
    env = env_ws(make_env)
    ids = seed(env, "public")
    caplog.set_level(logging.DEBUG)
    tkt = ticket(env, ids["run"])
    with connect(env, ids["run"], tkt) as ws:
        ws.receive_json()
    rejected(env, ids["run"], tkt)
    assert tkt not in "\n".join(r.getMessage() for r in caplog.records)
