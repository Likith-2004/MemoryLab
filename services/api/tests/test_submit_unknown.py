"""An orchestrator that accepted a submit but answered too slowly (or whose answer was lost) must not have its
run abandoned: that would leave a sandbox running on the user's key while ingest rejects its events."""

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from api.orchestrator_client import HttpOrchestrator, SubmitUnknown, UpstreamError
from conftest import Env, bearer
from test_runs_create import body

TOKEN = b"o" * 40


class Server:
    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.submitted: list[dict[str, Any]] = []
        self.killed: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
                if self.path == "/internal/runs":
                    outer.submitted.append(payload)             # accepted, whatever happens to the answer
                    time.sleep(outer.delay_s)
                    out = {"run_id": payload["run_id"], "status": "queued"}
                else:
                    outer.killed.append({"path": self.path, **payload})
                    out = {"result": "killed"}
                data = json.dumps(out).encode()
                try:
                    self.send_response(202 if self.path == "/internal/runs" else 200)
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *_: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def slow() -> Iterator[Server]:
    s = Server(delay_s=1.0)
    yield s
    s.close()


def test_a_read_timeout_on_submit_is_unknown_not_a_refusal(slow: Server) -> None:
    client = HttpOrchestrator(slow.url, TOKEN, timeout_s=0.3)
    with pytest.raises(SubmitUnknown):
        client.submit({"run_id": "r1"})
    assert slow.submitted == [{"run_id": "r1"}]                       # it really was accepted


def test_a_refused_connection_is_a_capacity_refusal_and_other_calls_keep_that_mapping(slow: Server) -> None:
    dead = HttpOrchestrator("http://127.0.0.1:1", TOKEN, timeout_s=0.3)       # nothing listens: never sent
    with pytest.raises(UpstreamError) as info:
        dead.submit({"run_id": "r1"})
    assert (info.value.status, info.value.code) == (503, "capacity_full")
    timing_out = HttpOrchestrator(slow.url, TOKEN, timeout_s=0.3)
    with pytest.raises(UpstreamError):                                  # only submit has the "unknown" outcome
        timing_out._call("POST", "/internal/runs", {"run_id": "r2"})


def post(env: Env, uid: str, game: str) -> Any:
    return env.client.post("/api/v1/runs", json=body(env, game), headers=bearer(uid))


def test_an_ambiguous_submit_keeps_the_row_keeps_the_quota_and_asks_for_a_kill(make_env: Any, slow: Server) -> None:
    env: Env = make_env(run_create_per_user_per_hour=1)
    env.orch.delegate = HttpOrchestrator(slow.url, TOKEN, timeout_s=0.3)
    uid, game = env.user(), env.game()
    r = post(env, uid, game)
    assert (r.status_code, r.json()["code"], r.headers["retry-after"]) == (503, "internal", "30")
    (row,) = env.admin.execute("select id, status, end_reason from public.runs where owner_id = %s", (uid,)).fetchall()
    assert (row["status"], row["end_reason"]) == ("queued", None)       # left for the reaper, not marked failed
    assert slow.submitted[0]["run_id"] == str(row["id"])
    assert env.orch.killed == [(str(row["id"]), "system", "submit_unknown")]
    assert slow.killed and slow.killed[0]["actor"] == "system" and slow.killed[0]["reason"] == "submit_unknown"
    env.admin.execute("update public.runs set status = 'failed' where owner_id = %s", (uid,))   # the reaper's doing
    again = post(env, uid, game)
    assert (again.status_code, again.json()["title"]) == (429, "Too many runs")                 # the quota was kept


def test_a_failing_kill_does_not_change_the_answer(env: Env) -> None:
    uid, game = env.user(), env.game()
    env.orch.fail_with = SubmitUnknown()

    def broken(run_id: str, actor: str, reason: str) -> str:
        raise RuntimeError("down")

    env.orch.kill = broken  # type: ignore[method-assign]
    r = post(env, uid, game)
    assert r.status_code == 503 and r.json()["code"] == "internal"
    assert env.admin.execute("select status from public.runs where owner_id = %s", (uid,)).fetchone()["status"] == "queued"  # type: ignore[index]


def test_a_refused_connection_still_abandons_the_run_and_refunds_the_quota(make_env: Any) -> None:
    env: Env = make_env(run_create_per_user_per_hour=1)
    env.orch.delegate = HttpOrchestrator("http://127.0.0.1:1", TOKEN, timeout_s=0.3)
    uid, game = env.user(), env.game()
    r = post(env, uid, game)
    assert (r.status_code, r.json()["code"]) == (503, "capacity_full")
    row = env.admin.execute("select status, end_reason from public.runs where owner_id = %s", (uid,)).fetchone()
    assert (row["status"], row["end_reason"]) == ("failed", "submit_refused")                   # type: ignore[index]
    env.orch.delegate = None
    assert post(env, uid, game).status_code == 202                      # the single hourly slot was refunded
