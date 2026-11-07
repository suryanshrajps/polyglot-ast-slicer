"""FastMCP server and CLI entry point.

Running the binary with no arguments starts the MCP server on stdio, which is what editors
like Cursor and Claude Desktop expect. Every CLI subcommand is a thin wrapper over the same
engine so behaviour never diverges between the two surfaces.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from watchfiles import watch

from polyglot_ast_slicer import __version__
from polyglot_ast_slicer.graph_engine import IGNORED_DIRS, GraphEngine
from polyglot_ast_slicer.parser import EXTENSION_TO_LANGUAGE

mcp = FastMCP(
    "polyglot-ast-slicer",
    version=__version__,
    instructions=(
        "Local AST call-graph index for this repository. Prefer get_ast_call_slice over "
        "reading whole files: it returns the target symbol plus its callers, callees, and "
        "the data types it touches, as exact byte-range source. Call index_codebase once "
        "per session, search_symbols when a name is uncertain, and get_blast_radius before "
        "changing a signature."
    ),
)

_ENGINES: dict[str, GraphEngine] = {}
_WATCHERS: dict[str, threading.Thread] = {}
_REGISTRY_LOCK = threading.Lock()

DEFAULT_REPO_ENV = "POLYGLOT_AST_REPO"
WATCH_ENV = "POLYGLOT_AST_WATCH"


def _resolve_repo(repo_path: str | None) -> Path:
    candidate = repo_path
    if not candidate:
        candidate = os.environ.get(DEFAULT_REPO_ENV)
        if not candidate:
            resolved = Path.cwd().resolve()
            home = Path.home().resolve()
            if resolved == home or resolved.parent == resolved:
                raise ValueError(
                    f"refusing to use {resolved} as the repository; "
                    "pass --repo (or set POLYGLOT_AST_REPO)"
                )
            return resolved
    return Path(candidate).expanduser().resolve()


def _get_engine(
    repo_path: str | None, watch_enabled: bool | None = None, initial_index: bool = True
) -> GraphEngine:
    resolved = str(_resolve_repo(repo_path))
    with _REGISTRY_LOCK:
        engine = _ENGINES.get(resolved)
        if engine is None:
            engine = GraphEngine(resolved)
            if initial_index:
                engine.index_repository(force_rebuild=False)
            _ENGINES[resolved] = engine

        if watch_enabled is None:
            watch_enabled = os.environ.get(WATCH_ENV, "1") != "0"
        if watch_enabled and resolved not in _WATCHERS:
            thread = threading.Thread(
                target=_watch_loop, args=(resolved, engine), daemon=True, name="polyglot-watch"
            )
            thread.start()
            _WATCHERS[resolved] = thread
        return engine


def _watch_loop(repo_path: str, engine: GraphEngine) -> None:
    """Background file watcher: splices edited files into the graph as they are saved."""
    root = Path(repo_path)

    def _relevant(change: Any, path: str) -> bool:
        candidate = Path(path)
        if candidate.suffix.lower() not in EXTENSION_TO_LANGUAGE:
            return False
        try:
            relative = candidate.resolve().relative_to(root)
        except ValueError:
            return False
        return not any(part in IGNORED_DIRS or part.startswith(".") for part in relative.parts[:-1])

    try:
        for changes in watch(repo_path, watch_filter=_relevant, debounce=200, step=50):
            for _, changed_path in changes:
                try:
                    engine.refresh_file(changed_path)
                except Exception:
                    continue
    except Exception:
        return


# ---------------------------------------------------------------------- MCP tools


@mcp.tool()
def index_codebase(repo_path: str = "", force_rebuild: bool = False) -> dict[str, Any]:
    """Index a Python/TypeScript repository into a call graph and warm the SQLite cache.

    Call this once per session (or after large refactors) before slicing. Incremental
    re-indexing is cheap: unchanged files are served from the cache by SHA-256.
    """
    try:
        engine = _get_engine(repo_path, initial_index=False)
        return engine.index_repository(force_rebuild=force_rebuild)
    except Exception as error:  # surfaced to the agent instead of killing the server
        return {"error": f"{type(error).__name__}: {error}", "repo_path": repo_path}


@mcp.tool()
def search_symbols(
    repo_path: str = "", query: str = "", kind: str | None = None, limit: int = 20
) -> dict[str, Any]:
    """Find symbols by name. Use this when you are unsure of the exact qualified name.

    ``kind`` filters to function | method | class | interface | type_alias.
    """
    try:
        return _get_engine(repo_path).search_symbols(query, kind=kind, limit=limit)
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}


@mcp.tool()
def get_ast_call_slice(
    repo_path: str = "",
    symbol_name: str = "",
    hops: int = 2,
    include_types: bool = True,
    max_tokens: int = 6000,
    include_code: bool = True,
    include_metadata: bool = False,
) -> dict[str, Any]:
    """Return the minimal source context an LLM needs to reason about one symbol.

    Returns ``compiled_context`` plus token metrics and omission diagnostics by default;
    the JSON payload is hard-capped at ``max_tokens``. Set ``include_metadata=true`` for
    caller/callee/type lists and region byte ranges. Ambiguous names return a candidate
    list instead of silently picking one.
    """
    try:
        return _get_engine(repo_path).get_ast_call_slice(
            symbol_name=symbol_name,
            hops=hops,
            include_types=include_types,
            max_tokens=max_tokens,
            include_code=include_code,
            include_metadata=include_metadata,
        )
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}


@mcp.tool()
def get_blast_radius(
    repo_path: str = "", symbol_name: str = "", max_depth: int = 5, include_types: bool = False
) -> dict[str, Any]:
    """List every dependent that could break if this symbol's signature or behaviour changes.

    Returns callers grouped by hop distance, the affected files, and a coarse risk level.
    """
    try:
        return _get_engine(repo_path).get_blast_radius(
            symbol_name=symbol_name, max_depth=max_depth, include_types=include_types
        )
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}


@mcp.tool()
def find_circular_dependencies(repo_path: str = "") -> dict[str, Any]:
    """Detect circular imports and mutual recursion with Tarjan's strongly connected components."""
    try:
        return _get_engine(repo_path).find_circular_dependencies()
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}


