"""Dogfood: index this repository and slice five of its own functions.

    python verify/dogfood_probe.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.parse_args()

    engine = GraphEngine(ROOT, cache_dir=Path("/tmp/pas-dogfood-cache"))
    stats = engine.index_repository(force_rebuild=True)
    candidates = [
        symbol
        for symbol in sorted(engine._symbols.values(), key=lambda s: s.symbol_id)
        if symbol.kind in ("function", "method")
        and symbol.qualified_name != "<module>"
        and symbol.file_path.startswith(str(ROOT / "polyglot_ast_slicer"))
    ]
    results = []
    for symbol in candidates[:5]:
        payload = engine.get_ast_call_slice(symbol.qualified_name, hops=1)
        results.append(
            {
                "symbol": symbol.qualified_name,
                "file": engine._rel(symbol.file_path),
                "line": symbol.start_line,
                "ok": "error" not in payload,
                "payload_tokens": payload.get("token_metrics", {}).get("payload_tokens"),
                "has_target": symbol.name in payload.get("compiled_context", ""),
            }
        )
    engine.close()
    result = {
        "files": stats["files_indexed"],
        "symbols": stats["total_symbols"],
        "edges": stats["total_edges"],
        "slices": results,
        "pass": len(results) == 5 and all(row["ok"] and row["has_target"] for row in results),
    }
    out = ROOT / "verify" / "results" / "dogfood.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
