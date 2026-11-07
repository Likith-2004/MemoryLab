"""Frozen harness semantics. Changing any value here requires a HARNESS_VERSION bump."""

import hashlib
import json

HARNESS_VERSION = "0.2.0"
PINNED_PYBOY = "2.9.0"

# Button vocabulary matches the architecture's `press` tool.
BUTTONS: tuple[str, ...] = ("A", "B", "Up", "Down", "Left", "Right", "Start", "Select")

MAX_BUTTONS_PER_ACTION = 8
HOLD_FRAMES_MIN = 1
HOLD_FRAMES_MAX = 60
WAIT_FRAMES_MAX = 600
ACTION_FRAMES_MAX = 600

# Release frames (the button stays released after each press in a sequence) are a per-game
# value stored in bundle.json and measured with `labharness calibrate`; they are part of
# the semantics hash. Bounds only are fixed here.
RELEASE_FRAMES_MIN = 1  # 0 would leave the release event pending at the step boundary
RELEASE_FRAMES_MAX = 60

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
    "render": "last frame of each action",
    "game_seed": None,
    "ram_digest_regions": [list(WRAM), list(HRAM), "cartridge RAM banks (when present)"],
}


def canonical_json(obj: object) -> bytes:
    """Canonical JSON used for every hash in the harness."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def semantics_sha256(release_frames: int) -> str:
    """Hash of every frozen rule, including the game's release-frame count."""
    return hashlib.sha256(canonical_json({**SEMANTICS, "release_frames": release_frames})).hexdigest()
