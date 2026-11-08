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

**M5-10 Heartbeat-timeout regression.** `test_a_sandbox_that_stops_heartbeating_is_killed_within_the_timeout_on_the_real_clock` runs the supervisor in a thread with a join deadline, so a missing check fails rather than hangs. Verified by stubbing the check (`elif False:`): it and the fake-clock test fail; restored, all pass.

**Process: local, reversible installs are within the charter** (owner instruction). They are decided without asking and logged here.

**M6-0 Supabase CLI installed locally** (`~/.local/bin/supabase`, user-level, no sudo) to run the local Supabase stack in Docker. RLS tests need `auth.uid()`, so M6 is not built on plain Postgres. Release 3.0.0 verified against the published sha256 (`checksums.txt`) before install. Project in `infra/supabase` (`project_id = memorylab`, access-token TTL 900 s, email confirmation required). Started with `supabase start -x studio,imgproxy,vector,edge-runtime,logflare,postgres-meta,supavisor,mailpit` (only db, auth, rest, kong, storage, realtime) to keep the footprint small; containers are labelled `com.supabase.cli.project=memorylab`, ports 54321-54324 (other projects on this daemon use none of them and are not touched). Stop with `supabase stop --workdir infra`. Local demo keys printed by the CLI are public defaults and never used outside this machine.

## M6: API and auth

**M6-1 Schema and RLS.** `infra/supabase/migrations/*`: every public table has RLS enabled and forced; user JWTs get column-level grants only (create a queued run request, change `title`/`visibility`, edit own `handle`); all state, counters, scores, memory, events and ingest state are service-role only. Admin = `profiles.role` via `public.is_admin()`, never a claim. 38 RLS test cases; 4 deliberate policy/grant mutations (`run_visible` always true, `is_admin` trusting claims, a user `status` grant, a dropped insert owner check), each caught.

**M6-2 Unlisted runs are readable by anyone who queries the table (REVIEW, spec-literal).** The plan says "others read if `visibility<>'private'`". Run ids are unguessable UUIDs and the API only lists the caller's own runs, but a direct PostgREST query can enumerate unlisted runs. Tightening needs a security-definer lookup-by-id path for unlisted runs (the public API would use it instead of table reads). Not done; owner decision wanted before public launch.

**M6-3 Visibility 404 everywhere.** One dependency (`load_visible_run`) is the only run lookup; missing, hidden and soft-deleted runs are indistinguishable (same status, body, header names; tested). A non-owner who can see a run gets 404 (not 403) on owner-only actions.

**M6-4 Signup goes through the API (`POST /api/v1/auth/signup`).** GoTrue answers 422 `user_already_exists` for an existing confirmed email and 200 with a user body for a new one: account enumeration. Our endpoint returns one 202 body for both and sends an existing account a recovery email instead of an error. Raw `/auth/v1/signup` is still reachable on the local stack: the deployment gateway must block it (strict xfail test records the leak and fails if GoTrue changes). CAPTCHA is not configured locally (NOT RUN).

**M6-5 Access tokens stay valid until `exp` (900 s) after sign-out; refresh tokens do not.** Tested against the real GoTrue (refresh after logout fails, global logout kills the other device). Admin routes re-check the session against GoTrue on every request (fails closed), so a signed-out admin loses admin at once.

**M6-6 JWT verification** is ES256/RS256 only (no HS*, no `none`; algorithm-confusion tested), JWKS cached with at most one refetch per 30 s for unknown `kid`s, `role` must be `authenticated` (anon and service keys are not sessions), `sub` must be a UUID.

**M6-7 Run creation order:** validate (cost: read guard only) -> key-failure lockout -> create quota (5/user/h, 10/IP/h) -> provider key check -> insert rows with the user's JWT (RLS) -> commit -> orchestrator submit. The key goes straight to the orchestrator and is not stored anywhere; a canary test dumps every public table. Creation is now one `api_private.create_run_request` call (see M6-11); a refused submit fails the row (`submit_refused`) and the reaper (orchestrator `reaper.py`) covers a process that dies between commit and submit.

**M6-8 Provider key validation is NOT RUN.** The API depends on a `KeyValidator`; the entrypoint wires one that rejects every key until a real validator can be tested with a model key (BLOCKED.md B-1).

