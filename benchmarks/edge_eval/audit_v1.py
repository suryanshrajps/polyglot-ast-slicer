"""Audits labelled v1 edges for multi-call lines that could match the claimed target."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPOS = ("requests", "click")


def short_name(target: str) -> str:
    return target.split(" (")[0].rsplit(".", 1)[-1]


def main() -> None:
    report = {}
    for repo in REPOS:
        base = ROOT / "labels" / repo
        packets = {p["edge_id"]: p for p in json.loads((base / "blind_packets.json").read_text())}
        labelled = [
            row for row in csv.DictReader((base / "labels.csv").open()) if row["verdict"]
        ]
        ambiguous = []
        missing_name = 0
        for row in labelled:
            packet = packets[row["edge_id"]]
            file_part, line_part = packet["call_site"].rsplit(":", 1)
            line = int(line_part)
            repo_root = Path("/tmp") / repo
            lines = (repo_root / file_part).read_text(errors="replace").splitlines()
            text = lines[line - 1] if 0 < line <= len(lines) else ""
            name = short_name(packet["target"])
            matches = len(re.findall(rf"(?<![\w$]){re.escape(name)}\s*\(", text))
            if matches == 0:
                missing_name += 1
            elif matches > 1:
                ambiguous.append(
                    {
                        "edge_id": row["edge_id"],
                        "call_site": packet["call_site"],
                        "target": packet["target"],
                        "matches": matches,
                    }
                )
        report[repo] = {
            "labelled": len(labelled),
            "multi_call_lines": len(ambiguous),
            "missing_target_name": missing_name,
            "examples": ambiguous[:8],
        }
    (ROOT / "labels" / "v1_audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
