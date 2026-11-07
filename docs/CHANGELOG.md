# Changelog

Format per milestone: scope, changes, tests/evidence, deviations, tag.

## M2: memory service (branch `m2-memory`, tag `m2-done`)

- Versioned-row virtual filesystem (`labharness.memory`, `memory_paths`): five closed-schema tools, caps (files, per-file, total, history), `mem.read/write/delete/reject` events with full diff, copy-on-write snapshots by `rev`, forks, per-step tree view, version diff.
- Tests: 87 (unit, hostile-input property tests against an independent reference, stateful model test with snapshot-immutability invariants, no-host-disk proof, 8-thread concurrency); 10 deliberate mutations all caught.
- Deviations: M2-2 (rev + content in events), M2-5 (history budget). See DECISIONS.md.

## M1-close: Milestone 1 close-out (branch `m1-close`)

- `release_frames` per game in `bundle.json` (harness 0.2.0, semantics hash updated, golden regenerated; RAM digests per step unchanged).
- Cartridge RAM banks included in the trace digest; probe ROM `probe_sram.gb`; PyBoy bank-slice quirk handled.
- `labharness run` / `labharness calibrate` CLI (`python -m labharness`).
- Tests: `tick()` ended (real QUIT + wrapped ticks), behavioural idle-frame boundary test (+ negative control), RAM-layout parsing, CLI success/failure paths.
- Licence audit (THIRD_PARTY.md, scripts/licence_audit.py); SECURITY.md U-1..U-3; README notes; HANDOFF.md, BLOCKED.md.
- Repo: uv workspace, Makefile (`make gate`), gitleaks.

## Milestone 1: headless emulator step loop and deterministic RAM state reader (branch `milestone-1-emulator`)

Added `packages/labharness` (emulator layer only: no provider, memory service, or agent loop)
and `packages/games/fixture` (test-only ROM, CC0).

- Emulator client running PyBoy 2.9.0 headless in a child process (timeouts, rlimits, kill-on-hang).
- Closed-schema actions (`press`, `wait`) with sequence semantics; `RELEASE_FRAMES = 7` measured and frozen.
- Versioned, validated state map; normalised telemetry (core + per-game extras).
- Session: scripted execution, per-step RAM/telemetry trace, score tracker, screenshot policy, frame/step caps.
- Checkpoint/restore at step boundaries with integrity checks.
- Game bundle loader (sha256-pinned ROM, start state, state map; PyBoy version pinned).
- Tests: see docs/MILESTONE-1.md.

Harness version `0.1.0`. Findings recorded in docs/DECISIONS.md (M1-6, M1-10) and docs/GBSTUDIO_SYMBOLS.md.
