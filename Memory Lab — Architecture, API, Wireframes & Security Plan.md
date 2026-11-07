# Memory Lab — Architecture, API, Wireframes & Security Plan

Nov 7, 2026 · @Manish P

## Summary and scope

We build Memory Lab: a web app where visitors launch, watch, fork and compare game-playing agent runs that differ in one memory variable, with every run fully logged and replayable. The game harness is reused; the experiment layer, isolation, BYOK handling and benchmark integrity are what we build.

**What we claim.** Memory Lab tests memory regimes (none, notes, capped notes, steered notes, dreamed notes, session resets) inside a game. Results are directional evidence about memory in this game, not a rerun of the Anthropic post's figures. The CL-Bench database task and the private Stainless/Harvey corpora are out of scope.

**What we do not claim.** That AI playing Pokemon is new (public since March 2025), that small memory beats large memory (until locked, repeated runs show it), any per-run cost (until a metered pilot measures it), or that our presets equal Anthropic's unreleased prompts.

**Locked decisions in this plan** (change any and the dependent sections change):

| # | Decision | Why |
| --- | --- | --- |
| D1 | Public lab runs on a redistributable homebrew ROM; Pokemon Red is local-only (owner's machine) until legal advice says otherwise | Only real legal exposure; architecture is ROM-agnostic |
| D2 | Agent loop runs server-side; visitor key held in worker memory only | Streaming, isolation and step caps are enforceable server-side |
| D3 | Two modes: Demo (anything goes, never ranked) and Benchmark (frozen config, ranked) | Leaderboard integrity |
| D4 | One run = one sandboxed container, no network except the model provider allowlist | Kill switch for cost and blast radius |
| D5 | Stack: FastAPI + Postgres (Supabase for auth + RLS) + Redis + S3-compatible object store + Next.js frontend | Matches your Python background; Supabase gives auth/RLS without a custom IdP |
| D6 | MVP = one model provider (Anthropic), fixed start state, none vs notes, live inspector, downloadable logs | Smallest build that produces a checkable result |

## Prior art and what we adopt

The strongest existing work is the PokeAgent Challenge, and it is a speedrun/battling benchmark, not a memory lab. Our gap is narrower than "AI plays Pokemon": controlled, forkable, visitor-runnable experiments where the variable under test is the memory regime.

| Project | What it does | What we take | Where we differ |
| --- | --- | --- | --- |
| [ClaudePlaysPokemonStarter](https://github.com/davidhershey/ClaudePlaysPokemonStarter) | Minimal PyBoy + Claude tool loop, RAM reader, BYO ROM | Loop shape, RAM-reader pattern | We add isolation, memory tools, logging, streaming, multi-user |
| [PokeAgent Challenge](https://pokeagentchallenge.com) | Battling + Emerald speedrun tracks, live leaderboard, open orchestration harness ([paper](https://arxiv.org/pdf/2603.15563)) | Reproducibility discipline; its reported failure modes (memory corruption cascades, reasoning loops) become inspector events we detect | Our score is memory behaviour under one changed variable, not speed or win rate |
| [PyBoy](https://github.com/Baekalfen/PyBoy) | Python Game Boy emulator, RAM access, save/load state, headless at hundreds of x realtime | Emulator core; save states power checkpoint forks | Verify LICENSE.md terms before vendoring; we import it as a dependency, unmodified |
| [GB Studio](https://sourceforge.net/mirror/gb-studio/) | MIT-licensed engine that exports real Game Boy ROMs | How we build our own redistributable test game | We author the game; we do not reuse third-party itch.io ROMs |

The PokeAgent paper's finding that harness design dominates model performance is the strongest argument for our approach: hold the harness fixed, vary only memory, and the comparison means something. [PokeAgent summary](https://api.emergentmind.com/papers/2603.15563)

**ROM finding that changes the plan.** Pokemon-like homebrew games on itch.io (D-MONs, Insectopia) are pay-what-you-want downloads, which is not a redistribution licence. So option (a) in the feasibility note becomes: build our own small creature RPG in GB Studio with CC-licensed tilesets, or get written permission from a homebrew author. Owning the game also lets us place RAM addresses for score, badges and map deliberately, which makes the state reader trivial and stable.

**Open check before launch:** search again for live memory-focused agent labs (e.g. Gemini/GPT "plays" streams with notes inspectors) so the landing page claims nothing false about novelty.

## System architecture

Six services and three stores; the only component that ever holds a visitor's key, or can reach a model provider, is the per-run sandbox, which is destroyed when the run ends.

&#91;embedded content: system architecture · 8 components, 1 sandbox per run\]

The browser talks only to the API (REST plus a read-only WebSocket); the sandbox never talks to the browser or the database directly, only to Ingest with a token scoped to its own run.

**Run lifecycle:** `queued` (key in orchestrator memory, 10 min max) → `starting` (sandbox up, manifest sealed) → `running` (steps; may pass through `session_boundary` and `dreaming`) → `stopping` (cap hit, user stop, admin kill, error) → `completed`, `capped`, `failed` or `killed`. Every transition is a `run.state` event; only the orchestrator and worker can write it.

## Agent harness and memory instrument

The harness is one versioned Python package (`labharness`) that every run imports at a pinned version; a run's manifest records that version, so two runs are comparable only if their harness hashes match.

### Step loop (per step)

1. `observe()` reads RAM via the game's state map (map id, x/y, party, flags, score, dialog buffer) and grabs the frame; frame sent only every K steps or on map/dialog change (config field `screenshot_policy`).
2. `build_prompt()` = frozen system prompt (preset) + injected top-layer memory (<= `inject_cap_chars`) + rolling context + observation.
3. `provider.call()` through the adapter (one interface: `step(messages, tools) -> ProviderResult{actions, summary, usage}`).
4. `execute()` runs tool calls in order: buttons advance N frames; memory tools hit the memory service; unknown tools are rejected and logged.
5. `meter.charge(usage)`; if any cap is hit, the step finishes and the run moves to `stopping`.
6. `emit()` appends events to the run's event log and publishes to the live stream.

### Tools exposed to the model

| Tool | Args | Limits | Logged as |
| --- | --- | --- | --- |
| `press` | `buttons[]` (A,B,Up,Down,Left,Right,Start,Select), `hold_frames` | <= 8 buttons/step | `action` |
| `wait` | `frames` | <= 600 | `action` |
| `mem_list` | `path?` | returns names + sizes | `mem.read` |
| `mem_read` | `path` | <= 8 KB returned | `mem.read` |
| `mem_write` | `path`, `content` | per-file and folder caps | `mem.write` (full diff) |
| `mem_delete` | `path` | none | `mem.delete` |
| `mem_search` | `query` | substring/BM25 over the run's folder only | `mem.read` |

No-memory arm: memory tools are absent from the tool schema, not merely discouraged.

### Memory service

- Storage: a per-run virtual filesystem stored as rows (`memory_files`, versioned), not the host disk. Paths are normalised, `[a-z0-9_\-/.]{1,120}`, depth <= 4, no `..`, no absolute paths. This removes path traversal by construction.
- Caps (config): `max_files`, `max_file_chars`, `max_total_chars`. A write over cap returns a tool error the model sees; it is logged as `mem.reject`.
- Every write creates a new version with the step number; snapshots are cheap (copy-on-write rows) and power the inspector timeline and forks.
- Content is opaque text: never rendered as HTML, never executed, never used as a URL, tool name or path outside the memory namespace.

### Instruction presets

Presets are immutable, versioned text blobs (`preset_id@version`), written by us from the post's descriptions and labelled "adaptation". Initial set: `none`, `general`, `failures_only`, `read_first_test_theories`. Custom text is allowed only in Demo mode and is stored verbatim with the run.

### Sessions and injected context

A run can be split into sessions at a fixed step count (`session_len`). At a boundary: model context is dropped, the emulator and memory persist, and only the file at `inject_path` (default `global.md`, capped) is injected. Session boundaries are events in the log.

### Dream worker

Runs between sessions (never mid-step), as a separate job with its own token cap that counts against the visitor's total cap. Inputs: the session transcript + memory snapshot. Allowed operations: correct, consolidate, prune, promote to `global.md`. It uses the same memory tools and caps; every change is a `dream.*` event with a diff. Output is shown side by side in the inspector.

### Checkpoints and forks

Every `checkpoint_every` steps (and at session boundaries) the worker stores: PyBoy save state, memory version pointer, transcript offset, RNG seeds, config hash. `fork` creates a new run whose parent is that checkpoint and whose config differs in exactly the fields the user changed; the diff is stored and shown. Benchmark forks may change one field only.

### Detectors (inspector signals)

Computed from events, never from model self-report: memory-vs-play step ratio, repeated-position loops (same map+xy N times), repeated failed actions, memory churn (writes later deleted), stale-note reads (note contradicted by later state), cap rejections.

## Data model

Postgres holds identity, configuration, metadata and indexes; the object store holds large immutable artifacts; Redis holds only ephemeral live state. API keys appear in none of them.

### Postgres tables (RLS on every table)

| Table | Key columns | Owner rule (RLS) |
| --- | --- | --- |
| `profiles` | `id` (= auth.uid), `handle`, `role` (user/admin), `created_at` | read/update self; admin read all |
| `games` | `id`, `name`, `rom_sha256`, `licence`, `state_map_version`, `public` (bool) | read if `public`; write service only |
| `start_states` | `id`, `game_id`, `savestate_sha256`, `label` | read if game public |
| `presets` | `id`, `version`, `text_sha256`, `text`, `label`, `is_adaptation` | read all; write service only |
| `run_configs` | `id`, `config_json`, `config_hash` (sha256 of canonical JSON), `mode` (demo/benchmark) | read if linked run visible |
| `runs` | `id`, `owner_id`, `config_id`, `parent_checkpoint_id?`, `status`, `visibility` (private/unlisted/public), `harness_version`, `model_id`, `provider`, caps, counters, `started_at`, `ended_at`, `end_reason` | owner all; others read if `visibility<>'private'`; status/counters written by service only |
| `run_events_index` | `run_id`, `seq`, `type`, `step`, `ts`, `blob_offset` | follows `runs` visibility |
| `memory_files` | `run_id`, `path`, `version`, `step`, `content`, `chars`, `deleted` | follows `runs` visibility; insert service only |
| `checkpoints` | `id`, `run_id`, `step`, `savestate_key`, `memory_version`, `transcript_offset`, `config_hash` | follows `runs` visibility |
| `run_scores` | `run_id`, `progress_score`, `steps`, `tokens_in/out`, `memory_ops`, `loops`, `est_cost_usd`, `verified` | read follows run; write service only |
| `experiments` | `id`, `owner_id`, `title`, `variable`, `run_ids[]`, `visibility` | owner write; read per visibility |
| `audit_log` | `id`, `actor_id`, `action`, `target`, `ip_hash`, `ts` | admin read only; insert service only |
| `reports` | `id`, `reporter_id`, `run_id`, `reason`, `status` | reporter read own; admin all |

Writes that matter (status, counters, scores, memory, events) are made only by the worker/service role, never by user JWTs. A user can create a run request and change `visibility`/`title`; nothing else.

### Object store layout (private bucket, signed URLs only)

```
roms/{rom_sha256}.gb                      # public games only; never user uploads in MVP
states/{savestate_sha256}.state
runs/{run_id}/manifest.json               # immutable after start
runs/{run_id}/events/{segment:06d}.jsonl.zst   # append-only, sealed per 1000 events
runs/{run_id}/frames/{step:07d}.png       # sampled frames
runs/{run_id}/checkpoints/{step}.state
runs/{run_id}/bundle.zip                  # export, built on completion, with sha256
```

### Run manifest (written before step 1, then sealed)

```json
{
  "run_id": "uuid", "mode": "benchmark",
  "harness_version": "0.3.1", "harness_sha": "git sha",
  "game": {"rom_sha256": "...", "state_map_version": 2},
  "start_state_sha256": "...", "seed": 1234,
  "provider": "anthropic", "model_id": "claude-sonnet-5-5",
  "preset": "read_first_test_theories@1",
  "memory": {"enabled": true, "max_files": 10, "max_total_chars": 20000, "inject_path": "global.md", "inject_cap_chars": 2000},
  "sessions": {"session_len": 500, "dream": false},
  "observation": {"screenshot_policy": "every_k_or_change", "k": 5, "frame_scale": 2},
  "caps": {"max_steps": 2000, "max_tokens": 4000000, "max_wall_s": 7200},
  "parent": null, "config_hash": "sha256:..."
}
```

### Event schema (one JSON object per line)

`{seq, run_id, step, ts, type, data}` with `type` in: `run.state`, `obs` (state diff, frame ref), `model.call` (usage, latency, provider request id; no prompt text if visibility is public and preset is custom), `model.summary`, `action`, `mem.read`, `mem.write` (path, before/after hash, diff), `mem.delete`, `mem.reject`, `session.boundary`, `dream.*`, `checkpoint`, `detector` (loop, churn, stale), `cap.hit`, `error` (sanitised). `seq` is gapless per run; the live stream and replay read the same events.

## API

All endpoints live under `/api/v1`, speak JSON, return errors as RFC 9457 `application/problem+json` with a stable `code`, and derive the caller from a verified Supabase JWT (`Authorization: Bearer`), never from a body or path field. Signup, login, password reset and sign-out are Supabase Auth flows called from the client; we configure them (section on security) rather than re-implement them.

**Auth levels:** `public` = no token; `user` = valid JWT; `owner` = JWT and `runs.owner_id = auth.uid` checked server-side; `admin` = JWT and `profiles.role='admin'` read from the DB, not from JWT claims.

### Catalogue and account

| Method + path | Auth | Purpose | Notes |
| --- | --- | --- | --- |
| `GET /health` | public | liveness | no version or dependency detail |
| `GET /me` | user | profile, active run count, limits |  |
| `DELETE /me` | user | delete account + private runs | public benchmark runs anonymised, not deleted |
| `GET /games` | public | playable games (public only) |  |
| `GET /games/{id}/start-states` | public | fixed start states |  |
| `GET /presets` | public | preset ids, versions, full text, adaptation label |  |
| `GET /models` | public | supported provider/model ids + price-table version and date | price table is a dated snapshot, labelled estimate |

### Runs

| Method + path | Auth | Purpose | Notes |
| --- | --- | --- | --- |
| `POST /runs/validate` | user | validate config, return resolved config + hash + mode eligibility | no key accepted here |
| `POST /runs` | user | create and start a run | body: `config`, `caps`, `visibility`, `api_key`. Returns `202 {run_id, status}`. Key validated with one cheap provider call; failure = generic `key_rejected` |
| `GET /runs` | user | my runs, cursor-paginated |  |
| `GET /runs/{id}` | owner, or anyone if unlisted/public | run detail, manifest, counters | 404 (not 403) when not visible |
| `PATCH /runs/{id}` | owner | title, visibility | benchmark runs cannot become private once on leaderboard without leaving it |
| `POST /runs/{id}/stop` | owner, admin | graceful stop after current step | idempotent |
| `DELETE /runs/{id}` | owner | soft delete, purge artifacts after 7 days |  |
| `POST /runs/{id}/stream-ticket` | as GET | single-use ticket, 60 s TTL, bound to run + user | keeps JWTs out of WebSocket URLs |
| `GET /runs/{id}/events?after_seq&limit` | as GET | replay / catch-up | limit <= 1000 |
| `GET /runs/{id}/frames/{step}` | as GET | 302 to signed URL (60 s) | nearest sampled frame |
| `GET /runs/{id}/memory?at_step` | as GET | tree: path, chars, version at that step |  |
| `GET /runs/{id}/memory/file?path&version` | as GET | file content | served as `text/plain`, `nosniff` |
| `GET /runs/{id}/memory/diff?path&from&to` | as GET | unified diff |  |
| `GET /runs/{id}/checkpoints` | as GET | list |  |
| `POST /runs/{id}/fork` | user who can see the run | new run from a checkpoint | body: `checkpoint_id`, `config_patch`, `caps`, `api_key`; benchmark forks reject patches touching more than one field |
| `GET /runs/{id}/score` | as GET | server-computed score |  |
| `GET /runs/{id}/export` | as GET | signed URL to bundle + sha256 | keys never present by construction; redaction scan re-run at build |
| `POST /runs/{id}/report` | user | flag abuse/content |  |

### Experiments and leaderboard

| Method + path | Auth | Purpose |
| --- | --- | --- |
| `POST /experiments` | user | group runs that differ in one declared variable |
| `GET /experiments/{id}` | owner or per visibility | runs, variable, summary stats (n, mean, spread) |
| `PATCH /experiments/{id}` | owner | title, visibility, add/remove own runs |
| `GET /experiments/{id}/compare?metric` | per visibility | aligned per-step series for charts |
| `GET /leaderboard?game&preset&model&memory` | public | verified benchmark runs only |

### Admin

| Method + path | Purpose |
| --- | --- |
| `GET /admin/runs?status` | live and queued runs, per-IP/per-user counts |
| `POST /admin/runs/{id}/kill` | immediate container kill |
| `POST /admin/breaker` | global stop-new-runs switch, reason required, audited |
| `GET /admin/reports`, `PATCH /admin/reports/{id}` | moderation |
| `POST /admin/leaderboard/{run_id}/unverify` | remove a run from ranking, reason audited |

### WebSocket: `GET /ws/runs/{id}?ticket=...`

Read-only. Server sends `{"t":"event",...}` (schema in Data model), `{"t":"frame","step","url"}` and `{"t":"hb"}` every 15 s. Client may send only `{"t":"resume","after_seq":n}`; anything else closes the socket with 1008. Origin must match our domain. Max 3 sockets per user per run, 200 viewers per run, then clients fall back to polling `/events`.

### Rate limits (enforced at the gateway and again in the API)

| Scope | Limit |
| --- | --- |
| `POST /runs`, `POST /runs/{id}/fork` | 5 per user per hour, 10 per IP per hour, 1 concurrent active run per user (beta), global concurrency cap |
| Key validation failures | 5 per user per hour, then 1 h lockout of run creation |
| Read endpoints | 120 per user per minute |
| Auth (Supabase) | Supabase limits + CAPTCHA on signup and reset |

### Error codes (stable)

`unauthenticated`, `not_found`, `validation_failed`, `key_rejected`, `caps_required`, `caps_exceed_limit`, `capacity_full` (with `Retry-After`), `rate_limited`, `breaker_open`, `benchmark_patch_invalid`, `internal` (opaque id only).

## Wireframes

Thirteen screens; the Live Run and Fork Compare screens carry the product, so they are drawn here, and every other screen is specified in the table below. Bars and dots are layout placeholders, not data.

&#91;embedded content: wireframe · live run screen, desktop\]

On phones (< 768 px) the Live Run screen becomes the game on top with tabs below: Log, Memory, Budget, Checkpoints. Stop run stays visible in the top bar at every width.

&#91;embedded content: wireframe · fork compare screen\]

Fork Compare is where a finding is made or not: the banner names the single changed field from the config diff, and the repeats strip refuses to look like a verdict.

### All screens

| Screen | Route | Purpose | Key elements | States to design |
| --- | --- | --- | --- | --- |
| Landing | `/` | what the lab tests and does not claim | one-sentence claim, a featured public replay (real, not mocked), "what we don't claim" list, start button | signed out / in |
| Sign in / up / reset | `/auth/*` (Supabase UI) | account | email + password, magic link, GitHub, CAPTCHA | uniform messages, email-sent, error |
| New run | `/runs/new` | configure and launch | step 1 mode (Demo/Benchmark suite + arm), step 2 game + start state, step 3 model + preset (full text visible) + memory caps, step 4 caps (required) + key field + key notice, review with config hash | validation errors, `capacity_full` with retry, `key_rejected` |
| Live run | `/runs/{id}` | watch and inspect | drawn above | queued (position), starting, running, session boundary, dreaming, stopping, ended (reason), disconnected (resuming) |
| Replay | `/runs/{id}?at=step` | same layout, scrubbable, no Stop | timeline scrub, export button | loading segments, missing frame |
| Memory file | drawer on Live/Replay | read one file over time | version list, plain-text content, diff view | empty, deleted, rejected writes |
| Fork | modal from checkpoint | create branch | shows parent config, editable fields (one only in Benchmark), caps, key | invalid patch |
| Fork Compare / Experiment | `/experiments/{id}` | compare arms | drawn above; add runs; finding text with n | n < 5 warning, runs still running |
| Leaderboard | `/leaderboard` | ranked verified runs | filters suite/arm/model, median + range + n, links to replays | empty suite, unverified hidden |
| My runs | `/runs` | library | list with status, mode, model, score, visibility toggle, delete | empty, many (paginated) |
| Account | `/settings` | profile, sessions, delete account | sign out everywhere, delete flow with confirmation |  |
| Admin | `/admin` | operate | live runs, kill, breaker, reports, unverify | breaker open banner |
| Legal, 404 | `/privacy`, `/terms`, `/licences`, any | required pages | game licence + attributions on `/licences` |  |

**Design direction.** Instrument-panel, not marketing: a neutral surface, one accent for "memory" everywhere (log chips, timeline ticks, tree highlights) so the eye can track memory activity across panels; monospace for file content and step numbers; pixel-crisp game canvas (`image-rendering: pixelated`). No gradients, no glass, no decorative motion; live updates animate only the newest log row, and `prefers-reduced-motion` turns that off.

## Security architecture and threat model

The two assets that justify most of this section are visitors' API keys and our compute bill; the third is leaderboard integrity. Every control below exists to protect one of those, and each has a named test in the QA plan.

### BYOK key path (end to end)

1. Browser sends the key once, in the `POST /runs` body over HTTPS (HSTS, TLS 1.2+). Never in a URL, header log, query string, localStorage or cookie. The input field is `type=password`, `autocomplete=off`, and cleared after submit.
2. API process: Pydantic model holds it as `SecretStr`; request-body logging is disabled for `/runs` and `/fork`; Sentry/OTel scrubbers drop any field named `api_key` and any string matching provider key prefixes.
3. API validates the key with one minimal provider call, then passes it to the orchestrator over an internal mTLS channel. It is never placed in Redis, Postgres, the job queue or object storage.
4. Orchestrator starts the run container and writes the key to the container's stdin; it is not an env var (env is visible via `docker inspect` and `/proc`). If no slot is free, the orchestrator holds it in memory for at most 10 minutes; restart = run fails `key_expired`, the user re-enters it.
5. Worker keeps it in one variable used only by the provider adapter. Before any event is written, a redaction filter replaces any occurrence of the key (and of known key patterns) with `[REDACTED]` in events, memory writes and errors. The same scan runs again when building exports.
6. Run ends (any reason) → container destroyed → key gone. No "remember my key" feature in any phase.

Page copy states this plainly and recommends a fresh key with a provider-side spend limit, deleted after use. Server-side caps are the second line, not the only one: the provider-side limit is what bounds loss if we fail.

### Run isolation

- One container per run (gVisor or Firecracker runtime), non-root, read-only root FS, `no-new-privileges`, all capabilities dropped, seccomp default, 1 vCPU / 1 GB / pids 128, tmpfs only.
- Egress: default deny; allowlist = the selected provider's API host + our internal event ingest. No DNS beyond those. The model cannot reach anything else even if a tool were abused.
- The worker has no DB credentials; it writes events/memory through a narrow internal ingest API using a per-run token scoped to that `run_id` (cannot write any other run).
- Hard kill: orchestrator enforces wall-clock cap and kills on missed heartbeats (30 s).

### Authentication (Supabase Auth)

- Email + password and magic link; optional GitHub OAuth. Email confirmation required before starting runs.
- Enumeration: login, signup, reset all return the same message and status ("If an account exists, we've sent an email"); signup for an existing email sends a "you already have an account" email instead of an error. Tested for timing differences.
- Sign-out calls `signOut({scope:'global'})` option available in settings; normal sign-out revokes the refresh token server-side. Access-token TTL 15 min, so a revoked session dies within that window; admin actions re-check session validity against Supabase on each request.
- CAPTCHA (Turnstile/hCaptcha) on signup and reset; Supabase auth rate limits on.

### Authorization

- FastAPI verifies the JWT signature, `aud`, `exp` locally (JWKS cached), then uses `sub` as the only identity. Roles come from `profiles.role` in the DB.
- Every run-scoped route goes through one dependency `load_visible_run(run_id, user)` that returns 404 for both missing and forbidden (no existence oracle). Unit-tested against IDOR.
- RLS on all tables as in Data model; the API uses the user's JWT for reads (so RLS applies) and the service role only inside the worker-ingest and admin services, never in the public API path or any browser bundle.
- Storage bucket private; frames/exports only via 60 s signed URLs issued after the same visibility check.

### Untrusted model output

Model text, summaries and memory files are attacker-controllable (a visitor can steer them, and game text flows into prompts). Controls: rendered as text nodes only (React escaping, no `dangerouslySetInnerHTML`, no markdown-to-HTML for memory); served as `text/plain` with `nosniff`; strict CSP (`default-src 'self'`, no inline script, frame-ancestors none); tool calls are validated against a closed schema; memory paths sandboxed by construction; no tool can fetch URLs or run code. Public runs with custom presets show a "user-written instructions" badge; reported content can be hidden by admins.

### Abuse and cost controls

- Caps required on every run (`caps_required`), bounded by server maxima per phase; server counts tokens from provider `usage`, not estimates.
- Concurrency: per user, per IP, global; queue with 10 min TTL; global circuit breaker (`/admin/breaker`) plus an automatic one when host CPU > 85% or error rate spikes.
- Our own spend: compute only (visitors pay model tokens). Hosting budget alarms; autoscaling has a hard max node count.
- The run gateway rejects keys for providers we do not allowlist, preventing us being used as a generic proxy.

### Leaderboard integrity

Scores computed by the server from RAM-derived events; client never submits scores. A run is `verified` only if: mode = benchmark, manifest hashes match an approved harness/preset/game/start state, no human input events, completed or capped normally, and a deterministic re-check of the score from the event log matches. Admin unverify is audited.

### Threats and owners

| Threat | Control | Test |
| --- | --- | --- |
| Key leaks to logs/events/exports | No persistence path; redaction filter; scrubbers | Canary key test: run with a fake key string, grep every sink |
| Cross-user run/memory access (IDOR) | `load_visible_run`, RLS, per-run ingest token | User A/B/anon matrix on every route |
| Service-role key in browser | Only in server services; bundle scan in CI | `grep` built bundles + gitleaks |
| Stored XSS via memory/model text | Text-only rendering, CSP, nosniff | XSS payload corpus written into memory |
| Prompt injection driving tools outward | Closed tool schema, egress deny, no URL tools | Injection corpus in preset + game text |
| Cost abuse / DoS | Caps, rate limits, queue TTL, breaker | Load test + cap overrun test |
| Account enumeration | Uniform responses, timing check | Scripted comparison of responses |
| Session survives sign-out | Refresh revocation, short access TTL | Logout then API call / refresh / second device |
| Leaderboard forgery | Server scoring, manifest hash checks | Tampered-manifest and replayed-event tests |
| ROM/IP exposure | Only own/licensed ROMs public; no uploads in MVP | Check `games.public` only for licensed ROMs |
| CSRF | Bearer tokens (no cookie auth on API); Origin check on WS | Cross-origin request test |
| Container escape | gVisor/Firecracker, dropped caps, read-only FS | Run CIS/trivy scans on images |

## Benchmark integrity and scoring

A leaderboard row is a claim about a frozen configuration, so Benchmark mode freezes everything except the variable under test and refuses to rank anything it cannot recompute.

### Demo vs Benchmark

| Field | Demo | Benchmark |
| --- | --- | --- |
| Game / start state | any public | approved list only |
| Preset | any, incl. custom text | approved preset versions only |
| Harness | current | approved pinned version |
| Memory caps, session length, dream | free | from an approved "suite" definition |
| Step budget | user choice within max | fixed by suite |
| Human input mid-run | none in any phase (keeps logs clean) | none |
| Ranked | never | if `verified` |

A **suite** is a named, versioned bundle (e.g. `memory-regimes-v1`) listing allowed arms, e.g. A: no memory; B: notes, uncapped; C: notes, 10 files / 20k chars; D: C + read-first preset; E: C + sessions of 500 steps; F: E + dream. Arms differ by declared fields only; the suite file is published.

### Scores (all server-computed from events)

- **Progress score:** game-defined milestones read from RAM (our homebrew game exposes a milestone bitfield). Primary metric = milestones reached within the step budget; tiebreak = steps to last milestone.
- **Efficiency:** steps, input/output tokens (from provider usage), wall time; estimated cost shown with the price-table date and the word "estimate".
- **Memory behaviour:** memory-op share of steps, writes, deletes, churn, final folder size, cap rejections, stale-note reads.
- **Failure signals:** loop count, repeated failed actions, session-boundary regressions (progress lost after a reset).

### Reporting rules shown on the site

- Leaderboard groups by suite + arm + model; each cell shows n runs, median and range, never a single best run.
- An arm with n < 5 is labelled "insufficient runs" and not ranked.
- Comparisons between arms show both distributions; the UI does not declare a winner. A finding statement is written by a human, linked to the experiment, with its n.
- Every row links to replays and the exportable bundle so anyone can recheck it.

## Infrastructure, deployment and observability

Start on one region with a small fixed worker pool; the run worker is the only part that scales with users, so it gets its own node group and a hard ceiling.

### Repository layout (monorepo)

```
memory-lab/
  apps/web/            # Next.js (App Router), TypeScript, Tailwind + own tokens
  services/api/        # FastAPI: public REST + WS fan-out
  services/orchestrator/   # schedules run containers, holds queued keys in memory only
  services/ingest/     # per-run-token event/memory writer (service role)
  packages/labharness/ # agent loop, provider adapters, memory tools, detectors
  packages/games/      # homebrew ROM source (GB Studio project), state maps
  infra/               # Terraform, Dockerfiles, k8s manifests, CSP config
  docs/                # HANDOFF, ARCHITECTURE, SECURITY, DECISIONS, CHANGELOG, TODO
```

### Runtime

| Component | Hosting (initial) | Scaling |
| --- | --- | --- |
| Web | Vercel or static on CDN | edge |
| API + WS | 2 containers behind a load balancer (sticky not required; WS fan-out via Redis pub/sub) | horizontal |
| Orchestrator | 1 active + 1 standby (leader election) | fixed |
| Run workers | dedicated node pool, gVisor runtime, 1 run per sandbox | ceiling set by budget; start at 8 concurrent |
| Postgres + Auth | Supabase (Pro) with PITR backups | vertical |
| Redis | managed, no persistence needed | small |
| Object store | S3/R2 private bucket, lifecycle rules | managed |

Hosting provider choice is open (decision list); pick the region nearest your expected viewers for stream latency; step latency is dominated by model API calls either way.

### CI/CD

PR checks: ruff + mypy, pytest (unit + RLS tests against a local Supabase), eslint + tsc, Playwright smoke, gitleaks, dependency audit (pip-audit, npm audit), trivy image scan, built-bundle scan for service-role key / provider key patterns. Deploys: staging on merge to `main`, production on tagged release with manual approval; DB migrations versioned and reviewed; rollback = previous image tag + down-migration or PITR.

### Observability

Structured JSON logs with request id; OpenTelemetry traces API → orchestrator → worker; metrics: active runs, queue depth, step latency p50/p95, provider error rate by code, cap hits, redaction-filter hits (should be \~0; any hit alerts), WS viewers. Alerts: breaker trips, redaction hit, worker crash loop, DB connections > 80%, spend alarm on hosting account. Log scrubbing as in Security; logs retained 30 days.

### Data retention

Private runs: kept until deleted by owner, artifacts purged 7 days after delete. Public benchmark runs: kept; owner account deletion anonymises them. Frames sampled, not full video, to bound storage.

## Testing and QA plan

Nothing ships on visual inspection; each layer has tests that fail loudly, and the security tests from the threat table run in CI on every PR.

| Layer | What | How |
| --- | --- | --- |
| Harness | step loop, tool validation, caps stop exactly at limit, unknown tool rejected | pytest with a fake provider returning scripted actions; no real API calls in CI |
| Memory service | path normalisation, traversal attempts, caps, versioning, snapshot at step | property tests (hypothesis) with hostile paths and sizes |
| Determinism | same start state + same scripted actions = same RAM trace and score | golden-trace test per game build |
| Provider adapters | request shape, usage parsing, error mapping, retries with backoff | recorded fixtures; one nightly live smoke with a capped test key from a vault |
| Redaction | canary key string never appears in any sink | run with fake key, then grep Postgres, object store, logs, traces, exports |
| API authz | every route × {anon, User A, User B, admin} × {private, unlisted, public} | generated matrix test; expects 404 not 403 for hidden |
| RLS | direct Supabase queries with each role | SQL tests in CI against local Supabase |
| Auth | enumeration (message, status, body shape, timing), logout → API, logout → refresh, revoked session on second device | scripted + Playwright |
| Web/XSS | payload corpus in memory files, summaries, run titles | Playwright asserts no script execution; CSP violation reports = fail |
| Abuse | rate limits, concurrency limits, queue TTL, breaker | k6 load test on staging |
| Containers | escape surface, egress deny, resource limits | trivy, attempt outbound request from worker (must fail) |
| UI | keyboard nav, focus, contrast, reduced motion, 360 px width, no horizontal scroll, loading/empty/error states | axe-core in Playwright + manual pass on phone |
| Benchmark | tampered manifest, replayed events, client-submitted score | integration tests expect `verified=false` |

**Pilot measurement (Phase 0 exit):** 3 metered runs per arm A and B on the homebrew game, 500 steps each, recording tokens per step with and without screenshots. These numbers, not arithmetic, set the default caps and any cost text on the site.

## Phased build plan

Five phases, each ending at a gate with testable exit criteria; nothing goes public until the security and repeatability gates have passed. Durations are not estimated here: Phase 0 measurements set them.

&#91;embedded content: phased build plan · 5 phases, 5 exit gates\]

Each phase starts on its own branch/worktree with a recorded checkpoint, ends with CHANGELOG and HANDOFF entries, and a failed gate means fixing and re-running that gate, not starting the next phase. The first build step, once you approve budget and supply a key via a vault link, is cloning the starter and PyBoy into `packages/` for Phase 0.

## Launch checklist, open decisions, sources

Public launch is blocked until every box below is ticked with evidence linked in SECURITY.md or CHANGELOG.md.

### Launch checklist

- [ ] Public game is our own GB Studio build or has written redistribution permission; licence file and asset attributions shipped
- [ ] Canary-key redaction test green on production build; redaction-hit alert wired
- [ ] Authz matrix and RLS tests green; service-role key absent from all bundles and Git history (gitleaks full history)
- [ ] Enumeration, sign-out, revoked-session and second-device tests green
- [ ] Worker egress deny verified from inside a production sandbox
- [ ] Rate limits, concurrency caps, queue TTL, breaker exercised under load on staging
- [ ] CSP, HSTS, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy` headers verified; no stack traces in prod errors
- [ ] Pilot token measurements published internally; default caps set from them; cost copy says "estimate" with price-table date
- [ ] Privacy Policy (keys, logs, retention, public runs) and Terms (BYOK responsibility, acceptable use) live and linked
- [ ] Accessibility pass (axe clean, keyboard, 360 px, reduced motion); 404 page; titles/OG/robots/sitemap; private runs `noindex`
- [ ] No placeholder content, fake stats or builder badges; favicon; domain + HTTPS
- [ ] Backups (PITR) restored once on staging; rollback rehearsed
- [ ] HANDOFF, ARCHITECTURE, SECURITY, DECISIONS, TODO, CHANGELOG current

### Decisions needed from you

| # | Decision | Recommended | Blocks |
| --- | --- | --- | --- |
| 1 | Public game: build own GB Studio creature RPG vs seek permission from a homebrew author | Build our own (small, 3-4 milestones, deliberate RAM map) | Phase 2 |
| 2 | Hosting provider for run workers (needs gVisor/Firecracker) | Managed k8s with gVisor node pool, or Fly Machines | Phase 1 end |
| 3 | Phase 0 budget + a fresh Anthropic key via a vault link, never chat | Small capped pilot | Phase 0 |
| 4 | Beta concurrency ceiling | 8 concurrent runs | Phase 2 |
| 5 | Second provider timing (OpenAI/Gemini adapters) | After benchmark v1 is stable | Phase 3 |

### Sources (opened 2026-11-07)

- [ClaudePlaysPokemonStarter](https://github.com/davidhershey/ClaudePlaysPokemonStarter) (from the feasibility note)
- [Anthropic: components of agent memory](https://www.anthropic.com/research/components-of-agent-memory) (from the feasibility note)
- [PyBoy repository](https://github.com/Baekalfen/PyBoy)
- [PokeAgent Challenge paper](https://arxiv.org/pdf/2603.15563) and [summary](https://api.emergentmind.com/papers/2603.15563)
- [GB Studio (MIT)](https://sourceforge.net/mirror/gb-studio/)
- [D-MONs on itch.io](https://lazydevs.itch.io/d-mons), [Insectopia on itch.io](https://boopgames-tay.itch.io/insectopia) (checked for licensing; no redistribution licence stated)
