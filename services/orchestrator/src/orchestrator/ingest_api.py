"""The orchestrator's client for the ingest service (orchestrator scope)."""

import time
from collections.abc import Callable
from typing import Any

import httpx
from ingest.tokens import issue_token
from labharness.providers.secret import SecretKey
from runlog.client import HttpEventSink


class IngestApiError(Exception):
    pass


class IngestApi:
    """One per run. Events go out one batch at a time so the lifecycle order is preserved."""

    def __init__(self, client: httpx.Client, run_id: str, secret: bytes, clock: Callable[[], float] = time.time) -> None:
        self._client, self.run_id, self._secret, self._clock = client, run_id, secret, clock
        token = SecretKey(issue_token(secret, run_id, "orchestrator", 4 * 3600, clock()))
        self._token = token
        self._sink = HttpEventSink(client, run_id, token, batch_size=1)
        self._synced = False

    def _sync(self) -> None:
        """Continue the orchestrator's batch numbering from where ingest says it stands (so a
        restarted orchestrator, or a second handle for the same run, never replays a batch number)."""
        if not self._synced:
            self._sink._batch_no = int(self.status()["next_batch_no"])
            self._synced = True

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._token.reveal()}"}

    def state(self, state: str, reason: str | None = None) -> None:
        data: dict[str, Any] = {"state": state}
        if reason:
            data["reason"] = reason
        self._sync()
        self._sink.emit("run.state", 0, data)

    def error(self, code: str, message: str = "") -> None:
        self._sync()
        self._sink.emit("error", 0, {"code": code, "message": message[:200]})

    def put_manifest(self, manifest: dict[str, Any]) -> None:
        r = self._client.put(f"/v1/runs/{self.run_id}/manifest", json=manifest, headers=self._headers())
        if r.status_code != 200:
            raise IngestApiError(f"manifest rejected ({r.status_code})")

    def status(self) -> dict[str, Any]:
        r = self._client.get(f"/v1/runs/{self.run_id}/status", headers=self._headers())
        if r.status_code != 200:
            raise IngestApiError(f"status failed ({r.status_code})")
        body: dict[str, Any] = r.json()
        return body

    def close(self) -> dict[str, Any]:
        r = self._client.post(f"/v1/runs/{self.run_id}/close", headers=self._headers())
        if r.status_code != 200:
            raise IngestApiError(f"close failed ({r.status_code})")
        body: dict[str, Any] = r.json()
        return body

    def worker_token(self, ttl_s: int) -> str:
        return issue_token(self._secret, self.run_id, "worker", ttl_s, self._clock())
