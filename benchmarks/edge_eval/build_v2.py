"""Builds v2 blind packets from tree-sitter call nodes, with valid canaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tree_sitter_language_pack import get_parser  # noqa: E402

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

SKIP_KINDS = {"module", "import"}


def calls_in(path: Path, language: str):
    source = path.read_bytes()
    tree = get_parser(language).parse(source)
    found = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in ("call", "call_expression", "new_expression"):
            callee = node.child_by_field_name("function") or node.child_by_field_name(
                "constructor"
            )
            name = receiver = None
            if callee is not None and callee.type == "identifier":
                name = source[callee.start_byte : callee.end_byte].decode(errors="replace")
            elif callee is not None and callee.type in ("attribute", "member_expression"):
                attr = callee.child_by_field_name("attribute") or callee.child_by_field_name(
                    "property"
                )
                obj = callee.child_by_field_name("object")
                if attr is not None:
                    name = source[attr.start_byte : attr.end_byte].decode(errors="replace")
                if obj is not None:
                    receiver = source[obj.start_byte : obj.end_byte].decode(errors="replace")
            if name:
                found.append((node, name, receiver, source))
        stack.extend(node.children)
    return found


def packet_for(repo: Path, engine, source_sym, target_sym, name, receiver, line):
    calls = calls_in(Path(source_sym.file_path), source_sym.language)
    matches = [
        item for item in calls
        if item[1] == name and item[0].start_point[0] + 1 == line
    ] or [item for item in calls if item[1] == name]
    if not matches:
        return None
    node, _, _, source = matches[0]
    start_line, start_col = node.start_point
    end_line, end_col = node.end_point
    text = source[node.start_byte : node.end_byte].decode(errors="replace")
    lines = source.decode(errors="replace").splitlines()
    context = []
    for number in range(max(1, start_line - 1), min(len(lines), end_line + 2) + 1):
        context.append(f"{number:>5} | {lines[number - 1]}")
        if number == start_line + 1:
            width = (
                end_col - start_col
                if end_line == start_line
                else len(lines[number - 1]) - start_col
            )
            context.append("      | " + " " * start_col + "^" * max(1, width))
    definition = []
    target_lines = Path(target_sym.file_path).read_text(errors="replace").splitlines()
    end = min(target_sym.end_line, target_sym.start_line + 9)
    for number in range(target_sym.start_line, end + 1):
        definition.append(f"{number:>5} | {target_lines[number - 1]}")
    return {
        "edge_id": hashlib.sha256(
            f"{source_sym.symbol_id}->{target_sym.symbol_id}:{start_line}:{start_col}".encode()
        ).hexdigest()[:12],
        "repo": repo.name,
        "file": str(Path(source_sym.file_path).relative_to(repo)),
        "call_text": text,
        "call_range": [start_line + 1, start_col, end_line + 1, end_col],
        "call_context": "\n".join(context),
        "callee": name,
        "receiver": receiver,
        "target": {
            "qualified_name": target_sym.qualified_name,
            "file": str(Path(target_sym.file_path).relative_to(repo)),
            "lines": f"{target_sym.start_line}-{target_sym.end_line}",
            "definition": "\n".join(definition),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--name", required=True)
    parser.add_argument("--mode", choices=("hard", "all"), default="hard")
    parser.add_argument("--per-kind", type=int, default=30)
    parser.add_argument("--wrong-ratio", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("labels"))
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    engine = GraphEngine(root)
    engine.index_repository(force_rebuild=True)
    rng = random.Random(args.seed)

    groups: dict[str, list] = {}
    for source_id, target_id, data in engine.graph.edges(data=True):
        if data.get("relation") not in ("calls", "constructs"):
            continue
        kind = str(data.get("resolution", "?")).split(":")[0]
        if args.mode == "hard" and kind in SKIP_KINDS:
            continue
        site = next(
            (
                call for call in engine._symbols[source_id].calls
                if call.name in (engine._symbols[target_id].name, data.get("name"))
            ),
            None,
        )
        if site is None or not site.line:
            continue
        groups.setdefault(kind, []).append((source_id, target_id, data, site))

    selected = []
    for kind in sorted(groups):
        pool = sorted(groups[kind], key=lambda item: (item[0], item[1]))
        rng.shuffle(pool)
        selected.extend((kind, *item) for item in pool[: args.per_kind])

    packets, key = [], {}
    for kind, source_id, target_id, _data, site in selected:
        source = engine._symbols[source_id]  # noqa: SLF001
        target = engine._symbols[target_id]  # noqa: SLF001
        packet = packet_for(root, engine, source, target, site.name, site.receiver, site.line)
        if packet is None:
            continue
        packets.append(packet)
        key[packet["edge_id"]] = {
            "seeded": False,
            "kind": kind,
            "source": source.qualified_name,
            "target": target.qualified_name,
        }

    by_name: dict[str, list] = {}
    for symbol in engine._symbols.values():  # noqa: SLF001
        by_name.setdefault(symbol.name, []).append(symbol)
    eligible = []
    for item in selected:
        kind, source_id, target_id, data, site = item
        target = engine._symbols[target_id]  # noqa: SLF001
        alternatives = [
            symbol for symbol in by_name.get(target.name, [])
            if symbol.symbol_id != target.symbol_id
        ]
        if alternatives:
            eligible.append((item, alternatives))
    n_wrong = min(max(1, round(len(packets) * args.wrong_ratio)), len(eligible))
    canaries = []
    for (kind, source_id, target_id, _data, site), alternatives in rng.sample(eligible, n_wrong):
        source = engine._symbols[source_id]  # noqa: SLF001
        target = engine._symbols[target_id]  # noqa: SLF001
        fake = sorted(alternatives, key=lambda symbol: symbol.symbol_id)[
            rng.randrange(len(alternatives))
        ]
        assert fake.name == site.name, f"canary name mismatch: {fake.name} != {site.name}"
        packet = packet_for(root, engine, source, fake, site.name, site.receiver, site.line)
        if packet is None:
            continue
        canaries.append(packet)
        key[packet["edge_id"]] = {
            "seeded": True,
            "kind": kind,
            "real_target": target.qualified_name,
            "fake_target": fake.qualified_name,
        }

    mixed = packets + canaries
    rng.shuffle(mixed)
    out = args.out / args.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "v2_packets.json").write_text(json.dumps(mixed, indent=2), encoding="utf-8")
    (out / "v2_answer_key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    print(f"{args.name}: {len(packets)} real + {len(canaries)} canaries = {len(mixed)}")


if __name__ == "__main__":
    main()
