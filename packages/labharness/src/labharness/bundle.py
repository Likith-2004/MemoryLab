"""Game bundle: ROM + start state + state map, each pinned by sha256 and verified on load."""

import hashlib
import json
import re
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from .constants import PINNED_PYBOY
from .errors import BundleError
from .rom import RomInfo, inspect_rom
from .statemap import StateMap, parse_statemap

_SHA = re.compile(r"^[0-9a-f]{64}$")
MAX_FILE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class GameBundle:
    game_id: str
    licence: str
    is_fixture: bool
    rom: bytes
    rom_info: RomInfo
    start_state: bytes
    start_state_sha256: str
    state_map: StateMap
    pyboy_version: str


def _read(root: Path, name: str, expected_sha: str, what: str) -> bytes:
    path = (root / name).resolve()
    if root.resolve() not in path.parents:
        raise BundleError(f"{what}: path escapes the bundle directory")
    if not path.is_file():
        raise BundleError(f"{what}: missing")
    with open(path, "rb") as fh:  # read-only; the harness never writes to bundle files
        data = fh.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise BundleError(f"{what}: too large")
    if hashlib.sha256(data).hexdigest() != expected_sha:
        raise BundleError(f"{what}: sha256 mismatch")
    return data


def load_bundle(directory: str | Path) -> GameBundle:
    root = Path(directory)
    try:
        meta = json.loads((root / "bundle.json").read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"bundle.json unreadable: {exc}") from exc
    try:
        game_id, licence = meta["game_id"], meta["licence"]
        rom_f, rom_sha = meta["rom"]["file"], meta["rom"]["sha256"]
        st_f, st_sha, st_pyboy = meta["start_state"]["file"], meta["start_state"]["sha256"], meta["start_state"]["pyboy_version"]
        sm_f, sm_sha = meta["state_map"]["file"], meta["state_map"]["sha256"]
        is_fixture = meta.get("fixture", False) is True
    except (KeyError, TypeError) as exc:
        raise BundleError(f"bundle.json missing field: {exc}") from exc
    for sha in (rom_sha, st_sha, sm_sha):
        if not isinstance(sha, str) or not _SHA.match(sha):
            raise BundleError("bundle.json: sha256 values must be 64 lowercase hex chars")
    rom = _read(root, rom_f, rom_sha, "rom")
    rom_info = inspect_rom(rom)
    state = _read(root, st_f, st_sha, "start_state")
    state_map = parse_statemap(_read(root, sm_f, sm_sha, "state_map"))
    if state_map.rom_sha256 != rom_info.sha256:
        raise BundleError("state map is for a different ROM build")
    if state_map.game_id != game_id:
        raise BundleError("state map game_id does not match bundle")
    installed = version("pyboy")
    if st_pyboy != installed or installed != PINNED_PYBOY:
        raise BundleError(f"start state made with pyboy {st_pyboy}; installed {installed}, pinned {PINNED_PYBOY}")
    return GameBundle(game_id, licence, is_fixture, rom, rom_info, state, st_sha, state_map, st_pyboy)
