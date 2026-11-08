from conftest import Env


def test_signup_answers_the_same_for_new_and_existing_emails(env: Env) -> None:
    env.gateway.existing.add("old@example.test")
    a = env.client.post("/api/v1/auth/signup", json={"email": "old@example.test", "password": "x" * 12})
    b = env.client.post("/api/v1/auth/signup", json={"email": "new@example.test", "password": "x" * 12})
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (202, a.json())
    assert env.gateway.recovered == ["old@example.test"]               # existing account: recovery mail, not an error


def test_signup_email_is_normalised_so_case_cannot_dodge_the_existing_check(env: Env) -> None:
    env.gateway.existing.add("old@example.test")
    env.client.post("/api/v1/auth/signup", json={"email": "OLD@Example.test", "password": "x" * 12})
    assert env.gateway.recovered == ["old@example.test"]


def test_signup_validates_input_without_echoing_the_password(env: Env) -> None:
    for body in ({"email": "nope", "password": "x" * 12}, {"email": "a@b.co", "password": "short"},
                 {"email": "a@b.co", "password": "x" * 12, "role": "admin"}):
        r = env.client.post("/api/v1/auth/signup", json=body)
        assert r.status_code == 422 and "x" * 12 not in r.text and r.json()["code"] == "validation_failed"
    assert env.gateway.signups == []


def test_signup_is_rate_limited_per_ip(make_env: object) -> None:
    env: Env = make_env(signup_per_ip_per_hour=2)  # type: ignore[operator]
    codes = [env.client.post("/api/v1/auth/signup", json={"email": f"u{i}@example.test", "password": "x" * 12}).status_code
             for i in range(3)]
    assert codes == [202, 202, 429]


def test_signup_when_auth_is_down_still_answers_the_uniform_202(env: Env) -> None:
    """GoTrue failures cannot reach the caller (that would tell them something about the address); they are counted."""
    env.gateway.down = True
    r = env.client.post("/api/v1/auth/signup", json={"email": "a@b.co", "password": "x" * 12})
    assert r.status_code == 202 and r.json()["message"].startswith("If an account exists")


def test_the_entrypoint_fails_at_start_when_unconfigured(monkeypatch: object) -> None:
    import pytest

    from api.main import UnconfiguredKeyValidator, build
    for var in ("MEMORYLAB_JWKS_URL", "MEMORYLAB_ORCHESTRATOR_TOKEN"):
        monkeypatch.delenv(var, raising=False)  # type: ignore[attr-defined]
    with pytest.raises(KeyError):
        build()
    assert UnconfiguredKeyValidator().validate("anthropic", "sk-ant-anything-at-all") is False
