import itertools
import uuid

import pytest

from runlog.store import LocalObjectStore


@pytest.fixture()
def store(tmp_path):  # type: ignore[no-untyped-def]
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture()
def run_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def clock():  # type: ignore[no-untyped-def]
    counter = itertools.count(1)
    return lambda: f"2026-11-08T00:00:{next(counter) % 60:02d}.000Z"


@pytest.fixture(scope="session")
def game_bundle():  # type: ignore[no-untyped-def]
    from pathlib import Path

    from labharness import load_bundle
    return load_bundle(Path(__file__).resolve().parents[2] / "games" / "fixture")
