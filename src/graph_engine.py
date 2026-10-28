# src/graph_engine.py
from __future__ import annotations

import os
import time
from collections import deque
from pathlib import Path
from typing import Any

import networkx as nx
import tiktoken

from src.cache import ASTSQLiteCache
from src.parser import EXTENSION_TO_LANGUAGE, PolyglotASTParser, SymbolNode, read_byte_slice

IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".venv", "venv", "node_modules", "__pycache__",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
    }
)


class GraphEngine:
    """AST-aware Call-Graph Engine backed by NetworkX and SQLite incremental caching."""

    def __init__(self, repo_path: str | Path, db_filename: str = ".ast_slicer_cache.db") -> None:
        self.repo_path = Path(repo_path).resolve()
        self.parser = PolyglotASTParser()
        self.cache = ASTSQLiteCache(self.repo_path / db_filename)
        self.graph: nx.DiGraph = nx.DiGraph()

        self._symbols_by_id: dict[str, SymbolNode] = {}
        self._ids_by_short_name: dict[str, set[str]] = {}
        self._ids_by_qualified_name: dict[str, set[str]] = {}
        self._ids_by_file: dict[str, set[str]] = {}

        try:
            self._encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            self._encoding = None

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is not None:
            return len(self._encoding.encode(text, disallowed_special=()))
        return max(1, len(text) // 4)

    def discover_source_files(self) -> list[Path]:
        files: list[Path] = []
        for root, dirs, filenames in os.walk(self.repo_path):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext in EXTENSION_TO_LANGUAGE:
                    files.append((Path(root) / fname).resolve())
        return sorted(files)

    def index_repository(self, force_rebuild: bool = False) -> dict[str, Any]:
        t0 = time.perf_counter()
        if force_rebuild:
            self.cache.clear()
            self.graph.clear()
            self._symbols_by_id.clear()
            self._ids_by_short_name.clear()
            self._ids_by_qualified_name.clear()
            self._ids_by_file.clear()

        source_files = self.discover_source_files()
        current_file_set = {str(p) for p in source_files}

        stale_files = set(self._ids_by_file.keys()) - current_file_set
        for stale in stale_files:
            self._remove_file_nodes(stale)
            self.cache.remove_file(stale)

        cache_hits = 0
        cache_misses = 0

        for fpath in source_files:
            res = self.cache.get_or_parse_file(fpath, self.parser, force_reparse=force_rebuild)
            if res.cache_hit:
                cache_hits += 1
            else:
                cache_misses += 1
            self._register_file_symbols(res.file_path, res.symbols)

        self._rebuild_all_edges()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        return {
            "repo_path": str(self.repo_path),
            "files_indexed": len(source_files),
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "total_symbols": self.graph.number_of_nodes(),
            "total_edges": self.graph.number_of_edges(),
            "elapsed_ms": round(elapsed_ms, 2),
        }

    def refresh_file(self, file_path: str | Path) -> dict[str, Any]:
        """Incrementally re-parses a saved file and splices its edges in sub-15ms."""
        t0 = time.perf_counter()
        resolved = str(Path(file_path).resolve())
        path_obj = Path(resolved)

        old_symbol_ids = set(self._ids_by_file.get(resolved, set()))
        affected_upstreams: set[str] = set()
        for old_id in old_symbol_ids:
            if old_id in self.graph:
                affected_upstreams.update(self.graph.predecessors(old_id))

        self._remove_file_nodes(resolved)

        if not path_obj.exists():
            self.cache.remove_file(resolved)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return {"file_path": resolved, "status": "deleted", "elapsed_ms": round(elapsed_ms, 3)}

        res = self.cache.get_or_parse_file(resolved, self.parser, force_reparse=False)
        self._register_file_symbols(resolved, res.symbols)

        nodes_to_relink = set(self._ids_by_file.get(resolved, set())) | (
            affected_upstreams - old_symbol_ids
        )
        for sym_id in nodes_to_relink:
            if sym_id in self._symbols_by_id:
                self._link_symbol_edges(self._symbols_by_id[sym_id])

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "file_path": resolved,
            "status": "updated",
            "cache_hit": res.cache_hit,
            "symbols_in_file": len(res.symbols),
            "elapsed_ms": round(elapsed_ms, 3),
        }

    def _remove_file_nodes(self, file_path: str) -> None:
        sym_ids = self._ids_by_file.pop(file_path, set())
        for sym_id in sym_ids:
            sym = self._symbols_by_id.pop(sym_id, None)
            if sym is not None:
                if sym.name in self._ids_by_short_name:
                    self._ids_by_short_name[sym.name].discard(sym_id)
                if sym.qualified_name in self._ids_by_qualified_name:
                    self._ids_by_qualified_name[sym.qualified_name].discard(sym_id)
            if sym_id in self.graph:
                self.graph.remove_node(sym_id)

    def _register_file_symbols(self, file_path: str, symbols: list[SymbolNode]) -> None:
        self._remove_file_nodes(file_path)
        file_set: set[str] = set()
        for sym in symbols:
            file_set.add(sym.symbol_id)
            self._symbols_by_id[sym.symbol_id] = sym
            self._ids_by_short_name.setdefault(sym.name, set()).add(sym.symbol_id)
            self._ids_by_qualified_name.setdefault(sym.qualified_name, set()).add(sym.symbol_id)
            self.graph.add_node(
                sym.symbol_id,
                name=sym.name,
                qualified_name=sym.qualified_name,
                kind=sym.kind,
                file_path=sym.file_path,
                language=sym.language,
                start_line=sym.start_line,
                end_line=sym.end_line,
            )
        self._ids_by_file[file_path] = file_set

    def _rebuild_all_edges(self) -> None:
        self.graph.remove_edges_from(list(self.graph.edges()))
        for sym in self._symbols_by_id.values():
            self._link_symbol_edges(sym)

    def _link_symbol_edges(self, sym: SymbolNode) -> None:
        if sym.symbol_id in self.graph:
            self.graph.remove_edges_from(list(self.graph.out_edges(sym.symbol_id)))

        for call_name in sym.calls:
            targets = self._resolve_targets(call_name, caller_sym=sym)
            for target_id in targets:
                self.graph.add_edge(sym.symbol_id, target_id, relation="calls")

        for type_name in sym.type_refs:
            targets = self._resolve_targets(type_name, caller_sym=sym)
            for target_id in targets:
                if target_id != sym.symbol_id:
                    self.graph.add_edge(sym.symbol_id, target_id, relation="uses_type")

    def _resolve_targets(self, identifier: str, caller_sym: SymbolNode) -> list[str]:
        candidates = (
            self._ids_by_qualified_name.get(identifier)
            or self._ids_by_short_name.get(identifier)
            or set()
        )
        if not candidates:
            return []

        # Tier 1: Same file match
        same_file = [
            cid for cid in candidates if self._symbols_by_id[cid].file_path == caller_sym.file_path
        ]
        if same_file:
            return sorted(same_file)

        # Tier 2: Explicitly imported module/symbol match
        if caller_sym.imports:
         import_matched = [
             cid
             for cid in candidates
             if any(
                 str(Path(self._symbols_by_id[cid].file_path).with_suffix("")).endswith(imp)
                 for imp in caller_sym.imports
             )
         ]
         if import_matched:
             return sorted(import_matched)

        # Tier 3: Repo-wide fallback
        return sorted(candidates)

    def resolve_query_symbol(self, symbol_name: str) -> SymbolNode | None:
        if symbol_name in self._symbols_by_id:
            return self._symbols_by_id[symbol_name]
        if symbol_name in self._ids_by_qualified_name and self._ids_by_qualified_name[symbol_name]:
            return self._symbols_by_id[sorted(self._ids_by_qualified_name[symbol_name])[0]]
        if symbol_name in self._ids_by_short_name and self._ids_by_short_name[symbol_name]:
            return self._symbols_by_id[sorted(self._ids_by_short_name[symbol_name])[0]]
        for qname, id_set in self._ids_by_qualified_name.items():
            if qname.endswith(f".{symbol_name}") and id_set:
                return self._symbols_by_id[sorted(id_set)[0]]
        return None

    @staticmethod
    def _deduplicate_overlapping_snippets(
        snippets: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Removes snippets whose byte range is completely enclosed by another snippet in the slice."""
        kept: list[dict[str, Any]] = []
        for candidate in snippets:
            c_file = candidate["file_path"]
            c_start, c_end = candidate["byte_range"]
            is_enclosed = False
            for other in snippets:
                if other["symbol_id"] == candidate["symbol_id"] or other["file_path"] != c_file:
                    continue
                o_start, o_end = other["byte_range"]
                if o_start <= c_start and c_end <= o_end and (o_end - o_start) > (c_end - c_start):
                    is_enclosed = True
                    break
            if not is_enclosed:
                kept.append(candidate)
        return kept

    def get_ast_call_slice(
        self,
        symbol_name: str,
        hops: int = 2,
        include_types: bool = True,
    ) -> dict[str, Any]:
        if self.graph.number_of_nodes() == 0:
            self.index_repository()

        target = self.resolve_query_symbol(symbol_name)
        if target is None:
            return {
                "error": f"Symbol '{symbol_name}' not found in indexed graph.",
                "available_symbols_sample": sorted(self._ids_by_qualified_name.keys())[:30],
            }

        hops = max(1, min(hops, 5))
        target_id = target.symbol_id

        upstream_ids: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque([(target_id, 0)])
        visited_up: set[str] = {target_id}
        while queue:
            curr_id, depth = queue.popleft()
            if depth >= hops:
                continue
            for pred_id in self.graph.predecessors(curr_id):
                edge_data = self.graph.get_edge_data(pred_id, curr_id) or {}
                if edge_data.get("relation") == "calls" and pred_id not in visited_up:
                    visited_up.add(pred_id)
                    upstream_ids[pred_id] = depth + 1
                    queue.append((pred_id, depth + 1))

        downstream_ids: dict[str, int] = {}
        queue = deque([(target_id, 0)])
        visited_down: set[str] = {target_id}
        while queue:
            curr_id, depth = queue.popleft()
            if depth >= hops:
                continue
            for succ_id in self.graph.successors(curr_id):
                edge_data = self.graph.get_edge_data(curr_id, succ_id) or {}
                if edge_data.get("relation") == "calls" and succ_id not in visited_down:
                    visited_down.add(succ_id)
                    downstream_ids[succ_id] = depth + 1
                    queue.append((succ_id, depth + 1))

        type_ids: set[str] = set()
        if include_types:
            core_nodes = {target_id} | set(upstream_ids.keys()) | set(downstream_ids.keys())
            for nid in core_nodes:
                for succ_id in self.graph.successors(nid):
                    edge_data = self.graph.get_edge_data(nid, succ_id) or {}
                    if edge_data.get("relation") == "uses_type" and succ_id not in core_nodes:
                        type_ids.add(succ_id)

        def _format_snippet(sym_id: str, role: str, hop_distance: int) -> dict[str, Any]:
            sym = self._symbols_by_id[sym_id]
            code = read_byte_slice(sym.file_path, sym.start_byte, sym.end_byte)
            return {
                "symbol_id": sym.symbol_id,
                "qualified_name": sym.qualified_name,
                "kind": sym.kind,
                "role": role,
                "hop_distance": hop_distance,
                "file_path": str(Path(sym.file_path).relative_to(self.repo_path)),
                "lines": f"{sym.start_line}-{sym.end_line}",
                "byte_range": [sym.start_byte, sym.end_byte],
                "code": code,
            }

        target_snippet = _format_snippet(target_id, "target", 0)
        upstream_snippets = [
            _format_snippet(sid, "upstream_caller", dist)
            for sid, dist in sorted(upstream_ids.items(), key=lambda x: (x[1], x[0]))
        ]
        downstream_snippets = [
            _format_snippet(sid, "downstream_callee", dist)
            for sid, dist in sorted(downstream_ids.items(), key=lambda x: (x[1], x[0]))
        ]
        type_snippets = [_format_snippet(sid, "data_schema", 1) for sid in sorted(type_ids)]

        raw_snippets = [target_snippet] + upstream_snippets + downstream_snippets + type_snippets
        deduped_snippets = self._deduplicate_overlapping_snippets(raw_snippets)

        combined_slice_text = "\n\n".join(
            f"# {s['file_path']}:{s['lines']} ({s['role']}: {s['qualified_name']})\n{s['code']}"
            for s in deduped_snippets
        )
        slice_tokens = self.count_tokens(combined_slice_text)

        touched_files = {
            self._symbols_by_id[sid].file_path
            for sid in ({target_id} | set(upstream_ids) | set(downstream_ids) | type_ids)
        }
        touched_files_text = "\n".join(
            Path(fp).read_text(encoding="utf-8", errors="replace")
            for fp in touched_files
            if Path(fp).exists()
        )
        touched_files_tokens = self.count_tokens(touched_files_text)

        repo_files = self.discover_source_files()
        full_repo_text = "\n".join(
            p.read_text(encoding="utf-8", errors="replace") for p in repo_files
        )
        full_repo_tokens = self.count_tokens(full_repo_text)

        vs_touched_reduction_pct = (
            round((1.0 - (slice_tokens / touched_files_tokens)) * 100.0, 2)
            if touched_files_tokens > 0
            else 0.0
        )
        vs_repo_reduction_pct = (
            round((1.0 - (slice_tokens / full_repo_tokens)) * 100.0, 2)
            if full_repo_tokens > 0
            else 0.0
        )

        return {
            "target": target_snippet,
            "hops": hops,
            "upstream_callers": upstream_snippets,
            "downstream_callees": downstream_snippets,
            "relevant_types_and_schemas": type_snippets,
            "token_metrics": {
                "slice_tokens": slice_tokens,
                "touched_full_files_tokens": touched_files_tokens,
                "full_repo_tokens": full_repo_tokens,
                "reduction_vs_touched_files_pct": vs_touched_reduction_pct,
                "reduction_vs_full_repo_pct": vs_repo_reduction_pct,
            },
            "compiled_context": combined_slice_text,
        }

    def get_blast_radius(self, symbol_name: str, max_depth: int = 5) -> dict[str, Any]:
        if self.graph.number_of_nodes() == 0:
            self.index_repository()

        target = self.resolve_query_symbol(symbol_name)
        if target is None:
            return {"error": f"Symbol '{symbol_name}' not found in indexed graph."}

        target_id = target.symbol_id
        impacted_by_hop: dict[int, list[dict[str, Any]]] = {}
        impacted_files: set[str] = set()

        queue: deque[tuple[str, int]] = deque([(target_id, 0)])
        visited: set[str] = {target_id}

        while queue:
            curr_id, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for pred_id in self.graph.predecessors(curr_id):
                if pred_id not in visited:
                    visited.add(pred_id)
                    sym = self._symbols_by_id[pred_id]
                    edge_data = self.graph.get_edge_data(pred_id, curr_id) or {}
                    rel_file = str(Path(sym.file_path).relative_to(self.repo_path))
                    impacted_files.add(rel_file)
                    impacted_by_hop.setdefault(depth + 1, []).append(
                        {
                            "symbol_id": sym.symbol_id,
                            "qualified_name": sym.qualified_name,
                            "kind": sym.kind,
                            "relation": edge_data.get("relation", "calls"),
                            "file_path": rel_file,
                            "lines": f"{sym.start_line}-{sym.end_line}",
                        }
                    )
                    queue.append((pred_id, depth + 1))

        total_impacted = sum(len(v) for v in impacted_by_hop.values())
        risk_level = (
            "CRITICAL" if total_impacted >= 10
            else "HIGH" if total_impacted >= 5
            else "MODERATE" if total_impacted >= 1
            else "LOW"
        )

        return {
            "target_symbol": target.qualified_name,
            "target_file": str(Path(target.file_path).relative_to(self.repo_path)),
            "risk_level": risk_level,
            "total_impacted_symbols": total_impacted,
            "impacted_files_count": len(impacted_files),
            "impacted_files": sorted(impacted_files),
            "dependents_by_hop": impacted_by_hop,
        }

    def find_circular_dependencies(self) -> dict[str, Any]:
        if self.graph.number_of_nodes() == 0:
            self.index_repository()

        symbol_cycles: list[list[dict[str, str]]] = []
        for scc in nx.strongly_connected_components(self.graph):
            if len(scc) > 1:
                cycle_nodes = [
                    {
                        "qualified_name": self._symbols_by_id[nid].qualified_name,
                        "file_path": str(
                            Path(self._symbols_by_id[nid].file_path).relative_to(self.repo_path)
                        ),
                    }
                    for nid in sorted(scc)
                ]
                symbol_cycles.append(cycle_nodes)
            elif len(scc) == 1:
                single_id = next(iter(scc))
                if self.graph.has_edge(single_id, single_id):
                    sym = self._symbols_by_id[single_id]
                    symbol_cycles.append(
                        [
                            {
                                "qualified_name": sym.qualified_name,
                                "file_path": str(Path(sym.file_path).relative_to(self.repo_path)),
                                "note": "direct self-recursion",
                            }
                        ]
                    )

        file_graph: nx.DiGraph = nx.DiGraph()
        for u, v in self.graph.edges():
            file_u = str(Path(self._symbols_by_id[u].file_path).relative_to(self.repo_path))
            file_v = str(Path(self._symbols_by_id[v].file_path).relative_to(self.repo_path))
            if file_u != file_v:
                file_graph.add_edge(file_u, file_v)

        file_cycles: list[list[str]] = [
            sorted(scc) for scc in nx.strongly_connected_components(file_graph) if len(scc) > 1
        ]

        return {
            "symbol_cycle_count": len(symbol_cycles),
            "symbol_cycles": symbol_cycles,
            "file_cycle_count": len(file_cycles),
            "file_cycles": file_cycles,
        }