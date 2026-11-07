import json
import shutil
from pathlib import Path

import pytest

from helpers import FIXTURE_DIR
from labharness import load_bundle
from labharness.errors import BundleError

FILES = ["bundle.json", "fixture.gb", "fixture.state", "fixture.statemap.json"]


@pytest.fixture()
def copy(tmp_path: Path) -> Path:
    for name in FILES:
        shutil.copy(FIXTURE_DIR / name, tmp_path / name)
    return tmp_path


def test_fixture_bundle_loads_and_is_labelled_fixture(bundle) -> None:  # type: ignore[no-untyped-def]
    assert bundle.is_fixture and bundle.licence == "CC0-1.0" and bundle.pyboy_version == "2.9.0"
    assert bundle.rom_info.sha256 == bundle.state_map.rom_sha256


@pytest.mark.parametrize("name", ["fixture.gb", "fixture.state", "fixture.statemap.json"])
def test_tampered_file_refused(copy: Path, name: str) -> None:
    data = bytearray((copy / name).read_bytes())
    data[-3] ^= 0x01
    (copy / name).write_bytes(bytes(data))
    with pytest.raises(BundleError, match="sha256 mismatch"):
        load_bundle(copy)


def edit(copy: Path, fn) -> None:  # type: ignore[no-untyped-def]
    meta = json.loads((copy / "bundle.json").read_text())
    fn(meta)
    (copy / "bundle.json").write_text(json.dumps(meta))


def test_path_escape_refused(copy: Path) -> None:
    edit(copy, lambda m: m["rom"].update(file="../fixture/fixture.gb"))
    with pytest.raises(BundleError, match="escapes"):
        load_bundle(copy)


def test_start_state_from_other_pyboy_version_refused(copy: Path) -> None:
    edit(copy, lambda m: m["start_state"].update(pyboy_version="2.8.0"))
    with pytest.raises(BundleError, match="pyboy"):
        load_bundle(copy)


def test_missing_field_and_missing_file(copy: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    edit(copy, lambda m: m.pop("licence"))
    with pytest.raises(BundleError):
        load_bundle(copy)
    with pytest.raises(BundleError):
        load_bundle(tmp_path_factory.mktemp("empty"))


def test_state_map_for_other_rom_refused(copy: Path) -> None:
    import hashlib
    doc = json.loads((copy / "fixture.statemap.json").read_text())
    doc["rom_sha256"] = "0" * 64
    raw = json.dumps(doc).encode()
    (copy / "fixture.statemap.json").write_bytes(raw)
    edit(copy, lambda m: m["state_map"].update(sha256=hashlib.sha256(raw).hexdigest()))
    with pytest.raises(BundleError, match="different ROM"):
        load_bundle(copy)
