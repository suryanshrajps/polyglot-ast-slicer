"""Builds blind recall packets (call sites only) and a separate tool-outcome key."""

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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_v2 import calls_in  # noqa: E402

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

FORMS = (
    "bare", "module_attr", "self_method", "super", "constructor",
    "aliased_import", "chained_attribute", "callback",
)

INSTRUCTIONS = """# Blind recall labeling

`packets.json` lists call sites with the exact call expression marked by `^^^^`.
No tool output is included.

For each site:
1. Read the call expression and surrounding code.
2. Find the definition this call actually refers to by reading the repository
   (imports, class hierarchy, aliases). Write `<path>:<line>` in `true_target`.
3. If you cannot determine it, leave `true_target` empty and say why in `notes`.

Do not use this project's tooling or any static analyzer; label by reading.
"""


def form_of(engine, source, site) -> str:
    receiver = site.receiver
    if site.kind == "new":
        return "constructor"
    if receiver is None:
        return "bare"
    if receiver.startswith("super"):
        return "super"
    if receiver in ("self", "cls", "this"):
        return "self_method"
    if "." in receiver:
        return "chained_attribute"
    binding = engine._bindings.get(source.file_path, {}).get(receiver)
    if binding and binding[0]:
        module = engine._module_name_of(binding[0])
        if module and module.split(".")[-1] != receiver:
            return "aliased_import"
        return "module_attr"
    return "module_attr"


def packet_for(root: Path, source, site) -> dict | None:
    calls = calls_in(Path(source.file_path), source.language)
    matches = [
        item for item in calls
        if item[1] == site.name and item[0].start_point[0] + 1 == site.line
    ] or [item for item in calls if item[1] == site.name]
    if not matches:
        return None
    node, _, receiver, source_bytes = matches[0]
    start_line, start_col = node.start_point
    end_line, end_col = node.end_point
    lines = source_bytes.decode(errors="replace").splitlines()
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
    return {
        "site_id": hashlib.sha256(
            f"{source.symbol_id}:{start_line}:{start_col}".encode()
        ).hexdigest()[:12],
        "repo": root.name,
        "file": str(Path(source.file_path).relative_to(root)),
        "call_text": source_bytes[node.start_byte : node.end_byte].decode(errors="replace"),
        "call_range": [start_line + 1, start_col, end_line + 1, end_col],
        "call_context": "\n".join(context),
        "callee": site.name,
        "receiver": receiver,
        "callee_short": site.name,
    }


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
    rng = random.Random(args.seed)

    groups: dict[str, list] = {}
    for source in engine._symbols.values():  # noqa: SLF001
        for site in source.calls:
            if not site.name or site.name not in engine._by_name:  # noqa: SLF001
                continue
            packet = packet_for(root, source, site)
            if packet is None:
                continue
            form = form_of(engine, source, site)
            packet["form"] = form
            groups.setdefault(form, []).append((source, site, packet))

    allocation = {
        form: min(args.n // max(1, len(groups)), len(pool))
        for form, pool in groups.items()
    }
    remaining = args.n - sum(allocation.values())
    while remaining > 0:
        candidates = [form for form, pool in groups.items() if allocation[form] < len(pool)]
        if not candidates:
            break
        best = max(candidates, key=lambda form: len(groups[form]) - allocation[form])
        allocation[best] += 1
        remaining -= 1

    chosen = []
    for form in sorted(groups):
        pool = groups[form]
        rng.shuffle(pool)
        chosen.extend(pool[: allocation[form]])

    packets, key = [], {}
    for source, site, packet in chosen:
        packets.append({k: v for k, v in packet.items() if k not in ("form",)})
        targets = [
            target_id for target_id in engine._by_name.get(site.name, set())  # noqa: SLF001
            if engine.graph.has_edge(source.symbol_id, target_id)
        ]
        key[packet["site_id"]] = {
            "form": packet["form"],
            "source": source.qualified_name,
            "call_site": f"{packet['file']}:{packet['call_range'][0]}",
            "tool_edge": (
                sorted(engine._symbols[t].qualified_name for t in targets)[:3] if targets else []
            ),
            "tool_outcome": "found" if targets else "no_edge",
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
    counts = {form: len(pool) for form, pool in sorted(groups.items())}
    print(f"{args.name}: {len(packets)} packets, forms={counts}")


if __name__ == "__main__":
    main()
