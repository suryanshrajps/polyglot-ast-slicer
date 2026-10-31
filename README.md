# Polyglot AST Call-Graph Slicer

**Stop pasting files into your AI. Give it the 3% of the repo that matters.**

A local, deterministic context compiler for AI coding agents. It parses your Python and
TypeScript/JavaScript with tree-sitter, builds a directed call graph, and serves *surgical*
N-hop source slices over the Model Context Protocol — exact byte ranges, no vector search, no
cloud, no API keys.

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## The problem

AI coding agents are bad at context, in two specific ways:

1. **RAG breaks code.** A chunker cuts a function in half, the agent sees half a signature, and it
   hallucinates the rest. Callers three directories away are simply never retrieved.
2. **Dumping files wastes the window.** A 40-file service is ~120k tokens. Pasted whole, most of
   it is irrelevant — and models measurably lose accuracy on information buried in the middle.

Neither approach knows that `process_invoice` calls `calculate_tax` in a different module.

## The fix

Ask for a symbol. Get back the target, its callers, its callees, and the types it touches —
nothing else.

```
$ polyglot-ast-slicer slice --symbol process_payment --hops 2
### backend/service.py:8-12 [process_payment] (target)
def process_payment(req: PaymentRequest) -> PaymentReceipt:
    if not validate_fraud_rules(req):
        raise ValueError("fraud check failed")
    fee = compute_platform_fee(req.amount_cents)
    return persist_ledger_entry(req, fee)

### backend/ledger.py:3-8 [validate_fraud_rules, compute_platform_fee] (downstream_callee)
...
```

Every snippet is an exact byte range from disk. The graph is built from imports and call sites,
so a function 40 files away is found *because it is actually called*, not because it is
semantically similar.

## Measured results

Synthetic polyglot corpus (4 files, 252 symbols, 12k tokens), `--hops 2`:

| Target | Whole touched files | Whole repo | AST slice | Saved vs files | Saved vs repo |
|---|---:|---:|---:|---:|---:|
| `process_payment` | 10,540 | 12,214 | 439 | **95.8%** | **96.4%** |
| `handle_checkout_webhook` | 10,540 | 12,214 | 441 | **95.8%** | **96.4%** |
| `submitOrderCheckout` | 1,674 | 12,214 | 168 | **90.0%** | **98.6%** |

Indexing: **30.6 ms** cold, **4.6 ms** warm for the same corpus. Single-file incremental
re-parse + edge splice: **4.6 ms p50** (SLA: 15 ms).

These are honest numbers on a small synthetic repo, not a claim about your codebase — run
`benchmarks/token_bench.py --repo <yours>` to get yours. Savings shrink on tiny files (a
2-line helper is not worth slicing) and grow with file size and repo breadth.

**Real-repo check (v0.3.0).** 100 random call edges per repo, each resolved independently
with Jedi 0.20.1 and canonicalized through re-export sites:

| Repo | Edges sampled | Supported | Precision | Recall* |
|---|---:|---:|---:|---:|
| `psf/requests` @ `3c7baaa` | 100 | 99 | **99%** | 70.3% |
| `pallets/click` @ `2247b35` | 100 | 100 | **100%** | 85.3% |

\* Recall is conditional on Jedi resolving the call site to a project symbol; sites it cannot
resolve (untyped fixtures, dynamic receivers) are excluded. Removing the old name-only
fallback cost recall and bought precision — a deliberate trade.

**Scale (synthetic, one function per file, measured on this machine).**

| Files | Cold | Warm | Query p50 | Refresh p95 | RSS |
|---:|---:|---:|---:|---:|---:|
| 5,000 | 1.3 s | 755 ms | 1.1 ms | — | 105 MB |
| 20,000 | 6.1 s | 3.2 s | 3.1 ms | — | 212 MB |
| 50,000 | 19.0 s | 10.8 s | 3.1 ms | 1.2 ms | 400 MB |

Warm start is O(edges) because the graph is rebuilt from the cache on every open; 50k files
takes 10.8 s, which is the main scale caveat.

