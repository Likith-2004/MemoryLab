"""HTTP client for /api/v1 plus the GoTrue calls a CLI needs (sign in, refresh, sign out). It is the only place that
talks to the network, and it keeps the access token fresh: refresh shortly before expiry, and once more after a 401."""

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .config import Config
from .exitcodes import exit_code_for
from .session import Session, SessionStore, from_token_response


class CliError(Exception):
    """Anything the CLI reports and exits on. `code` selects the exit code (exitcodes.EXIT_CODES)."""

    def __init__(self, code: str, title: str, status: int = 0, retry_after: str | None = None) -> None:
        super().__init__(title)
        self.code, self.title, self.status, self.retry_after = code, title, status, retry_after

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.code)


class Api:
    def __init__(self, cfg: Config, store: SessionStore, client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg, self.store, self._clock = cfg, store, clock
        self.http = client or httpx.Client(timeout=30.0, follow_redirects=False)

    # ---- session -------------------------------------------------------------------------------------------
    def session(self) -> Session:
        s = self.store.load()
        if s is None:
            raise CliError("not_logged_in", "not logged in: run `memorylab login`")
        return s

    def _fresh_token(self) -> str:
        s = self.session()
        if s.expired(self._clock()):
            s = self.refresh(s)
        return s.access_token

    def refresh(self, s: Session | None = None) -> Session:
        s = s or self.session()
        body = self._auth("POST", "/token", params={"grant_type": "refresh_token"},
                          json={"refresh_token": s.refresh_token}, expect_session_error=True)
        fresh = from_token_response(body, self._clock())
        fresh.user_id, fresh.email = fresh.user_id or s.user_id, fresh.email or s.email
        self.store.save(fresh)                                          # the refresh token rotates: keep the new one
        return fresh

    def _auth(self, method: str, path: str, *, params: dict[str, str] | None = None, json: Any = None,
              token: str | None = None, expect_session_error: bool = False) -> dict[str, Any]:
        if not self.cfg.publishable_key:
            raise CliError("config", "MEMORYLAB_PUBLISHABLE_KEY is not set")
        headers = {"apikey": self.cfg.publishable_key}
        if token:
            headers["authorization"] = f"Bearer {token}"
        try:
            resp = self.http.request(method, self.cfg.auth_url.rstrip("/") + path, params=params, json=json, headers=headers)
        except httpx.HTTPError:
            raise CliError("network", "could not reach the auth service") from None
        if resp.status_code >= 400:
            if expect_session_error:
                raise CliError("not_logged_in", "your session has expired: run `memorylab login`", resp.status_code)
            code = ("internal" if resp.status_code >= 500 else "rate_limited" if resp.status_code == 429
                    else "unauthenticated")
            raise CliError(code, _auth_message(resp), resp.status_code)
        if resp.status_code == 204 or not resp.content:
            return {}
        out = resp.json()
        return out if isinstance(out, dict) else {}

    def sign_in(self, email: str, password: str) -> Session:
        body = self._auth("POST", "/token", params={"grant_type": "password"}, json={"email": email, "password": password})
        s = from_token_response(body, self._clock())
        self.store.save(s)
        return s

    def send_magic_link(self, email: str) -> None:
        self._auth("POST", "/otp", json={"email": email, "create_user": False})

    def verify_code(self, email: str, pasted: str) -> Session:
        """`pasted` is the 6-digit code from the email, or the sign-in link itself (the default template only has a link)."""
        pasted = pasted.strip()
        if "token=" in pasted:
            q = parse_qs(urlparse(pasted).query)
            token_hash = (q.get("token") or q.get("token_hash") or [""])[0]
            payload: dict[str, Any] = {"type": (q.get("type") or ["magiclink"])[0], "token_hash": token_hash}
        else:
            payload = {"type": "email", "email": email, "token": pasted}
        body = self._auth("POST", "/verify", json=payload)
        s = from_token_response(body, self._clock())
        self.store.save(s)
        return s

    def sign_out(self, everywhere: bool) -> None:
        s = self.store.load()
        if s is not None:
            try:
                self._auth("POST", "/logout", params={"scope": "global" if everywhere else "local"}, token=s.access_token)
            except CliError:
                pass                                                    # already invalid: still forget it locally
        self.store.clear()

    # ---- API calls ------------------------------------------------------------------------------------------
    def request(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None,
                auth: bool = True, raw: bool = False) -> Any:
        """JSON in, JSON out; an error answer becomes CliError with the API's stable `code`. `raw` returns the
        response (redirects are not followed) for the frame and export downloads."""
        url = self.cfg.api_base + path
        refreshed = False
        while True:
            headers = {"authorization": f"Bearer {self._fresh_token()}"} if auth else {}
            try:
                resp = self.http.request(method, url, json=json, params=params, headers=headers)
            except httpx.HTTPError:
                raise CliError("network", f"could not reach {self.cfg.api_url}") from None
            if resp.status_code == 401 and auth and not refreshed:
                refreshed = True
                self.refresh()
                continue
            break
        if resp.status_code >= 400:
            raise _api_error(resp)
        if raw:
            return resp
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, json=body, **kw)

    def patch(self, path: str, body: Any, **kw: Any) -> Any:
        return self.request("PATCH", path, json=body, **kw)

    def delete(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("DELETE", path, json=body, **kw)

    def download(self, url: str) -> bytes:
        """A signed URL: no credentials are sent to it."""
        try:
            resp = self.http.get(url)
        except httpx.HTTPError:
            raise CliError("network", "could not download the file") from None
        if resp.status_code != 200:
            raise CliError("not_found" if resp.status_code == 404 else "internal", f"download failed ({resp.status_code})",
                           resp.status_code)
        return resp.content


def _api_error(resp: httpx.Response) -> CliError:
    try:
        body = resp.json()
        code, title = str(body.get("code", "")), str(body.get("title", ""))
    except (ValueError, AttributeError):
        code, title = "", ""
    if not code:
        code = "internal" if resp.status_code >= 500 else "validation_failed"
    if code == "validation_failed" and "forking is not available" in title:
        code = "fork_disabled"
    return CliError(code, title or f"HTTP {resp.status_code}", resp.status_code, resp.headers.get("retry-after"))


def _auth_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("msg") or resp.json().get("error_description") or "authentication failed")
    except (ValueError, AttributeError):
        return "authentication failed"
