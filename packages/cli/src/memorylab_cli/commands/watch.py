"""`runs watch`: stream ticket, then the WebSocket live tail; on a drop, a new ticket and a resume from the last seq; after
repeated failures, poll /events. Ends when the run reaches a final state."""

import json
import time
from collections.abc import Callable
from typing import Any

from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect
from websockets.typing import Origin

from ..app import App
from ..errors import CliError
from ..exitcodes import EXIT_CODES, OK

FINAL = {"completed": OK, "capped": OK, "failed": EXIT_CODES["run_failed"], "killed": EXIT_CODES["run_killed"]}
MAX_WS_FAILURES = 3
POLL_INTERVAL_S = 2.0
RESUME_GAP_S = 1.1            # the server allows one resume a second per socket


def format_event(e: dict[str, Any]) -> str:
    data = e.get("data", {})
    if e.get("type") == "run.state":
        body = f"{data.get('state')}" + (f" ({data.get('reason')})" if data.get("reason") else "")
    else:
        body = json.dumps(data, sort_keys=True)
    return f"{e.get('seq', 0):>6}  step {e.get('step', 0):>5}  {e.get('type', ''):<14} {body[:300]}"


class Watcher:
    def __init__(self, app: App, run_id: str, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.app, self.run_id, self.sleep = app, run_id, sleep
        self.last_seq = 0
        self.final: str | None = None

    def _show(self, e: dict[str, Any]) -> None:
        seq = int(e.get("seq", 0))
        if seq <= self.last_seq:
            return                                                        # a resume may repeat events
        self.last_seq = seq
        if self.app.out.as_json:
            print(json.dumps(e, sort_keys=True), flush=True)
        else:
            self.app.out.line(format_event(e))
        if e.get("type") == "run.state" and e.get("data", {}).get("state") in FINAL:
            self.final = str(e["data"]["state"])

    def _ticket(self) -> str:
        t = self.app.api.post(f"/runs/{self.run_id}/stream-ticket", auth=self.app.api.store.load() is not None)
        return str(t["ticket"])

    def live(self) -> bool:
        """One WebSocket session. True if it ended cleanly because the run finished."""
        ticket = self._ticket()
        url = f"{self.app.cfg.ws_base}/ws/runs/{self.run_id}?ticket={ticket}"
        try:
            with connect(url, origin=Origin(self.app.cfg.web_url), open_timeout=10, max_size=2**20, close_timeout=2) as ws:
                if self.last_seq:
                    ws.send(json.dumps({"t": "resume", "after_seq": self.last_seq}))
                while self.final is None:
                    try:
                        raw = ws.recv(timeout=60)
                    except TimeoutError:
                        raise WebSocketException("no heartbeat") from None
                    msg = json.loads(raw)
                    if msg.get("t") == "event":
                        self._show({k: v for k, v in msg.items() if k != "t"})
                    # heartbeats and frame messages need no output here
            return self.final is not None
        except (WebSocketException, OSError, ValueError, InvalidStatus):
            return False

    def poll(self) -> None:
        while self.final is None:
            page = self.app.api.get(f"/runs/{self.run_id}/events", params={"after_seq": self.last_seq, "limit": 1000},
                                    auth=self.app.api.store.load() is not None)
            for e in page["events"]:
                self._show(e)
            if self.final is None:
                if not page["events"]:
                    run = self.app.api.get(f"/runs/{self.run_id}", auth=self.app.api.store.load() is not None)
                    if run["status"] in FINAL:
                        self.final = run["status"]
                        break
                self.sleep(POLL_INTERVAL_S)

    def run(self, *, poll_only: bool, from_start: bool) -> int:
        if not from_start:
            page = self.app.api.get(f"/runs/{self.run_id}/events", params={"after_seq": 0, "limit": 1000},
                                    auth=self.app.api.store.load() is not None)
            while page["events"]:
                self.last_seq = page["events"][-1]["seq"]
                page = self.app.api.get(f"/runs/{self.run_id}/events", params={"after_seq": self.last_seq, "limit": 1000},
                                        auth=self.app.api.store.load() is not None)
        failures = 0
        while self.final is None and not poll_only and failures < MAX_WS_FAILURES:
            if self.live():
                break
            failures += 1
            self.app.out.note(f"disconnected; resuming ({failures}/{MAX_WS_FAILURES})")
            self.sleep(RESUME_GAP_S * failures)
        if self.final is None:
            if not poll_only:
                self.app.out.note("falling back to polling /events")
            self.poll()
        assert self.final is not None
        return FINAL[self.final]


def watch_run(app: App, run_id: str, *, poll: bool = False, from_start: bool = True) -> int:
    try:
        return Watcher(app, run_id).run(poll_only=poll, from_start=from_start)
    except KeyboardInterrupt:
        raise CliError("generic", "interrupted") from None
