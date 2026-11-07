"""Shared test helpers: fixture bundle location, build-symbol reader, and the scripted route."""

from pathlib import Path

from labharness import Press, Wait
from labharness.actions import Action

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "games" / "fixture"


def read_sym() -> dict[str, int]:
    """Addresses as emitted by the ROM build (independent of the state map file)."""
    out: dict[str, int] = {}
    for line in (FIXTURE_DIR / "fixture.sym").read_text().splitlines():
        if line and not line.startswith(";"):
            addr, name = line.split(maxsplit=1)
            out[name] = int(addr.split(":")[1], 16)
    return out


def route() -> list[Action]:
    """Fixed script touching every fixture feature: movement, both maps, dialogue, flags,
    all five milestone conditions, party HP change, and idle waits."""
    return [
        Press(("Right", "Right", "Right"), 1),     # x=3
        Press(("Down", "Down", "Down"), 1),        # (3,3) -> milestone bit 0
        Press(("Start",), 3),                      # dialogue opens (map 1 text)
        Wait(5),
        Press(("B",), 2),                          # dialogue closes
        Press(("A",), 1),                          # flag bit 0
        Press(("Right",) * 8, 1),
        Press(("Right",) * 5, 1),                  # x: 3 -> 16 => map 2, x=0 -> milestone bit 1
        Press(("Start",), 1),                      # dialogue on map 2 -> milestone bit 2
        Press(("B",), 1),
        Press(("Right",) * 5, 2),                  # x=5
        Press(("Down",) * 2, 2),                   # y=5 -> (5,5) on map 2 -> milestone bit 3
        Wait(30),
        Press(("Down",) * 8, 1),
        Press(("Down",) * 2, 1),                   # y=15 -> milestone bit 8
        Press(("Left", "Up", "Select"), 4),
        Wait(600),
    ]


def other_route() -> list[Action]:
    """A different script, for negative controls."""
    return [Press(("Down", "Down"), 1), Press(("Right",), 5), Wait(17)]
