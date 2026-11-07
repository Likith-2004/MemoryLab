# Local gate commands. Everything runs on this machine; nothing is deployed.
BIN := $(CURDIR)/.venv/bin
PKGS := packages/labharness packages/runlog services/ingest

.PHONY: sync test lint secrets gate
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

gate: lint test secrets
