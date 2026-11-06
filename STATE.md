# STATE — evidence-based verification

Verified `2026-11-06` against HEAD `efa799b` (`feat(ts): CommonJS exports and require destructuring`).
Environment: macOS 26.6.2 arm64, Python 3.12.4, uv 0.11.16, FastMCP 4.1.4 (lock resolved from
`fastmcp>=2.1.0`), tiktoken 0.14.0, tree-sitter-language-pack 1.22.1.

Rules used here: **PASS** means reproduced in this session; **FAIL** means an explicit expected
result did not hold; **UNVERIFIED** means no run/harness; **BLOCKED** means the environment stopped
the check. Full raw known-case output: `verify/known_cases_output.txt`. Measurement excerpts:
`verify/measurements.txt`.

Verdict: the package is real, installs, the gates pass on a fresh clone, and every MCP tool answers.
It is **not v1.0.0-ready**: incremental indexing can silently keep a stale graph, TypeScript
`super` produces a false cycle, two edge kinds collide, and two common TS/JS import forms are lost.

## Scoreboard

| Area | PASS | FAIL | UNVERIFIED | BLOCKED |
|---|---:|---:|---:|---:|
| Repo state | 4 | 1 | 0 | 0 |
| Fresh-clone gates | 5 | 0 | 0 | 0 |
| Grammar loading | 4 | 0 | 0 | 0 |
| MCP tools (`tools/list` + 8 tools × 2 repos) | 17 | 0 | 0 | 0 |
| CLI commands | 8 | 0 | 0 | 0 |
| Known-failure suite | 24 | 7 | 0 | 0 |
| Measured claims | 13 | 0 | 3 | 0 |
| Packaging | 9 | 0 | 0 | 0 |
| Cross-platform cache | 0 | 2 | 0 | 0 |
| Docs / CI / README | 5 | 5 | 1 | 0 |
| **TOTAL** | **89** | **15** | **4** | **0** |

Known-case suite alone: **24 PASS / 7 FAIL / 0 UNVERIFIED / 0 BLOCKED** (31 cases).

## 1. Verified working

### 1.1 Repo state and versions

- `pyproject.toml` `version = "0.4.0"`, `polyglot_ast_slicer/__init__.py` `0.4.0`,
  `polyglot-ast-slicer --version` → `polyglot-ast-slicer 0.4.0`, newest CHANGELOG entry
  `## 0.4.0 — 2026-10-31`, tag `v0.4.0` exists. **No version mismatch.**
- Tags: `v0.3.0`, `v0.4.0`. Branch `main` is ahead of `origin/main` by 28 commits.
- `plugins/` is absent and nothing named `plugin` is tracked. **Gone.**
- `AGENTS.md` exists in the working tree and contains `Follow CLAUDE.md`, but it is listed in
  `.gitignore` (line `AGENTS.md`), so a fresh clone has no `AGENTS.md`. **Pointer is local only.**
- Untracked at session start: `benchmarks/ground_truth/python_eval.py` (left untouched).

### 1.2 Gates on a fresh clone

```bash
git clone /Users/suryanshraj/polyglot-ast-slicer /tmp/pas-verify
cd /tmp/pas-verify
UV_CACHE_DIR=/tmp/uvcache uv sync --all-extras
```

| Gate | Command | Result |
|---|---|---|
| ruff | `uv run ruff check .` | `All checks passed!` exit 0 |
| mypy strict | `uv run mypy --strict polyglot_ast_slicer` | `Success: no issues found in 6 source files` exit 0 |
| pytest | `HOME=/tmp/tshome POLYGLOT_AST_CACHE_DIR=/tmp/pas-cache uv run pytest -rs` | `83 passed in 1.20s`, 0 skipped |
| lock | `uv lock --check` | `Resolved 87 packages in 4ms` exit 0 |
| metrics | `HOME=/tmp/tshome uv run python scripts/generate_metrics.py --check` | `METRICS.md matches committed artifacts` exit 0 |

Test counts: 83 total = cache 7, graph_engine 24, parser 15, phase1 22, server 9, slicer 2,
`benchmarks/edge_eval/test_score_labels.py` 4. README says 75 (stale).

Default sandbox run (`uv run pytest` with the real HOME) fails: SQLite cache under
`~/Library/Caches/polyglot-ast-slicer` is unwritable, and 3 TS tests skip with
`Download cache lock error: open lock file .../tree-sitter-language-pack/v1.22.1/.download.lock:
Operation not permitted (os error 1)` for `tsx`, `tsx`, `javascript`. With `HOME=/tmp/tshome`
all four grammars load:

