# polyglot-ast-slicer

A local call-graph context tool for AI coding agents.
It parses Python and TypeScript/JavaScript with tree-sitter, builds a directed call graph, and
returns byte-exact source slices over MCP.
It does not execute code, has no network calls at query time, and keeps its index outside the
repository.

## What it does

- Indexes a repository and resolves call edges from imports, receivers, annotations and
  constructor calls, with an explicit confidence per edge.
- Slices one symbol plus N call hops into exact byte ranges under a token budget.
- Exposes the graph through MCP and a CLI (`index`, `slice`, `blast`, `cycles`, `search`,
  `stats`, `impact`).

## Install

```bash
uvx --from polyglot-ast-slicer polyglot-ast-slicer --help
```

Or install the command:

```bash
uv tool install polyglot-ast-slicer
```

Requires Python 3.12+. The SQLite index lives in the OS cache directory, never in the
repository.

## MCP configuration

Cursor (`~/.cursor/mcp.json`), Claude Desktop (`claude_desktop_config.json`) and Claude Code
(`.mcp.json` in a project) use the same server entry:

```json
{
  "mcpServers": {
    "polyglot-ast-slicer": {
      "command": "uvx",
      "args": [
        "--from", "polyglot-ast-slicer",
        "polyglot-ast-slicer", "serve",
        "--repo", "/absolute/path/to/your/repo"
      ]
    }
  }
}
```

Claude Code can add it directly:

```bash
claude mcp add polyglot-ast-slicer -- \
  uvx --from polyglot-ast-slicer polyglot-ast-slicer serve --repo /absolute/path/to/your/repo
```

## Worked example: Python

```console
$ polyglot-ast-slicer slice --repo verify/fixtures/readme/python --symbol process_payment
### backend/service.py:1-7 [Ledger, Ledger.record] (data_schema, downstream_callee)
class Ledger:
    def __init__(self) -> None:
        self.entries: list[int] = []

    def record(self, amount: int) -> int:
        self.entries.append(amount)
        return sum(self.entries)

### backend/service.py:10-12 [process_payment] (target)
def process_payment(amount: int, ledger: Ledger) -> int:
    fee = amount // 10
    return ledger.record(amount - fee)
```

## Worked example: TypeScript

```console
$ polyglot-ast-slicer slice --repo verify/fixtures/readme/typescript --symbol processPayment
### src/service.ts:1-8 [Ledger, Ledger.record] (data_schema, downstream_callee)
export class Ledger {
  private entries: number[] = [];

  record(amount: number): number {
    this.entries.push(amount);
    return this.entries.reduce((total, value) => total + value, 0);
  }
}

### src/service.ts:10-13 [processPayment] (target)
export function processPayment(amount: number, ledger: Ledger): number {
  const fee = Math.floor(amount / 10);
  return ledger.record(amount - fee);
}
```

## Results

Ground truth is Jedi for Python and the TypeScript compiler API for TS/JS. Scope is
production source only (test, story and benchmark files are excluded per repository);
held-out repositories were never used for tuning. Intervals are 95% Wilson score intervals.
Precision counts a site as wrong when the tool reports a different project declaration;
recall counts resolved sites as missed when the tool reports no edge.

| Repo | Language | Pinned SHA | Sites | Precision (95% CI) | High-conf | Recall (95% CI) | Source |
|---|---|---|---:|---|---:|---|---|
| `requests` | Python | `3c7baaa` | 307 | 100.0% (95% CI 98.6%–100.0%) | 257/257 | 89.9% (95% CI 86.0%–92.8%) | main |
| `click` | Python | `2247b35` | 740 | 99.8% (95% CI 99.0%–100.0%) | 460/461 | 74.6% (95% CI 71.3%–77.6%) | main |
| `flask` | Python | `49943b5` | 348 | 100.0% (95% CI 98.2%–100.0%) | 193/193 | 60.3% (95% CI 55.1%–65.3%) | main |
| `werkzeug` | Python | `6604c3b` | 1426 | 99.9% (95% CI 99.5%–100.0%) | 945/946 | 75.7% (95% CI 73.4%–77.9%) | held-out |
| `zod` | TypeScript | `ca42965` | 557 | 100.0% (95% CI 99.2%–100.0%) | 417/417 | 81.1% (95% CI 77.7%–84.2%) | main |
| `nest` | TypeScript | `416830c` | 1998 | 100.0% (95% CI 99.8%–100.0%) | 1442/1442 | 89.2% (95% CI 87.8%–90.5%) | main |
| `bulletproof-react` | TypeScript | `9506629` | 213 | 100.0% (95% CI 97.6%–100.0%) | 159/159 | 74.6% (95% CI 68.4%–80.0%) | main |
| `node-fetch` | JavaScript | `8b3320d` | 89 | 100.0% (95% CI 95.6%–100.0%) | 84/84 | 94.4% (95% CI 87.5%–97.6%) | main |
| `express` | JavaScript | `1faf228` | 67 | 100.0% (95% CI 84.5%–100.0%) | 20/20 | 31.3% (95% CI 21.5%–43.2%) | main |
| `zustand` | TypeScript | `9fed2eb` | 43 | 100.0% (95% CI 90.8%–100.0%) | 38/38 | 88.4% (95% CI 75.5%–94.9%) | held-out |
| `moment` | JavaScript | `485d9a7` | 794 | 100.0% (95% CI 99.2%–100.0%) | 507/507 | 63.9% (95% CI 60.5%–67.1%) | held-out |
| **Python (all)** | | | 2821 | 99.9% (95% CI 99.7%–100.0%) | 1855/1857 (99.9%) | 75.1% (95% CI 73.5%–76.6%) | |
| **TypeScript (all)** | | | 2811 | 100.0% (95% CI 99.8%–100.0%) | 2056/2056 (100.0%) | 86.5% (95% CI 85.2%–87.7%) | |
| **JavaScript (all)** | | | 950 | 100.0% (95% CI 99.4%–100.0%) | 611/611 (100.0%) | 64.4% (95% CI 61.3%–67.4%) | |

