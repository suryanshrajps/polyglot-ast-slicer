"""Builds a hard-kind blind sample with seeded wrong edges.

Hard canaries replace the target with a plausible neighbour: another method of the same
class, the same method on a sibling class, or a same-named function in another module.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

SKIP_KINDS = {"module", "import"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--name", required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--per-kind", type=int, default=30)
    parser.add_argument("--wrong-ratio", type=float, default=0.15)
    parser.add_argument("--out", type=Path, default=Path("labels"))
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    engine = GraphEngine(root)
    engine.index_repository(force_rebuild=True)
    rng = random.Random(args.seed)

    groups: dict[str, list[tuple[str, str, dict]]] = {}
    for source, target, data in engine.graph.edges(data=True):
        if data.get("relation") not in ("calls", "constructs"):
            continue
        kind = str(data.get("resolution", "?")).split(":")[0]
        if kind in SKIP_KINDS:
            continue
        groups.setdefault(kind, []).append((source, target, data))

    selected = []
    for kind in sorted(groups):
        pool = sorted(groups[kind], key=lambda item: item[0] + item[1])
        rng.shuffle(pool)
        selected.extend((kind, *edge) for edge in pool[: args.per_kind])
    total = len(selected)
    n_wrong = max(1, math.ceil(total * args.wrong_ratio / (1 - args.wrong_ratio)))
    n_wrong = min(n_wrong, total)

    packets, key = [], {}
    for kind, source_id, target_id, data in selected:
        source = engine._symbols[source_id]  # noqa: SLF001
        target = engine._symbols[target_id]  # noqa: SLF001
        eid = hashlib.sha256(f"{source_id}->{target_id}".encode()).hexdigest()[:12]
        site = next(
            (call for call in source.calls if call.name in (target.name, data.get("name"))),
            None,
        )
        line = site.line if site and site.line else source.start_line
        lines = Path(source.file_path).read_text(errors="replace").splitlines()
        start, end = max(1, line - 6), min(len(lines), line + 6)
        packets.append(
            {
                "edge_id": eid,
                "kind": kind,
                "call_site": f"{Path(source.file_path).relative_to(root)}:{line}",
                "call_context": "\n".join(
                    f"{number:>5} | {lines[number - 1]}" for number in range(start, end + 1)
                ),
                "target": (
                    f"{target.qualified_name} "
                    f"({Path(target.file_path).relative_to(root)}:"
                    f"{target.start_line}-{target.end_line})"
                ),
                "target_definition": _definition(target),
            }
        )
        key[eid] = {
            "seeded": False,
            "kind": kind,
            "source": source.qualified_name,
            "target": target.qualified_name,
        }

    # Hard canaries: pick from selected edges, replace with a plausible neighbour.
    canaries = []
    for kind, source_id, target_id, data in rng.sample(selected, n_wrong):
        source = engine._symbols[source_id]  # noqa: SLF001
        target = engine._symbols[target_id]  # noqa: SLF001
        fake = _neighbour(engine, source, target)
        if fake is None:
            continue
        digest = hashlib.sha256(
            f"hard:{source_id}:{target_id}:{fake.symbol_id}".encode()
        ).hexdigest()
        eid = digest[:12]
        lines = Path(source.file_path).read_text(errors="replace").splitlines()
        site = next(
            (call for call in source.calls if call.name in (target.name, data.get("name"))),
            None,
        )
        line = site.line if site and site.line else source.start_line
        start, end = max(1, line - 6), min(len(lines), line + 6)
        canaries.append(
            {
                "edge_id": eid,
                "kind": kind,
                "call_site": f"{Path(source.file_path).relative_to(root)}:{line}",
                "call_context": "\n".join(
                    f"{number:>5} | {lines[number - 1]}" for number in range(start, end + 1)
                ),
                "target": (
                    f"{fake.qualified_name} "
                    f"({Path(fake.file_path).relative_to(root)}:"
                    f"{fake.start_line}-{fake.end_line})"
                ),
                "target_definition": _definition(fake),
            }
        )
        key[eid] = {
            "seeded": True,
            "kind": kind,
            "real_target": target.qualified_name,
            "fake_target": fake.qualified_name,
        }

    mixed = packets + canaries
    rng.shuffle(mixed)
    out = args.out / args.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "hard_packets.json").write_text(json.dumps(mixed, indent=2), encoding="utf-8")
    (out / "hard_answer_key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    with (out / "hard_labels.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["edge_id", "verdict", "justification"])
        for packet in mixed:
            writer.writerow([packet["edge_id"], "", ""])
    print(f"{args.name}: {total} real + {len(canaries)} seeded = {len(mixed)} packets")


def _definition(symbol, limit: int = 12) -> str:
    lines = Path(symbol.file_path).read_text(errors="replace").splitlines()
    end = min(symbol.end_line, symbol.start_line + limit - 1)
    return "\n".join(
        f"{number:>5} | {lines[number - 1]}"
        for number in range(symbol.start_line, end + 1)
    )


def _neighbour(engine, source, target):
    """Wrong method in the right class, sibling-class method, or other same-named symbol."""
    owner = target.qualified_name.rsplit(".", 1)[0] if "." in target.qualified_name else ""
    candidates = []
    for symbol in engine._symbols.values():  # noqa: SLF001
        if symbol.symbol_id == target.symbol_id or symbol.kind not in ("function", "method"):
            continue
        same_class = owner and symbol.qualified_name.rsplit(".", 1)[0] == owner
        same_name = symbol.name == target.name
        if same_class or same_name:
            candidates.append(symbol)
    if not candidates:
        return None
    return sorted(candidates, key=lambda symbol: symbol.symbol_id)[0]


if __name__ == "__main__":
    main()
