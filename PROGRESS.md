# Progress toward v1.0.0

## Phase 1 — scope cut: DONE
- Removed impacted-test selection from `impact_of_diff`, the MCP tool, CLI and METRICS.
- `impact_of_diff` now returns changed and impacted symbols only.
- Next: Phase 2 (TS/JS completion).

## Phase 2 — TS/JS completion: NOT STARTED
Needed: `module.exports`/`exports.x`, require destructuring, class members/`super`,
default/named/namespace imports re-verified, grammars verified. JSX, `require()` and
`tsconfig extends` already exist from earlier work.

## Phase 3 — automatic ground truth: NOT STARTED
No Jedi/TS-compiler harness in this session.

## Phases 4-7 — gates, packaging, README, release: NOT STARTED
v1.0.0 is **not tagged**; the per-language precision/recall gates have not been measured.