@mcp.tool()
def explain_edge(repo_path: str = "", source: str = "", target: str = "") -> dict[str, Any]:
    """Explain why an edge exists: call-site line, confidence and resolution path."""
    try:
        return _get_engine(repo_path).explain_edge(source, target)
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}


@mcp.tool()
def impact_of_diff(
    repo_path: str = "",
    base: str = "HEAD~1",
    head: str = "HEAD",
    diff: str | None = None,
    max_depth: int = 3,
) -> dict[str, Any]:
    """Map a git diff to changed symbols and impacted caller symbols."""
    try:
        return _get_engine(repo_path).impact_of_diff(
            base=base, head=head, diff=diff, max_depth=max_depth
        )
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}


@mcp.tool()
def get_index_stats(repo_path: str = "") -> dict[str, Any]:
    """Report index size, language mix, token totals, cache health, and unresolved call sites."""
    try:
        return _get_engine(repo_path).get_index_stats()
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}


# ---------------------------------------------------------------------- CLI


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _cmd_index(args: argparse.Namespace) -> int:
    stats = _get_engine(args.repo, watch_enabled=False, initial_index=False).index_repository(
        force_rebuild=args.force
    )
    if args.json:
        _print_json(stats)
        return 0
    print(f"Indexed {stats['files_indexed']} files, {stats['total_symbols']} symbols, "
          f"{stats['total_edges']} edges in {stats['elapsed_ms']} ms")
    print(f"Cache: {stats['cache_hits']} hits / {stats['cache_misses']} misses")
    return 0


def _cmd_slice(args: argparse.Namespace) -> int:
    engine = _get_engine(args.repo, watch_enabled=False)
    result = engine.get_ast_call_slice(
        symbol_name=args.symbol,
        hops=args.hops,
        include_types=not args.no_types,
        max_tokens=args.max_tokens,
        include_code=not args.no_code,
        include_metadata=args.metadata,
    )
    if args.json:
        _print_json(result)
        return 0 if "error" not in result else 1
    if "error" in result:
        print(result["error"], file=sys.stderr)
        return 1
    print(result["compiled_context"])
    metrics = result["token_metrics"]
    print(
        f"\n-- {metrics['slice_tokens']} slice tokens vs {metrics['touched_files_tokens']} "
        f"in touched files ({metrics['reduction_vs_touched_files_pct']}% saved)",
        file=sys.stderr,
    )
    return 0


def _cmd_blast(args: argparse.Namespace) -> int:
    result = _get_engine(args.repo, watch_enabled=False).get_blast_radius(
        symbol_name=args.symbol, max_depth=args.depth, include_types=args.types
    )
    if args.json:
        _print_json(result)
        return 0 if "error" not in result else 1
    if "error" in result:
        print(result["error"], file=sys.stderr)
        return 1
    print(f"Blast radius of {result['target']['qualified_name']}: {result['risk_level']}")
    for hop in sorted(result["dependents_by_hop"], key=int):
        print(f"  hop {hop}:")
        for item in result["dependents_by_hop"][hop]:
            print(f"    - {item['qualified_name']} ({item['file_path']}:{item['lines']})")
    return 0


