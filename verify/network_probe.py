"""No network at query time: block sockets after indexing, then run every query.

    python verify/network_probe.py --json
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402


def build_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="ast-net-")) / "repo"
    repo.mkdir(parents=True)
    (repo / "u.py").write_text("def helper(x: int) -> int:\n    return x + 1\n", encoding="utf-8")
    (repo / "a.py").write_text(
        "from u import helper\n\n\ndef run() -> int:\n    return helper(1)\n", encoding="utf-8"
    )
    (repo / "u.ts").write_text(
        "export function helper_ts(x: number): number { return x + 1; }\n", encoding="utf-8"
    )
    (repo / "a.ts").write_text(
        'import { helper_ts } from "./u";\n'
        "export function run_ts(): number { return helper_ts(1); }\n",
        encoding="utf-8",
    )
    return repo


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.parse_args()

    repo = build_repo()
    engine = GraphEngine(repo, cache_dir=repo.parent / "cache")
    engine.index_repository(force_rebuild=True)

    def blocked(*_: Any, **__: Any) -> Any:
        raise AssertionError("network access during query")

    original_socket = socket.socket
    original_connect = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo
    socket.socket = blocked  # type: ignore[assignment]
    socket.create_connection = blocked  # type: ignore[assignment]
    socket.getaddrinfo = blocked  # type: ignore[assignment]
    checks: dict[str, object] = {}
    try:
        checks["slice_py"] = "compiled_context" in engine.get_ast_call_slice("run", hops=1)
        checks["slice_ts"] = "compiled_context" in engine.get_ast_call_slice("run_ts", hops=1)
        checks["blast"] = "total_impacted_symbols" in engine.get_blast_radius("run")
        checks["cycles"] = "self_recursive_count" in engine.find_circular_dependencies()
        checks["search"] = engine.search_symbols("helper")["count"] >= 1
        checks["stats"] = "total_symbols" in engine.get_index_stats()
        checks["explain"] = engine.explain_edge("run", "helper").get("exists") is True
        checks["diff"] = "changed_symbols" in engine.impact_of_diff(
            diff="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-def run() -> int:\n+def run() -> int:\n"
        )
    finally:
        socket.socket = original_socket
        socket.create_connection = original_connect
        socket.getaddrinfo = original_getaddrinfo
        engine.close()
    result = {"checks": checks, "pass": all(checks.values())}
    out = ROOT / "verify" / "results" / "network.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
