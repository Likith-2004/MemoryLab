"""Deterministic step session: scripted actions in, RAM trace and score out.

No model provider is involved; any iterable of validated actions (a scripted list, or a
fake provider in tests) drives the session.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Self

from .actions import Action, action_to_dict
from .bundle import GameBundle
from .constants import HARNESS_VERSION, RELEASE_FRAMES, canonical_json, semantics_sha256
from .emulator import Emulator, Limits
from .errors import CheckpointError, EmulatorFailure, StepLimitExceeded
from .score import ScoreTracker
from .screenshots import ScreenshotPolicy, encode_png, frame_sha256, scale_nearest
from .telemetry import RamView, Snapshot, parse_snapshot


@dataclass(frozen=True)
class StepRecord:
    step: int
    frames: int
    action: dict[str, Any] | None
    snapshot: Snapshot
    ram_sha256: str
    score: int
    new_milestones: tuple[int, ...]
    frame_sha256: str | None
    frame_png: bytes | None

    def trace_entry(self) -> dict[str, Any]:
        """The deterministic part of the record (PNG bytes excluded; its pixel hash is in)."""
        return {
            "step": self.step,
            "frames": self.frames,
            "action": self.action,
            "snapshot": self.snapshot.to_dict(),
            "ram_sha256": self.ram_sha256,
            "score": self.score,
            "new_milestones": list(self.new_milestones),
            "frame_sha256": self.frame_sha256,
        }


@dataclass(frozen=True)
class Trace:
    manifest_sha256: str
    entries: tuple[dict[str, Any], ...]
    score: int
    tiebreak_steps: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            canonical_json({"manifest_sha256": self.manifest_sha256, "entries": list(self.entries)})
        ).hexdigest()


@dataclass(frozen=True)
class Checkpoint:
    """Taken only at a step boundary. Holds everything needed to resume exactly."""

    state: bytes
    state_sha256: str
    step: int
    frames: int
    tracker: dict[str, Any]
    snapshot_sha256: str
    ram_sha256: str
    rom_sha256: str
    semantics_sha256: str
    pyboy_version: str


class Session:
    def __init__(
        self,
        bundle: GameBundle,
        limits: Limits | None = None,
        screenshots: ScreenshotPolicy | None = None,
    ) -> None:
        self.bundle = bundle
        self.limits = limits or Limits()
        self.screenshots = screenshots or ScreenshotPolicy()
        self.emulator = Emulator(bundle.rom, self.limits)
        self.tracker = ScoreTracker()
        self.step = 0
        self._prev: Snapshot | None = None
        self._ranges = bundle.state_map.read_ranges()
        self._opened = False

    # -- lifecycle -----------------------------------------------------------------
    def open(self) -> StepRecord:
        """Start the emulator, load the fixed start state, and observe step 0."""
        self.emulator.start()
        self.emulator.load_state(self.bundle.start_state, 0)
        self._opened = True
        return self._observe(0, 0, None)

    def close(self) -> None:
        self.emulator.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def manifest(self) -> dict[str, Any]:
        b = self.bundle
        return {
            "harness_version": HARNESS_VERSION,
            "semantics_sha256": semantics_sha256(),
            "release_frames": RELEASE_FRAMES,
            "pyboy_version": self.emulator.pyboy_version,
            "game": {"id": b.game_id, "rom_sha256": b.rom_info.sha256, "state_map_version": b.state_map.version,
                     "state_map_sha256": b.state_map.sha256, "fixture": b.is_fixture},
            "start_state_sha256": b.start_state_sha256,
            "game_seed": None,
            "cgb": b.rom_info.cgb,
            "observation": {"screenshot_policy": self.screenshots.mode, "k": self.screenshots.k,
                            "frame_scale": self.screenshots.frame_scale},
            "caps": {"max_steps": self.limits.max_steps, "max_total_frames": self.limits.max_total_frames},
        }

    def manifest_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.manifest())).hexdigest()

    # -- stepping ------------------------------------------------------------------
    def execute(self, action: Action) -> StepRecord:
        """Run one action as one step. Raises StepLimitExceeded / FrameLimitExceeded
        before touching the emulator when a cap would be crossed."""
        if not self._opened:
            raise EmulatorFailure("session is not open")
        if self.step >= self.limits.max_steps:
            raise StepLimitExceeded(f"step cap {self.limits.max_steps} reached")
        frames = self.emulator.execute(action)
        self.step += 1
        return self._observe(self.step, frames, action_to_dict(action))

    def run_script(self, actions: Iterable[Action]) -> Trace:
        """Open the session, run every action, and return the full deterministic trace."""
        records = [self.open()]
        for action in actions:
            records.append(self.execute(action))
        return Trace(
            self.manifest_sha256(),
            tuple(r.trace_entry() for r in records),
            self.tracker.score,
            self.tracker.tiebreak_steps,
        )

    def _observe(self, step: int, frames: int, action: dict[str, Any] | None) -> StepRecord:
        sm = self.bundle.state_map
        snapshot = parse_snapshot(sm, RamView(self._ranges, self.emulator.read_ram(self._ranges)))
        digest = self.emulator.ram_digest()
        new = self.tracker.update(step, snapshot.milestones)
        png = fsha = None
        if self.screenshots.should_capture(step, self._prev, snapshot):
            rgb = self.emulator.frame_rgb()
            fsha = frame_sha256(rgb)
            png = encode_png(scale_nearest(rgb, self.screenshots.frame_scale))
        self._prev = snapshot
        return StepRecord(step, frames, action, snapshot, digest, self.tracker.score, tuple(new), fsha, png)

    # -- checkpoints ---------------------------------------------------------------
    def checkpoint(self) -> Checkpoint:
        """Snapshot at a step boundary. The child refuses if any input is pending."""
        if not self._opened or self._prev is None:
            raise CheckpointError("session is not open")
        state = self.emulator.save_state()
        return Checkpoint(
            state=state,
            state_sha256=hashlib.sha256(state).hexdigest(),
            step=self.step,
            frames=self.emulator.frames,
            tracker=self.tracker.to_dict(),
            snapshot_sha256=self._prev.sha256(),
            ram_sha256=self.emulator.ram_digest(),
            rom_sha256=self.bundle.rom_info.sha256,
            semantics_sha256=semantics_sha256(),
            pyboy_version=self.emulator.pyboy_version or "",
        )

    def restore(self, cp: Checkpoint) -> None:
        """Resume from a checkpoint; verifies identity before and RAM after loading."""
        if hashlib.sha256(cp.state).hexdigest() != cp.state_sha256:
            raise CheckpointError("checkpoint state was modified")
        if cp.rom_sha256 != self.bundle.rom_info.sha256:
            raise CheckpointError("checkpoint is for a different ROM")
        if cp.semantics_sha256 != semantics_sha256() or cp.pyboy_version != self.emulator.pyboy_version:
            raise CheckpointError("checkpoint was made under different harness/emulator semantics")
        if not (0 <= cp.step <= self.limits.max_steps and 0 <= cp.frames <= self.limits.max_total_frames):
            raise CheckpointError("checkpoint step/frame counters exceed this session's caps")
        tracker = ScoreTracker.from_dict(cp.tracker)
        self.emulator.load_state(cp.state, cp.frames)
        snapshot = parse_snapshot(
            self.bundle.state_map, RamView(self._ranges, self.emulator.read_ram(self._ranges))
        )
        if snapshot.sha256() != cp.snapshot_sha256 or self.emulator.ram_digest() != cp.ram_sha256:
            self.emulator.close()  # state is not what the checkpoint promised: never reuse it
            raise CheckpointError("RAM after restore does not match the checkpoint")
        self.step, self.tracker, self._prev = cp.step, tracker, snapshot
