"""Baselines, 90% selection and miss classes for the click fault-injection run."""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from pathlib import Path

BASE = Path("/tmp/sb_click")
ROOT = Path(__file__).resolve().parents[2]


def test_ids() -> dict[str, str]:
    mapping = {}
    for path in (BASE / "tests").rglob("test_*.py"):
        source = path.read_text(errors="replace")
        tree = ast.parse(source)
        relative = str(path.relative_to(BASE))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and child.name.startswith("test"):
                        mapping[f"{relative}::{node.name}::{child.name}"] = source
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name.startswith("test"):
                mapping.setdefault(f"{relative}::{node.name}", source)
    return mapping


def suffix(node: str) -> str:
    parts = node.split("::")
    return "::".join(parts[-2:]) if len(parts) >= 2 else node


def hit(node: str, selected: str) -> bool:
    return node == selected or node.endswith("::" + suffix(selected))


def covered(failing: list[str], selected: list[str]) -> int:
    return sum(1 for node in failing if any(hit(node, s) for s in selected))


def classify(node: str, tests: dict[str, str], name: str) -> str:
    base = re.sub(r"\[.*\]$", "", node)
    source = tests.get(base, tests.get(node, ""))
    if "[" in node:
        return "parametrization"
    if "getattr(" in source or "partial(" in source:
        return "dynamic_dispatch"
    if "conftest" in source or "getfixturevalue" in source:
        return "fixture_or_conftest"
    if re.search(rf"\b{re.escape(name)}\b", source):
        return "unresolved_call"
    return "other"


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(len(values) * q))], 3)


def main() -> None:
    payload = json.loads((ROOT / "benchmarks/fault/results.json").read_text())
    summary, rows = payload["summary"], payload["rows"]
    total = summary["total_tests"]
    tests = test_ids()

    # Source file per function, from the pristine graph.
    import sys
    sys.path.insert(0, str(ROOT))
    from polyglot_ast_slicer.graph_engine import GraphEngine

    engine = GraphEngine(str(BASE), cache_dir="/tmp/fault-click-cache")
    engine.index_repository(force_rebuild=False)
    file_by_name = {symbol.qualified_name: str(Path(symbol.file_path).relative_to(BASE.resolve()))
                    for symbol in engine._symbols.values()}
    engine.close()

    results = []
    for row in rows:
        if row["import_time"] or not row["failing"]:
            continue
        failing = row["failing_ids"]
        name = row["function"].rsplit(".", 1)[-1]
        source_file = file_by_name.get(row["function"], "")
        module = Path(source_file).stem
        mention = [node for node, source in tests.items()
                   if re.search(rf"\b{re.escape(name)}\b", source)]
        same_file = [node for node in tests if node.startswith(f"tests/test_{module}.py::")]
        impacted = row["impacted_ids"]
        covered_so_far, prefix = 0, None
        for index, item in enumerate(impacted, start=1):
            if any(hit(f, item) for f in failing):
                covered_so_far += 1
            if covered_so_far / len(failing) >= 0.9:
                prefix = index
                break
        missed = [f for f in failing if not any(hit(f, i) for i in impacted)]
        classes = Counter(classify(node, tests, name) for node in missed)
        results.append({
            "stratum": row["stratum"], "function": row["function"],
            "recall": row["recall"], "precision": row["precision"],
            "selection": row["selection_fraction"],
            "selection_90": (prefix / total) if prefix else None,
            "all_tests": (1.0, 1.0),
            "mention": (covered(failing, mention) / len(failing), len(mention) / total),
            "same_file": (covered(failing, same_file) / len(failing),
                          len(same_file) / total),
            "classes": dict(classes),
            "examples": {k: [n for n in missed if classify(n, tests, name) == k][:5]
                         for k in ("unresolved_call", "dynamic_dispatch",
                                   "fixture_or_conftest", "parametrization", "other")},
        })

    output = {"total_tests": total, "rows": results,
              "classes": dict(sum((Counter(r["classes"]) for r in results), Counter()))}
    for stratum in ("low", "medium", "high"):
        part = [r for r in results if r["stratum"] == stratum]
        output[stratum] = {
            "n": len(part),
            "recall": [quantile([r["recall"] for r in part], q) for q in (0.1, 0.5, 0.9)],
            "precision": [quantile([r["precision"] for r in part if r["precision"] is not None],
                                   q) for q in (0.1, 0.5, 0.9)],
            "selection": [quantile([r["selection"] for r in part], q) for q in (0.1, 0.5, 0.9)],
            "selection_90": [quantile([r["selection_90"] for r in part if r["selection_90"]],
                                      q) for q in (0.1, 0.5, 0.9)],
            "mention_recall_p50": quantile([r["mention"][0] for r in part], 0.5),
            "mention_selection_p50": quantile([r["mention"][1] for r in part], 0.5),
            "same_file_recall_p50": quantile([r["same_file"][0] for r in part], 0.5),
            "same_file_selection_p50": quantile([r["same_file"][1] for r in part], 0.5),
        }
    (ROOT / "benchmarks/fault/analysis.json").write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({k: output[k] for k in ("total_tests", "classes", "low", "medium", "high")},
                     indent=2))


if __name__ == "__main__":
    main()
