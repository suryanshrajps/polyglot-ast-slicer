"""Fault injection on click: 30 functions, stratified by graph in-degree."""

from __future__ import annotations

import ast
import json
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

BASE = Path("/tmp/sb_click")
SHA = "2247b35ea1c47c727d7a06e51fa280e12a863ff6"
ROOT = Path(__file__).resolve().parents[2]
ENC = tiktoken.get_encoding("cl100k_base")
FAILED_RE = re.compile(r"^FAILED (\S+?)(?:\s|$)")


def tokens(text: str) -> int:
    return len(ENC.encode(text, disallowed_special=()))


def clone() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="fault-click-"))
    subprocess.run(["git", "clone", "-q", str(BASE), str(tmp)], check=True)
    subprocess.run(["git", "-C", str(tmp), "checkout", "-q", SHA], check=True)
    return tmp


def run_suite(repo: Path) -> dict:
    env = {"PYTHONPATH": str(repo / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=180,
    )
    failed = []
    for line in completed.stdout.splitlines():
        match = FAILED_RE.match(line.strip())
        if match:
            failed.append(match.group(1))
    summary = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
    return {"failed": sorted(set(failed)), "summary": summary, "returncode": completed.returncode}


def inject(repo: Path, symbol) -> tuple[str, Path]:
    path = repo / Path(symbol.file_path).relative_to(BASE.resolve())
    source = path.read_text()
    tree = ast.parse(source)
    candidates = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == symbol.name
        and symbol.start_line <= node.lineno <= symbol.end_line
    ]
    node = min(candidates, key=lambda n: abs(n.lineno - symbol.start_line))
    body = node.body
    insert_at = body[0].lineno
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        insert_at = body[0].end_lineno + 1
    indent = " " * body[0].col_offset
    lines = source.splitlines()
    lines.insert(insert_at - 1, f"{indent}raise RuntimeError('FAULT')")
    path.write_text("\n".join(lines) + "\n")
    relative = path.relative_to(repo)
    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", "--", str(relative)],
        capture_output=True, text=True, check=True,
    ).stdout
    return diff, path


def impacted_ids(impact: dict) -> list[str]:
    ids = []
    for item in impact.get("impacted_tests", []):
        qname = item["qualified_name"].replace(".", "::")
        ids.append(f"{item['file_path']}::{qname}")
    return sorted(set(ids))


def matches(failing: str, impacted: str) -> bool:
    return failing == impacted or failing.endswith("::" + impacted.split("::", 1)[-1])


def main() -> None:
    shutil.rmtree("/tmp/fault-click-cache", ignore_errors=True)
    engine = GraphEngine(str(BASE), cache_dir="/tmp/fault-click-cache")
    engine.index_repository(force_rebuild=True)

    functions = [
        symbol for symbol in engine._symbols.values()
        if symbol.kind in ("function", "method") and "test" not in symbol.file_path.lower()
        and symbol.language == "python"
    ]
    scored = []
    for symbol in functions:
        degree = engine.graph.in_degree(symbol.symbol_id)
        stratum = "low" if degree == 0 else "medium" if degree <= 5 else "high"
        scored.append((stratum, degree, symbol))
    rng = random.Random(42)
    chosen = []
    for stratum in ("low", "medium", "high"):
        pool = sorted((item for item in scored if item[0] == stratum),
                      key=lambda item: item[2].symbol_id)
        rng.shuffle(pool)
        chosen.extend(pool[:10])

    baseline = clone()
    first = run_suite(baseline)
    second = run_suite(baseline)
    preexisting = sorted(set(first["failed"]) | set(second["failed"]))
    flaky = sorted(set(first["failed"]) ^ set(second["failed"]))
    total_tests = sum(
        int(value) for value in re.findall(r"(\d+) (?:passed|failed|xfailed)", first["summary"])
    )
    shutil.rmtree(baseline, ignore_errors=True)
    engine.close()

    results = []
    for stratum, degree, symbol in chosen:
        repo = clone()
        try:
            diff, _ = inject(repo, symbol)
            run = run_suite(repo)
            failing = [node for node in run["failed"] if node not in preexisting]
            import_time = total_tests > 0 and len(failing) / total_tests > 0.5
            engine_copy = GraphEngine(str(repo), cache_dir="/tmp/fault-click-cache")
            engine_copy.index_repository(force_rebuild=True)
            impact = engine_copy.impact_of_diff(diff=diff, max_depth=3)
            impacted = impacted_ids(impact)
            matched = [node for node in failing if any(matches(node, i) for i in impacted)]
            results.append({
                "function": symbol.qualified_name, "stratum": stratum, "degree": degree,
                "failing": len(failing), "impacted": len(impacted), "matched": len(matched),
                "recall": len(matched) / len(failing) if failing else None,
                "precision": len(matched) / len(impacted) if impacted else None,
                "selection_fraction": len(impacted) / total_tests if total_tests else None,
                "payload_tokens": tokens(json.dumps(impact)),
                "import_time": import_time,
                "failing_ids": failing[:50], "impacted_ids": impacted[:100],
                "missed_ids": [node for node in failing if node not in matched][:50],
            })
            engine_copy.close()
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    usable = [row for row in results if not row["import_time"] and row["failing"] > 0]
    summary = {"total_tests": total_tests, "preexisting": preexisting, "flaky": flaky,
               "runs": len(results), "usable": len(usable),
               "uncovered": sum(1 for row in results if row["failing"] == 0),
               "import_time": sum(1 for row in results if row["import_time"])}
    for stratum in ("low", "medium", "high"):
        rows = [row for row in usable if row["stratum"] == stratum]
        summary[stratum] = {
            "n": len(rows),
            "recall_p50": (
                round(statistics.median([r["recall"] for r in rows]), 3) if rows else None
            ),
            "precision_p50": (
                round(
                    statistics.median(
                        [r["precision"] for r in rows if r["precision"] is not None]
                    ),
                    3,
                )
                if rows else None
            ),
            "selection_p50": (
                round(statistics.median([r["selection_fraction"] for r in rows]), 5)
                if rows else None
            ),
        }
    (ROOT / "benchmarks/fault/results.json").write_text(
        json.dumps({"summary": summary, "rows": results}, indent=2) + "\n"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