Default payloads are capped at 6,000 tokens **on the exact JSON returned**. The worst real hub
found, `src/click/decorators.py::command`, now returns 5,493 tokens and reports 321 omitted
upstream entries instead of the 89,698-token payload measured in v0.2.0.

## Install

```bash
git clone https://github.com/suryanshraj/polyglot-ast-slicer.git
cd polyglot-ast-slicer
uv tool install .        # or: pip install .
```

Requires Python 3.12+. No accounts, no network calls at query time.

## Connect it to your agent

The server speaks MCP over stdio and starts automatically when invoked with no arguments.

**Cursor** — `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "polyglot-ast-slicer": {
      "command": "polyglot-ast-slicer",
      "args": ["serve", "--repo", "/absolute/path/to/your/repo"]
    }
  }
}
```

**Claude Desktop** — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "polyglot-ast-slicer": {
      "command": "polyglot-ast-slicer",
      "args": ["serve", "--repo", "/absolute/path/to/your/repo"]
    }
  }
}
```

Options that apply to every tool call:

| Env var | Default | Meaning |
|---|---|---|
| `POLYGLOT_AST_REPO` | `.` | Default repo when `repo_path` is omitted |
| `POLYGLOT_AST_WATCH` | `1` | Set `0` to disable the background file watcher |

## MCP tools

| Tool | What it answers |
|---|---|
| `index_codebase` | Build/refresh the graph. Incremental via SHA-256 cache. |
| `get_ast_call_slice` | "Give me exactly the context for this symbol." 6k payload cap by default. |
| `explain_edge` | "Why does this edge exist?" Call-site line, confidence, resolution path. |
| `impact_of_diff` | "What does this diff touch and which tests are impacted?" |
| `get_blast_radius` | "What breaks if I change this signature?" Grouped by hop. |
| `find_circular_dependencies` | Tarjan SCC over both the call graph and the import graph. |
| `search_symbols` | "What is this thing actually called?" |
| `get_index_stats` | Files, symbols, edges, language mix, cache health, skipped files. |

`get_ast_call_slice` returns `compiled_context`, token metrics and omission diagnostics by
default. Caller/callee/type metadata and region byte ranges require
`include_metadata=True`; they roughly doubled the payload in v0.2.0.

### Hard token budgets

```python
get_ast_call_slice(symbol_name="process_payment", hops=3, max_tokens=6000)  # default
```

The cap applies to the exact JSON payload. Fan-out is capped per hop (10) and in total (40);
symbols above 150 degree are treated as hubs and omitted. `diagnostics.omitted` reports what
was dropped and `diagnostics.expand_hint` says how to get it back.

## How names are resolved (the part that matters)

A call graph is only as good as its edges. Most tools match bare names repo-wide, which
silently links `calculate_tax` in `tax_us.py` to the one in `tax_eu.py`. This engine assigns
an explicit confidence per edge and refuses to guess below a threshold:

| Confidence | Evidence |
|---:|---|
| 0.98 | Same-file definition of the bare name |
| 0.95 | Imported binding (`from pkg.tax import calculate_tax`) |
| 0.95 | `self`/`this`/`cls` member exists on the enclosing class |
| 0.90 | Receiver type known from annotation or `x = Foo()` |
| 0.90 | Receiver is a module alias (`import tax as t` → `t.compute`) |
| 0.85 | Member inherited from a resolved base class |
| 0.90 | Nested/local symbol within the caller's lexical scope |
| — | Anything weaker → **no edge at all** (no name-only fallback) |

Ambiguous queries are refused rather than guessed: `get_ast_call_slice("get")` returns
`code: "ambiguous_symbol"` with every candidate, and you re-query with
`pkg/billing.py::BillingEngine.process_invoice`. Direct recursion is kept as a self-edge and
reported separately under `self_recursive`.

## CLI

```bash
polyglot-ast-slicer index  --repo . --json          # build/refresh index
polyglot-ast-slicer slice  --symbol process_payment # print the compiled context
polyglot-ast-slicer blast  --symbol process_payment # downstream impact
polyglot-ast-slicer cycles                          # circular imports + recursion
polyglot-ast-slicer search invoice                  # find symbols
polyglot-ast-slicer stats                           # index/cache health
polyglot-ast-slicer serve                           # MCP over stdio (default)
```

`--json` on any subcommand emits the full structured payload. Human output goes to stdout,
diagnostics to stderr, so `slice ... > context.md` does the obvious thing.

## How it works

```
discover files (honours .gitignore, size caps)
      │
      ▼
