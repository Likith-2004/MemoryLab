# Memory Lab

[![CI](https://github.com/TheManishCode/MemoryLab/actions/workflows/ci.yml/badge.svg)](https://github.com/TheManishCode/MemoryLab/actions/workflows/ci.yml)
[![Security](https://github.com/TheManishCode/MemoryLab/actions/workflows/security.yml/badge.svg)](https://github.com/TheManishCode/MemoryLab/actions/workflows/security.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/TheManishCode/MemoryLab/badge)](https://scorecard.dev/viewer/?uri=github.com/TheManishCode/MemoryLab)

A security-first, multi-tenant platform for benchmarking how LLM agents use **memory** while playing a game. Visitors launch, watch, fork and
compare game-playing agent runs that differ in **one** memory variable; every run is fully logged and replayable, and scores are computed by the
lab from the game's own memory, never reported by the agent.

> **Status: not publicly deployed.** Everything runs locally; there is no hosted instance. What is built, what is verified and what still needs
> the owner is in [`docs/LAUNCH.md`](docs/LAUNCH.md) and [`docs/BLOCKED.md`](docs/BLOCKED.md). No licence has been chosen yet (BLOCKED B-16), so
> until one is added all rights are reserved.

## What is in the repository

| Part | Where |
|---|---|
| Emulator harness (PyBoy, deterministic, checkpoints, memory service, agent loop, dream job) | `packages/labharness` |
| Event log, redaction, export bundles, replay verification | `packages/runlog` |
| Ingest, egress proxy, sandbox worker, orchestrator, Docker filter | `services/ingest`, `egressproxy`, `worker`, `orchestrator`, `dockerproxy` |
| Public API (RLS-backed, JWT) and the accounts service | `services/api`, `services/accounts` |
| Web app (Next.js, 13 screens, httpOnly-cookie session proxy, CSP nonces) | `apps/web` |
| Command line | `packages/cli` |
| Database, containers, compose stack | `infra/` |

The specification (architecture, API, wireframes, security plan) is the long document at the repository root. Design decisions are in
[`docs/DECISIONS.md`](docs/DECISIONS.md), the security design and its evidence in [`docs/SECURITY.md`](docs/SECURITY.md).

## Quickstart

You need Linux, Docker, [uv](https://docs.astral.sh/uv/), Node 24, the [Supabase CLI](https://supabase.com/docs/guides/cli) and
[gitleaks](https://github.com/gitleaks/gitleaks).

```bash
uv sync --all-packages --all-groups                       # one venv for every Python package (Python 3.14)
supabase start --workdir infra -x studio,imgproxy,vector,edge-runtime,logflare,postgres-meta,supavisor
make gate                                                  # lint, types, every test suite, web build + Playwright, bundle scan, secret scan
make launch                                                # build every image, boot the whole compose stack, play a run and a fork through the CLI
supabase stop --workdir infra
```

`make gate` and `make launch` are what CI runs (see below). Nothing in them needs a model key: the launch smoke scripts the model's answers and
everything else is the production code path. Real model calls are not exercised until a capped key exists (BLOCKED B-1).

## The CLI

```bash
export MEMORYLAB_API_URL=http://127.0.0.1:8000 MEMORYLAB_AUTH_URL=http://127.0.0.1:54321/auth/v1 MEMORYLAB_PUBLISHABLE_KEY=<publishable key>
.venv/bin/memorylab login --email you@example.org
.venv/bin/memorylab runs new --config run.json        # the provider key comes from a hidden prompt, --api-key-stdin or MEMORYLAB_PROVIDER_KEY, never an argument
.venv/bin/memorylab runs watch <run-id>
.venv/bin/memorylab runs checkpoints <run-id>
.venv/bin/memorylab fork <run-id> --checkpoint <id> --max-steps 50 --max-tokens 100000 --max-wall-s 600
.venv/bin/memorylab --help
```

## CI and supply chain

Every pull request and push to `main` runs three workflows; none needs a repository secret.

- **CI** (`.github/workflows/ci.yml`): Python lint, types and tests against the Supabase CLI's local stack; the web app (lint, types, unit tests,
  build, bundle scan, Playwright); the images, sandbox and Docker-filter tests and the compose launch smoke. The single required check is `gate`.
- **Security** (`.github/workflows/security.yml`): gitleaks over the full history, CodeQL (security-extended), pip-audit, npm audit and
  osv-scanner, hadolint, trivy (Dockerfiles and every image), actionlint and zizmor on the workflows. The single required check is `security`.
- **Scorecard** and **Release**: OpenSSF Scorecard weekly; on a `v*` tag, images are built, published to GHCR with an SBOM, a keyless cosign
  signature and a provenance attestation. Nothing is deployed.

Actions are pinned to commit SHAs and kept current by Dependabot. Details and the repository settings the owner must turn on:
[`docs/SECURITY.md`](docs/SECURITY.md#ci-and-supply-chain).

## Contributing and security

Read [`CONTRIBUTING.md`](CONTRIBUTING.md). **Report vulnerabilities privately**, not in an issue: see [`SECURITY.md`](SECURITY.md).
