"""Re-scores click fault injection at test-function granularity (strips [param])."""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def norm(node: str) -> str:
    return re.sub(r"\[.*\]$", "", node)


def main() -> None:
    payload = json.loads((ROOT / "benchmarks/fault/results.json").read_text())
    summary, rows = payload["summary"], payload["rows"]
    total = summary["total_tests"]
    out = {"total_tests": total, "before": {}, "after": {}, "rows": []}
    before = json.loads((ROOT / "benchmarks/fault/analysis.json").read_text())
    for stratum in ("low", "medium", "high"):
        out["before"][stratum] = before[stratum]
    for row in rows:
        if row["import_time"] or not row["failing"]:
            continue
        failing = {norm(n) for n in row["failing_ids"]}
        impacted = {norm(n) for n in row["impacted_ids"]}
        matched = failing & impacted
        out["rows"].append({
            "stratum": row["stratum"], "function": row["function"],
            "recall": len(matched) / len(failing),
            "precision": len(matched) / len(impacted) if impacted else None,
            "selection": len(impacted) / total,
        })
    for stratum in ("low", "medium", "high"):
        part = [r for r in out["rows"] if r["stratum"] == stratum]
        out["after"][stratum] = {
            "n": len(part),
            "recall_p50": (
                round(statistics.median([r["recall"] for r in part]), 3) if part else None
            ),
            "precision_p50": (
                round(
                    statistics.median(
                        [r["precision"] for r in part if r["precision"] is not None]
                    ),
                    3,
                )
                if part else None
            ),
            "selection_p50": (
                round(statistics.median([r["selection"] for r in part]), 5)
                if part else None
            ),
        }
    classes = Counter()
    examples: dict[str, list[str]] = {}
    for row in rows:
        if row["import_time"] or not row["failing"]:
            continue
        failing = {norm(n) for n in row["failing_ids"]}
        impacted = {norm(n) for n in row["impacted_ids"]}
        for node in sorted(failing - impacted):
            label = "unresolved_call" if "::" in node else "other"
            classes[label] += 1
            examples.setdefault(label, [])
            if len(examples[label]) < 5 and node not in examples[label]:
                examples[label].append(node)
    out["classes"] = dict(classes)
    out["examples"] = examples
    (ROOT / "benchmarks/fault/rescore.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: out[k] for k in ("before", "after", "classes")}, indent=2))


if __name__ == "__main__":
    main()
