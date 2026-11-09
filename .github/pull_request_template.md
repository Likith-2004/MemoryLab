## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## How it was tested

- [ ] `make gate` passes locally (or I explain below what I could not run)
- [ ] `make launch` passes (only if I touched `infra/`, a Dockerfile, an entrypoint or `services/dockerproxy`)
- [ ] A new or changed behaviour has a test that fails without the change

## Security checklist

- [ ] No secret, key, token or personal data is added, logged, committed or put in a test fixture (a deliberate fake goes in `.gitleaks.toml` by exact value)
- [ ] Provider keys still never leave the request body: not logged, stored, put in an environment variable or an event
- [ ] Anything from a game, a model or a visitor is treated as untrusted text (rendered as text, never as HTML or part of a command)
- [ ] New or changed endpoints keep the authorisation model (a hidden run is a 404, the owner/admin checks are tested)
- [ ] No new network path, capability, mount or host namespace for a container; `services/dockerproxy` rules are unchanged or its tests grew with the change
- [ ] A workflow change pins actions to full SHAs, sets per-job `permissions`, and puts event values in `env:`
- [ ] New dependencies are justified, their licences are acceptable (`docs/THIRD_PARTY.md`), and the lockfile is updated
- [ ] Docs updated (`CHANGELOG`, `DECISIONS`, `SECURITY`, `BLOCKED` as relevant)

## Notes for the reviewer

<!-- Anything risky, anything not run, anything you would check twice. -->
