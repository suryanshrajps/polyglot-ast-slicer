"""Entry-file and grep baselines for the Stage B v2 entries."""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_v2 import CACHE, PATHS, WORK, changed_symbols, git, parse_old  # noqa: E402

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ENC = tiktoken.get_encoding("cl100k_base")


def tokens(text: str) -> int:
    return len(ENC.encode(text, disallowed_special=()))


def main() -> None:
    rows = json.loads((ROOT / "benchmarks/stage_b/results_v2.json").read_text())
    results, examples = [], []
    for row in rows:
        repo = PATHS[row["repo"].removeprefix("sb_")]
        sha = row["sha"]
        parent = git(repo, "rev-parse", f"{sha}^").strip()
        tree = (WORK / f"base-{repo.name}-{sha[:8]}").resolve()
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
            entry_recall = (
                sum(1 for s in others if s.file_path == target.file_path) / len(others)
                if others else None
            )
            entry_tokens = tokens(Path(target.file_path).read_text(errors="replace"))
            grep = subprocess.run(
                ["grep", "-rn", "-C", "20", target.name, str(tree)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            ).stdout
            results.append({
                "repo": repo.name, "sha": sha, "target": target.qualified_name,
                "entry_file_recall": entry_recall, "entry_file_tokens": entry_tokens,
                "grep_recall": row["grep_recall"], "grep_tokens": row["grep_tokens"],
                "payload_tokens": row["payload_tokens"],
            })
            if len(examples) < 3:
                for symbol in others:
                    if symbol.name in grep:
                        hit = next(
                            (line for line in grep.splitlines() if symbol.name in line), ""
                        )
                        examples.append({"repo": repo.name, "co_changed": symbol.name,
                                         "grep_line": hit[:160]})
                        break
        finally:
            engine.close()
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
                           capture_output=True)
    summary = {
        "n": len(results),
        "entry_file_recall_p50": round(statistics.median(
            [r["entry_file_recall"] for r in results if r["entry_file_recall"] is not None]
        ), 3),
        "entry_file_tokens_p50": round(statistics.median(
            [r["entry_file_tokens"] for r in results]
        ), 1),
        "grep_recall_p50": round(statistics.median([r["grep_recall"] for r in results]), 3),
        "grep_tokens_p50": round(statistics.median([r["grep_tokens"] for r in results]), 1),
        "payload_tokens_p50": round(statistics.median(
            [r["payload_tokens"] for r in results]
        ), 1),
        "grep_examples": examples,
    }
    (ROOT / "benchmarks/stage_b/baselines.json").write_text(json.dumps(
        {"summary": summary, "rows": results}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
