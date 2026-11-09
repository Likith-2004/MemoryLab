"""Worker-side event sinks: redaction wrapper and the HTTP client for the ingest service."""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
from labharness.providers.secret import SecretKey

from .redaction import Redactor


class IngestRejected(Exception):
    """The ingest service refused our data (4xx): fatal for the run, never retried."""


class IngestUnavailable(Exception):
    """Ingest could not be reached after bounded retries."""


class RedactingSink:
    """Wraps any EventSink; every string in every event passes through the Redactor first."""

    def __init__(self, inner: Any, redactor: Redactor) -> None:
        self._inner, self._redactor = inner, redactor

    def emit(self, type_: str, step: int, data: dict[str, Any]) -> None:
        self._inner.emit(type_, step, self._redactor.obj(data))


class LocalLogSink:
    """Writes events straight into a local EventLogWriter (offline runs and tests)."""

    def __init__(self, writer: Any) -> None:
        self._writer = writer

    def emit(self, type_: str, step: int, data: dict[str, Any]) -> None:
        self._writer.append([(type_, step, data)])


class HttpEventSink:
    """Batches events to POST /v1/runs/{run_id}/events. Each batch has a monotonically
    increasing `batch_no`, so a retry after a lost response is acknowledged, not duplicated."""

    def __init__(self, client: httpx.Client, run_id: str, token: SecretKey, *, batch_size: int = 50,
                 max_retries: int = 5, sleep: Callable[[float], None] = time.sleep, max_buffer: int = 5000) -> None:
        self._client, self._run_id, self._token = client, run_id, token
        self._batch_size, self._max_retries, self._sleep, self._max_buffer = batch_size, max_retries, sleep, max_buffer
        self._buffer: list[dict[str, Any]] = []
        self._batch_no = 1

    def emit(self, type_: str, step: int, data: dict[str, Any]) -> None:
        if len(self._buffer) >= self._max_buffer:
            raise IngestUnavailable("event buffer full")
        self._buffer.append({"type": type_, "step": step, "data": data})
        if len(self._buffer) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        batch, rest = self._buffer[: self._batch_size], self._buffer[self._batch_size :]
        self._post(f"/v1/runs/{self._run_id}/events", {"batch_no": self._batch_no, "events": batch})
        self._batch_no += 1
        self._buffer = rest
        if rest:
            self.flush()

    def upload_frame(self, step: int, png: bytes) -> None:
        self._request("PUT", f"/v1/runs/{self._run_id}/frames/{step}", content=png,
                      headers={"content-type": "image/png"})

    def upload_checkpoint(self, cp: Any, context: dict[str, Any]) -> None:
        """Store a checkpoint's emulator state and model context (the `checkpoint` event that announces them is logged after this)."""
        base = f"/v1/runs/{self._run_id}/checkpoints/{cp.step}"
        self._request("PUT", f"{base}/state", content=cp.state, headers={"content-type": "application/octet-stream"})
        self._request("PUT", f"{base}/context", content=json.dumps(context, separators=(",", ":")).encode(),
                      headers={"content-type": "application/json"})

    def fetch_parent(self) -> tuple[dict[str, Any], bytes]:
        """For a forked run: the parent's checkpoint event, context and memory history, and the emulator state."""
        headers = {"authorization": f"Bearer {self._token.reveal()}"}
        try:
            meta = self._client.get(f"/v1/runs/{self._run_id}/parent", headers=headers)
            state = self._client.get(f"/v1/runs/{self._run_id}/parent/state", headers=headers)
        except httpx.HTTPError:
            raise IngestUnavailable("ingest could not be reached for the parent checkpoint") from None
        for r in (meta, state):
            if r.status_code >= 500:
                raise IngestUnavailable(f"ingest failed ({r.status_code})")
            if r.status_code != 200:
                raise IngestRejected(f"ingest refused the parent checkpoint ({r.status_code})")
        body = meta.json()
        if not isinstance(body, dict):
            raise IngestRejected("malformed parent payload")
        return body, state.content

    def _post(self, path: str, body: dict[str, Any]) -> None:
        self._request("POST", path, json=body)

    def _request(self, method: str, path: str, **kw: Any) -> None:
        headers = {"authorization": f"Bearer {self._token.reveal()}", **kw.pop("headers", {})}
        for attempt in range(self._max_retries + 1):
            failure: str | None = None
            try:
                resp = self._client.request(method, path, headers=headers, **kw)
            except httpx.HTTPError:
                failure = "unreachable"
            else:
                if resp.status_code < 300:
                    return
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise IngestRejected(f"ingest rejected the request ({resp.status_code})")
                failure = f"status {resp.status_code}"
            if attempt == self._max_retries:
                raise IngestUnavailable(f"ingest failed after {attempt + 1} attempts ({failure})")
            self._sleep(min(10.0, 0.2 * 2**attempt))
