import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from conftest import KID, PRIVATE, Env, bearer, mint


def test_a_valid_token_reaches_a_user_route(env: Env) -> None:
    uid = env.user()
    r = env.client.get("/api/v1/me", headers=bearer(uid))
    assert r.status_code == 200 and r.json()["id"] == uid and r.json()["role"] == "user"


@pytest.mark.parametrize("make", [
    lambda uid: {},                                                             # no header
    lambda uid: {"authorization": "Bearer"},
    lambda uid: {"authorization": "Bearer not.a.jwt"},
    lambda uid: {"authorization": "Basic abc"},
    lambda uid: bearer(uid, exp_in=-10),                                        # expired
    lambda uid: bearer(uid, aud="other"),                                       # wrong audience
    lambda uid: bearer(uid, kid="unknown-kid"),                                 # unknown key
    lambda uid: bearer(uid, role="anon"),                                       # anon key is not a session
    lambda uid: bearer(uid, role="service_role"),
    lambda uid: bearer(uid, key=ec.generate_private_key(ec.SECP256R1())),       # signed by someone else
    lambda uid: bearer("not-a-uuid"),
])
def test_bad_tokens_are_401_on_user_routes(env: Env, make: object) -> None:
    uid = env.user()
    r = env.client.get("/api/v1/me", headers=make(uid))  # type: ignore[operator]
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
    assert r.headers["content-type"].startswith("application/problem+json")


def test_alg_none_and_hs256_confusion_are_rejected(env: Env) -> None:
    import base64
    import json
    uid = env.user()
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    none_token = f"{b64({'alg': 'none', 'kid': KID, 'typ': 'JWT'})}.{b64({'sub': uid, 'aud': 'authenticated', 'exp': int(time.time()) + 600, 'role': 'authenticated'})}."
    assert env.client.get("/api/v1/me", headers={"authorization": f"Bearer {none_token}"}).status_code == 401
    # an HS256 token keyed with the public JWK bytes (classic algorithm confusion)
    import hashlib
    import hmac

    from cryptography.hazmat.primitives import serialization
    pub = PRIVATE.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    signing = f"{b64({'alg': 'HS256', 'kid': KID, 'typ': 'JWT'})}.{b64({'sub': uid, 'aud': 'authenticated', 'exp': int(time.time()) + 600, 'role': 'authenticated'})}"
    sig = base64.urlsafe_b64encode(hmac.new(pub, signing.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    hs = f"{signing}.{sig}"
    assert env.client.get("/api/v1/me", headers={"authorization": f"Bearer {hs}"}).status_code == 401


def test_a_bad_token_on_a_public_route_is_401_not_silently_anonymous(env: Env) -> None:
    assert env.client.get("/api/v1/games").status_code == 200
    assert env.client.get("/api/v1/games", headers={"authorization": "Bearer junk"}).status_code == 401


def test_identity_comes_from_the_token_never_the_body_or_headers(env: Env) -> None:
    a, b = env.user(), env.user()
    r = env.client.get("/api/v1/me", headers={**bearer(a), "x-user-id": b}, params={"user_id": b})
    assert r.json()["id"] == a


def test_a_jwks_outage_fails_closed(env: Env, make_env: object) -> None:
    from api.auth import JwtVerifier, Unauthenticated

    def boom() -> dict[str, object]:
        raise RuntimeError("down")

    v = JwtVerifier(boom, audience="authenticated", issuer=None, algorithms=("ES256",))
    with pytest.raises(Unauthenticated):
        v.verify(mint(env.user()))


def test_unknown_kid_refetches_at_most_once_per_interval() -> None:
    from api.auth import JwtVerifier, Unauthenticated
    from conftest import JWK
    now = [1000.0]
    calls = [0]

    def fetch() -> dict[str, object]:
        calls[0] += 1
        return {"keys": [JWK]}

    v = JwtVerifier(fetch, audience="authenticated", issuer=None, algorithms=("ES256",), clock=lambda: now[0])
    for _ in range(20):
        with pytest.raises(Unauthenticated):
            v.verify(mint("00000000-0000-0000-0000-000000000001", kid="attacker-kid"))
    assert calls[0] == 1                                                    # no fetch amplification
