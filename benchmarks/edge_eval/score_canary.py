"""Scores the blind canary pass against the seeded key and the original labels."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABELS = ROOT / "labels" / "requests"


def wilson(k: int, n: int) -> tuple[float, float]:
    if not n:
        return (0.0, 0.0)
    z = 1.959963984540054
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - m), min(1.0, c + m))


def kappa(a: list[str], b: list[str]) -> float | None:
    if len(a) != len(b) or not a:
        return None
    po = sum(1 for x, y in zip(a, b, strict=True) if x == y) / len(a)
    labels = sorted(set(a) | set(b))
    pe = sum((a.count(label) / len(a)) * (b.count(label) / len(b)) for label in labels)
    if pe >= 1.0:
        return None
    return (po - pe) / (1 - pe)


def main() -> None:
    key = json.loads((LABELS / "canary_answer_key.json").read_text())
    verdicts = {
        row["edge_id"]: row["verdict"]
        for row in csv.DictReader((LABELS / "canary_labels.csv").open())
    }
    original = {
        row["edge_id"]: row["verdict"]
        for row in csv.DictReader((LABELS / "labels.csv").open())
    }
    seeded = [eid for eid, entry in key.items() if entry["seeded"]]
    real = [eid for eid, entry in key.items() if not entry["seeded"]]
    false_accepts = [eid for eid in seeded if verdicts.get(eid) == "correct"]
    false_rejects = [eid for eid in real if verdicts.get(eid) == "incorrect"]
    unsure = [eid for eid, verdict in verdicts.items() if verdict == "unsure"]

    shared = sorted(set(real) & set(original))
    a = [original[eid] for eid in shared]
    b = [verdicts[eid] for eid in shared]
    agreements = sum(1 for x, y in zip(a, b, strict=True) if x == y)
    result = {
        "seeded": len(seeded),
        "real": len(real),
        "seeded_labeled_correct": len(false_accepts),
        "false_accept_rate": len(false_accepts) / len(seeded) if seeded else 0.0,
        "false_accept_ci95": list(wilson(len(false_accepts), len(seeded))),
        "real_labeled_incorrect": len(false_rejects),
        "false_reject_rate": len(false_rejects) / len(real) if real else 0.0,
        "unsure": len(unsure),
        "unsure_rate": len(unsure) / len(verdicts) if verdicts else 0.0,
        "agreement_with_original": agreements,
        "agreement_rate": agreements / len(shared) if shared else 0.0,
        "kappa": kappa(a, b),
        "false_accept_ids": false_accepts,
        "false_reject_ids": false_rejects,
    }
    (LABELS / "canary_result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
