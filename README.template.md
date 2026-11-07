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

`serve` uses `--repo` when given, otherwise `POLYGLOT_AST_REPO`, otherwise the current
working directory. A project-level Cursor config (`.cursor/mcp.json`) can therefore omit
the repository:

```json
{
  "mcpServers": {
    "polyglot-ast-slicer": {
      "command": "uvx",
      "args": ["polyglot-ast-slicer", "serve"]
    }
  }
}
```

Claude Code:

```bash
claude mcp add polyglot-ast-slicer -- uvx polyglot-ast-slicer serve
```

Claude Desktop does not guarantee the server's working directory, so pass the repository
explicitly in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "polyglot-ast-slicer": {
      "command": "uvx",
      "args": [
        "polyglot-ast-slicer", "serve",
        "--repo", "/absolute/path/to/your/repo"
      ]
    }
  }
}
```

## Worked example: Python

```console
$ polyglot-ast-slicer slice --repo verify/fixtures/readme/python --symbol process_payment
{{python_example}}
```

## Worked example: TypeScript

```console
$ polyglot-ast-slicer slice --repo verify/fixtures/readme/typescript --symbol processPayment
{{typescript_example}}
```

## Results

Ground truth is Jedi for Python and the TypeScript compiler API for TS/JS. Scope is
production source only (test, story and benchmark files are excluded per repository);
held-out repositories were never used for tuning. Intervals are 95% Wilson score intervals.
Precision counts a site as wrong when the tool reports a different project declaration;
recall counts resolved sites as missed when the tool reports no edge.

{{results_table}}

## Performance

Measured by `verify/scale_probe.py` on the development machine, one process per corpus.
Each refresh is a one-line edit to one of 30 distinct files; latency varies with file size
and disk state, so the table reports median and p95 rather than a fixed promise. Synthetic
corpora use seed 42.

{{performance_table}}

## Slice size comparison

Default slice payload tokens (tiktoken on the exact JSON), the entry symbol's whole file, and
every unique file containing its callers and callees. Thirty symbols per language,
stratified as 10 per repository (seed 42). These are sizes only; they do not show that a
slice is sufficient.

{{size_table}}

## Languages

Generated from the parser extension map.

{{language_table}}

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
uv run pytest                       # {{test_count}} tests collected
uv run python scripts/generate_metrics.py --check
uv run ruff check .
uv run mypy polyglot_ast_slicer
```

Measured numbers live in [METRICS.md](METRICS.md), generated from the committed scripts and
pinned repositories. CI runs the offline `--check` and fails if METRICS.md or README.md drifts.

## License

MIT — see [LICENSE](LICENSE).
