# Progress toward v1.0.0

## Phase 1 — scope cut: DONE
- Removed impacted-test selection from `impact_of_diff`, the MCP tool, CLI and METRICS.
- `impact_of_diff` now returns changed and impacted symbols only.
- Next: Phase 2 (TS/JS completion).

## Phase 2 — TS/JS completion: DONE (code only, unmeasured)
- Added `module.exports.x` / `exports.x` function and class extraction.
- Added `require()` object destructuring (`const {helper} = require(...)`).
- Existing: JSX edges, namespace require, `tsconfig extends`/paths, barrels, `.mjs`/`.cjs`.
- Grammars load with `HOME=/tmp/tshome`; the default sandbox blocks the download.
- Precision and recall remain UNMEASURED (Phase 3 not built).

## Phase 3 — automatic ground truth: NOT STARTED
No Jedi/TS-compiler harness in this session.

## Phases 4-7 — gates, packaging, README, release: NOT STARTED
v1.0.0 is **not tagged**; the per-language precision/recall gates have not been measured.

## Verification pass — 2026-11-06 (HEAD efa799b): DONE
- Fresh clone: ruff, `mypy --strict`, `uv lock --check` and `metrics --check` pass; pytest is
  83 passed / 0 skipped with `HOME=/tmp/tshome` and a writable cache dir.
- All 8 MCP tools answer over stdio on a Python and a TS fixture; all 8 CLI commands exit 0.
- Known-failure suite: 24 PASS / 7 FAIL / 0 UNVERIFIED / 0 BLOCKED
  (`verify/check_known_cases.py`, raw output in `verify/known_cases_output.txt`).
- Seven confirmed failures: module-level `__main__` calls are not attributed; `constructs` and
  `uses_type` cannot coexist; property getter is dropped when a setter exists; incremental refresh
  misses a newly added imported module; TS default imports; `.mjs` imports of `"./x.js"`; TS
  `super.m()` becomes a false self-edge. Six further missing-edge gaps are listed in `STATE.md`.
- Precision (requests 35/99 canonical, click 30/100), conditional recall (46/64, 64/75), the
  synthetic token benchmark and the 5k scale run were reproduced from pinned SHAs. Stage B was
  re-derived from `results_v2.json`; zod/hard-kind labels and unconditional recall remain
  UNMEASURED. CI was not executed.
- Packaging: wheel + sdist, clean-venv install, CLI index/slice on Python and TS, and the MCP
  server through `uv tool run --from <wheel>` all work.
- v1.0.0 is **not ready**. Next: fix the seven failures, re-run labeled samples, then release
  hardening (stale docs, AGENTS.md tracking, Windows cache path, CI run).
