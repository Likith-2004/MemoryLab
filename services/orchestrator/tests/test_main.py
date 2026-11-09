"""The process entrypoint reads everything from the environment and refuses a weak or missing secret at start."""

import pytest

from orchestrator.main import config_from_env

ENV = {"MEMORYLAB_ORCHESTRATOR_TOKEN": "t" * 40, "MEMORYLAB_TOKEN_SECRET": "s" * 40, "MEMORYLAB_WORKER_IMAGE": "memorylab-worker:dev",
       "MEMORYLAB_SANDBOX_NETWORK": "memorylab-sandbox", "MEMORYLAB_PROXY_URL": "http://egress:3128",
       "MEMORYLAB_SANDBOX_INGEST_URL": "http://ingest:8000", "MEMORYLAB_INGEST_URL": "http://ingest:8000", "MEMORYLAB_GAMES_DIR": "/opt/games"}


def test_the_config_comes_from_the_environment() -> None:
    cfg, token = config_from_env(dict(ENV, MEMORYLAB_MAX_CONCURRENT="3", MEMORYLAB_SANDBOX_RUNTIME="runsc"))
    assert token == b"t" * 40 and cfg.token_secret == b"s" * 40 and cfg.max_concurrent == 3 and cfg.resources.runtime == "runsc"
    assert cfg.allow_fake_provider is False                                      # never switchable from the environment
    assert config_from_env(ENV)[0].resources.runtime is None


@pytest.mark.parametrize("name", sorted(ENV))
def test_a_missing_value_fails_at_start(name: str) -> None:
    with pytest.raises(KeyError):
        config_from_env({k: v for k, v in ENV.items() if k != name})


@pytest.mark.parametrize("name", ["MEMORYLAB_ORCHESTRATOR_TOKEN", "MEMORYLAB_TOKEN_SECRET"])
def test_a_short_secret_fails_at_start(name: str) -> None:
    with pytest.raises(ValueError):
        config_from_env(dict(ENV, **{name: "short"}))


def test_the_secrets_can_come_from_files(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "t").write_text("t" * 40 + "\n")
    (tmp_path / "s").write_text("s" * 40 + "\n")
    env = {k: v for k, v in ENV.items() if k not in ("MEMORYLAB_ORCHESTRATOR_TOKEN", "MEMORYLAB_TOKEN_SECRET")}
    cfg, token = config_from_env({**env, "MEMORYLAB_ORCHESTRATOR_TOKEN_FILE": str(tmp_path / "t"), "MEMORYLAB_TOKEN_SECRET_FILE": str(tmp_path / "s")})
    assert token == b"t" * 40 and cfg.token_secret == b"s" * 40
    with pytest.raises(ValueError):                                              # both forms at once: refused, not silently one of them
        config_from_env({**ENV, "MEMORYLAB_TOKEN_SECRET_FILE": str(tmp_path / "s")})
