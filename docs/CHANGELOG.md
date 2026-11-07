# Changelog

Format per milestone: scope, changes, tests/evidence, deviations, tag.

## M4: event log, manifest, replay, export, ingest, redaction (branch `m4-runlog`, tag `m4-done`)

- `packages/runlog`: strict event model, run lifecycle state machine with writer roles, redaction (exact key + encodings + patterns), object store protocol + local implementation, zstd-segmented gapless event log with checksums and crash recovery, write-once sealed manifest, deterministic export bundle with credential scan, replay/verification (log integrity, detector recomputation, **re-execution of recorded actions against the emulator**), worker sinks (redacting, local, HTTP with idempotent batches).
- `services/ingest` (FastAPI): per-run HMAC tokens, scope separation, quotas, body limits, frame validation, manifest sealing, close + export.
- Loop changes: `action` events carry the validated action (enables re-execution), `content_filter` for memory writes, `frame_sink`.
- Python pinned to 3.14.
- Tests: 193 new (runlog 143, ingest 53 less overlap; gate total in HANDOFF); 12 deliberate mutations caught.
- Found and fixed by tests: full-tail resume bug, base64-alignment redaction gap, events accepted after a final state, validation errors echoing input, chunked-body 413, verifier crash on malformed bundles.
- Deviations: see DECISIONS M4-*.

## M3: agent loop and provider adapter (branch `m3-agent`, tag `m3-done`)

- Provider interface (`providers.base`), Anthropic adapter over httpx (retries, backoff, error mapping, no redirects, canary-key tested), `SecretKey`, `FakeProvider`, provider/model allowlist.
- Agent loop with hard caps on steps, tokens (provider-reported usage), wall time and frames; user stop; detectors (position loop, failed actions, memory/play ratio, churn, stale-note read, cap rejections) computed from events; closed game tool schemas; memory tools absent in the no-memory arm.
- Presets `none`, `general`, `failures_only`, `read_first_test_theories` (version 1, labelled adaptation, text hashes pinned); frozen base prompt; rolling context with API-valid structure.
- Untrusted game text handling (SECURITY.md U-1, U-5).
- Tests: 148 new (481 total in the gate): adapter 56, prompt/tools/presets 40, detectors and loop 52; all with the fake provider or a mock transport; 11 deliberate mutations caught.
- NOT RUN: any live model call (BLOCKED.md B-1).

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