```
python OK, typescript OK, tsx OK, javascript OK
```

### 1.3 Parsers really supported (from `parser.py`)

| Language | Extensions |
|---|---|
| python | `.py`, `.pyi` |
| typescript | `.ts`, `.mts`, `.cts` |
| tsx | `.tsx`, `.jsx` |
| javascript | `.js`, `.mjs`, `.cjs` |

Symbol kinds: `function`, `method`, `class`, `interface`, `type_alias`. `.jsx` is parsed with the
`tsx` grammar. Tree-sitter-language-pack grammars for all four loaded.

### 1.4 MCP over real stdio

Client: FastMCP `Client` + `StdioTransport`, server launched as
`python -m polyglot_ast_slicer serve --repo <repo>` (`verify/mcp_probe.py`). `initialize` and
`tools/list` succeeded on both repos; all eight tools returned without an `error` key.

| Tool (short description) | Python repo | TS repo |
|---|---|---|
| `index_codebase` — build/refresh graph | 3 files, 11 symbols, 7 edges | 6 files, 11 symbols, 8 edges |
| `search_symbols` — find by name | 1 match | 1 match |
| `get_ast_call_slice` — N-hop context | 422 chars, 132 tokens | 512 chars, 150 tokens |
| `get_blast_radius` — upstream dependents | LOW, 1 impacted | LOW, 1 impacted |
| `find_circular_dependencies` — SCCs | 0 symbol cycles, 1 self (`fact`), 0 file cycles | 0/0/0 |
| `explain_edge` — edge provenance | `exists: true` (`extra`→`entry`) | `exists: true` (`run`→`helper`) |
| `impact_of_diff` — diff→symbols | 1 changed, 0 impacted | 1 changed, 0 impacted |
| `get_index_stats` — counts/cache/skips | 11 symbols, 7 edges | 11 symbols, 8 edges |

Default slice response keys are exactly `compiled_context`, `token_metrics`, `diagnostics`.

### 1.5 CLI commands

All run against the fixtures with exit 0: `index`, `slice`, `blast`, `cycles`, `search`, `stats`,
`impact` (`--json` where applicable) and `serve` (stdio handshake above). `--version` works.
`impact_of_diff` now returns changed/impacted symbols only, no tests.

### 1.6 Known-failure checks that passed (24)

`verify/check_known_cases.py`, fixtures in a temp dir, cache outside the repo.

- `py-a` `self.request()` → `Client.request`, not the module `request`.
- `py-b` two `super().__init__()` subclasses → no symbol cycle, no self-recursion.
- `py-c` delegating same-named method → no false self-edge.
- `py-d` real recursion → `fact → fact` and reported under `self_recursive`.
- `py-f` duplicate name → `code: ambiguous_symbol`, candidates `['A.dup', 'dup']`.
- `py-g` functions named `parse`, `log`, `open`, `error` all get edges.
- `py-i` `from ..core import x as renamed` and `from pkg import x` re-export both resolve.
- `py-k` `getattr(obj, name)()` and `obj[name]()` produce no edges.
- TS: paths alias, `export *` barrel, named import, namespace import, JSX `<Button />`,
  CommonJS require destructuring, `module.exports.x`/`exports.x`, `.cjs` require.
- Safety: escaping symlink skipped (`symlink escapes repository`), read-only checkout indexes,
  default cache lands in `$HOME/Library/Caches/polyglot-ast-slicer` (outside repo), allowed-root
  rejection, empty/broken/latin-1 files index, binary and 5 MB files are skipped without crash.
- Payload: default keys, target snippet appears exactly once in the JSON, tiktoken count on the
  exact JSON equals `token_metrics.payload_tokens` (226 = 226).

### 1.7 Packaging

`uv build` produced `polyglot_ast_slicer-0.4.0-py3-none-any.whl` and
`polyglot_ast_slicer-0.4.0.tar.gz`. Clean `uv venv` + wheel install: `--version` 0.4.0; indexed
and sliced the Python fixture (3 files/11 symbols/7 edges; slice 259 tokens) and the TS fixture
(6 files/11 symbols/8 edges; slice 310 tokens). `uv tool run --from <wheel> polyglot-ast-slicer`
returned 0.4.0, and an MCP handshake through that exact uvx path listed all 8 tools and indexed
the Python fixture.

### 1.8 Measured claims reproduced

Fresh clones of `https://github.com/psf/requests.git` at `3c7baaa...` and
`https://github.com/pallets/click.git` at `2247b35...` (both SHAs matched `generate_metrics.py`).

