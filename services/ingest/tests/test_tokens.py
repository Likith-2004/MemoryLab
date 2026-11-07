import base64
import json
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ingest.tokens import MAX_TOKEN_CHARS, TokenError, issue_token, verify_token

SECRET = b"k" * 32
RUN = str(uuid.uuid4())
NOW = 1_000_000.0


def test_roundtrip_and_scopes() -> None:
    for scope in ("worker", "orchestrator"):
        claims = verify_token(SECRET, issue_token(SECRET, RUN, scope, 60, NOW), NOW)
        assert (claims.run_id, claims.scope) == (RUN, scope)


def test_expiry_boundaries() -> None:
    token = issue_token(SECRET, RUN, "worker", 60, NOW)
    assert verify_token(SECRET, token, NOW + 60)           # exp == now: still valid
    with pytest.raises(TokenError):
        verify_token(SECRET, token, NOW + 61)


def test_wrong_secret_and_tampering() -> None:
    token = issue_token(SECRET, RUN, "worker", 60, NOW)
    with pytest.raises(TokenError):
        verify_token(b"x" * 32, token, NOW)
    version, payload, mac = token.split(".")
    doc = json.loads(base64.urlsafe_b64decode(payload + "=="))
    for change in ({"scope": "orchestrator"}, {"rid": str(uuid.uuid4())}, {"exp": doc["exp"] + 9999}):
        forged = base64.urlsafe_b64encode(json.dumps({**doc, **change}).encode()).decode().rstrip("=")
        with pytest.raises(TokenError):
            verify_token(SECRET, f"{version}.{forged}.{mac}", NOW)
    flipped = mac[:-1] + ("A" if mac[-1] != "A" else "B")
    with pytest.raises(TokenError):
        verify_token(SECRET, f"{version}.{payload}.{flipped}", NOW)


@pytest.mark.parametrize("bad", [None, 5, b"v1.a.b", [], "", "v1", "v1.a", "v1.a.b.c", "v2.x.y", "." * 3, "x" * (MAX_TOKEN_CHARS + 1)])
def test_malformed_tokens(bad: object) -> None:
    with pytest.raises(TokenError):
        verify_token(SECRET, bad, NOW)


def test_issue_validation() -> None:
    for kwargs in ({"secret": b"short"}, {"run_id": "nope"}, {"scope": "admin"}, {"ttl_s": 0}, {"ttl_s": 10**9}):
        args = {"secret": SECRET, "run_id": RUN, "scope": "worker", "ttl_s": 60, **kwargs}
        with pytest.raises(ValueError):
            issue_token(args["secret"], args["run_id"], args["scope"], args["ttl_s"], NOW)  # type: ignore[arg-type]


@settings(max_examples=500, deadline=None)
@given(st.text(max_size=200) | st.binary(max_size=100) | st.none() | st.integers())
def test_arbitrary_input_only_ever_raises_token_error(junk: object) -> None:
    try:
        verify_token(SECRET, junk, NOW)
    except TokenError:
        pass


@settings(max_examples=200, deadline=None)
@given(st.integers(0, 3), st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_=", max_size=30))
def test_token_with_mutated_part_is_rejected(part: int, junk: str) -> None:
    pieces = issue_token(SECRET, RUN, "worker", 60, NOW).split(".")
    if part < 3 and pieces[part] != junk:
        pieces[part] = junk
        with pytest.raises(TokenError):
            verify_token(SECRET, ".".join(pieces), NOW)
