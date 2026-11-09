# Contributing

Thanks for helping. Memory Lab handles other people's API keys and runs untrusted game text through a model, so changes are reviewed with the
security design in mind (`docs/SECURITY.md`). Small, focused pull requests are easiest to review.

## Setup

See the README quickstart: `uv sync --all-packages --all-groups`, `supabase start --workdir infra -x studio,imgproxy,vector,edge-runtime,logflare,postgres-meta,supavisor`.
Linux and Python 3.14 only (the emulator child process uses `resource` limits and `/proc`).

## Before you open a pull request

```bash
make gate     # ruff, mypy (strict), every test suite, web lint/types/unit/build/bundle scan/Playwright, gitleaks
make launch   # only if you touched infra/, a Dockerfile, a service entrypoint or the filter: images + the compose smoke
```

The same checks run in CI; the required ones are `gate` and `security`. A change is not done until its tests exist and pass: a bug fix comes with a
test that fails without it. Tests that need the database fail rather than skip when `MEMORYLAB_REQUIRE_DB=1` (the gate sets it).

## Rules that CI will not catch for you

- **Never commit a secret**, not even a test one that looks real. Deliberate fake credentials live in the redaction/canary tests and are allowlisted
  in `.gitleaks.toml` by exact value; do not allowlist a path or a pattern.
- **Provider keys are never** an argument, a log line, a file, an environment variable of a service, a database column or an event. Use the existing
  `SecretKey`/`SecretStr` paths and add a canary assertion for any new place a key could travel.
- **Everything that comes from a game, a model or a visitor is untrusted text**: render it as text, never as HTML; do not put it in a command.
- **Security-sensitive files** (`services/dockerproxy`, `infra/supabase/migrations`, `services/api/src/api/auth.py`, the web `proxy.ts`/`bff`,
  `.github/`) need a maintainer's review (see `CODEOWNERS`). A new migration is never edited after it is merged: add another.
- **Workflows**: pin every third-party action to a full commit SHA with a `# vX.Y.Z` comment, set `permissions` per job, never use
  `pull_request_target`, and pass `${{ github.event.* }}` values through `env:`, never into a `run:` script. `actionlint` and `zizmor` run in CI.
- Update the docs your change touches: `docs/CHANGELOG.md`, `docs/DECISIONS.md` for a decision, `docs/SECURITY.md` for a control, and
  `docs/BLOCKED.md` if something now needs the owner.

## Commits

One logical change per commit, imperative mood, a subject under about 72 characters that says what changed and, where it is not obvious, why:

```
API: user_busy from the orchestrator is the active-run answer (was a 502)
```

Prefixes such as `API:`, `Web:`, `CI:`, `Docs:` are welcome but not required. Explain the reason in the body when the diff cannot.
Rebase on `main` before review; do not force-push over a review in progress.

## Reporting security problems

Not here: see [`SECURITY.md`](SECURITY.md).