**M6-9 Postgres `RunRepository`** (`ingest.pg_repository`): `lock()` is one transaction with `select ... for update` on the run row, re-entrant per thread, rolled back on error. The object-store write happens before the commit, so a crash between them leaves the log ahead of the index; `EventLogWriter` reconciles its tail on restart (existing M4 behaviour), but this window is not separately tested. `runs.status` widened to the full lifecycle in migration 3.

**M6-10 Licences:** psycopg is LGPL-3.0 (weak copyleft, same handling as PyBoy), PyJWT MIT, cryptography Apache/BSD.

**M6-11 Writes go through `api_private`.** Users have no INSERT on `runs/run_configs/reports/experiments` and no UPDATE on `experiments`; `api_private.create_run_request`, `abandon_run_request`, `file_report` are security-definer, derive the owner from `auth.uid()`, and enforce caps, one-active-run (under an advisory lock) and report limits. `api_private` must never be in PostgREST's `schemas`. The API connects as `memorylab_api` (no superuser, no BYPASSRLS) and switches role per request. Quota is refunded only for `capacity_full`, `breaker_open` and the DB-level `active_run_exists` race; an ambiguous submit (`SubmitUnknown`: request sent, no answer) keeps the row and the quota and asks the orchestrator to kill the run.

**M6-12 Unlisted means link-only.** Row policies on `runs`, `run_configs` and the child tables use `run_listable` (owner, admin, public). The API reads a single run through `api_private.get_visible_run` and its children through `api_private.run_*` readers, which re-check visibility. `run_visible` (admits unlisted) is only for lookups by id (`file_report`, parent checkpoint).

