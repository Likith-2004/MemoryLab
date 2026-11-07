# Memory Lab

A security-first, multi-tenant platform for benchmarking how LLM agents use memory while
playing a game. Specification: `Memory Lab — Architecture, API, Wireframes & Security Plan.md`.
Decisions and deviations: `docs/DECISIONS.md`. Current state: `docs/HANDOFF.md`.

```bash
uv sync --all-packages --all-groups     # one venv for every Python package
make gate                               # lint + type check + tests + secret scan
.venv/bin/labharness run --bundle packages/games/fixture --script my_script.json
```

Everything runs locally. Nothing in this repository is deployed or published.

Packages: see `packages/labharness/README.md` (emulator harness) and `packages/games/fixture`
(test-only CC0 ROMs).
