"""Polyglot AST Call-Graph Slicer.

A deterministic, local-first context compiler for AI coding agents: parse Python and
TypeScript/JavaScript with tree-sitter, build a directed call graph, and serve surgical
N-hop source slices over the Model Context Protocol.
"""

from __future__ import annotations

from polyglot_ast_slicer.cache import ASTSQLiteCache, CacheLookupResult, CacheStats
from polyglot_ast_slicer.graph_engine import GraphEngine, SliceOptions
from polyglot_ast_slicer.parser import (
    CallSite,
    GrammarUnavailable,
    ImportRef,
    ParsedFile,
    PolyglotASTParser,
    SymbolNode,
    read_byte_slice,
)

__version__ = "0.3.0"

__all__ = [
    "ASTSQLiteCache",
    "CacheLookupResult",
    "CacheStats",
    "CallSite",
    "GrammarUnavailable",
    "GraphEngine",
    "ImportRef",
    "ParsedFile",
    "PolyglotASTParser",
    "SliceOptions",
    "SymbolNode",
    "__version__",
    "read_byte_slice",
]