def _cmd_cycles(args: argparse.Namespace) -> int:
    result = _get_engine(args.repo, watch_enabled=False).find_circular_dependencies()
    if args.json:
        _print_json(result)
        return 0
    print(f"Symbol cycles: {result['symbol_cycle_count']}")
    for cycle in result["symbol_cycles"]:
        print("  " + " <-> ".join(node["qualified_name"] for node in cycle))
    print(f"Self-recursive symbols: {result['self_recursive_count']}")
    for item in result["self_recursive"]:
        print(f"  {item['qualified_name']} ({item['file_path']}:{item['lines']})")
    print(f"Import cycles: {result['file_cycle_count']}")
    for cycle in result["file_cycles"]:
        print("  " + " <-> ".join(cycle))
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    result = _get_engine(args.repo, watch_enabled=False).search_symbols(
        args.query, kind=args.kind, limit=args.limit
    )
    if args.json:
        _print_json(result)
        return 0
    for item in result["results"]:
        print(
            f"{item['qualified_name']:<50} {item['kind']:<10} "
            f"{item['file_path']}:{item['lines']}"
        )
    if not result["results"]:
        print("No matches.", file=sys.stderr)
    return 0


def _cmd_impact(args: argparse.Namespace) -> int:
    result = _get_engine(args.repo, watch_enabled=False).impact_of_diff(
        base=args.base, head=args.head, max_depth=args.depth
    )
    if args.json:
        _print_json(result)
        return 0 if "error" not in result else 1
    if "error" in result:
        print(result["error"], file=sys.stderr)
        return 1
    print(
        f"Changed symbols: {result['total_changed_symbols']} "
        f"in {len(result['changed_files'])} files"
    )
    for item in result["changed_symbols"]:
        print(f"  ~ {item['qualified_name']} ({item['file_path']}:{item['lines']})")
    print(f"Impacted symbols: {result['total_impacted_symbols']} ({result['risk_level']})")
    for item in result["impacted_symbols"]:
        print(f"  <- {item['qualified_name']} hop {item['hop_distance']}")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    result = _get_engine(args.repo, watch_enabled=False).get_index_stats()
    _print_json(result)
    return 0


def _serve(repo_path: str | None) -> int:
    try:
        _get_engine(repo_path)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    mcp.run(transport="stdio")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    return _serve(args.repo)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polyglot-ast-slicer",
        description="Token-efficient AST call-graph slices for AI agents (local MCP).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo",
        default=None,
        help="Repository root (default: POLYGLOT_AST_REPO, else the current directory)",
    )
    common.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    p_index = sub.add_parser("index", parents=[common], help="Index a repository")
    p_index.add_argument("--force", action="store_true", help="Ignore the cache and re-parse")
    p_index.set_defaults(handler=_cmd_index)

    p_slice = sub.add_parser("slice", parents=[common], help="Print an N-hop call slice")
    p_slice.add_argument("--symbol", required=True, help="Function, method, or class name")
    p_slice.add_argument("--hops", type=int, default=2, help="BFS depth (default: 2)")
    p_slice.add_argument("--max-tokens", type=int, default=None, help="Hard payload token budget")
    p_slice.add_argument(
        "--metadata", action="store_true", help="Include caller/callee/type metadata"
    )
    p_slice.add_argument("--no-types", action="store_true", help="Skip data-type snippets")
    p_slice.add_argument("--no-code", action="store_true", help="Metadata only, no source")
    p_slice.set_defaults(handler=_cmd_slice)

    p_blast = sub.add_parser("blast", parents=[common], help="Upstream blast radius")
    p_blast.add_argument("--symbol", required=True, help="Target symbol")
    p_blast.add_argument("--depth", type=int, default=5, help="Max upstream depth")
    p_blast.add_argument("--types", action="store_true", help="Include type/inheritance edges")
    p_blast.set_defaults(handler=_cmd_blast)

    p_cycles = sub.add_parser("cycles", parents=[common], help="Circular dependencies")
    p_cycles.set_defaults(handler=_cmd_cycles)

    p_search = sub.add_parser("search", parents=[common], help="Search symbols by name")
    p_search.add_argument("query", help="Substring to match")
    p_search.add_argument("--kind", default=None, help="Filter by symbol kind")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(handler=_cmd_search)

    p_impact = sub.add_parser(
        "impact", parents=[common], help="Diff to changed and impacted symbols"
    )
    p_impact.add_argument("--base", default="HEAD~1", help="Base git revision")
    p_impact.add_argument("--head", default="HEAD", help="Head git revision")
    p_impact.add_argument("--depth", type=int, default=3, help="Upstream hop depth")
    p_impact.set_defaults(handler=_cmd_impact)

    p_stats = sub.add_parser("stats", parents=[common], help="Index and cache statistics")
    p_stats.set_defaults(handler=_cmd_stats)

    p_serve = sub.add_parser("serve", parents=[common], help="Run the MCP server on stdio")
    p_serve.set_defaults(handler=_cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        code = _serve(None)
        if code:
            raise SystemExit(code)
        return

    parser = build_parser()
    args = parser.parse_args(arguments)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        raise SystemExit(2)
    raise SystemExit(handler(args))


if __name__ == "__main__":
    main()