tree-sitter parse ──► symbols, byte spans, call sites, imports, type hints
      │
      ▼
SQLite cache (SHA-256 + mtime/size; zlib payloads; schema-versioned)
      │
      ▼
NetworkX DiGraph ──► import-aware resolution with confidence
      │
      ├─► BFS N hops  ──► merged byte-range regions ──► compiled_context
      ├─► reverse BFS ──► blast radius / risk level
      └─► Tarjan SCC  ──► symbol cycles + import cycles
```

Design notes:

- **No execution.** Nothing is imported, evaluated, or run. Parsing is purely syntactic.
- **Deterministic.** Sorted traversal everywhere; two independent indexes of the same tree
  produce byte-identical slice payloads (there is a test for exactly this).
- **Incremental.** Editing one file re-parses one file. If `mtime` and size are unchanged the
  AST is reused without reading the file; a touch without content change falls back to SHA-256.
- **Overlap merging.** A class span and its method spans collapse into one region, so code is
  never emitted twice and token counts are not inflated.
- **Re-export chains are followed.** `from .generic import *` in a package `__init__.py` still
  links `import pkg; pkg.fn()` to the module that defines `fn` (up to 6 hops, cycle-safe).
  TypeScript `export * from "./x"` barrels and `tsconfig` `baseUrl`/`paths` aliases resolve too.
- **Method slices carry context.** Each region is prefixed with its enclosing `class X:` and the
  file imports it references, so an agent can edit without re-reading the file.
- **Degrades gracefully.** A missing grammar (e.g. `.tsx` when the grammar cannot be fetched),
  a binary file, an oversized file, or a symlink escaping the repository is reported in
  `skipped_files` instead of aborting the index.
- **Never writes into your repo.** The SQLite index lives in the OS cache directory
  (`~/Library/Caches/polyglot-ast-slicer` or `$XDG_CACHE_HOME`), keyed by repository path.
  Read-only checkouts index normally.
- **Path allowlist.** Set `POLYGLOT_AST_ALLOWED_ROOTS` (path-separator separated) to refuse
  indexing anything outside those roots.

## What it does not do

Being precise about this is part of being trustworthy:

- **Dynamic dispatch is not resolved.** `getattr(obj, name)()`, dependency injection, and
  `*args` forwarding produce no edges. Measured recall is 70–85% on the sites Jedi can resolve;
  end-to-end recall on untyped code is lower and not measured here.
- **`tsconfig` aliases** are supported for `baseUrl` and `paths`; `extends` is not merged yet.
- **Monorepo package boundaries** are inferred from the repo root, `src/`, and `lib/`.
- **Decorators and metaclasses** that rewrite functions are not interpreted.
- **No cross-language edges** — Python calling into TypeScript is not linked.
- Only Python and TypeScript/JavaScript are parsed.

## Development

```bash
uv sync --all-extras
uv run pytest              # 75 tests
uv run python benchmarks/token_bench.py
uv run ruff check .
uv run mypy polyglot_ast_slicer
```

Layout:

```
polyglot_ast_slicer/
  parser.py        tree-sitter → symbols, call sites, imports, type hints
  cache.py         versioned SQLite cache (SHA-256 + mtime/size, zlib)
  graph_engine.py  resolution, BFS slices, blast radius, Tarjan SCC
  server.py        FastMCP tools + CLI
  __main__.py      python -m polyglot_ast_slicer
tests/             75 tests across parser, cache, engine, server, safety, differential
benchmarks/        token-reduction + latency benchmark
```

## License

MIT — see [LICENSE](LICENSE).
