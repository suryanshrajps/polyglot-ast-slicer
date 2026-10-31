"""Scores label CSVs against packet keys, with Wilson intervals and kappa."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

HARD_EXCLUDED = {"module", "import"}


def wilson(k: int, n: int) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    z = 1.959963984540054
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def load_labels(path: Path) -> dict[str, str]:
    return {
        row["edge_id"]: row["verdict"]
        for row in csv.DictReader(path.open(encoding="utf-8"))
        if row["verdict"]
    }


def stats(ids: list[str], labels: dict[str, str], key: dict) -> dict:
    correct = sum(1 for eid in ids if labels.get(eid) == "correct")
    incorrect = sum(1 for eid in ids if labels.get(eid) == "incorrect")
    unsure = sum(1 for eid in ids if labels.get(eid) == "unsure")
    total = len(ids)
    decided = correct + incorrect
    return {
        "n": total,
        "correct": correct,
        "incorrect": incorrect,
        "unsure": unsure,
        "precision_unsure_incorrect": correct / total if total else 0.0,
        "precision_ci95_inclusive": list(wilson(correct, total)),
        "precision_unsure_excluded": correct / decided if decided else 0.0,
        "precision_ci95_excluded": list(wilson(correct, decided)),
    }


def kappa(a: list[str], b: list[str]) -> float | None:
    if not a or len(a) != len(b):
        return None
    po = sum(1 for x, y in zip(a, b, strict=True) if x == y) / len(a)
    labels = sorted(set(a) | set(b))
    pe = sum((a.count(x) / len(a)) * (b.count(x) / len(b)) for x in labels)
    if pe >= 1.0:
        return None
    return (po - pe) / (1 - pe)


def score(labels: dict[str, str], key: dict, canary_key: dict | None = None) -> dict:
    by_kind: dict[str, list[str]] = {}
    for eid in labels:
        entry = key.get(eid)
        if entry is None:
            continue
        by_kind.setdefault(entry["kind"], []).append(eid)

    per_kind = {kind: stats(ids, labels, key) for kind, ids in sorted(by_kind.items())}
    hard_ids = [eid for eid, entry in key.items() if entry["kind"] not in HARD_EXCLUDED]
    hard_ids = [eid for eid in hard_ids if eid in labels]

    result: dict = {
        "labelled": len(labels),
        "per_kind": per_kind,
        "hard_edges": stats(hard_ids, labels, key),
    }
    if canary_key:
        seeded = [eid for eid, entry in canary_key.items() if entry.get("seeded")]
        real = [eid for eid, entry in canary_key.items() if not entry.get("seeded")]
        fake_accepts = sum(1 for eid in seeded if labels.get(eid) == "correct")
        false_rejects = sum(1 for eid in real if labels.get(eid) == "incorrect")
        result["canary"] = {
            "seeded": len(seeded),
            "false_accepts": fake_accepts,
            "false_accept_rate": fake_accepts / len(seeded) if seeded else 0.0,
            "false_accept_ci95": list(wilson(fake_accepts, len(seeded))),
            "real": len(real),
            "false_rejects": false_rejects,
            "false_reject_rate": false_rejects / len(real) if real else 0.0,
            "false_reject_ci95": list(wilson(false_rejects, len(real))),
        }
    return result


def compare(first: dict[str, str], second: dict[str, str]) -> dict:
    shared = sorted(set(first) & set(second))
    a = [first[eid] for eid in shared]
    b = [second[eid] for eid in shared]
    agreements = sum(1 for x, y in zip(a, b, strict=True) if x == y)
    value = kappa(a, b)
    return {
        "shared": len(shared),
        "agreements": agreements,
        "percent_agreement": agreements / len(shared) if shared else 0.0,
        "kappa": value,
        "kappa_status": "undefined (degenerate marginals)" if value is None else "ok",
    }


def fmt(value: float) -> str:
    return f"{value * 100:.1f}%"


def to_markdown(result: dict) -> str:
    lines = [
        "## Label scores",
        "",
        "| Group | n | Correct | Unsure | Precision (unsure=incorrect) | "
        "Precision (unsure excluded) |",
        "|---|---:|---:|---:|---|---|",
    ]
    groups = {"hard edges": result["hard_edges"], **result["per_kind"]}
    for name, entry in groups.items():
        low, high = entry["precision_ci95_inclusive"]
        low2, high2 = entry["precision_ci95_excluded"]
        lines.append(
            f"| `{name}` | {entry['n']} | {entry['correct']} | {entry['unsure']} | "
            f"{fmt(entry['precision_unsure_incorrect'])} (95% CI {fmt(low)}–{fmt(high)}) | "
            f"{fmt(entry['precision_unsure_excluded'])} "
            f"(95% CI {fmt(low2)}–{fmt(high2)}) |"
        )
    if "canary" in result:
        canary = result["canary"]
        lines += [
            "",
            f"Canary: {canary['false_accepts']}/{canary['seeded']} false accepts "
            f"({fmt(canary['false_accept_rate'])}), "
            f"{canary['false_rejects']}/{canary['real']} false rejects.",
        ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--labels2", type=Path)
    parser.add_argument("--packets", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--canary-key", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()

    key = json.loads(args.key.read_text())
    canary = json.loads(args.canary_key.read_text()) if args.canary_key else None
    labels = load_labels(args.labels)
    result = score(labels, key, canary)
    if args.labels2:
        result["agreement"] = compare(labels, load_labels(args.labels2))
    if args.out:
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(to_markdown(result), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
