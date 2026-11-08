"""JWT verification. Signature, `aud`, `exp` (and `iss` when configured) are checked locally against
a cached JWKS; `sub` is the only identity. Roles are never read from claims (see db.is_admin)."""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWK

JWKS_TTL_S = 600.0
JWKS_MIN_REFETCH_S = 30.0   # unknown `kid` triggers at most one refetch per interval (no fetch amplification)


class Unauthenticated(Exception):
    pass


@dataclass(frozen=True)
class User:
    id: str
    token: str = ""


class JwtVerifier:
    def __init__(self, fetch_jwks: Callable[[], dict[str, Any]], *, audience: str, issuer: str | None,
                 algorithms: tuple[str, ...], clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch_jwks
        self._aud = audience
        self._iss = issuer
        self._algs = list(algorithms)
        self._clock = clock
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = -1e12

    def _refresh(self) -> None:
        now = self._clock()
        if now - self._fetched_at < JWKS_MIN_REFETCH_S:
            return
        self._fetched_at = now
        try:
            keys = self._fetch().get("keys", [])
        except Exception:  # noqa: BLE001 - a JWKS outage must fail closed, not crash the request
            return
        self._keys = {k["kid"]: PyJWK(k) for k in keys if isinstance(k, dict) and "kid" in k}

    def _key(self, kid: str) -> PyJWK:
        if kid not in self._keys or self._clock() - self._fetched_at > JWKS_TTL_S:
            self._refresh()
        key = self._keys.get(kid)
        if key is None:
            raise Unauthenticated
        return key

    def verify(self, token: str) -> User:
        try:
            header = jwt.get_unverified_header(token)
            alg, kid = header.get("alg"), header.get("kid")
            if alg not in self._algs or not isinstance(kid, str):
                raise Unauthenticated
            claims = jwt.decode(token, self._key(kid).key, algorithms=self._algs, audience=self._aud,
                                issuer=self._iss, options={"require": ["exp", "sub", "aud"]})
            sub = str(uuid.UUID(str(claims["sub"])))
        except Unauthenticated:
            raise
        except (jwt.PyJWTError, ValueError, KeyError, TypeError):
            raise Unauthenticated from None
        if claims.get("role") != "authenticated":      # anon/service keys are not user sessions
            raise Unauthenticated
        return User(sub, token)


def http_jwks_fetcher(url: str) -> Callable[[], dict[str, Any]]:
    def fetch() -> dict[str, Any]:
        resp = httpx.get(url, timeout=3.0, follow_redirects=False)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}
    return fetch
