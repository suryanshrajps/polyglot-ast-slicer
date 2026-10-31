"""Stage B v2: non-test entry points, budgeted co-change recall, baselines, failures."""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WORK = Path("/tmp/sb_work_v2")
CACHE = Path("/tmp/sb_cache_v2")
PATHS = {"requests": Path("/tmp/sb_requests"), "click": Path("/tmp/sb_click"),
         "flask": Path("/tmp/sb_flask")}
ENC = tiktoken.get_encoding("cl100k_base")
BUDGETS = (3000, 6000, 12000, 10**9)


def tokens(text: str) -> int:
    return len(ENC.encode(text, disallowed_special=()))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    ).stdout


def parse_old(diff: str) -> dict[str, set[int]]:
    files, current = {}, None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].split("\t")[0]
        elif line.startswith("@@") and current:
            match = re.search(r"-(\d+)(?:,(\d+))?", line)
            if match:
                start, count = int(match.group(1)), int(match.group(2) or 1)
                if start:
                    files.setdefault(current, set()).update(range(start, start + max(count, 1)))
    return files


def changed_symbols(engine, tree: Path, files):
    found = []
    for relative, lines in files.items():
        absolute = str((tree / relative).resolve())
        for sid in engine._by_file.get(absolute, set()):
            symbol = engine._symbols[sid]
            if symbol.kind not in ("function", "method"):
                continue
            if any(symbol.start_line <= line <= symbol.end_line for line in lines):
                found.append(symbol)
    return sorted(found, key=lambda s: s.symbol_id)


def covered_ids(payload):
    ids = {payload["target"]["symbol_id"]}
    for key in ("upstream_callers", "downstream_callees", "relevant_types_and_schemas"):
        ids |= {item["symbol_id"] for item in payload.get(key, [])}
    for region in payload.get("regions", []):
        ids |= set(region.get("symbol_ids", []))
    return ids


def reachable(engine, start, hops=2):
    seen = {start}
    frontier = {start}
    for _ in range(hops):
        nxt = set()
        for node in frontier:
            nxt |= set(engine.graph.successors(node)) | set(engine.graph.predecessors(node))
        nxt -= seen
        seen |= nxt
        frontier = nxt
    return seen


def main() -> None:
    commits = json.loads((ROOT / "benchmarks/stage_b/commits.json").read_text())
    rows, dropped = [], 0
    for item in commits:
        repo = PATHS[item["repo"]]
        sha = item["sha"]
        parent = git(repo, "rev-parse", f"{sha}^").strip()
        tree = (WORK / f"{repo.name}-{sha[:8]}").resolve()
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
                       capture_output=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(tree), parent],
                       check=True, capture_output=True)
        engine = None
        try:
            engine = GraphEngine(tree, cache_dir=CACHE)
            engine.index_repository(force_rebuild=False)
            diff = git(repo, "diff", "-U0", parent, sha, "--")
            changed = changed_symbols(engine, tree, parse_old(diff))
            non_test = [s for s in changed if "test" not in s.file_path.lower()]
            if len(non_test) < 2:
                dropped += 1
                continue
            for target in non_test[:3]:
                others = [s for s in non_test if s.symbol_id != target.symbol_id]
                cover = {}
                for budget in BUDGETS:
                    payload = engine.get_ast_call_slice(
                        target.symbol_id, hops=2, max_tokens=budget, include_metadata=True
                    )
                    if "error" in payload:
                        cover[budget] = set()
                    else:
                        cover[budget] = covered_ids(payload)
                reach = reachable(engine, target.symbol_id)
                recall = {
                    budget: sum(1 for s in others if s.symbol_id in ids) / len(others)
                    for budget, ids in cover.items()
                }
                grep = subprocess.run(
                    ["grep", "-rn", "-C", "20", target.name, str(tree)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                ).stdout
                grep_recall = sum(1 for s in others if s.name in grep) / len(others)
                unlimited = cover[BUDGETS[-1]]
                touched = {
                    region["file_path"] for region in engine.get_ast_call_slice(
                        target.symbol_id, hops=2, max_tokens=BUDGETS[-1], include_metadata=True
                    ).get("regions", [])
                }
                touched_recall = sum(
                    1 for s in others
                    if str(Path(s.file_path).relative_to(tree)) in touched
                ) / len(others)
                missed = [s for s in others if s.symbol_id not in unlimited]
                classes = Counter()
                for symbol in missed:
                    if symbol.symbol_id in reach:
                        classes["connected_pruned"] += 1
                    elif symbol.file_path == target.file_path:
                        classes["not_connected_same_file"] += 1
                    else:
                        classes["not_connected_other"] += 1
                rows.append({
                    "repo": repo.name, "sha": sha, "target": target.qualified_name,
                    "recall": {str(b): recall[b] for b in BUDGETS},
                    "reachable2": sum(1 for s in others if s.symbol_id in reach) / len(others),
                    "grep_recall": grep_recall, "touched_recall": touched_recall,
                    "classes": dict(classes),
                    "payload_tokens": tokens(json.dumps(engine.get_ast_call_slice(
                        target.symbol_id, hops=2), separators=(",", ":"))),
                    "grep_tokens": tokens(grep),
                })
        finally:
            if engine is not None:
                engine.close()
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
                           capture_output=True)
    (ROOT / "benchmarks/stage_b/results_v2.json").write_text(json.dumps(rows, indent=2) + "\n")
    summary = {
        "entries": len(rows), "dropped_commits": dropped,
        "recall": {
            str(b): round(statistics.median([r["recall"][str(b)] for r in rows]), 3)
            for b in BUDGETS
        },
        "reachable2_median": round(statistics.median([r["reachable2"] for r in rows]), 3),
        "grep_recall_median": round(statistics.median([r["grep_recall"] for r in rows]), 3),
        "touched_recall_median": round(statistics.median([r["touched_recall"] for r in rows]), 3),
        "classes": dict(sum((Counter(r["classes"]) for r in rows), Counter())),
        "payload_median": round(statistics.median([r["payload_tokens"] for r in rows]), 1),
        "grep_tokens_median": round(statistics.median([r["grep_tokens"] for r in rows]), 1),
    }
    (ROOT / "benchmarks/stage_b/summary_v2.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
