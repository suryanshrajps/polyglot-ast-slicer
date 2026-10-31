"""Exports 30 blind edges for human labeling; no model verdicts are included."""

from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, default=Path("labels"))
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()

    packets = []
    for path in sorted(args.labels.glob("*/blind_packets.json")):
        packets.extend(json.loads(path.read_text(encoding="utf-8")))
    if not packets:
        raise SystemExit("no blind packets found; run sample_edges.py first")

    rng = random.Random(args.seed)
    chosen = rng.sample(packets, min(args.n, len(packets)))
    out = args.labels / "gold_for_human.csv"
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["edge_id", "repo", "call_site", "call_context", "target", "target_definition",
             "verdict", "justification"]
        )
        for packet in chosen:
            writer.writerow(
                [
                    packet["edge_id"],
                    packet["repo"],
                    packet["call_site"],
                    packet["call_context"],
                    packet["target"],
                    packet["target_definition"],
                    "",
                    "",
                ]
            )
    print(f"wrote {len(chosen)} blind edges to {out}")


if __name__ == "__main__":
    main()
