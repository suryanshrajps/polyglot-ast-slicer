# Contributing

## Setup

```bash
uv sync --all-extras
make gate          # offline: ruff, mypy, pytest, lock, metrics-check
make install-hooks # routes git commit through .githooks/pre-commit
make metrics       # network: clones pinned repos and rewrites artifacts + METRICS.md
```

## Gates (must pass before a PR is reviewed)

1. `uv run pytest` — no failures. Skips are only allowed for a grammar that cannot be
   downloaded; the skip must name the grammar.
2. `uv run ruff check .`
3. `uv run mypy polyglot_ast_slicer`
4. `uv lock --check`
5. `uv run python scripts/generate_metrics.py --check` — offline; renders `METRICS.md` from
   the committed `metrics/*.json` artifacts, never clones. `make metrics` is the networked
   path: it clones the pinned repos (verifying their SHAs), reruns the harnesses, and rewrites
   both the artifacts and `METRICS.md`. Never hand-edit either.
6. A wheel install smoke test: `uv build --wheel`, install into a fresh venv, run
   `polyglot-ast-slicer --version`.

## Resolution changes

- Prefer a missing edge to a wrong edge. Every edge must carry `relation`, `confidence`,
  `resolution` and `line`; do not add a name-only fallback.
- Run `benchmarks/edge_eval/precision.py` on `requests` and `click` before and after. Precision
  must not drop below 98%; if it does, revert the rule.
- New inference rules need a regression fixture plus the precision delta in the PR description.

## Languages

Do not add a language without a manifest (tier, grammar source, limitations), golden fixtures,
two pinned repositories and a labelled 100-edge sample. Anything below its tier's precision
gate ships as experimental and hidden by default.

## Commits

Conventional commits (`feat:`, `fix:`, `docs:`, `test:`). One logical change per commit; gates
must pass in every commit you push.
