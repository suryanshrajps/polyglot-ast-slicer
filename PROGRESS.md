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