| Claim | Command | Result |
|---|---|---|
| requests precision | `precision.py /tmp/pas-eval-repos/requests 100 --json` | strict 35, canonical 99, CI 94.55–99.82 |
| click precision | same on click | strict 30, canonical 100, CI 96.30–100 |
| requests recall | `recall.py ... 150 --json` | 46/64 = 71.875%, CI 59.87–81.41 |
| click recall | same on click | 64/75 = 85.333%, CI 75.62–91.61 |
| synthetic tokens | `token_bench.py --json` | 4 files/252 symbols/16 edges; 94.91/94.89/89.96% saved; slice 537/539/168 |
| scale 5000 | `scale_bench.py --files 5000 --json` | 5000 files, 5000 symbols, 5000 edges |
| metrics | `generate_metrics.py --check` | matches |
| canaries | `score_canary.py --repo requests/click` | 20 seeded/0 accepted; 15 seeded/0 accepted; 100/100 agreement |
| hand labels | count `labels/{requests,click}/labels.csv` | 100 correct / 0 incorrect / 0 unsure each |
| Stage B summary | recomputed medians from `results_v2.json` | all values equal `summary_v2.json` except `dropped_commits` (not stored in results) |
| pinned SHAs / label keys | `git rev-parse HEAD`; `labels/*/answers.json`, `canary_answer_key.json` | exist and match |

## 2. Broken, ranked by harm to a user

1. **Incremental refresh does not see a newly added module.** `a.py` imports `b`; after `b.py` is
   created, `refresh_file(a.py)` leaves **0 edges** while a full rebuild has `use → thing`.
   Evidence: `py-l` `incremental=[] rebuild=[('use','calls','thing')]`. A user editing in a live
   session gets a stale graph with no error. Cause (code): `_module_to_file` is not rebuilt in
   `refresh_file`, so the import stays unresolved. Highest harm: silent wrong answers.
2. **TypeScript `super.method()` becomes a false self-edge.** `Child.greet` calls `super.greet()`;
   the graph stores `Child.greet → Child.greet` (confidence 0.95,
   `enclosing_member:Child`), never `Base.greet`, and `find_circular_dependencies` reports
   `Child.greet` as self-recursive. Evidence: `ts-super_this` `self=['Child.greet']`.
   Cause: `_resolve_call_site` checks the enclosing class before the `super` base walk.
3. **Call and type edges cannot coexist on one pair.** `def make() -> Foo: return Foo()` produces
   only `uses_type`, not `constructs`; `networkx.DiGraph` keeps one edge per ordered pair and the
   type pass overwrites the call edge. Evidence: `py-h` `relations=['uses_type']`. This drops a
   real call from slices/blast radius and under-reports fan-out.
4. **TypeScript default imports are unresolved.** `import main from "./u"` + `main()` yields
   **0 edges** (`ts-default_import`). The binding maps the local name to the original name
   `"default"`, and no symbol is named `default`. Common React/TS pattern, silently missing.
5. **`.mjs` imports of the form `"./u.js"` are unresolved.** `esm → helper` is missing
   (`mjs-import`, 0 edges). Cause: `_first_ts_file` sees the `.js` suffix and tries only
   `src/u.js`, not the sibling `src/u.ts`.
6. **Module-level calls under `if __name__ == "__main__":` are not attributed.** `work()` gets no
   caller (`py-e`, 0 edges). Top-level call sites are never assigned to a symbol, so scripts have
   no caller/impact information.
7. **A property setter silently replaces the getter.** Parsed symbols contain two `C.value`
   definitions (lines 3 and 7) with the same `symbol_id`; the graph keeps only the setter span
   `(7, 9)`. Evidence: `py-j`. The getter body is not sliceable and `self.value` has no edge.
8. **Removed test-selection feature still leaks into docs/benchmarks.** `README.md:112` says
   `impact_of_diff` answers "which tests are impacted"; `CHANGELOG.md:13` says it returns
   "impacted tests"; `server.py:393` help says "symbols/tests"; `benchmarks/stage_b/run.py:104`
   and `benchmarks/fault/run_click.py:85` still read `impacted_tests` (now absent).
9. **Docs are stale in four more places.** README says 75 tests twice (actual 83); says "`extends`
   is not merged yet" although `_load_tsconfig_file` follows `extends` and the test passes; says
   `ambiguous_symbol` returns "every candidate" but `_ambiguous_response` returns `[:20]`
   (21 candidates → error says 21, list has 20); says "CI runs the same generator with `--check`"
   but CI runs `generate_metrics.py` (full, networked) and then `git diff --exit-code`.
