# Local gate commands. Everything runs on this machine; nothing is deployed.
BIN := $(CURDIR)/.venv/bin
PKGS := packages/labharness packages/runlog services/ingest services/egressproxy services/worker services/orchestrator services/api services/accounts packages/cli

.PHONY: sync test lint secrets gate web images smoke launch
sync:
	uv sync --all-packages --all-groups

test:
	@for p in $(PKGS); do echo "== test $$p"; (cd $$p && $(BIN)/python -m pytest tests -q -p no:cacheprovider) || exit 1; done

lint:
	$(BIN)/ruff check packages services scripts
	@for p in $(PKGS); do echo "== mypy $$p"; (cd $$p && $(BIN)/mypy src) || exit 1; done

secrets:
	gitleaks git --no-banner
	gitleaks dir --no-banner .

# Web: lint (incl. the no-raw-html rule), types, unit tests, production build, the built-bundle secret scan (also against this stack's
# real secret keys), then the browser tests (Playwright + axe + Lighthouse) against that build and the full local stack.
SUPABASE := $(HOME)/.local/bin/supabase
web:
	$(BIN)/ruff check apps/web/e2e
	cd apps/web && npm run lint && npm run typecheck && npm test && MEMORYLAB_PUBLISHABLE_KEY=build-placeholder npm run build
	cd apps/web && MEMORYLAB_SCAN_FORBIDDEN="$$($(SUPABASE) status --workdir $(CURDIR)/infra -o env 2>/dev/null | grep -E '^(SECRET_KEY|SERVICE_ROLE_KEY|JWT_SECRET)=' | cut -d= -f2- | tr -d '"' | paste -sd,)" npm run scan
	cd apps/web && npx playwright test

gate: export MEMORYLAB_REQUIRE_DB=1
gate: lint test web secrets

# Launch readiness (docs/LAUNCH.md): build every image, then boot the whole stack from infra/compose.yaml and play a scripted run and a
# fork through the CLI, with the header, hardening and key-leak checks. Needs `supabase start --workdir infra` and Docker.
images:
	infra/docker/build.sh --all

smoke:
	$(BIN)/python scripts/launch_smoke.py

launch: images smoke
