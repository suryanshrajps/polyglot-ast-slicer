"""Audits v2 canaries from commit bddc65b for owner plausibility."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_v2 import _plausible  # noqa: E402

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OLD = "bddc65b"


def show(path: str):
    out = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{OLD}:{path}"],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def main() -> None:
    report = {}
    for repo, name in (("requests", "requests_hard"), ("click", "click_hard")):
        repo_root = (Path("/tmp") / repo).resolve()
        engine = GraphEngine(str(repo_root))
        engine.index_repository(force_rebuild=True)
        key = show(f"labels/{name}/v2_answer_key.json")
        packets = {p["edge_id"]: p for p in show(f"labels/{name}/v2_packets.json")}
        failures = []
        for eid, entry in key.items():
            if not entry.get("seeded"):
                continue
            packet = packets[eid]
            line = packet["call_range"][0]
            candidates = []
            for sid in engine._by_file.get(str(repo_root / packet["file"]), set()):
                symbol = engine._symbols[sid]  # noqa: SLF001
                if symbol.start_line <= line <= symbol.end_line:
                    candidates.append(symbol)
            source = min(
                candidates,
                key=lambda symbol: symbol.end_line - symbol.start_line,
                default=None,
            )
            if source is None:
                failures.append({"edge_id": eid, "reason": "source not found"})
                continue
            fake = next(
                (
                    symbol for symbol in engine._symbols.values()  # noqa: SLF001
                    if symbol.qualified_name == entry["fake_target"]
                    and str(Path(symbol.file_path).relative_to(repo_root))
                    == packet["target"]["file"]
                ),
                None,
            )
            if fake is None:
                failures.append({"edge_id": eid, "reason": "fake target not found"})
                continue
            if not _plausible(engine, source, fake):
                failures.append(
                    {
                        "edge_id": eid,
                        "call": packet["file"],
                        "fake": fake.qualified_name,
                        "reason": "owner not same-file/imported/inherited",
                    }
                )
        report[name] = {"seeded": sum(1 for v in key.values() if v.get("seeded")),
                        "failures": len(failures), "examples": failures[:5]}
    (ROOT / "labels" / "canary_plausibility.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