## Performance

Measured by `verify/scale_probe.py` on the development machine, one process per corpus.
Synthetic corpora use seed 42. Latency is machine-dependent.

| Corpus | Files | Cold ms | Warm ms | Incremental p95 ms | Query ms | Peak RSS MB |
|---|---:|---:|---:|---:|---:|---:|
| synthetic-5000 | 5000 | 1918.6 | 1307.0 | 541.59 | 4.1 | 130.2 |
| synthetic-20000 | 20000 | 9761.3 | 5624.2 | 2609.09 | 42.7 | 243.9 |
| synthetic-50000 | 50000 | 23531.6 | 14054.9 | 5477.02 | 16.9 | 448.0 |
| python-requests | 37 | 240.9 | 72.0 | 43.34 | 2.4 | 103.0 |
| ts-zod | 169 | 390.8 | 59.8 | 20.28 | 0.9 | 105.7 |

## Slice size comparison

Default slice payload tokens (tiktoken on the exact JSON), the entry symbol's whole file, and
every unique file containing its callers and callees. Thirty random symbols per language,
seed 42. These are sizes only; they do not show that a slice is sufficient.

| Language | Symbols | Payload tokens p10 / median / p90 | Entry file p10 / median / p90 | Caller+callee files p10 / median / p90 |
|---|---:|---|---|---|
| python | 30 | 242 / 3473 / 5934 | 1466 / 5953 / 9114 | 2846 / 8694 / 31961 |
| typescript | 30 | 234 / 1977 / 4938 | 1195 / 39169 / 39169 | 1195 / 39169 / 40544 |

## Languages

Generated from the parser extension map.

| Extension | Language | Tree-sitter grammar |
|---|---|---|
| `.cjs` | javascript | `javascript` |
| `.cts` | typescript | `typescript` |
| `.js` | javascript | `javascript` |
| `.jsx` | tsx | `tsx` |
| `.mjs` | javascript | `javascript` |
| `.mts` | typescript | `typescript` |
| `.py` | python | `python` |
| `.pyi` | python | `python` |
| `.ts` | typescript | `typescript` |
| `.tsx` | tsx | `tsx` |

## What it does not do

- **Dynamic dispatch and reflection.** `getattr(obj, name)()`, `obj[name]()`, dependency
  injection, and `*args` forwarding produce no edge.
- **Monkeypatching and runtime rewriting.** Decorators, metaclasses and prototype mutation are
  not interpreted.
- **Callbacks and higher-order dataflow.** A function passed as a value is not followed when it
  is invoked through an unrelated parameter.
- **Same-file siblings.** A bare call is resolved only when the binding is unambiguous;
  computed member calls and unresolved receivers are left without an edge.
- **Cross-language edges.** Python calling into TypeScript (or the reverse) is not linked.
- **Test selection.** `impact_of_diff` returns changed and impacted symbols; it does not choose
  tests.
- **Express-style prototype patterns.** Assigned function bodies are indexed, but prototype
  dispatch and dynamic property lookup are not resolved.

## When another tool fits better

If you need type-aware refactoring, rename, or exhaustive reference search, use a language
server such as pyright or tsserver, or a code index such as SCIP or Sourcegraph. If you need
retrieval by meaning rather than by call edges, use an embedding or vector-search tool. This
tool is for exact call-context retrieval.

## Development

```bash
uv sync --all-extras
uv run pytest                       # 103 tests collected
uv run python scripts/generate_metrics.py --check
uv run ruff check .
uv run mypy polyglot_ast_slicer
```

Measured numbers live in [METRICS.md](METRICS.md), generated from the committed scripts and
pinned repositories. CI runs the offline `--check` and fails if METRICS.md or README.md drifts.

## License

MIT — see [LICENSE](LICENSE).
