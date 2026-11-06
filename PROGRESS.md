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

## TS/JS correctness and ground truth — 2026-11-06
- Fixes, one commit each, each with a regression test: default imports `47cbf95`;
  emitted-specifier mapping `./u.js` -> `u.ts`/`u.tsx` and `.mjs`/`.cjs`/`.jsx`
  `e261292`; `super.m()` against bases before the enclosing class `ae0a9ea`;
  test-only coverage for `this.m()`, annotated `this.field.m()`,
  `export { a as b } from`, `import type`, `a?.b()`, and imports in
  conditional/try blocks `b3f5f1d` (all passed before the test commit).
- New exhaustive harness `benchmarks/ground_truth/ts_eval.mjs` (TypeScript
  5.9.3): every call, `new`, and JSX tag resolved with
  `checker.getSymbolAtLocation`, aliases followed. Five pinned repos:
  zod `ca42965` (src, no tests/benchmarks), nest `416830c` (packages, no
  tests/specs; class-based + tsconfig paths), bulletproof-react `9506629`
  (apps/react-vite/src, no tests/stories), node-fetch `8b3320d` (src, ESM),
  express `1faf228` (lib, CommonJS). Scope assumption: production source only,
  matching the Python harness's `src/`-only scope.
- Before the cause-driven fix cycle: TS+TSX 2768 sites, 2148 correct, 0 wrong,
  620 missed; precision 100.0%, high-confidence 100.0% (1823/1823), recall
  77.6% (95% CI 76.0-79.1%). JS+JSX 136 sites, 86 correct, 0 wrong, 50 missed;
  precision 100.0%, high-confidence 100.0% (86/86), recall 63.2%
  (95% CI 54.9-70.9%).
- 30 sampled misses per group. TS top cause (17/30): import basenames containing
  a dot (`shared.utils`, `validation.pipe`, `http.exception`) were treated as
  files with an unknown extension, so `.ts` was never tried; fix `371448e`.
  JS top cause (18/30): `app.set = function`, `Foo.prototype.m = function` had
  no symbol, so their bodies had no caller; fix `653a0b3`.
- First hand audit found 5/30 compiler-unjudged edges wrong (`send` ->
  `res.send`, `resolve` -> `View.prototype.resolve`, `accepts`, `vary`);
  fix `41b7477` (property assignments are methods; unresolved external import
  bindings are not same-file definitions; Python path unchanged).
- After: TS+TSX 2768 sites, 2394 correct, 0 wrong, 374 missed; precision 100.0%
  (95% CI 99.8-100.0%), high-confidence 100.0% (2018/2018), recall 86.5%
  (95% CI 85.2-87.7%). JS+JSX 156 sites, 105 correct, 0 wrong, 51 missed;
  precision 100.0% (95% CI 96.5-100.0%), high-confidence 100.0% (104/104),
  recall 67.3% (95% CI 59.6-74.2%). Per repo: zod 81.1%, nest 89.2%,
  bulletproof-react 74.6%, node-fetch 94.4%, express 31.3%.
- Gate (high-confidence precision >=95%, overall >=90%, recall >=50%):
  TS+TSX PASS, JS+JSX PASS. Final hand audit: 30/30 compiler-unjudged edges
  correct, cited by file:line in `metrics/ts_ground_truth.json`.
- Python ground truth re-ran unchanged: precision 99.9% (1038/1039 correct,
  356 missed of 1395), high-confidence 99.9% (910/911), recall 74.4%.
  `metrics/python_ground_truth.json` has no diff.
- Artifact `metrics/ts_ground_truth.json`; rendered into `METRICS.md`;
  `scripts/generate_metrics.py --check` passes.

## Held-out ground truth — 2026-11-06
- New pinned repos, never used for tuning: werkzeug `6604c3b` (Python, `src/`),
  zustand `9fed2eb` (TS, `src/`, tests excluded), moment `485d9a7` (JS, `src/`,
  test excluded). Production source only.
- Runner: `benchmarks/ground_truth/heldout_eval.py`; artifact
  `metrics/heldout_ground_truth.json`.
- werkzeug: 1426 sites, 1080 correct, 1 wrong, 345 missed, 14 ambiguous (12
  reported); precision 99.91% (95% CI 99.48-99.98), high-confidence 99.89%
  (946/947), recall 75.74% (95% CI 73.44-77.89).
- zustand: 43 sites, 38 correct, 0 wrong, 5 missed; precision 100.0%
  (95% CI 90.82-100.0), high-confidence 100% (38/38), recall 88.37%
  (95% CI 75.52-94.93).
- moment: 794 sites, 507 correct, 0 wrong, 287 missed; precision 100.0%
  (95% CI 99.25-100.0), high-confidence 100% (507/507), recall 63.85%
  (95% CI 60.45-67.12). No fixes were made after these runs.

## Stale items — 2026-11-06
- Deleted `benchmarks/stage_b/run.py` and `benchmarks/fault/run_click.py`; removed the
  Stage B v2 section from METRICS rendering. No `impacted_tests` reads remain.
- Test count is generated: `scripts/generate_metrics.py` collects pytest and writes
  `metrics/tests.json` (currently 99); METRICS renders it. README no longer hardcodes a number.
- Fixed `impact_of_diff` wording (README, CHANGELOG, CLI help); `tsconfig extends` and the
  ambiguity cap (20 candidates; `search_symbols` with higher `limit` lists the rest) wording;
  CI wording no longer claims `--check` before CI does.
- Updated `labels/README.md` (requests/click 100 correct each; zod unlabeled; express unused).
- `AGENTS.md` is now tracked (removed from `.gitignore`).

## Cross-platform paths — 2026-11-06
- `default_cache_dir()` now takes `LOCALAPPDATA` on Windows (falling back to
  `%USERPROFILE%\AppData\Local`) and avoids `os.getuid` on platforms without it.
- Candidate selection moved to `_cache_candidates(platform, env, home)`; tests cover
  simulated Windows (drive letter + backslashes), macOS (`~/Library/Caches`) and Linux
  (`XDG_CACHE_HOME` / `~/.cache`).
- Added a case-insensitive-filesystem repo-path test (runs on APFS/NTFS, skips on
  case-sensitive Linux) and a CRLF test for Python and TS indexing plus byte-range slicing.
- Test count regenerated to 103.

## CI matrix — 2026-11-06
- `.github/workflows/ci.yml`: ubuntu/macos/windows x Python 3.12/3.13; ruff, mypy,
  pytest, offline `generate_metrics.py --check`, `uv build --wheel`, then install the
  wheel into a clean venv and run `verify/ci_smoke.py` on Python and TS fixtures.
- Added `verify/fixtures/smoke/{python,typescript}` and `verify/ci_smoke.py`.
- Symlink test skips when the platform cannot create symlinks (Windows CI).
- CI has NOT been executed. Push commands: `git push origin main` (triggers the push
  workflow) or `gh workflow run ci.yml`.
