import json

import httpx
import pytest
from labharness.providers.secret import SecretKey

from runlog.client import (
    HttpEventSink,
    IngestRejected,
    IngestUnavailable,
    LocalLogSink,
    RedactingSink,
)
from runlog.eventlog import EventLogReader, EventLogWriter
from runlog.redaction import Redactor

RUN = "11111111-2222-3333-4444-555555555555"
TOKEN = "v1.payload.signature-signature"


class Server:
    def __init__(self, *responses: object) -> None:
        self.queue = list(responses)
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, int)
        return httpx.Response(item, json={"ok": True})


def sink(server: Server, **kw: object) -> tuple[HttpEventSink, list[float]]:
    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(server), base_url="http://ingest.test")
    return HttpEventSink(client, RUN, SecretKey(TOKEN), sleep=sleeps.append, **kw), sleeps  # type: ignore[arg-type]


def bodies(server: Server) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(c.content) for c in server.calls]


def test_events_are_batched_with_increasing_batch_numbers() -> None:
    server = Server(200)
    s, _ = sink(server, batch_size=3)
    for i in range(7):
        s.emit("obs", i, {"i": i})
    assert [b["batch_no"] for b in bodies(server)] == [1, 2] and len(server.calls) == 2
    s.flush()
    assert [b["batch_no"] for b in bodies(server)] == [1, 2, 3] and [len(b["events"]) for b in bodies(server)] == [3, 3, 1]
    s.flush()                                                                     # nothing buffered: no call
    assert len(server.calls) == 3
    assert server.calls[0].url.path == f"/v1/runs/{RUN}/events"
    assert server.calls[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_a_retry_resends_the_same_batch_number() -> None:
    server = Server(503, httpx.ReadTimeout("t"), 429, 200)
    s, sleeps = sink(server, batch_size=1)
    s.emit("obs", 0, {})
    assert [b["batch_no"] for b in bodies(server)] == [1, 1, 1, 1] and sleeps == [0.2, 0.4, 0.8]
    s.emit("obs", 1, {})                                                          # next batch continues from 2
    assert bodies(server)[-1]["batch_no"] == 2


def test_client_errors_are_fatal_and_not_retried() -> None:
    for status in (400, 401, 403, 404, 409, 413, 422):
        server = Server(status)
        s, sleeps = sink(server, batch_size=1)
        with pytest.raises(IngestRejected):
            s.emit("obs", 0, {})
        assert len(server.calls) == 1 and sleeps == []


def test_persistent_outage_raises_after_bounded_retries() -> None:
    server = Server(500)
    s, sleeps = sink(server, batch_size=1, max_retries=3)
    with pytest.raises(IngestUnavailable):
        s.emit("obs", 0, {})
    assert len(server.calls) == 4 and len(sleeps) == 3
    assert max(sleeps) <= 10.0


def test_buffer_is_bounded() -> None:
    s, _ = sink(Server(200), batch_size=100, max_buffer=5)
    for i in range(5):
        s.emit("obs", i, {})
    with pytest.raises(IngestUnavailable, match="buffer"):
        s.emit("obs", 5, {})


def test_token_never_appears_in_repr_or_in_the_body() -> None:
    server = Server(200)
    s, _ = sink(server, batch_size=1)
    s.emit("obs", 0, {})
    assert TOKEN not in repr(s) and TOKEN not in str(vars(s)) and all(TOKEN not in c.content.decode() for c in server.calls)


def test_frame_upload_uses_put_and_png_content_type() -> None:
    server = Server(200)
    s, _ = sink(server)
    s.upload_frame(7, b"\x89PNG...")
    call = server.calls[0]
    assert (call.method, call.url.path, call.headers["content-type"]) == ("PUT", f"/v1/runs/{RUN}/frames/7", "image/png")


def test_redacting_sink_scrubs_before_forwarding() -> None:
    class Collect:
        def __init__(self) -> None:
            self.got: list[tuple[str, int, dict]] = []  # type: ignore[type-arg]

        def emit(self, type_: str, step: int, data: dict) -> None:  # type: ignore[type-arg]
            self.got.append((type_, step, data))

    inner = Collect()
    secret = "super-secret-visitor-key-0123"
    r = Redactor([secret])
    RedactingSink(inner, r).emit("error", 3, {"message": f"bad {secret}", "nested": [secret]})
    assert secret not in json.dumps(inner.got) and r.hits == 2


def test_local_log_sink_writes_to_the_log(store, run_id, clock) -> None:  # type: ignore[no-untyped-def]
    w = EventLogWriter(store, run_id, clock=clock)
    LocalLogSink(w).emit("obs", 1, {"x": 1})
    assert [e.data for e in EventLogReader(store, run_id).iter_events()] == [{"x": 1}]
