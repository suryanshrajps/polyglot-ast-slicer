"""Incremental differential: 20 random add/delete/rename edits on a real repo.

After every edit the incrementally updated graph must equal a from-scratch rebuild.

    python verify/incremental_probe.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

EVAL_REPOS = Path(os.environ.get("POLYGLOT_AST_EVAL_REPOS", "/tmp/pas-eval-repos"))
REAL = {
    "python-requests": EVAL_REPOS / "requests",
    "ts-zod": EVAL_REPOS / "zod",
}
SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}


def snapshot(engine: GraphEngine) -> set[tuple[str, ...]]:
    symbols = set(engine._symbols)
    edges = {
        (
            source,
            target,
            str(data.get("relation")),
            str(data.get("line")),
            str(data.get("name")),
            str(data.get("receiver")),
        )
        for source, target, data in engine.graph.edges(data=True)
    }
    return {("symbol", item) for item in symbols} | {("edge", *item) for item in edges}


def sources(repo: Path) -> list[Path]:
    return sorted(
        path
        for path in repo.rglob("*")
        if path.is_file()
        and path.suffix in SUFFIXES
        and ".git" not in path.parts
        and "node_modules" not in path.parts
    )


def apply(repo: Path, rng: random.Random, index: int) -> str:
    files = sources(repo)
    if not files:
        return "noop"
    choice = rng.randrange(4)
    target = rng.choice(files)
    if choice == 0:  # append a function
        addition = (
            f"\ndef extra_{index}():\n    return {index}\n"
            if target.suffix == ".py"
            else f"\nexport function extra_{index}(): number {{ return {index}; }}\n"
        )
        target.write_text(
            target.read_text(errors="replace") + addition,
            encoding="utf-8",
        )
        return f"append:{target.relative_to(repo)}"
    if choice == 1 and len(files) > 3:  # delete
        target.unlink()
        return f"delete:{target.relative_to(repo)}"
    if choice == 2:  # rename
        renamed = target.with_name(f"renamed_{index}{target.suffix}")
        target.rename(renamed)
        return f"rename:{target.relative_to(repo)}->{renamed.relative_to(repo)}"
    new = repo / f"added_{index}{target.suffix}"
    body = (
        f"def added_{index}():\n    return {index}\n"
        if target.suffix == ".py"
        else f"export function added_{index}(): number {{ return {index}; }}\n"
    )
    new.write_text(body, encoding="utf-8")
    return f"add:{new.relative_to(repo)}"


def run(name: str, source: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ast-inc-") as directory:
        work = Path(directory) / "repo"
        shutil.copytree(source, work, ignore=shutil.ignore_patterns(".git", "node_modules"))
        engine = GraphEngine(work, cache_dir=Path(directory) / "cache")
        engine.index_repository(force_rebuild=True)
        rng = random.Random(42)
        operations: list[str] = []
        mismatches: list[dict[str, object]] = []
        for index in range(20):
            operations.append(apply(work, rng, index))
            engine.index_repository(force_rebuild=False)
            fresh = GraphEngine(work, cache_dir=Path(directory) / f"fresh-{index}")
            fresh.index_repository(force_rebuild=True)
            got, want = snapshot(engine), snapshot(fresh)
            fresh.close()
            if got != want:
                mismatches.append(
                    {
                        "edit": index,
                        "operation": operations[-1],
                        "missing": sorted(want - got)[:5],
                        "extra": sorted(got - want)[:5],
                    }
                )
        engine.close()
        return {"repo": name, "edits": 20, "mismatches": mismatches, "operations": operations}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.parse_args()
    rows = {name: run(name, repo) for name, repo in REAL.items()}
    payload = {"results": rows}
    out = ROOT / "verify" / "results" / "incremental.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
