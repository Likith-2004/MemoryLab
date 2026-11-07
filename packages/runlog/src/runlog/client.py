"""Worker-side event sinks: redaction wrapper and the HTTP client for the ingest service."""

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
