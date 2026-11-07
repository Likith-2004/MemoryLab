import os
import subprocess
from pathlib import Path

import pytest

from helpers import FIXTURE_DIR

RGBDS = Path(os.environ.get("RGBDS_DIR", Path.home() / ".local/opt/rgbds-1.0.1"))


@pytest.mark.skipif(not (RGBDS / "rgbasm").exists(), reason="pinned rgbds 1.0.1 not installed")
def test_fixture_rom_and_symbols_rebuild_byte_for_byte(tmp_path: Path) -> None:
    version = subprocess.run([RGBDS / "rgbasm", "--version"], capture_output=True, text=True, check=True).stdout
    assert version.strip() == "rgbasm v1.0.1"
    subprocess.run([FIXTURE_DIR / "build.sh", str(tmp_path)], check=True, capture_output=True,
                   env={**os.environ, "RGBDS_DIR": str(RGBDS)})
    for name in ("fixture.gb", "fixture.sym"):
        assert (tmp_path / name).read_bytes() == (FIXTURE_DIR / name).read_bytes(), f"{name} is stale"
