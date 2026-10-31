"""Traces three co-changed symbols that are <=2 hops away but absent from the slice."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import networkx as nx  # noqa: E402
from run_v2 import CACHE, PATHS, WORK, changed_symbols, covered_ids, git, parse_old, reachable  # noqa: E402

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    rows = json.loads((ROOT / "benchmarks/stage_b/results_v2.json").read_text())
    chosen = [row for row in rows if row["classes"].get("connected_pruned", 0) > 0][:3]
    cases = []
    for row in chosen:
        repo = PATHS[row["repo"].removeprefix("sb_")]
        sha = row["sha"]
        parent = git(repo, "rev-parse", f"{sha}^").strip()
        tree = (WORK / f"diag-{repo.name}-{sha[:8]}").resolve()
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
                       capture_output=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(tree), parent],
                       check=True, capture_output=True)
        engine = GraphEngine(tree, cache_dir=CACHE)
        try:
            engine.index_repository(force_rebuild=False)
            changed = changed_symbols(
                engine, tree, parse_old(git(repo, "diff", "-U0", parent, sha, "--"))
            )
            non_test = [s for s in changed if "test" not in s.file_path.lower()]
            target = next(s for s in non_test if s.qualified_name == row["target"])
            others = [s for s in non_test if s.symbol_id != target.symbol_id]
            payload = engine.get_ast_call_slice(
                target.symbol_id, hops=2, max_tokens=10**9, include_metadata=True
            )
            covered = covered_ids(payload)
            reach = reachable(engine, target.symbol_id)
            missing = [s for s in others if s.symbol_id in reach and s.symbol_id not in covered]
            if not missing:
                continue
            victim = missing[0]
            graph = engine.graph.to_undirected()
            path = nx.shortest_path(graph, target.symbol_id, victim.symbol_id)
            relations = []
            directions = []
            for left, right in zip(path, path[1:], strict=False):
                forward = engine.graph.has_edge(left, right)
                backward = engine.graph.has_edge(right, left)
                data = engine.graph.get_edge_data(left, right) or engine.graph.get_edge_data(
                    right, left
                ) or {}
                relations.append(data.get("relation"))
                directions.append(
                    "forward"
                    if forward and not backward
                    else "backward"
                    if backward and not forward
                    else "both"
                )
            cases.append({
                "repo": row["repo"], "sha": sha, "target": target.qualified_name,
                "missing": victim.qualified_name,
                "missing_file": str(Path(victim.file_path).relative_to(tree)),
                "path": [engine._symbols[n].qualified_name for n in path],
                "relations": relations,
                "directions": directions,
                "degrees": [engine.graph.degree(n) for n in path],
                "omitted": payload.get("diagnostics", {}).get("omitted"),
                "max_total_snippets": 40,
                "explanation": (
                    "path uses a non-call relation"
                    if any(r in ("uses_type", "inherits") for r in relations)
                    else "mixed upstream/downstream path, not covered by either directional BFS"
                    if len(set(directions)) > 1
                    else "hub cap (degree >= 150)"
                    if max(engine.graph.degree(n) for n in path) >= 150
                    else "snippet or budget cap"
                ),
            })
        finally:
            engine.close()
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
                           capture_output=True)
    (ROOT / "benchmarks/stage_b/diagnosis.json").write_text(json.dumps(cases, indent=2) + "\n")
    print(json.dumps(cases, indent=2))


if __name__ == "__main__":
    main()