10. **`labels/README.md` is stale.** It says labels are "Currently empty: hand-labeling has not
    been performed", but `labels/requests/labels.csv` and `labels/click/labels.csv` each contain
    100 verdicts (all `correct`).
11. **Missing edges that did not fail their stated checks.**
    `super().__init__()` produced no edge at all (`py-b`, only inheritance edges); `A().run()`
    delegation produced only `constructs B.run → A`, not `B.run → A.run` (`py-c`). The checks
    only required "no cycle"/"no recursion", so they pass, but the graph loses these calls.
12. **`AGENTS.md` is not shipped.** It is ignored and absent from a fresh clone, so the agent
    pointer only exists in this checkout.
13. **Cache-dir cross-platform handling is partial.** `default_cache_dir()` has a macOS branch,
    `XDG_CACHE_HOME`, and a `~/.cache`/temp fallback, but no Windows `LOCALAPPDATA` branch; there
    are no tests that simulate Windows or macOS paths (`grep -rn 'win32|darwin|Library/Caches'
    tests` returns nothing).

## 3. Unverified or blocked

**BLOCKED: 0.** Two sandbox issues were cleared, not left blocked:

- `uv sync` initially failed: `Failed to initialize cache at /Users/.../.cache/uv ... Operation
  not permitted (os error 1)`. Workaround: `UV_CACHE_DIR=/tmp/uvcache`.
- Grammar downloads failed under the real HOME: `Download cache lock error .../.download.lock:
  Operation not permitted`. Workaround: `HOME=/tmp/tshome`.
- Run all gates yourself:
  `HOME=/tmp/tshome POLYGLOT_AST_CACHE_DIR=/tmp/pas-cache UV_CACHE_DIR=/tmp/uvcache make gate`
- Run the known-case suite yourself:
  `HOME=/tmp/tshome POLYGLOT_AST_CACHE_DIR=/tmp/pas-cache uv run python verify/check_known_cases.py`

**UNVERIFIED (4):**

1. **CI was never executed.** `.github/workflows/ci.yml` is `ubuntu-latest`, matrix Python
   `["3.12", "3.13"]`; it runs sync, ruff, mypy, pytest, full metrics generation, a metrics diff,
   and `uv build --wheel`. Command to run it: `gh workflow run ci.yml` (needs push/auth).
2. **Zod hand precision and all hard-kind precision are UNMEASURED.** `labels/zod/labels.csv` has
   100 empty verdicts; no `hard_labels.csv` exists. No command can produce them without labeling.
3. **Unconditional recall is UNMEASURED.** Only conditional recall (Jedi-resolvable sites) exists.
4. **Stage B v2 was not re-run end-to-end.** The 30 pinned commit worktrees are not present, so
   the full 22-entry run is NOT RE-RUN; the summary numbers were re-derived from
   `results_v2.json` and match. Command if the repos are restored:
   `uv run python benchmarks/stage_b/run_v2.py`.

Skipped by instruction: 20k/50k scale runs. Not re-measured: the historical "roughly doubled
payload in v0.2.0" claim. Not independently verified: the 100 human verdicts (the CSVs exist and
sum to 100 correct; this session did not human-audit them).

## 4. README claim audit

