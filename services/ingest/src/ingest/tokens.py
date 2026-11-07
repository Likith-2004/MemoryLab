"""Per-run bearer tokens: `v1.<payload>.<mac>`, HMAC-SHA256 over the payload.

A token names exactly one run and one scope ("worker" or "orchestrator") and expires. It cannot
be used for any other run by construction (the service compares the token's run to the path).
The signing secret is a service secret shared by the orchestrator and ingest; it is not a
visitor key and never reaches the sandbox (only the minted token does).
"""

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

SCOPES = frozenset({"worker", "orchestrator"})
MAX_TOKEN_CHARS = 512
MIN_SECRET_BYTES = 32
_RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class TokenError(Exception):
    """Invalid, expired or malformed token. The message is deliberately uniform."""


@dataclass(frozen=True)
class Claims:
    run_id: str
    scope: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: bytes, payload_b64: str) -> str:
    return _b64(hmac.new(secret, b"v1." + payload_b64.encode(), hashlib.sha256).digest())


def issue_token(secret: bytes, run_id: str, scope: str, ttl_s: int, now: float) -> str:
    if len(secret) < MIN_SECRET_BYTES:
        raise ValueError("token secret must be at least 32 bytes")
    if scope not in SCOPES or not _RUN_ID.fullmatch(run_id) or not 1 <= ttl_s <= 86_400 * 2:
        raise ValueError("invalid token parameters")
    payload = _b64(json.dumps({"rid": run_id, "scope": scope, "exp": int(now) + ttl_s}, sort_keys=True,
                              separators=(",", ":")).encode())
    return f"v1.{payload}.{_mac(secret, payload)}"


def verify_token(secret: bytes, token: object, now: float) -> Claims:
    try:
        if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS:
            raise TokenError
        version, payload_b64, mac = token.split(".")
        if version != "v1" or not hmac.compare_digest(mac, _mac(secret, payload_b64)):
            raise TokenError
        doc = json.loads(_unb64(payload_b64))
        run_id, scope, exp = doc["rid"], doc["scope"], doc["exp"]
        if (set(doc) != {"rid", "scope", "exp"} or scope not in SCOPES or not _RUN_ID.fullmatch(run_id)
                or isinstance(exp, bool) or not isinstance(exp, int) or exp < now):
            raise TokenError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise TokenError("invalid or expired token") from None
    return Claims(run_id, scope)
