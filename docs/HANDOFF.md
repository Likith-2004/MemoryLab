# Handoff

Current state, updated at every milestone.

## State
- Milestone 1 (emulator harness) and M1-close are implemented and tested. See CHANGELOG.md.
- M2 memory service implemented and tested (`labharness.memory`).
- M3 agent loop, Anthropic adapter (mock-tested only), presets, detectors implemented.
- M4 `runlog` (events, lifecycle, redaction, store, segmented log, manifest, bundle, replay/verify, client sinks) and `services/ingest` implemented.
- Python is pinned to 3.14 (stdlib zstd). Packages: `packages/labharness`, `packages/runlog`, `services/ingest`.
- M5: `services/egressproxy`, `services/worker`, `services/orchestrator`, `infra/docker` (images `memorylab-worker:dev`, `memorylab-egress:dev`; build with `infra/docker/build.sh`). Docker integration tests run when the images exist (they only touch `memorylab`-labelled objects).
- Next: M6 API and auth (needs local Supabase: check Docker availability).

## How to run
```bash
uv sync --all-packages --all-groups
make gate
```

## Environment on this machine
- rgbds 1.0.1 at `~/.local/opt/rgbds-1.0.1` (build tool for fixture ROMs).
- gitleaks 8.30.1 at `~/.local/bin/gitleaks`.
- Docker available; gVisor (`runsc`) not installed.
- No `.env` (no model key): real-model checks are NOT RUN (BLOCKED.md B-1).

## Gotchas
- PyBoy child is spawned: guard entry points with `if __name__ == "__main__":`.
- Changing the fixture ROM changes every hash and the golden trace; see packages/games/fixture/README.md.
