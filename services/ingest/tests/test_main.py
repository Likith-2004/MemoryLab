"""The process entrypoint fails at start on a missing or short secret, before it opens anything."""

import pytest

from ingest.main import build


def test_a_missing_secret_fails_at_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORYLAB_TOKEN_SECRET", raising=False)
    with pytest.raises(KeyError):
        build()


def test_a_short_secret_fails_at_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYLAB_TOKEN_SECRET", "short")
    with pytest.raises(ValueError):
        build()
