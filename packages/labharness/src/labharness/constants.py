"""Frozen harness semantics. Changing any value here requires a HARNESS_VERSION bump."""

import hashlib
import json

HARNESS_VERSION = "0.1.0"
PINNED_PYBOY = "2.9.0"

# Button vocabulary matches the architecture's `press` tool.
BUTTONS: tuple[str, ...] = ("A", "B", "Up", "Down", "Left", "Right", "Start", "Select")

MAX_BUTTONS_PER_ACTION = 8
HOLD_FRAMES_MIN = 1
HOLD_FRAMES_MAX = 60
WAIT_FRAMES_MAX = 600
ACTION_FRAMES_MAX = 600

# Frames the button stays released after each press in a sequence. Measured on the test
# fixture by labharness.calibration.measure_release_frames (smallest value for which
# repeated presses of every direction each register exactly once); asserted by tests.
RELEASE_FRAMES = 7

# Game-agnostic RAM regions a state map may read, and the regions hashed into traces.
WRAM = (0xC000, 0xE000)
HRAM = (0xFF80, 0xFFFF)

SEMANTICS = {
    "button_sequence": "press, hold, release, release_frames, next",
    "buttons": list(BUTTONS),
    "max_buttons_per_action": MAX_BUTTONS_PER_ACTION,
    "hold_frames": [HOLD_FRAMES_MIN, HOLD_FRAMES_MAX],
    "wait_frames_max": WAIT_FRAMES_MAX,
    "action_frames_max": ACTION_FRAMES_MAX,
    "release_frames": RELEASE_FRAMES,
    "render": "last frame of each action",
    "game_seed": None,
    "ram_digest_regions": [list(WRAM), list(HRAM)],
}


def canonical_json(obj: object) -> bytes:
    """Canonical JSON used for every hash in the harness."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def semantics_sha256() -> str:
    return hashlib.sha256(canonical_json(SEMANTICS)).hexdigest()
