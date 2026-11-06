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

## Python correctness and independent measurement — 2026-11-06
- Fixes (each with a regression test in `tests/test_phase1.py`): module reconciliation on
  refresh `85158b0`; call+type relations `5ea44d1`; `TYPE_CHECKING` alias handling in precision
  `eed9b40`; module-scope `<module>` symbols `1f25cd0`; getter/setter + overload combined spans
  `3c93a0b`; Python `super()`/constructed receiver `18901fa`; nested module-scope imports
  (`if TYPE_CHECKING`, try/except fallbacks) `c86c186`.
- After each fix: ruff, mypy strict, pytest, lock and metrics-check passed. Edge precision stayed
  at canonical 100/100 for requests and click; conditional recall moved from 46/64 to 45/58
  (71.9% -> 77.6%) for requests and 64/75 to 73/77 (85.3% -> 94.8%) for click. The requests
  numerator moved because `<module>` symbols changed the sampled call-site population.
- New exhaustive harness `benchmarks/ground_truth/python_eval.py`: every raw tree-sitter call
  site under `src/` in requests `3c7baaa`, click `2247b35`, flask `49943b5`, resolved with Jedi
  (`follow_imports=True`); per-repo and per-form precision/recall with Wilson intervals.
  Artifact: `metrics/python_ground_truth.json`; rendered into `METRICS.md`; `--check` passes.
- Results: 1395 project sites; 1038 correct, 1 wrong, 356 missed; overall precision 99.90%,
  recall 74.41%, high-confidence (>=0.95) precision 99.89% (910/911). Gate (>=95% high-conf,
  >=90% overall) passes; recall is reported, not gated.
- Independent hand audit: 30/30 random edges Jedi cannot judge were correct, cited by file:line
  in `metrics/python_ground_truth.json`.
- Fix cycle after the 30-case audit: 12/30 were unresolved receiver types, 7/30 bare unresolved
  callees, 6/30 self/attribute chains. The single fix was module-scope imports hidden under
  `if TYPE_CHECKING:`/try-except (the largest concrete receiver-typing defect); it added 19
  correct edges overall (requests +3, click +13, flask +3). No further tuning.
