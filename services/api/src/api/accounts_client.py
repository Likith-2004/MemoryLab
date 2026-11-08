"""Client for the internal accounts service (service-role work the API itself never does: erasing an account)."""

from typing import Protocol

import httpx


class AccountsError(Exception):
    def __init__(self, status: int, code: str, retry_after_s: int | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.retry_after_s = status, code, retry_after_s


class AccountsClient(Protocol):
    def erase(self, user_id: str) -> None: ...


class HttpAccounts:
    def __init__(self, base_url: str, token: bytes, timeout_s: float = 60.0) -> None:
        if len(token) < 32:
            raise ValueError("accounts token must be at least 32 bytes")
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s, follow_redirects=False,
                                  headers={"authorization": "Bearer " + token.decode()})

    def erase(self, user_id: str) -> None:
        try:
            resp = self._http.post("/internal/accounts/erase", json={"user_id": user_id})
        except httpx.HTTPError:
            raise AccountsError(503, "internal", 30) from None
        if resp.status_code == 409:
            retry = resp.headers.get("retry-after")
            raise AccountsError(409, "runs_still_active", int(retry) if retry and retry.isdigit() else 5)
        if resp.status_code >= 400:
            raise AccountsError(503 if resp.status_code == 503 else 502, "internal", 30)   # no upstream detail passes
