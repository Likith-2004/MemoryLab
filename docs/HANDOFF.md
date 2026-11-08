# Handoff

Current state, updated at every milestone.

## State
- Milestone 1 (emulator harness) and M1-close are implemented and tested. See CHANGELOG.md.
- M2 memory service implemented and tested (`labharness.memory`).
- M3 agent loop, Anthropic adapter (mock-tested only), presets, detectors implemented.
- M4 `runlog` (events, lifecycle, redaction, store, segmented log, manifest, bundle, replay/verify, client sinks) and `services/ingest` implemented.
- Python is pinned to 3.14 (stdlib zstd). Packages: `packages/labharness`, `packages/runlog`, `services/ingest`.
- M5: `services/egressproxy`, `services/worker`, `services/orchestrator`, `infra/docker` (images `memorylab-worker:dev`, `memorylab-egress:dev`; build with `infra/docker/build.sh`). Docker integration tests run when the images exist (they only touch `memorylab`-labelled objects).
- M6 (branch `m6-api`): local Supabase in `infra/supabase`, `services/api` (JWT, RLS, runs, admin, uniform signup), `ingest.pg_repository`. See CHANGELOG for what is built and what is not.
- Next: finish M6 (remaining routes: `DELETE /me`, stream tickets + WebSocket, fork, frames, export, experiments, admin reports/unverify, queued-row reaper) and resolve DECISIONS M6-2 / M6-7 with the owner; then M7.

## How to run
```bash
uv sync --all-packages --all-groups
supabase start --workdir infra -x studio,imgproxy,vector,edge-runtime,logflare,postgres-meta,supavisor   # mailpit stays on (auth tests read mail)
make gate          # needs the stack above; MEMORYLAB_REQUIRE_DB=1 is set by the target
supabase stop --workdir infra
```
`supabase db reset --workdir infra` re-applies the migrations (needed after adding one).
Run the API: `MEMORYLAB_JWKS_URL=... MEMORYLAB_ORCHESTRATOR_TOKEN=... MEMORYLAB_OBJECT_STORE=... uvicorn --factory api.main:build`.

## Environment on this machine
- rgbds 1.0.1 at `~/.local/opt/rgbds-1.0.1` (build tool for fixture ROMs).
- gitleaks 8.30.1 at `~/.local/bin/gitleaks`.
- Docker available; gVisor (`runsc`) not installed. Supabase CLI 3.0.0 at `~/.local/bin/supabase` (DECISIONS M6-0).
- No `.env` (no model key): real-model checks are NOT RUN (BLOCKED.md B-1).

## Gotchas
- PyBoy child is spawned: guard entry points with `if __name__ == "__main__":`.
- Changing the fixture ROM changes every hash and the golden trace; see packages/games/fixture/README.md.
