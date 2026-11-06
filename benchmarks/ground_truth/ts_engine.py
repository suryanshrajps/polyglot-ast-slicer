"""Dumps the tool's TS/JS symbols and call edges for the TypeScript ground-truth harness.

``ts_eval.mjs`` resolves true call targets with the TypeScript compiler API and compares
them with this dump; keeping the engine data in a separate process avoids duplicating the
Python indexer in Node.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402


def dump(root: str) -> dict[str, object]:
    engine = GraphEngine(root)
    engine.index_repository(force_rebuild=True)
    symbols = [
        {
            "id": symbol.symbol_id,
            "name": symbol.name,
            "qualified_name": symbol.qualified_name,
            "kind": symbol.kind,
            "file": symbol.file_path,
            "rel": engine._rel(symbol.file_path),  # noqa: SLF001
            "start_line": symbol.start_line,
            "end_line": symbol.end_line,
            "start_byte": symbol.start_byte,
            "end_byte": symbol.end_byte,
            "is_default": symbol.is_default,
        }
        for symbol in engine._symbols.values()  # noqa: SLF001
    ]
    edges = [
        {
            "source": source,
            "target": target,
            "relation": data["relation"],
            "confidence": data.get("confidence"),
            "line": data["line"],
            "name": data.get("name"),
            "receiver": data.get("receiver"),
            "resolution": data.get("resolution"),
        }
        for source, target, data in engine.graph.edges(data=True)
        if data.get("relation") in ("calls", "constructs") and data.get("line") is not None
    ]
    engine.close()
    return {"root": str(Path(root).resolve()), "symbols": symbols, "edges": edges}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = dump(args.root)
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
