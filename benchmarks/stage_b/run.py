"""Stage B: offline outcome study over pinned bug-fix commits."""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WORK = Path("/tmp/sb_work")
CACHE = Path("/tmp/sb_cache")
ENC = tiktoken.get_encoding("cl100k_base")
def tokens(text: str) -> int:
    return len(ENC.encode(text, disallowed_special=()))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True
    ).stdout


def parse_old_lines(diff: str) -> dict[str, set[int]]:
    files: dict[str, set[int]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].split("\t")[0]
        elif line.startswith("@@") and current:
            match = re.search(r"-(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2) or 1)
                if start > 0:
                    files.setdefault(current, set()).update(range(start, start + max(count, 1)))
    return files


def symbols_at(engine: GraphEngine, repo: Path, files: dict[str, set[int]]) -> list:
    found = []
    for relative, lines in files.items():
        absolute = str((repo / relative).resolve())
        for symbol_id in engine._by_file.get(absolute, set()):
            symbol = engine._symbols[symbol_id]
            if symbol.kind not in ("function", "method", "class"):
                continue
            if any(symbol.start_line <= line <= symbol.end_line for line in lines):
                found.append(symbol)
    return sorted(found, key=lambda symbol: symbol.symbol_id)


PATHS = {"requests": Path("/tmp/sb_requests"), "click": Path("/tmp/sb_click"),
         "flask": Path("/tmp/sb_flask")}


def run_commit(item: dict, base_repo: Path) -> dict:
    sha = item["sha"]
    repo = base_repo
    parent = git(repo, "rev-parse", f"{sha}^").strip()
    tree = (WORK / f"{repo.name}-{sha[:8]}").resolve()
    if tree.exists():
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
                       capture_output=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(tree), parent],
                   check=True, capture_output=True)
    try:
        engine = None
        engine = GraphEngine(tree, cache_dir=CACHE)
        engine.index_repository(force_rebuild=False)
        diff = git(repo, "diff", "-U0", parent, sha, "--")
        changed = symbols_at(engine, tree, parse_old_lines(diff))
        changed = [symbol for symbol in changed if symbol.kind in ("function", "method")]
        if not changed:
            return {"repo": repo.name, "sha": sha, "status": "no_changed_function"}
        target = changed[0]
        payload = engine.get_ast_call_slice(
            target.symbol_id, hops=2, include_metadata=True
        )
        covered = {s["symbol_id"] for group in (
            payload.get("upstream_callers", []), payload.get("downstream_callees", []),
            payload.get("relevant_types_and_schemas", []),
        ) for s in group}
        covered |= {
            sid for region in payload.get("regions", [])
            for sid in region.get("symbol_ids", [])
        }
        covered.add(target.symbol_id)
        others = [symbol for symbol in changed if symbol.symbol_id != target.symbol_id]
        co_recall = (
            sum(1 for symbol in others if symbol.symbol_id in covered) / len(others)
            if others else None
        )
        impact = engine.impact_of_diff(base=parent, head=sha)
        impacted_tests = {t["symbol_id"] for t in impact.get("impacted_tests", [])}
        changed_tests = [symbol for symbol in changed if "test" in symbol.file_path.lower()]
        test_recall = (
            sum(1 for symbol in changed_tests if symbol.symbol_id in impacted_tests)
            / len(changed_tests) if changed_tests else None
        )
        slim = engine.get_ast_call_slice(target.symbol_id, hops=2)
        payload_tokens = tokens(json.dumps(slim, separators=(",", ":")))
        touched_files = {region["file_path"] for region in payload.get("regions", [])}
        touched_tokens = sum(
            tokens((tree / relative).read_text(errors="replace")) for relative in touched_files
        )
        grep = subprocess.run(
            ["grep", "-rn", "-C", "20", target.name, str(tree)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout
        grep_tokens = tokens(grep)
        repo_map = "\n".join(
            f"{Path(symbol.file_path).relative_to(tree)}:{symbol.start_line}: {symbol.signature}"
            for symbol in engine._symbols.values()
        )
        map_tokens = tokens(repo_map)
        return {
            "repo": repo.name, "sha": sha, "subject": item["subject"],
            "status": "ok", "target": target.qualified_name,
            "co_change_recall": co_recall, "test_recall": test_recall,
            "payload_tokens": payload_tokens, "touched_tokens": touched_tokens,
            "grep_tokens": grep_tokens, "map_tokens": map_tokens,
            "budget": slim["token_metrics"]["budget_tokens"],
        }
    finally:
        if engine is not None:
            engine.close()
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
                       capture_output=True)


def main() -> None:
    commits = json.loads((ROOT / "benchmarks/stage_b/commits.json").read_text())
    results = []
    for item in commits:
        base = PATHS[item["repo"]]
        try:
            results.append(run_commit(item, base))
        except Exception as error:  # noqa: BLE001
            results.append({"repo": base.name, "sha": item["sha"], "status": f"error: {error}"})
    (ROOT / "benchmarks/stage_b/results.json").write_text(json.dumps(results, indent=2) + "\n")
    ok_rows = [row for row in results if row.get("status") == "ok"]

    def series(key):
        return sorted(row[key] for row in ok_rows if row.get(key) is not None)

    def quantile(values, q):
        if not values:
            return None
        return round(values[min(len(values) - 1, int(len(values) * q))], 3)

    def summary(key):
        values = series(key)
        return {"p10": quantile(values, 0.10), "p50": quantile(values, 0.50),
                "p90": quantile(values, 0.90), "min": values[0] if values else None,
                "max": values[-1] if values else None}

    baselines = {
        "payload_beats_grep": sum(
            1 for row in ok_rows if row["payload_tokens"] < row["grep_tokens"]
        ),
        "payload_beats_map": sum(
            1 for row in ok_rows if row["payload_tokens"] < row["map_tokens"]
        ),
        "payload_beats_touched": sum(
            1 for row in ok_rows if row["payload_tokens"] < row["touched_tokens"]
        ),
        "grep_wins": sum(
            1 for row in ok_rows if row["grep_tokens"] < row["payload_tokens"]
        ),
        "map_wins": sum(
            1 for row in ok_rows if row["map_tokens"] < row["payload_tokens"]
        ),
    }
    stage = {"n": len(results), "ok": len(ok_rows), "co_change": summary("co_change_recall"),
             "test_recall": summary("test_recall"), "payload": summary("payload_tokens"),
             "touched": summary("touched_tokens"), "grep": summary("grep_tokens"),
             "map": summary("map_tokens"), "baselines": baselines,
             "failures": [row for row in results if row.get("status") != "ok"]}
    (ROOT / "benchmarks/stage_b/summary.json").write_text(json.dumps(stage, indent=2) + "\n")
    lines = ["# Stage B results", "", f"Commits: {len(results)}, usable: {len(ok_rows)}", "",
             "| Metric | p10 | p50 | p90 | min | max |", "|---|---:|---:|---:|---:|---:|"]
    for name in ("co_change", "test_recall", "payload", "touched", "grep", "map"):
        entry = stage[name]
        lines.append(f"| {name} | {entry['p10']} | {entry['p50']} | {entry['p90']} | "
                     f"{entry['min']} | {entry['max']} |")
    lines += ["", "Baselines: payload beats grep "
              f"{baselines['payload_beats_grep']}/{len(ok_rows)}, map "
              f"{baselines['payload_beats_map']}/{len(ok_rows)}, touched "
              f"{baselines['payload_beats_touched']}/{len(ok_rows)}; "
              f"grep wins {baselines['grep_wins']}, map wins {baselines['map_wins']}.", ""]
    lines += ["## Failures", ""] + [f"- {row['repo']} {row['sha'][:8]}: {row['status']}"
                                     for row in results if row.get("status") != "ok"]
    (ROOT / "benchmarks/stage_b/RESULTS.md").write_text("\n".join(lines) + "\n")
    ok = [row for row in results if row.get("status") == "ok"]
    def med(key):
        values = [row[key] for row in ok if row.get(key) is not None]
        return round(statistics.median(values), 3) if values else None
    print(json.dumps({
        "commits": len(results), "ok": len(ok),
        "median_co_change_recall": med("co_change_recall"),
        "median_test_recall": med("test_recall"),
        "median_payload_tokens": med("payload_tokens"),
        "median_touched_tokens": med("touched_tokens"),
        "median_grep_tokens": med("grep_tokens"),
        "median_map_tokens": med("map_tokens"),
        "failures": [r for r in results if r.get("status") != "ok"][:5],
    }, indent=2))


if __name__ == "__main__":
    main()
