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

## Autonomous charter and M1-close (2026-11-08)

Entry format for charter decisions: ID, context, options, choice, reason, how to reverse.

**C-1 Charter supersedes the per-milestone approval gate.** Context: the earlier instruction required explicit human approval between milestones; the autonomous build charter says to proceed without asking and review only at the end. Choice: follow the charter; each milestone still passes its own gate (tests, lint, strict types, gitleaks) before merge. Reverse: stop after any tag `m<N>-done`.

**C-2 No model key available.** Context: no `.env` exists. Choice: every real-model check is marked "NOT RUN"; all CI uses the fake provider; BLOCKED.md B-1. Reverse: add the key to `.env`.

**M1C-1 `release_frames` is per game (supersedes M1-5's global constant).** Context: the value is a property of how a game handles input, measured on the fixture only. Options: keep global; per-bundle. Choice: required `release_frames` (1..60) in `bundle.json`, measured with `labharness calibrate`, included in `semantics_sha256` and the manifest; `Press` cost is computed per game and checked against the 600-frame cap at execution. Harness bumped to 0.2.0; the golden trace was regenerated and the diff was only `manifest_sha256`/`trace_sha256` (per-step RAM digests unchanged). Reverse: restore the constant in `constants.py` and drop the bundle field.

**M1C-2 Cartridge RAM is part of the RAM digest.** Digest = sha256(WRAM, HRAM, every external RAM bank), bank layout from the ROM header (MBC2: one 512-byte bank). Quirk: PyBoy 2.9.0 rejects a bank slice ending exactly at the bank size, so the last byte of each bank is read separately; tested with a probe ROM (`probe_sram.gb`, MBC5+RAM+battery) by writing each bank and the last byte. Reverse: drop the bank loop (digest returns to WRAM+HRAM).

**M1C-3 uv workspace at repo root.** One venv/lock (`/.venv`, `uv.lock`) for every Python package so services can import `labharness`. Reverse: per-package venvs.

**M1C-4 gitleaks 8.30.1 binary** from the upstream release, sha256 verified, in `~/.local/opt`. No sudo available.

**M1C-5 Licence audit** (docs/THIRD_PARTY.md): all runtime dependencies are permissive or weak copyleft (PyBoy LGPL-3.0, pysdl2-dll MPL-2.0). Rule going forward: no GPL/AGPL/SSPL/BUSL dependencies. REVIEW: LGPL image-distribution question needs legal sign-off before any public launch (LAUNCH.md).

**M1C-6 `tick()` ended is a typed worker error**; tested with PyBoy's real QUIT event and a wrapped `tick` at each phase (wait, hold, release).

## M2 memory service (2026-11-08)

**M2-1 Paths are accepted only in canonical form, never rewritten.** Context: the plan says paths are "normalised" with regex `[a-z0-9_\-/.]{1,120}`, depth <= 4, no `..`, no absolute paths. Options: silently rewrite (lowercase, collapse `//`, strip `./`), or reject. Choice: reject anything not already canonical, with an informative tool error. Reason: rewriting creates aliasing (`A` and `a`, `a//b` and `a/b`) and ambiguity in logs; rejecting is simpler to prove safe (property test against an independent reference implementation). Also rejected: empty, `.` and `..` segments (but `...` and `..a` are legal names; paths are only keys). Reverse: add a normalising step in `check_path`.

**M2-2 Added `rev` (global per store) next to the per-path `version`; `mem.write` events carry the full `content`.** DEVIATION from the data-model and event-schema sections. Context: snapshots and checkpoints need one pointer into memory history, and the ingest service (M4) must rebuild `memory_files` rows from events; a diff alone cannot do that reliably. Choice: rows are `(run_id, path, version, step, rev, content, chars, deleted)`; `mem.write` data = path, version, step, rev, before/after sha256, chars, created, unified diff, content. Redaction (M4) applies to content and diff. Reverse: drop `rev`, point snapshots at a step, reconstruct from diffs.

**M2-3 `mem_search` is case-insensitive substring only.** The plan allows "substring/BM25". BM25 adds ranking state for little benefit at <= 10 small files. Results ranked by match count then path, <= 20 results, 200-char snippets, query 1..200 chars. Reverse: add BM25 behind the same tool.

**M2-4 `max_file_chars` <= 8192 so `mem_read` never truncates** (plan: "<= 8 KB returned"). A model that cannot read the rest of a file is worse off than with a lower cap. Defaults: per-file 4000, total 20000, files 10 (manifest example). Caps count characters, not bytes.

**M2-5 History budget (added caps).** DEVIATION (additive). Every write keeps its version, so rows and characters retained are bounded: `max_history_rows` (5000) and `max_history_chars` (8M); writes beyond that are rejected with reason `history_cap`, logged as `mem.reject`. Reason: resource exhaustion; unbounded versioning could grow worker RAM with steps x tool calls x 8 KB. Reverse: raise or remove the limits in `MemoryConfig`.

**M2-6 Content rules.** Opaque text stored verbatim. Rejected: non-strings, unpaired surrogates (cannot be encoded to UTF-8/JSONL), control characters other than tab/LF/CR, and DEL. Never interpreted, executed, used as a URL, tool name or path (tested with an XSS/injection payload corpus; rendering rules are enforced in M7).

**M2-7 The live store is in worker memory; persistence is event-driven.** The worker has no DB credentials (plan). The ingest service builds `memory_files` from `mem.*` events (M4). No host disk is ever used (AST check on the modules' imports/calls plus a test that makes file opens raise).

**M2-8 Error conventions.** Bad model input never raises: it becomes a `mem.reject` outcome with a stable reason (`invalid_path`, `invalid_content`, `invalid_args`, `unknown_tool`, `memory_disabled`, `path_conflict`, `files_cap`, `total_cap`, `file_cap`, `history_cap`, `not_found` for delete). Reading a missing file is a normal `mem.read` with `found: false`. Programmer errors (a step that goes backwards) raise `ValueError`. Identical rewrites still create a version (empty diff). A file cannot also be a folder (`path_conflict`).

**M2-9 Mutations are serialised with a lock** so caps stay exact if the agent loop and the dream worker (M8) ever share a store; tested with 8 concurrent writers.

## M3 agent loop and provider adapter (2026-11-08)

**M3-1 Direct HTTPS via httpx instead of the vendor SDK.** Options: `anthropic` SDK; raw httpx. Choice: raw httpx. Reason: full control over redirects (never followed), proxy/CA environment (ignored), exception chaining (so no request/headers ever ride on an error), response size, retry policy, and a much smaller dependency surface for a component that handles visitor keys. Cost: we track API shape changes ourselves. Reverse: implement `Provider` with the SDK; the interface is unchanged.

**M3-2 Provider-neutral interface.** `Provider.step(ProviderRequest) -> ProviderResult` (plan: `step(messages, tools) -> ProviderResult{actions, summary, usage}`), with explicit system blocks, per-call timeout and output-token budget added to the request. Adapters convert to wire format; the loop knows no vendor.

**M3-3 `tool_choice: {"type": "any"}`.** Every step must act. REVIEW: unverified with the newest models; text-only replies are tolerated (the step still counts) if a provider ignores it. Also unverified: prompt caching on the system block (harmless if ignored) and that `temperature` is omitted.

**M3-4 Token accounting counts every reported token** (input + output + cache creation + cache read). Conservative for the cap; cost estimates (M6) will weight them. The token cap can be overshot by at most one model call (usage is only known afterwards); the output budget for each call shrinks to the remaining cap.

**M3-5 A step is one model call.** It can contain several tool calls (cap 8 per step, a platform limit, not in the plan) and produces one observation; `Session.advance` + `Session.finish_step` split the old `execute`. A memory-only or text-only step still counts and is observed. Trace entries for loop steps record `{"batch": [...]}`.

**M3-6 Cap semantics.** Checked before each model call and after each step: steps, tokens, wall (injectable clock; each provider call timeout is bounded by the wall time left), frames. A cap tripped mid-step lets the step finish (frame cap: remaining calls in that step are answered `not_executed`), then `cap.hit` -> `stopping` -> `capped`. User stop = `completed/user_stop`; provider or emulator errors = `failed`; `killed` is orchestrator-only. Unexpected exceptions are recorded (`error: internal`), the run is marked failed, and the exception is re-raised.

**M3-7 Presets.** Four presets, version 1, texts written by us (no access to Anthropic's text) and labelled "adaptation: written by Memory Lab, not Anthropic's prompt". Each text is pinned by hash in tests; edits need a new version. `none` has empty text. The no-memory arm must use `none` (validated in `RunConfig`). Custom presets exist for Demo mode only (the API enforces the mode in M6). The system prompt = frozen base prompt (`base@1`, hash pinned) + preset text; pinned global notes go in a separate uncached system block.

**M3-8 What the model sees.** Core state, extras (minus configured hidden ones) and game text inside `<game_text>`; milestones are hidden by default so the score is not in the prompt. Screenshots follow the screenshot policy and only the current turn carries an image. Rolling context keeps the last `context_steps` (default 8) steps in an API-valid structure.

**M3-9 Detectors from events only.** Position loop (stuck streak 8; oscillation over 12), repeated failed action (4; failure = rejected or a lone press that changed nothing), memory/play ratio (window 20, >0.5), churn (delete within 10 steps of write), stale note read (**proxy**: note read on a different map than it was written on, not rewritten since; REVIEW: "contradicted by later state" has no exact event-only definition), cap rejections (3/10/25). Batch recomputation equals live detection (property test), which is how replay reproduces them.

**M3-10 Event payloads.** `obs` carries the full snapshot plus a `changed` flag; `model.call` carries usage/latency/request id but no prompt text; `action` carries bounded printable args; `error` carries a stable code and a sanitised message. Redaction at the sink boundary lands in M4.

**M3-11 Model allowlist** (`providers.registry`): claude-fable-5-1, claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5 on api.anthropic.com. The run gateway (M6) must call `check_model`.

## M4 run log, ingest, redaction (2026-11-08)

**M4-1 Python 3.14 is the project's floor.** Context: the plan specifies `.jsonl.zst` segments. Options: add the `zstandard` package; use Python 3.14's stdlib `compression.zstd`. Choice: stdlib (no dependency); `.python-version` and `requires-python >= 3.14`. Reverse: depend on `zstandard` and relax the floor.

**M4-2 Local-directory object store behind an `ObjectStore` protocol (DEVIATION from "S3-compatible store"), REVIEW.** No cloud accounts are allowed; the interface (put-once/overwrite, get, list, delete, append) matches what an S3/MinIO backend needs. Immutable keys use link-or-fail; the unsealed tail uses `append` (an S3 backend would emulate it with read-modify-write on a small tail object). Logged as BLOCKED B-2.

**M4-3 Event log format.** One canonical JSON object per line `{seq, run_id, step, ts, type, data}`; `seq` and `ts` assigned by ingest (clients cannot supply them); segments sealed at 1000 events into `.jsonl.zst` with sha256 in `segments.json`; the tail stays `.jsonl`. Readers verify checksums, gaplessness, run id and re-validate every line; decompression is capped (80 MB per segment). A writer resumed after a crash seals a full tail and refuses a sealed copy that disagrees with its tail.

**M4-4 Run lifecycle is enforced at ingest.** Transition table from the plan; the worker may write `running`, `session_boundary`, `dreaming`, `stopping`, `completed`, `capped`, `failed`; the orchestrator `queued`, `starting`, `failed`, `killed`. No event may follow a final state. Replay re-checks the same rules from the recorded log.

**M4-5 Tokens.** `v1.<payload>.<hmac>`: run id + scope + expiry, HMAC-SHA256 with a service secret (>= 32 bytes) shared by the orchestrator and ingest. Stateless, so no token table; a run is revoked by closing it. The secret is a service secret, not a visitor key, and never enters the sandbox (only the minted token does). REVIEW: a rotation story (key id in the token) is needed before production.

**M4-6 Idempotent batches.** Each batch carries `batch_no` (starting at 1, shared with the orchestrator's own batches); a repeat of the last batch is acknowledged with the same response; any other number is 409. Prevents duplication on retry without client-chosen sequence numbers.

**M4-7 Redaction layers.** Worker `Redactor` (exact key + encodings + known patterns) wraps the sink and filters memory writes; ingest scrubs patterns; export refuses to build on a hit; logs are scrubbed. Documented limit: a pattern-less key the worker misses cannot be caught downstream (SECURITY U-6). `redactor.hits` is the alert metric.

**M4-8 Export bundle.** Fixed entry names only (`manifest.json`, `events.jsonl`, `memory/final.json`, `memory/history.jsonl`, `score.json`, `bundle.json`, `frames/NNNNNNN.png`), no user-controlled names (no zip-slip on extraction), memory shipped as JSON rows rather than files, deterministic bytes (fixed timestamps, sorted entries, so identical runs give identical sha256), verified offline with `python -m runlog verify` (optionally re-executing the game).

**M4-9 Verification by re-execution.** `action` events now carry the validated action; `runlog.replay.reexecute` replays them on a fresh emulator and compares every step's RAM digest and score to the recorded `obs`. This is the deterministic score re-check the plan's leaderboard verification needs (M9).

**M4-10 Quotas.** Per run: 200k events, 256 MiB event data, 3000 frames (256 KiB each, RGB PNG <= 640x576), 100 events per batch, 1 MiB request bodies (413 up front on Content-Length and while streaming). Reverse: edit `Limits`.

**M4-11 Frames are validated, not trusted.** PNG header and Pillow `verify()`, RGB only, bounded dimensions; the same step with different bytes is a 409.

**M4-12 Run records live behind a `RunRepository` interface.** The in-memory implementation serves tests and development; the Postgres implementation comes with the API (M6), which owns run creation.

## M5 sandbox and orchestrator (2026-11-08)

**M5-1 DEVIATION: hardened Docker instead of gVisor/Firecracker.** `runsc` is not installed and cannot be (no sudo). Each run gets one container: non-root (10001), read-only root, tmpfs `/tmp` (noexec,nosuid,nodev), all caps dropped, `no-new-privileges`, 1 vCPU / 1 GB (swap equal) / 128 pids, no volumes, no log driver, `--init`. `SandboxResources.runtime="runsc"` switches gVisor on with no other change. REVIEW: a shared-kernel container is a weaker boundary; gVisor/Firecracker is required before any public launch (LAUNCH.md).

**M5-2 Egress = internal Docker network + allowlist proxy.** Sandboxes sit on an `--internal` network with no route out; the only reachable host is `services/egressproxy` (CONNECT for the provider, plain HTTP forwarding for ingest). Exact host:port allowlist, no wildcards; names resolved by the proxy and only vetted addresses dialled (no rebinding); the provider entry refuses private/loopback/link-local resolution; bounded heads, idle time, connections, bytes. The orchestrator refuses to start a run on a network that is not internal (fail closed; tested against the default bridge).

**M5-3 Key delivery.** The job spec (with the key) goes to the container's stdin only: never argv, env, labels or volumes (checked with `docker inspect`, `docker top` and `/proc/1/environ` in a real container). The orchestrator holds the key in a `KeyBox` (memory only), takes it once while serialising the job, and discards it when the run ends, expires (10 min queue TTL) or is cancelled. Python cannot zero string bytes; dropping every reference and never persisting it is the achievable guarantee.

**M5-4 Heartbeat and deadlines.** Worker prints `{"hb":n}`; silence >30 s after the first beat, no beat within 60 s of start, or the wall cap + 30 s grace kills the container (`docker kill` + process kill + forced removal confirmed absent). Worker output is parsed as a closed set of line shapes and bounded (4 KB/line); everything else is counted and dropped.

**M5-5 Finalisation.** After the sandbox ends the orchestrator asks ingest for the run's status; if the worker did not reach a final state it writes `failed` (reason: kill reason, worker `fatal` type, or exit code) or `killed` (admin), then closes the run (bundle built). Container removal always runs.

**M5-6 Orchestrator API is internal, bearer-token authenticated (DEVIATION from "internal mTLS", REVIEW).** Authentication is middleware that runs before routing/body parsing (unauthenticated callers get 401 and learn nothing about the schema). mTLS needs certificate infrastructure that does not exist locally; the app must only be exposed on an internal network. Same pre-routing auth fix applied to ingest.

**M5-7 Server maxima** (`ServerLimits`) bound every request's caps (`caps_exceed_limit`); admission limits: 1 run per user, 2 per IP, 8 global, queue 32, breaker with required reason, all audited.

**M5-8 Orchestrator and worker number ingest batches separately** (per scope), so neither can desynchronise the other; the orchestrator resyncs its counter from ingest's status.

**M5-9 Docker hygiene.** All objects are labelled/named `memorylab`; cleanup acts only on exact container names built from a validated run id (another project's names are refused). The daemon on this machine hosts other projects; none were touched.

## Open (not decided here)

- Real public game and its RAM map (spec decision #1; Option A track).
- Hosting / sandbox runtime (spec decision #2; Milestone 3).