**M6-13 DELETE /me** (`{"confirm": "DELETE"}`, session re-checked, 3 attempts/h) calls the internal accounts service, the only holder of service-role and GoTrue-admin access besides ingest and the reaper. It kills active runs (409 `runs_still_active` if one does not end within 20 s), moves verified public runs to the tombstone profile (title cleared, config kept for reproducibility), soft-deletes and tombstone-owns every other run (blanking its config JSON, so the purge job still finds the rows), writes an audit row with no actor, then deletes the auth user (ends sessions and refresh tokens). Verified *unlisted* runs are deleted, not kept (assumption; the spec says public). Public non-benchmark runs are deleted (reviewer's assumption). The tombstone profile cannot be deleted (trigger) or used to start runs (`erased_at`).

**M6-14 Fork** (`POST /runs/{id}/fork`): the source goes through `load_visible_run`; the checkpoint is read with `api_private.run_checkpoint(run, checkpoint)` (id AND run must match, same 404 as missing, malformed ids are 404 too); the parent and the stored `fork_patch` are set by the server through `create_run_request` (migration 9 also records `runs.game`). A "field" is a top-level config key whose value differs, plus `caps` if the caps differ; a benchmark fork (source or result mode) may change one. It shares the create quota, the one-active-run rule and the key-failure lockout. **Not built:** the worker cannot start from a checkpoint yet (`Session.restore` exists; the job spec and worker do not use it), so the orchestrator refuses any submit with `parent` (422) rather than run a fork from step 0. The API route is tested against a fake orchestrator only.

**M6-15 Frames and export.** `GET /runs/{id}/frames/{step}` is a 302 (no-store, no-referrer) to a 60 s signed URL for the nearest sampled frame at or before `step`, 404 if none. `GET /runs/{id}/export` answers `{sha256, bytes, url, expires_in}` for runs in a final status whose `run_ingest_state.bundle_sha256` is set (read through `api_private.run_bundle`); before issuing the URL the API checks the stored bundle against that hash and re-runs the redaction scan over every file in the zip (refusing unreadable or oversized zips) — `409 export_not_ready | export_refused`. 10 exports/min per identity. Signed URLs come from a `UrlSigner`; the only implementation here is `LocalUrlSigner` (HMAC over key + expiry, served by `/_blob/...`, mounted only when `MEMORYLAB_BLOB_SIGNING_SECRET` is set) — production presigns from the private bucket (BLOCKED.md B-2). A URL is never logged.

**M6-16 Live stream.** `POST /runs/{id}/stream-ticket` (as GET; 20/min per identity) returns a 32-byte url-safe ticket valid 60 s; only its sha256 is stored (`stream_tickets`, service-only table) with the run and the caller (null user for an anonymous viewer). `GET /ws/runs/{id}?ticket=` admits a socket only if the Origin is in `MEMORYLAB_ALLOWED_ORIGINS` (empty = nothing connects), the ticket is well formed, redeems atomically (`DELETE ... RETURNING`: once; consumed whether valid or not, but a refused Origin does not burn it), belongs to this run, and the run is visible to the ticket's own user (anon for anon tickets) now. Limits: 3 sockets per viewer per run (anon viewers per address), 200 per run, in-process. The client may send only `{"t":"resume","after_seq":n}` of at most 1 KB: 1009 for an oversized message, 1008 for anything else (also binary, extra keys, bad numbers). The server streams the log from seq 1 (or the resumed seq) as `{"t":"event",...}`, a `{"t":"frame","step","url"}` after an `obs`, and `{"t":"hb"}` every 15 s, and re-checks the viewer's visibility on each heartbeat (private or deleted: close 1008). Run uvicorn with `--ws-max-size 1024` too: the 1 KB cap in the app only applies after the server has read the message. The ticket is in the URL, so keep access logs off for `/ws/` (the app never logs it).

**M6-17 Experiments.** A user's experiment groups their own runs that differ in one declared variable (a top-level run-config field; compared on the canonical config, so spelling out a default is not a difference; caps count as a field). Ownership is enforced by a trigger on `experiments` for every writer, the service role included (only added runs are checked, so a run deleted later does not block edits); users have no table write access, only `api_private.create_experiment`/`update_experiment` (owner-only, live account, max 100 per user, 50 runs each). `unlisted` is link-only here too (`api_private.get_visible_experiment`). A viewer sees a run inside an experiment only if it is **listable for them** (owner, admin, public): a private run in a public experiment does not leak, an unlisted run is not published by being put in one, and nothing reveals how many runs were hidden; stats (n, mean, min, max, stdev over scored runs, `n_below_5` and `runs_still_running` warnings) and `GET /experiments/{id}/compare?metric=score|tokens` (one aligned step axis, forward-filled, ≤ 5000 steps, ≤ 20 000 events per run, 10/min) use only those runs, and the logs of hidden runs are never opened.

**M6-18 Admin moderation.** `GET /admin/reports?status&limit&before`, `PATCH /admin/reports/{id}` (`open -> actioned | dismissed`, once, reason required, who/when stored), `POST /admin/leaderboard/{run}/unverify` (reason required; the run stays off: `run_scores_guard` refuses re-verifying it; the owner may then make it private). Every admin action — those two, kill, and breaker open **and** close — writes `audit_log` (actor, action, target, reason, ip_hash = salted hash, never the address); report and unverify write it in the same transaction as the change, kill and breaker write and commit it before acting (an attempt that fails downstream is on record; an action whose audit row cannot be written does not happen). A trigger enforces the report transitions for every writer. Admin routes keep the live GoTrue session check and the DB role check (404 for non-admins), plus the per-user write limit. Note: the ip_hash salt is per process unless `ip_salt` is supplied, so hashes do not correlate across restarts.

**M6-19 Round-4 changes.** `experiments.run_ids` has no select privilege for user roles (column grants); the owner's list and run set come from `api_private.my_experiments` / `my_experiment_run_ids`, and a catalogue test lists every array/jsonb column readable by a user role (allowlist: `run_configs.config_json`, `runs.fork_patch`). Fork is disabled by default (`MEMORYLAB_FORK_ENABLED`, BLOCKED.md B-6) and refuses before any quota or key step. A WebSocket resume is at most 1/s per socket and charged to the viewer's read limit. Admin kill/breaker audit rows are committed as `requested` before the call and closed out as `ok`/`failed`/`unknown`; `audit_log` is append-only apart from that one transition.

## Open (not decided here)

- Real public game and its RAM map (spec decision #1; Option A track).
- Hosting / sandbox runtime (spec decision #2; Milestone 3).
