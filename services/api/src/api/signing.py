"""Signed GET URLs for private objects (frames, export bundles).

Production serves these from the private bucket with the provider's presigned URLs (BLOCKED.md B-2: no cloud
account here). `LocalUrlSigner` is the development stand-in: an HMAC over the key and an expiry, verified by a
route the API mounts only when this signer is configured. Either way the URL is a bearer capability for exactly one
object for `ttl_s` seconds, so it is never logged and never cached."""

import base64
import hashlib
import hmac
import time
from collections.abc import Callable
from typing import Protocol

from runlog.store import BadKey, check_key


class UrlSigner(Protocol):
    def sign_get(self, key: str, ttl_s: int) -> str: ...


class LocalUrlSigner:
    PATH = "/_blob/"

    def __init__(self, base_url: str, secret: bytes, clock: Callable[[], float] = time.time) -> None:
        if len(secret) < 32:
            raise ValueError("signing secret must be at least 32 bytes")
        self._base, self._secret, self._clock = base_url.rstrip("/"), secret, clock

    def _mac(self, key: str, exp: int) -> str:
        return hmac.new(self._secret, f"{key}\n{exp}".encode(), hashlib.sha256).hexdigest()

    def sign_get(self, key: str, ttl_s: int) -> str:
        check_key(key)
        exp = int(self._clock()) + ttl_s
        token = base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")
        return f"{self._base}{self.PATH}{token}?exp={exp}&sig={self._mac(key, exp)}"

    def verify(self, token: str, exp: str, sig: str) -> str | None:
        """The object key if the signature is valid and unexpired, else None (one answer for every failure)."""
        try:
            key = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
            check_key(key)
            expires = int(exp)
        except (ValueError, UnicodeDecodeError, BadKey):
            return None
        if not hmac.compare_digest(self._mac(key, expires), sig) or self._clock() >= expires:
            return None
        return key
