"""Clients for the internal orchestrator API and provider key validation.

The key travels only as an argument to `submit`; it is never stored, logged, or returned."""

from typing import Any, Protocol

import httpx


class UpstreamError(Exception):
    def __init__(self, status: int, code: str, retry_after_s: int | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.retry_after_s = status, code, retry_after_s


class OrchestratorClient(Protocol):
    def submit(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def stop(self, run_id: str, actor: str) -> str: ...
    def kill(self, run_id: str, actor: str, reason: str) -> str: ...
    def snapshot(self) -> dict[str, Any]: ...
    def breaker(self, open_: bool, actor: str, reason: str) -> dict[str, Any]: ...


class KeyValidator(Protocol):
    def validate(self, provider: str, api_key: str) -> bool: ...


_PASS_THROUGH = {"caps_exceed_limit", "capacity_full", "rate_limited", "breaker_open", "validation_failed"}


class HttpOrchestrator:
    def __init__(self, base_url: str, token: bytes, timeout_s: float = 10.0) -> None:
        if len(token) < 32:
            raise ValueError("orchestrator token must be at least 32 bytes")
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s, follow_redirects=False,
                                  headers={"authorization": "Bearer " + token.decode()})

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            resp = self._http.request(method, path, json=body)
        except httpx.HTTPError:
            raise UpstreamError(503, "capacity_full", 30) from None
        if resp.status_code >= 400:
            try:
                code = str(resp.json().get("code", "internal"))
            except ValueError:
                code = "internal"
            retry = resp.headers.get("retry-after")
            if code not in _PASS_THROUGH:
                raise UpstreamError(502, "internal")     # upstream detail never reaches the caller
            raise UpstreamError(resp.status_code, code, int(retry) if retry and retry.isdigit() else None)
        data = resp.json()
        return data if isinstance(data, dict) else {}

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/internal/runs", payload)

    def stop(self, run_id: str, actor: str) -> str:
        return str(self._call("POST", f"/internal/runs/{run_id}/stop", {"actor": actor}).get("result", ""))

    def kill(self, run_id: str, actor: str, reason: str) -> str:
        return str(self._call("POST", f"/internal/runs/{run_id}/kill", {"actor": actor, "reason": reason})
                   .get("result", ""))

    def snapshot(self) -> dict[str, Any]:
        return self._call("GET", "/internal/runs")

    def breaker(self, open_: bool, actor: str, reason: str) -> dict[str, Any]:
        return self._call("POST", "/internal/breaker", {"open": open_, "actor": actor, "reason": reason})
