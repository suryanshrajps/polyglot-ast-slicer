"""Slice size comparison: 30 random symbols per language.

For each symbol: default slice payload tokens (tiktoken on the exact payload JSON), the
entry symbol's whole file, and every unique file containing its callers/callees. These are
sizes only; no claim is made that a slice is sufficient.

    python benchmarks/size_compare.py --repos-dir /tmp/pas-eval-repos --json
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

REPOS = {
    "python": ("requests", "src"),
    "typescript": ("zod", "src"),
}
CALL_RELATIONS = {"calls", "constructs"}
SAMPLE_SIZE = 30


def quantiles(values: list[int]) -> dict[str, float]:
    ordered = sorted(values)

    def at(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
        return float(ordered[index])

    return {"median": float(statistics.median(ordered)), "p10": at(0.10), "p90": at(0.90)}


def measure(repo: Path, src: str, language: str) -> dict[str, object]:
    root = repo
    engine = GraphEngine(root, cache_dir=Path("/tmp/pas-size-cache") / language)
    engine.index_repository(force_rebuild=True)
    source_root = (root / src).resolve()
    candidates = [
        symbol
        for symbol in engine._symbols.values()
        if symbol.kind in ("function", "method")
        and symbol.qualified_name != "<module>"
        and symbol.file_path.startswith(str(source_root))
    ]
    candidates.sort(key=lambda symbol: symbol.symbol_id)
    rng = random.Random(42)
    chosen = rng.sample(candidates, min(SAMPLE_SIZE, len(candidates)))

    text_cache: dict[str, int] = {}

    def file_tokens(path: str) -> int:
        if path not in text_cache:
            text_cache[path] = engine.count_tokens(
                Path(path).read_text(encoding="utf-8", errors="replace")
            )
        return text_cache[path]

    rows = []
    for symbol in chosen:
        payload = engine.get_ast_call_slice(
            f"{engine._rel(symbol.file_path)}::{symbol.qualified_name}"
        )
        if "error" in payload:
            continue
        files = {symbol.file_path}
        for source, _, data in engine.graph.in_edges(symbol.symbol_id, data=True):
            if data.get("relation") in CALL_RELATIONS:
                files.add(engine._symbols[source].file_path)
        for _, target, data in engine.graph.out_edges(symbol.symbol_id, data=True):
            if data.get("relation") in CALL_RELATIONS:
                files.add(engine._symbols[target].file_path)
        rows.append(
            {
                "symbol": symbol.qualified_name,
                "file": engine._rel(symbol.file_path),
                "line": symbol.start_line,
                "payload_tokens": engine.count_tokens(json.dumps(payload)),
                "entry_file_tokens": file_tokens(symbol.file_path),
                "caller_callee_files_tokens": sum(file_tokens(path) for path in sorted(files)),
            }
        )
    engine.close()
    return {
        "repo": repo.name,
        "sha": subprocess_sha(repo),
        "symbols": len(rows),
        "payload_tokens": quantiles([row["payload_tokens"] for row in rows]),
        "entry_file_tokens": quantiles([row["entry_file_tokens"] for row in rows]),
        "caller_callee_files_tokens": quantiles(
            [row["caller_callee_files_tokens"] for row in rows]
        ),
        "rows": rows,
    }


def subprocess_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos-dir", type=Path, default=Path("/tmp/pas-eval-repos"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = {}
    for language, (name, src) in REPOS.items():
        repo = args.repos_dir / name
        if not repo.exists():
            raise SystemExit(f"missing repo: {repo}")
        results[language] = measure(repo, src, language)
    payload = {"seed": 42, "per_language": results}
    out = ROOT / "metrics" / "size_comparison.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
