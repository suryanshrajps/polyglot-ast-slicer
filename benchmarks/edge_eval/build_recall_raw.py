"""Recall packets sampled from raw tree-sitter nodes, independent of CallSite."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tree_sitter_language_pack import get_parser  # noqa: E402

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

INSTRUCTIONS = """# Blind recall labeling

`packets.json` lists call sites and callback references with the exact expression marked by
`^^^^`. No tool output is included.

For each site, read the repository and write the definition it refers to as `<path>:<line>`
in `true_target`. If it cannot be determined, leave it blank and explain in `notes`.

Do not use this project's tooling or any static analyzer.
"""


def nodes(path: Path, language: str):
    source = path.read_bytes()
    tree = get_parser(language).parse(source)
    return source, tree.root_node


def callee_info(node, source):
    callee = node.child_by_field_name("function") or node.child_by_field_name("constructor")
    if callee is None:
        return None, None
    if callee.type == "identifier":
        return source[callee.start_byte : callee.end_byte].decode(errors="replace"), None
    if callee.type in ("attribute", "member_expression"):
        attr = callee.child_by_field_name("attribute") or callee.child_by_field_name("property")
        obj = callee.child_by_field_name("object")
        name = source[attr.start_byte : attr.end_byte].decode(errors="replace") if attr else None
        receiver = source[obj.start_byte : obj.end_byte].decode(errors="replace") if obj else None
        return name, receiver
    return None, None


def candidates(engine, path: Path, language: str):
    source, root = nodes(path, language)
    found = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in ("call", "call_expression", "new_expression"):
            name, receiver = callee_info(node, source)
            if name and name in engine._by_name:  # noqa: SLF001
                found.append(
                    (
                        node,
                        name,
                        receiver,
                        "constructor" if node.type == "new_expression" else None,
                    )
                )
        if node.type in ("argument_list", "arguments"):
            for child in node.named_children:
                if child.type in ("identifier", "attribute", "member_expression"):
                    text = source[child.start_byte : child.end_byte].decode(errors="replace")
                    short = text.rsplit(".", 1)[-1]
                    if short in engine._by_name:  # noqa: SLF001
                        found.append(
                            (
                                child,
                                short,
                                text.rsplit(".", 1)[0] if "." in text else None,
                                "callback",
                            )
                        )
        stack.extend(node.children)
    return source, found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--name", required=True)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("labels"))
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    engine = GraphEngine(root)
    engine.index_repository(force_rebuild=True)
    rng = random.Random(args.seed)

    groups: dict[str, list] = {}
    for symbol in engine._symbols.values():  # noqa: SLF001
        if symbol.language not in ("python",):
            continue
        source, items = candidates(engine, Path(symbol.file_path), symbol.language)
        for node, name, receiver, forced in items:
            line = node.start_point[0] + 1
            owner = next(
                (
                    candidate for candidate in engine._by_file.get(symbol.file_path, set())
                    if (
                        engine._symbols[candidate].start_line <= line  # noqa: SLF001
                        <= engine._symbols[candidate].end_line  # noqa: SLF001
                    )
                ),
                None,
            )
            if owner is None:
                continue
            owner_symbol = engine._symbols[owner]  # noqa: SLF001
            is_constructor = forced == "constructor" or any(
                engine._symbols[target].kind == "class"  # noqa: SLF001
                for target in engine._by_name.get(name, set())  # noqa: SLF001
            )
            if is_constructor:
                form = "constructor"
            elif forced == "callback":
                form = "callback"
            elif receiver is None:
                form = "bare"
            elif receiver.startswith("super"):
                form = "super"
            elif receiver in ("self", "cls", "this"):
                form = "self_method"
            elif "." in receiver:
                form = "chained_attribute"
            else:
                binding = engine._bindings.get(owner_symbol.file_path, {}).get(receiver)
                form = "aliased_import" if binding and binding[0] else "module_attr"
            groups.setdefault(form, []).append(
                {"source": owner_symbol, "node": node, "name": name, "receiver": receiver,
                 "form": form, "source_bytes": source}
            )

    forms = sorted(groups)
    quota = args.n // max(1, len(forms))
    allocation = {form: min(quota, len(groups[form])) for form in forms}
    remainder = args.n - sum(allocation.values())
    while remainder > 0:
        for form in forms:
            if remainder <= 0:
                break
            if allocation[form] < len(groups[form]):
                allocation[form] += 1
                remainder -= 1

    packets, key = [], {}
    for form in sorted(groups):
        pool = groups[form]
        rng.shuffle(pool)
        for item in pool[: allocation[form]]:
            node = item["node"]
            source = item["source_bytes"]
            start_line, start_col = node.start_point
            end_line, end_col = node.end_point
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
            site_id = hashlib.sha256(
                f"{item['source'].symbol_id}:{start_line}:{start_col}".encode()
            ).hexdigest()[:12]
            packets.append(
                {
                    "site_id": site_id,
                    "repo": root.name,
                    "file": str(Path(item["source"].file_path).relative_to(root)),
                    "call_text": source[node.start_byte : node.end_byte].decode(errors="replace"),
                    "call_range": [start_line + 1, start_col, end_line + 1, end_col],
                    "call_context": "\n".join(context),
                    "callee": item["name"],
                    "receiver": item["receiver"],
                }
            )
            sites = [
                site for site in item["source"].calls
                if site.name == item["name"] and site.line == start_line + 1
            ]
            targets = [
                target for target in engine._by_name.get(item["name"], set())  # noqa: SLF001
                if engine.graph.has_edge(item["source"].symbol_id, target)
            ]
            if not sites:
                outcome = "no_call_site_extracted"
            elif targets:
                outcome = "found"
            else:
                outcome = "no_edge"
            key[site_id] = {
                "form": form,
                "source": item["source"].qualified_name,
                "call_site": f"{packets[-1]['file']}:{start_line + 1}",
                "tool_outcome": outcome,
                "tool_edges": sorted(engine._symbols[t].qualified_name for t in targets)[:3],  # noqa: SLF001
            }

    out = args.out / args.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "recall_packets.json").write_text(json.dumps(packets, indent=2), encoding="utf-8")
    (out / "recall_answer_key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    with (out / "recall_labels.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["site_id", "true_target", "notes"])
        for packet in packets:
            writer.writerow([packet["site_id"], "", ""])
    bundle = out / "labeler_bundle"
    bundle.mkdir(exist_ok=True)
    (bundle / "LABELING.md").write_text(INSTRUCTIONS, encoding="utf-8")
    (bundle / "packets.json").write_text(json.dumps(packets, indent=2), encoding="utf-8")
    sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    (bundle / "repo_sha.txt").write_text(f"{root.name} {sha}\n", encoding="utf-8")
    unextracted = sum(
        1 for entry in key.values() if entry["tool_outcome"] == "no_call_site_extracted"
    )
    print(json.dumps({"name": args.name, "packets": len(packets),
                      "forms": {f: allocation[f] for f in sorted(allocation)},
                      "unextracted": unextracted}))


if __name__ == "__main__":
    main()
