# Changelog

Format per milestone: scope, changes, tests/evidence, deviations, tag.

## M6: API and auth (branch `m6-api`, in progress: core delivered, remaining routes listed in HANDOFF)

- Local Supabase (CLI 3.0.0, Docker) with 3 migrations: core schema + RLS on every table, soft delete, ingest state. Access-token TTL 900 s, email confirmation required.
- `services/api`: JWT verification (ES256/RS256 via JWKS), RLS-backed reads under the caller's role, one visibility dependency (404 for missing/hidden/deleted), catalogue, runs (validate, create, list, get, patch, stop, delete, events, score, memory tree/file/diff, checkpoints, report), leaderboard, admin (runs, kill, breaker) with a live session re-check, uniform signup, rate limits, strict caps.
- `ingest.pg_repository`: Postgres `RunRepository` with row-lock transactions.
- Tests: API 231 (RLS 38, auth 17, authz matrix 136, real GoTrue 13 incl. 1 strict xfail documenting GoTrue's own signup enumeration, run creation and canary 15, admin 6, signup 6), ingest +7 against Postgres. Mutations caught: 4 RLS, 4 API, 1 session re-check, 1 repository lock.
- Gate (`make gate`) now needs the local stack running (`MEMORYLAB_REQUIRE_DB=1` makes an absent database a failure, not a skip).
- Found and fixed along the way: `run_configs` policy compared the wrong `id` (non-owners could never read a visible run's config); validation failures were consuming the creation quota; the first schema's `runs.status` set did not match the run lifecycle.
- NOT RUN: provider key validation (B-1), CAPTCHA, gateway rules, real signup email delivery beyond local mailpit, any load/abuse test (k6).
- NOT BUILT yet: `DELETE /me`, stream tickets + WebSocket, fork, frames (signed URLs), export, experiments and compare, admin reports/unverify, the queued-row reaper, `GET /runs/{id}/manifest`.
- Deviations/findings: DECISIONS M6-1..M6-10 (M6-2 and M6-7 need owner review).

## M5: sandbox and orchestrator (branch `m5-sandbox`, tag `m5-done`)

- Egress allowlist proxy, worker (stdin job, heartbeats, graceful stop), orchestrator (validation against server maxima, queue/TTL/limits/breaker, hardened container per run, heartbeat/wall kill, finalisation, audit), Dockerfiles with lockfile-hashed deps and pruned SDL libraries.
- Ingest: per-scope batch numbering, status endpoint, auth before body parsing.
- Tests: real-container probes (hardening, no egress, no key in inspect/top/environ, pids/memory limits, heartbeat kill). DEVIATION: Docker, not gVisor (M5-1).
- NOT RUN: gVisor; real provider egress.
- Provenance: a prior session ended without a state note and left 24 uncommitted files (modified docs, labharness agent/provider, ingest, Makefile, lockfile, plus the new egressproxy, worker, orchestrator and infra trees). They were reviewed and committed as M5 work. The review found `elif False:` in place of the heartbeat-timeout check in `orchestrator/sandbox.py`; it was restored and a real-clock regression test added (fails when the check is stubbed out).

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
