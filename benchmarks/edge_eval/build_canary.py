"""Builds a blind canary set: real edges plus seeded wrong edges.

The key is written to a separate file and must not be opened until labeling finishes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--name", default="requests")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--wrong", type=int, default=20)
    parser.add_argument("--labels", type=Path, default=Path("labels"))
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    engine = GraphEngine(root)
    engine.index_repository(force_rebuild=True)
    base = args.labels / args.name
    packets = json.loads((base / "blind_packets.json").read_text(encoding="utf-8"))
    answers = json.loads((base / "answers.json").read_text(encoding="utf-8"))

    by_id = {packet["edge_id"]: packet for packet in packets}
    rng = random.Random(args.seed)

    # Deterministic candidate pool: same short name, different owner.
    by_name: dict[str, list] = {}
    for symbol in engine._symbols.values():  # noqa: SLF001
        by_name.setdefault(symbol.name, []).append(symbol)

    eligible = []
    for eid, answer in sorted(answers.items()):
        packet = by_id[eid]
        match = re.search(r"\(([^:]+):(\d+)-(\d+)\)", packet["target"])
        if match is None:
            continue
        relative, start_line = match.group(1), int(match.group(2))
        real = next(
            (
                symbol
                for symbol in engine._symbols.values()  # noqa: SLF001
                if symbol.qualified_name == answer["target"]
                and str(Path(symbol.file_path).relative_to(root)) == relative
                and symbol.start_line == start_line
            ),
            None,
        )
        if real is None:
            continue
        call_line = next(
            (line for line in packet["call_context"].splitlines()
             if line.strip().startswith(packet["call_site"].rsplit(":", 1)[1] + " |")),
            "",
        )
        if "." not in call_line:
            continue
        alternatives = [
            symbol for symbol in by_name.get(real.name, [])
            if symbol.symbol_id != real.symbol_id
            and symbol.qualified_name != real.qualified_name
            and symbol.kind in ("function", "method", "class")
        ]
        if not alternatives:
            continue
        eligible.append((eid, real, alternatives))

    rng.shuffle(eligible)
    chosen = eligible[: args.wrong]
    key = {}
    canaries = []
    for eid, real, alternatives in chosen:
        fake = sorted(alternatives, key=lambda symbol: symbol.symbol_id)[
            rng.randrange(len(alternatives))
        ]
        packet = dict(by_id[eid])
        digest = hashlib.sha256(
            f"canary:{eid}:{fake.symbol_id}:{args.seed}".encode()
        ).hexdigest()
        new_id = digest[:12]
        packet["edge_id"] = new_id
        packet["target"] = (
            f"{fake.qualified_name} "
            f"({Path(fake.file_path).relative_to(root)}:{fake.start_line}-{fake.end_line})"
        )
        definition_lines = Path(fake.file_path).read_text(errors="replace").splitlines()
        start = fake.start_line
        end = min(fake.end_line, start + 24)
        packet["target_definition"] = "\n".join(
            f"{number:>5} | {definition_lines[number - 1]}" for number in range(start, end + 1)
        )
        canaries.append(packet)
        key[new_id] = {
            "seeded": True,
            "real_edge_id": eid,
            "real_target": real.qualified_name,
            "fake_target": fake.qualified_name,
            "fake_file": str(Path(fake.file_path).relative_to(root)),
        }

    for packet in packets:
        key[packet["edge_id"]] = {"seeded": False, "real_edge_id": packet["edge_id"]}

    mixed = packets + canaries
    rng.shuffle(mixed)
    (base / "canary_packets.json").write_text(json.dumps(mixed, indent=2), encoding="utf-8")
    (base / "canary_answer_key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    with (base / "canary_labels.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["edge_id", "verdict", "justification"])
        for packet in mixed:
            writer.writerow([packet["edge_id"], "", ""])
    print(
        f"{args.name}: {len(mixed)} packets ({len(canaries)} seeded wrong), "
        "key at canary_answer_key.json"
    )


if __name__ == "__main__":
    main()
