"""Writes a labeler bundle (no key) and the v2 human gold set."""

from __future__ import annotations

import csv
import json
import random
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPOS = ("requests_hard", "click_hard")

INSTRUCTIONS = """# Blind labeling instructions

You are given `packets_v2.json`. Each packet shows one call expression and the target the
tool claims it invokes.

For every packet:
1. Read `call_text`, `call_range` and the marked `call_context`. The `^^^^` line marks the
   exact call node; do not guess from the whole line.
2. Decide whether the claimed `target` is the definition that this call actually invokes.
   Check owner/module, not just the short name. Watch for sibling classes, overrides,
   different methods of the same class, and same-named functions in other modules.
3. Write a verdict: `correct`, `incorrect` or `unsure`.
4. Write a one-line justification citing `file:line` for the call and the target.

Do not use the repository's tooling, this project's code, or any static analyzer. Do not
look for an answer key; none is included. Label independently.
"""


def main() -> None:
    for name in REPOS:
        base = ROOT / "labels" / name
        packets = json.loads((base / "v2_packets.json").read_text())
        bundle = base / "labeler_bundle"
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "LABELING.md").write_text(INSTRUCTIONS, encoding="utf-8")
        (bundle / "packets_v2.json").write_text(json.dumps(packets, indent=2), encoding="utf-8")
        repo = "requests" if name.startswith("requests") else "click"
        sha = subprocess.run(
            ["git", "-C", f"/tmp/{repo}", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        (bundle / "repo_sha.txt").write_text(f"{repo} {sha}\n", encoding="utf-8")
        print(f"{bundle}: {len(packets)} packets, sha {sha[:7]}")

    packets = []
    for name in REPOS:
        packets.extend(json.loads((ROOT / "labels" / name / "v2_packets.json").read_text()))
    rng = random.Random(2026)
    chosen = rng.sample(packets, 30)
    out = ROOT / "labels" / "gold_v2_for_human.csv"
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["edge_id", "repo", "file", "call_text", "call_range", "call_context",
             "callee", "receiver", "claimed_target", "target_definition", "verdict",
             "justification"]
        )
        for packet in chosen:
            writer.writerow(
                [
                    packet["edge_id"], packet["repo"], packet["file"], packet["call_text"],
                    packet["call_range"], packet["call_context"], packet["callee"],
                    packet["receiver"], packet["target"]["qualified_name"],
                    packet["target"]["definition"], "", "",
                ]
            )
    print(f"gold: {len(chosen)} blind edges at {out}")


if __name__ == "__main__":
    main()
