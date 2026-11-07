import pytest

from helpers import FIXTURE_DIR
from labharness import GameBundle, load_bundle


@pytest.fixture(scope="session")
def bundle() -> GameBundle:
    return load_bundle(FIXTURE_DIR)