| README claim | Verdict | Evidence |
|---|---|---|
| "3% of the repo" | NO SCRIPT | Rhetorical; not a measured claim |
| "40-file service is ~120k tokens" | NO SCRIPT | Illustrative |
| Every snippet is an exact byte range | SUPPORTED | `test_symbol_snippet_is_byte_exact`; `read_byte_slice` |
| Numbers in METRICS, pinned SHAs, 95% Wilson | SUPPORTED | Precision/recall re-run; generator `wilson()` |
| "CI runs the same generator with `--check`" | STALE | CI runs `generate_metrics.py` + `git diff`, not `--check` |
| 6,000-token cap; 10/hop; 40 total; hub degree 150 | SUPPORTED | Constants: `DEFAULT_MAX_TOKENS=6000`, `MAX_CALLERS/CALLEES_PER_HOP=10`, `MAX_TOTAL_SNIPPETS=40`, `HUB_DEGREE=150` |
| `impact_of_diff` answers "which tests are impacted?" | STALE | Test selection removed in `a24bce6`; response has no test keys |
| `include_metadata=True` adds callers/callees/types/regions | SUPPORTED | Payload test; default keys exclude them |
| Confidence table / no name-only fallback | SUPPORTED | `test_no_name_only_edges`, code comment and resolution paths |
| Ambiguous query lists "every candidate" | STALE | 21 candidates → 20 returned (`candidates[:20]`) |
| Direct recursion kept and reported | SUPPORTED | `py-d`; `self_recursive` |
| All CLI commands shown | SUPPORTED | All 8 exercised, exit 0 |
| Two indexes produce byte-identical slices | SUPPORTED | `test_indexing_is_deterministic` passed |
| Incremental re-parses one file | PARTIAL | True for edits, but new imported module is not relinked (`py-l`) |
| Overlap merging never emits code twice | SUPPORTED | `test_slice_merges_overlapping_regions`; marker count 1 |
| Re-export chains up to 6 hops | SUPPORTED | `py-i`; `_symbol_in_file` depth cap 6 |
| TS `export *` barrels and `baseUrl`/`paths` | SUPPORTED | `ts-barrel`, `ts-paths_alias` |
| Method slices carry `class X:` + imports | SUPPORTED | `test_slice_includes_enclosing_class_and_imports`; observed `# import:` |
| Graceful skips (grammar/binary/size/symlink) | SUPPORTED | `safe-weird_files`, `safe-symlink_escape` |
| Cache outside repo; read-only checkout works | SUPPORTED | `safe-cache_outside_repo`, `safe-readonly_checkout` |
| `POLYGLOT_AST_ALLOWED_ROOTS` rejects outside roots | SUPPORTED | `safe-allowed_roots`; env-variant also rejected |
| Dynamic dispatch produces no edges | SUPPORTED | `py-k` |
| "`tsconfig` extends is not merged yet" | STALE | `_load_tsconfig_file` follows `extends`; `test_tsconfig_extends_relative_and_package` passes |
| Monorepo boundaries from root/`src`/`lib` | UNVERIFIED | Code adds `src`/`lib` module aliases; no boundary test |
| No cross-language edges | UNVERIFIED | No cross-language edge code; not negatively tested |
| Only Python and TS/JS | SUPPORTED | Extension map (plus `.pyi`, `.mts`, `.cts`, `.jsx`, `.mjs`, `.cjs`) |
| "75 tests" (twice) | STALE | 83 collected/passed |
| `uv tool install .` / `pip install .` | SUPPORTED | Wheel + clean venv + `uv tool run` tested |
| "No network calls at query time" | UNVERIFIED | Code inspection; grammar fetch happens on first parse, not per query |
| Server auto-starts with no args | SUPPORTED | `test_no_subcommand_starts_mcp` passed |
| `POLYGLOT_AST_REPO` / `POLYGLOT_AST_WATCH` | PARTIAL | Both read in `server.py`; watch loop not exercised end-to-end |

## 5. Gaps for the next three prompts

### Prompt A — Python correctness and measurement
- Fix incremental relinking when a new module appears (`refresh_file` must rebuild/refresh the
  module index; regression: `py-l`).
- Fix the one-edge-per-pair model so `constructs` and `uses_type` can both exist (or make the
  type pass not overwrite calls); recheck slice/blast fan-out.
- Attribute module-level statements, especially `if __name__ == "__main__":`, to a module symbol
  so scripts have callers (`py-e`).
- Give getter/setter (and overloads) distinct identities or a combined span (`py-j`).
- Add `super().__init__()` and `A().run()` delegation edges (`py-b`, `py-c`) without creating
  cycles.
- Measure unconditional recall and the hard-kind/zod packets; the headline 100/100 is
  self-labeled and has no independent human audit.

### Prompt B — TypeScript/JavaScript correctness and measurement
- Resolve default imports (`import x from ...`) and `.js` specifiers that point at `.ts` files
  (`ts-default_import`, `mjs-import`).
- Resolve `super.m()` against base classes before the enclosing class; assert no self-edge.
- Add TS/JS precision/recall harnesses and pinned repos; today only Python has labeled samples.
- Revisit CommonJS/ESM coverage (`require`, `module.exports`, `.cjs` pass; `.mjs` does not) and
  the express sample that was blocked at 1 edge.

### Prompt C — Release hardening and final testing
- Update every stale claim: 75 tests, `impact_of_diff` tests, `tsconfig extends`, "every
  candidate", CI `--check`, `labels/README.md`; remove `impacted_tests` reads from the two
  benchmark scripts.
- Track `AGENTS.md` (or stop calling it a repo pointer); add Windows `LOCALAPPDATA` handling and
  tests that simulate Windows/macOS paths.
- Run CI at least once and record the result; the wheel/uvx smoke path exists but CI only builds.
- Add 20k/50k scale runs and an independent human audit of the 100-edge packets before tagging
  v1.0.0.
