PY ?= uv run --extra dev

.PHONY: gate lint types test lock metrics metrics-check install-hooks

gate: lint types test lock metrics-check

lint:
	$(PY) ruff check .

types:
	$(PY) mypy polyglot_ast_slicer

test:
	$(PY) pytest

lock:
	uv lock --check

metrics:
	$(PY) python scripts/generate_metrics.py

metrics-check:
	$(PY) python scripts/generate_metrics.py --check

install-hooks:
	git config core.hooksPath .githooks
	@echo "pre-commit hook installed"
