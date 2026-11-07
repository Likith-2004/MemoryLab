# Local gate commands. Everything runs on this machine; nothing is deployed.
PY := .venv/bin/python

.PHONY: sync test lint secrets gate
sync:
	uv sync --all-packages --all-groups

test:
	cd packages/labharness && ../../$(PY) -m pytest tests -q -p no:cacheprovider

lint:
	.venv/bin/ruff check packages scripts
	cd packages/labharness && ../../.venv/bin/mypy src

secrets:
	gitleaks git --no-banner
	gitleaks dir --no-banner .

gate: lint test secrets
