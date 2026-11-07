"""Measure the release-frame constant on a bundle (used once to freeze RELEASE_FRAMES)."""

from dataclasses import dataclass

from .bundle import GameBundle
from .emulator import Emulator, Limits
from .telemetry import RamView, parse_snapshot

DIRECTIONS = ("Right", "Down", "Left", "Up")
PRESSES = 4
MIN_CANDIDATE = 1  # 0 would leave the release event pending at the boundary
MAX_CANDIDATE = 20


@dataclass(frozen=True)
class Calibration:
    release_frames: int
    passes: dict[int, bool]


def measure_release_frames(bundle: GameBundle) -> Calibration:
    """Smallest R such that, for every R' in R..MAX_CANDIDATE, PRESSES consecutive
    1-frame presses of each direction (R' release frames between them) each change the
    player's position. Everything runs on one emulator, restoring the start state per trial."""
    sm = bundle.state_map
    ranges = sm.read_ranges()

    def position(emu: Emulator) -> tuple[int, int, int]:
        s = parse_snapshot(sm, RamView(ranges, emu.read_ram(ranges)))
        return (s.map_id, s.x, s.y)

    def press(emu: Emulator, button: str, release: int) -> None:
        emu._call({"cmd": "press", "buttons": [button], "hold": 1, "release": release})

    passes: dict[int, bool] = {}
    with Emulator(bundle.rom, Limits()) as emu:
        for release in range(MIN_CANDIDATE, MAX_CANDIDATE + 1):
            ok = True
            for direction in DIRECTIONS:
                emu.load_state(bundle.start_state)
                for _ in range(6):  # move to the middle of the map so no direction is blocked
                    press(emu, "Right", 60)
                    press(emu, "Down", 60)
                before = position(emu)
                for _ in range(PRESSES):
                    press(emu, direction, release)
                    after = position(emu)
                    ok &= after != before
                    before = after
            passes[release] = ok
    for candidate in range(MIN_CANDIDATE, MAX_CANDIDATE + 1):
        if all(passes[r] for r in range(candidate, MAX_CANDIDATE + 1)):
            return Calibration(candidate, passes)
    raise RuntimeError("no release-frame value up to MAX_CANDIDATE registers every press")
