PY ?= uv run --extra dev

.PHONY: gate lint types test lock metrics install-hooks

gate: lint types test lock metrics

lint:
	$(PY) ruff check .

types:
	$(PY) mypy polyglot_ast_slicer

test:
	$(PY) pytest

lock:
	uv lock --check

metrics:
	$(PY) python scripts/generate_metrics.py --check

install-hooks:
	git config core.hooksPath .githooks
	@echo "pre-commit hook installed"
