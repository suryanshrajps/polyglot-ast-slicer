"""Builds blind labeling packets for a stratified edge sample.

Shows only the call site and the claimed target definition. Confidence, resolution path and
any Jedi verdict are stored separately in ``answers.json`` for scoring after labeling.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402


def edge_id(source: str, target: str) -> str:
    return hashlib.sha256(f"{source}->{target}".encode()).hexdigest()[:12]


def allocate(sizes: dict[tuple[str, str], int], total: int) -> dict[tuple[str, str], int]:
    keys = sorted(sizes)
    allocation = {key: 1 for key in keys if sizes[key] > 0}
    remaining = total - sum(allocation.values())
    while remaining > 0:
        candidates = [key for key in keys if allocation.get(key, 0) < sizes[key]]
        if not candidates:
            break
        candidates.sort(key=lambda key: (-(sizes[key] - allocation.get(key, 0)), key))
        allocation[candidates[0]] = allocation.get(candidates[0], 0) + 1
        remaining -= 1
    return allocation


def context(path: str, line: int, radius: int = 6) -> str:
    lines = Path(path).read_text(errors="replace").splitlines()
    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    return "\n".join(f"{number:>5} | {lines[number - 1]}" for number in range(start, end + 1))


def definition(symbol, limit: int = 25) -> str:
    lines = Path(symbol.file_path).read_text(errors="replace").splitlines()
    start = symbol.start_line
    end = min(symbol.end_line, start + limit - 1)
    return "\n".join(f"{number:>5} | {lines[number - 1]}" for number in range(start, end + 1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--name", required=True)
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("labels"))
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    engine = GraphEngine(root)
    engine.index_repository(force_rebuild=True)

    edges = [
        (u, v, data)
        for u, v, data in engine.graph.edges(data=True)
        if data.get("relation") in ("calls", "constructs")
    ]
    sizes: dict[tuple[str, str], int] = {}
    for _, _, data in edges:
        key = (str(data.get("confidence")), str(data.get("resolution", "?")).split(":")[0])
        sizes[key] = sizes.get(key, 0) + 1
    allocation = allocate(sizes, args.n)

    rng = random.Random(args.seed)
    by_stratum: dict[tuple[str, str], list[tuple[str, str, dict]]] = {}
    for edge in edges:
        key = (str(edge[2].get("confidence")), str(edge[2].get("resolution", "?")).split(":")[0])
        by_stratum.setdefault(key, []).append(edge)

    chosen = []
    for key in sorted(by_stratum):
        pool = sorted(by_stratum[key], key=lambda item: item[0] + item[1])
        rng.shuffle(pool)
        chosen.extend(pool[: allocation.get(key, 0)])

    packets = []
    answers = {}
    for source_id, target_id, data in chosen:
        source = engine._symbols[source_id]  # noqa: SLF001
        target = engine._symbols[target_id]  # noqa: SLF001
        eid = edge_id(source_id, target_id)
        site = next(
            (call for call in source.calls if call.name in (target.name, data.get("name"))),
            None,
        )
        line = site.line if site and site.line else source.start_line
        packets.append(
            {
                "edge_id": eid,
                "repo": args.name,
                "call_site": f"{Path(source.file_path).relative_to(root)}:{line}",
                "call_context": context(source.file_path, line),
                "target": (
                    f"{target.qualified_name} "
                    f"({Path(target.file_path).relative_to(root)}:"
                    f"{target.start_line}-{target.end_line})"
                ),
                "target_definition": definition(target),
            }
        )
        answers[eid] = {
            "source": source.qualified_name,
            "target": target.qualified_name,
            "confidence": data.get("confidence"),
            "resolution": data.get("resolution"),
            "relation": data.get("relation"),
        }

    out = args.out / args.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "blind_packets.json").write_text(json.dumps(packets, indent=2), encoding="utf-8")
    (out / "answers.json").write_text(json.dumps(answers, indent=2), encoding="utf-8")
    with (out / "labels.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["edge_id", "verdict", "justification"])
        for packet in packets:
            writer.writerow([packet["edge_id"], "", ""])
    print(f"{args.name}: wrote {len(packets)} packets to {out}")
    engine.close()


if __name__ == "__main__":
    main()
