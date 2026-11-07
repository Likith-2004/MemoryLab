# Decisions

Format: ID, decision, reason, status. Spec decisions D1-D6 live in the architecture document.

## Milestone 1 (approved 2026-11-08 unless noted)

| ID | Decision | Reason |
|---|---|---|
| M1-1 | ROM for Milestone 1 is a **test-only fixture** (hand-written RGBDS, CC0), labelled `fixture: true`. GB Studio (Option A) is a separate design track; only its symbol-export path is verified here. No Option B (no third-party or Pokemon ROM). | Verifiable RAM map with no legal exposure (D1). |
| M1-2 | Telemetry = game-agnostic core (`map_id`, `x`, `y`, `dialog_active`, `milestones`) + per-game **extras declared in the state map** (`party`, `flags`, `dialog_text` with charmap, plain `uint`). Out-of-range data is clamped and reported in `anomalies`, never silently dropped. | Reader stays game-agnostic. |
| M1-3 | Score = popcount of the OR of every milestone mask ever seen; tiebreak = step at which the last milestone was first reached (`first_step` per bit kept). | Approved. |
| M1-4 | Button **sequence** semantics: for each button press, hold `hold_frames`, release, wait `RELEASE_FRAMES`, next. `hold_frames` 1..60, <= 8 buttons, `wait` 1..600, total frames per action <= 600 (validated). Not a chord. | Approved. |
| M1-5 | `RELEASE_FRAMES = 7`, **measured on the fixture** (`calibration.py`: smallest R where repeated 1-frame presses of every direction each register, for all R' >= R up to 20) and frozen in `constants.py`; part of `semantics_sha256`. A test re-measures it. A real game must be re-measured and the harness version bumped. | Approved. Release must be >= 1 anyway: with 0 the release event is still pending at the boundary. |
| M1-6 | Checkpoints only at step boundaries with no pending input. **Deviation from wording:** PyBoy's delayed-input heap (`queued_input`) is a private Cython attribute and cannot be read from Python. Emptiness is guaranteed by construction (worker uses only `button_press`/`button_release`, never delayed input; a test greps the worker for the forbidden calls) and asserted via what is observable: `pyboy.events == []` and the worker's held-button set is empty. Restore re-verifies RAM digest + snapshot hash and fails closed. | Closest enforceable form of the approved rule. |
| M1-7 | PyBoy runs in a **spawned child process** with a per-call timeout (default 30 s; child killed on expiry), startup timeout, `RLIMIT_AS` (default 2 GiB, floor 768 MiB: measured, 512 MiB cannot import PyBoy), `RLIMIT_CPU`, no core dumps. Parent<->child wire format is JSON header + raw bytes (no pickle from the child). | Approved. |
| M1-8 | `seed` dropped; manifest carries `game_seed: null`. The emulator has no seed; reproducibility comes from identical ROM + start state + input timing. | Approved. |
| M1-9 | Frames scaled in the harness, integer nearest-neighbour (1..8), deterministic PNG; trace stores a hash of the *unscaled* RGB pixels. | Approved. |
| M1-10 | Pin and record `pyboy==2.9.0` (checked in the child and in every bundle/checkpoint). RTC cartridges (types 0x0F, 0x10) refused in all modes. **ROM is passed to PyBoy as bytes, never a path**, and the child always `stop(save=False)`: PyBoy then has no path from which to create `.ram`/`.rtc` files (stronger than a read-only path; tested). | Approved; implementation stricter than "read-only path". |
| M1-11 | Toolchain: `uv` venv + `uv.lock`; rgbds is a **build tool only** (not a runtime dependency). No sudo was available, so rgbds **1.0.1** was installed from the upstream release tarball (sha256 `80a5cad8...6dad9` verified) to `~/.local/opt/rgbds-1.0.1`, not the distro's 1.0.4. The fixture ROM rebuilds byte-for-byte from it (tested). | Approved install; version pinned for reproducibility. |
| M1-12 | State maps are strict JSON (schema 1): hex-string addresses, only WRAM `0xC000-0xDFFF` / HRAM `0xFF80-0xFFFE`, no overlaps, closed key sets, bound to a ROM sha256. The committed fixture map is generated from the build's `.sym`, and a test re-checks every address against it. Per-field `endian`; BCD is not implemented (not needed by the fixture). | No invented addresses. |

## Open (not decided here)

- Real public game and its RAM map (spec decision #1; Option A track).
- Hosting / sandbox runtime (spec decision #2; Milestone 3).
