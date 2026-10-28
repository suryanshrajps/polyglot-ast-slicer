# src/server.py
from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from watchfiles import watch

from src.graph_engine import GraphEngine
from src.parser import EXTENSION_TO_LANGUAGE

mcp = FastMCP("polyglot-ast-slicer")

_ENGINES: dict[str, GraphEngine] = {}
_WATCHERS: dict[str, threading.Thread] = {}
_LOCK = threading.Lock()


def _get_engine(repo_path: str, enable_watcher: bool = True) -> GraphEngine:
    resolved = str(Path(repo_path).resolve())
    with _LOCK:
        if resolved not in _ENGINES:
            engine = GraphEngine(resolved)
            engine.index_repository(force_rebuild=False)
            _ENGINES[resolved] = engine
            if enable_watcher and resolved not in _WATCHERS:
                watcher_thread = threading.Thread(
                    target=_background_file_watcher,
                    args=(resolved, engine),
                    daemon=True,
                )
                watcher_thread.start()
                _WATCHERS[resolved] = watcher_thread
        return _ENGINES[resolved]


def _background_file_watcher(repo_path: str, engine: GraphEngine) -> None:
    """Watches the repository for file saves and splices AST edges in sub-15ms."""
    try:
        for changes in watch(repo_path, raise_interrupt=False):
            for _, changed_path in changes:
                ext = Path(changed_path).suffix.lower()
                if ext in EXTENSION_TO_LANGUAGE:
                    with _LOCK:
                        engine.refresh_file(changed_path)
    except Exception:
        pass


@mcp.tool()
def index_codebase(repo_path: str, force_rebuild: bool = False) -> dict[str, Any]:
    """
    Indexes a Python (.py) and TypeScript/JavaScript (.ts/.tsx/.js) repository into
    a directed AST call graph using Tree-sitter and caches SHA-256 hashes in SQLite.
    """
    engine = _get_engine(repo_path)
    with _LOCK:
        return engine.index_repository(force_rebuild=force_rebuild)


@mcp.tool()
def get_ast_call_slice(
    repo_path: str,
    symbol_name: str,
    hops: int = 2,
    include_types: bool = True,
) -> dict[str, Any]:
    """
    Performs an N-hop Breadth-First Search (BFS) call-graph slice around `symbol_name`,
    returning exact byte-range source snippets for upstream callers, downstream callees,
    and directly referenced data types/schemas alongside tiktoken reduction metrics.
    """
    engine = _get_engine(repo_path)
    with _LOCK:
        return engine.get_ast_call_slice(
            symbol_name=symbol_name,
            hops=hops,
            include_types=include_types,
        )


@mcp.tool()
def find_circular_dependencies(repo_path: str) -> dict[str, Any]:
    """
    Detects circular dependencies, mutual recursion loops, and file-level import cycles
    across the codebase using Tarjan's Strongly Connected Components (SCC) algorithm.
    """
    engine = _get_engine(repo_path)
    with _LOCK:
        return engine.find_circular_dependencies()


@mcp.tool()
def get_blast_radius(
    repo_path: str,
    symbol_name: str,
    max_depth: int = 5,
) -> dict[str, Any]:
    """
    Calculates the upstream blast radius of `symbol_name`, returning all transitive
    callers and dependent files across the repository grouped by hop distance.
    """
    engine = _get_engine(repo_path)
    with _LOCK:
        return engine.get_blast_radius(symbol_name=symbol_name, max_depth=max_depth)


def main() -> None:
    # Default to MCP stdio mode when launched with no CLI subcommands (e.g., by Cursor/Claude)
    if len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1] == "mcp"):
        mcp.run(transport="stdio")
        return

    parser = argparse.ArgumentParser(
        prog="polyglot-ast-slicer",
        description="Polyglot AST Call-Graph Slicer CLI & Local MCP Server",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_idx = sub.add_parser("index", help="Index a repository and populate SQLite AST cache")
    p_idx.add_argument("--repo", default=".", help="Path to repository")
    p_idx.add_argument("--force", action="store_true", help="Force full cache rebuild")

    p_slice = sub.add_parser("slice", help="Extract an N-hop AST call slice for a symbol")
    p_slice.add_argument("--repo", default=".", help="Path to repository")
    p_slice.add_argument("--symbol", required=True, help="Function, method, or class name")
    p_slice.add_argument("--hops", type=int, default=2, help="BFS hop depth (default: 2)")

    p_blast = sub.add_parser("blast", help="Compute upstream blast radius for a symbol")
    p_blast.add_argument("--repo", default=".", help="Path to repository")
    p_blast.add_argument("--symbol", required=True, help="Target symbol name")
    p_blast.add_argument("--depth", type=int, default=5, help="Max upstream hop depth")

    p_cycles = sub.add_parser("cycles", help="Detect circular dependencies using Tarjan's SCC")
    p_cycles.add_argument("--repo", default=".", help="Path to repository")

    args = parser.parse_args()
    engine = _get_engine(args.repo, enable_watcher=False)

    if args.command == "index":
        out = engine.index_repository(force_rebuild=args.force)
    elif args.command == "slice":
        out = engine.get_ast_call_slice(symbol_name=args.symbol, hops=args.hops)
    elif args.command == "blast":
        out = engine.get_blast_radius(symbol_name=args.symbol, max_depth=args.depth)
    elif args.command == "cycles":
        out = engine.find_circular_dependencies()
    else:
        out = {"error": f"Unknown command: {args.command}"}

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()