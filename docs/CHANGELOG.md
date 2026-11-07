# Changelog

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
