"""Generate the fixture's state map, start state and bundle.json from fixture.gb/.sym.

Addresses come from the symbol file produced by the ROM build, never typed by hand.
Run:  .venv/bin/python tools/make_fixture_assets.py ../games/fixture
"""

import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path

from labharness.actions import Wait
from labharness.emulator import Emulator
from labharness.rom import inspect_rom

CHARMAP = {f"0x{0x80 + i:02X}": chr(ord("A") + i) for i in range(26)} | {"0x7F": " "}


def read_sym(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith(";"):
            addr, name = line.split(maxsplit=1)
            out[name] = int(addr.split(":")[1], 16)
    return out


def main(directory: str) -> None:
    root = Path(directory)
    rom = (root / "fixture.gb").read_bytes()
    info = inspect_rom(rom)
    sym = read_sym(root / "fixture.sym")

    def h(name: str, offset: int = 0) -> str:
        return f"0x{sym[name] + offset:04X}"

    statemap = {
        "schema": 1,
        "state_map_version": 1,
        "game_id": "mlfixture",
        "rom_sha256": info.sha256,
        "core": {
            "map_id": {"addr": h("wMapId"), "width": 1},
            "x": {"addr": h("wX"), "width": 1},
            "y": {"addr": h("wY"), "width": 1},
            "dialog_active": {"addr": h("wDialog"), "width": 1},
            "milestones": {"addr": h("wMilestones"), "width": 2},
        },
        "extras": {
            "party": {
                "type": "struct_list",
                "count": {"addr": h("wPartyCount"), "width": 1},
                "count_max": 6,
                "base": h("wParty"),
                "stride": 4,
                "fields": {
                    "species": {"offset": 0, "width": 1},
                    "level": {"offset": 1, "width": 1},
                    "hp": {"offset": 2, "width": 1},
                    "max_hp": {"offset": 3, "width": 1},
                },
            },
            "flags": {"type": "bitfield", "addr": h("wFlags"), "width": 1, "names": {"0": "pressed_a"}},
            "dialog_text": {
                "type": "text", "addr": h("wDialogText"), "max_len": 16, "terminator": 0x50, "charmap": CHARMAP,
            },
            "input_frames": {"type": "uint", "addr": h("wInputFrames"), "width": 2},
            "frame_counter": {"type": "uint", "addr": h("wFrames"), "width": 2},
        },
    }
    sm_raw = (json.dumps(statemap, indent=2, sort_keys=True) + "\n").encode()
    (root / "fixture.statemap.json").write_bytes(sm_raw)

    marker = sym["wInitDone"]
    with Emulator(rom) as emu:  # cold boot: the only place a start state is produced
        for _ in range(100):
            emu.execute(Wait(10))
            if emu.read_ram([(marker, 1)]) == b"\xa5":
                break
        else:
            raise SystemExit("fixture did not finish initialising")
        emu.execute(Wait(60))
        state = emu.save_state()
    (root / "fixture.state").write_bytes(state)

    bundle = {
        "game_id": "mlfixture",
        "fixture": True,
        "licence": "CC0-1.0",
        "rom": {"file": "fixture.gb", "sha256": info.sha256},
        "start_state": {
            "file": "fixture.state", "sha256": hashlib.sha256(state).hexdigest(), "pyboy_version": version("pyboy"),
        },
        "state_map": {"file": "fixture.statemap.json", "sha256": hashlib.sha256(sm_raw).hexdigest()},
    }
    (root / "bundle.json").write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    print(json.dumps(bundle, indent=2))


if __name__ == "__main__":
    main(sys.argv[1])
