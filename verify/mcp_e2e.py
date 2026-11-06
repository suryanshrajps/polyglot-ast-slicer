"""MCP end-to-end through ``uv tool run --from <wheel>`` on real stdio clients.

    python verify/mcp_e2e.py --wheel dist/polyglot_ast_slicer-1.0.0-py3-none-any.whl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402

TOOLS = [
    "index_codebase",
    "search_symbols",
    "get_ast_call_slice",
    "get_blast_radius",
    "find_circular_dependencies",
    "explain_edge",
    "impact_of_diff",
    "get_index_stats",
]
DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-def run() -> int:\n+def run() -> int:\n"


def payload(result: Any) -> Any:
    if getattr(result, "data", None) is not None:
        return result.data
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    blocks = getattr(result, "content", None) or []
    return "\n".join(getattr(block, "text", "") for block in blocks)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def fixtures() -> dict[str, Path]:
    base = Path(tempfile.mkdtemp(prefix="ast-mcp-"))
    python = base / "python"
    write(python / "util.py", "def helper() -> int:\n    return 1\n")
    write(
        python / "app.py",
        "from util import helper\n\n\ndef run() -> int:\n    return helper()\n",
    )
    ts = base / "ts"
    write(ts / "util.ts", "export function helper(): number { return 1; }\n")
    write(
        ts / "app.ts",
        'import { helper } from "./util";\n'
        "export function run(): number { return helper(); }\n",
    )
    mixed = base / "mixed"
    write(mixed / "py" / "__init__.py", "")
    write(mixed / "py" / "util.py", "def helper_py() -> int:\n    return 1\n")
    write(
        mixed / "py" / "app.py",
        "from py.util import helper_py\n\n\ndef run_py() -> int:\n    return helper_py()\n",
    )
    write(mixed / "ts" / "util.ts", "export function helper_ts(): number { return 1; }\n")
    write(
        mixed / "ts" / "app.ts",
        'import { helper_ts } from "./util";\n'
        "export function run_ts(): number { return helper_ts(); }\n",
    )
    monorepo = base / "monorepo"
    for package in ("services", "services/alpha", "services/beta"):
        write(monorepo / package / "__init__.py", "")
    write(monorepo / "services/alpha/util.py", "def helper_alpha() -> int:\n    return 1\n")
    write(
        monorepo / "services/alpha/app.py",
        "from services.alpha.util import helper_alpha\n\n\n"
        "def run_alpha() -> int:\n    return helper_alpha()\n",
    )
    write(monorepo / "services/beta/util.py", "def helper_beta() -> int:\n    return 2\n")
    write(monorepo / "services/beta/app.py", "def helper_beta() -> int:\n    return 3\n")
    return {"python": python, "ts": ts, "mixed": mixed, "monorepo": monorepo}


async def probe(repo: Path, wheel: Path, symbol: str, helper: str) -> dict[str, Any]:
    env = dict(os.environ)
    env["POLYGLOT_AST_WATCH"] = "0"
    env["POLYGLOT_AST_CACHE_DIR"] = str(repo.parent / "cache")
    transport = StdioTransport(
        command="uv",
        args=[
            "tool",
            "run",
            "--from",
            str(wheel),
            "polyglot-ast-slicer",
            "serve",
            "--repo",
            str(repo),
        ],
        env=env,
        cwd=str(repo),
    )
    out: dict[str, Any] = {}
    async with Client(transport) as client:
        tools = await client.list_tools()
        out["tools"] = sorted(tool.name for tool in tools)
        calls: list[tuple[str, dict[str, Any]]] = [
            ("index_codebase", {}),
            ("get_index_stats", {}),
            ("search_symbols", {"query": "helper"}),
            ("get_ast_call_slice", {"symbol_name": symbol, "include_metadata": True}),
            ("get_blast_radius", {"symbol_name": symbol}),
            ("find_circular_dependencies", {}),
            ("explain_edge", {"source": symbol, "target": helper}),
            ("impact_of_diff", {"diff": DIFF}),
        ]
        for name, arguments in calls:
            try:
                out[name] = payload(
                    await client.call_tool(name, {"repo_path": str(repo), **arguments})
                )
            except Exception as error:  # noqa: BLE001
                out[name] = {"exception": f"{type(error).__name__}: {error}"}
    return out


async def run(wheel: Path) -> dict[str, Any]:
    roots = fixtures()
    results: dict[str, Any] = {}
    symbols = {
        "python": ("run", "helper"),
        "ts": ("run", "helper"),
        "mixed": ("run_py", "helper_py"),
        "monorepo": ("run_alpha", "helper_alpha"),
    }
    for name, repo in roots.items():
        symbol, helper = symbols[name]
        results[name] = await probe(repo, wheel, symbol, helper)

    python = results["python"]
    ts = results["ts"]
    mixed = results["mixed"]
    mono = results["monorepo"]
    mixed_stats = mixed.get("get_index_stats") or {}
    mixed_slice = mixed.get("get_ast_call_slice") or {}
    mono_slice = mono.get("get_ast_call_slice") or {}
    checks = {
        "tools_8": all(set(result["tools"]) == set(TOOLS) for result in results.values()),
        "python_edge": any(
            item["qualified_name"] == "helper"
            for item in python["get_ast_call_slice"].get("downstream_callees", [])
        ),
        "ts_edge": any(
            item["qualified_name"] == "helper"
            for item in ts["get_ast_call_slice"].get("downstream_callees", [])
        ),
        "mixed_both_languages": set(mixed_stats.get("languages", {})) >= {"python", "typescript"},
        "no_cross_language": "helper_ts" not in str(mixed_slice)
        and "helper_py" in str(mixed_slice),
        "monorepo_boundary": "helper_beta" not in str(mono_slice)
        and "helper_alpha" in str(mono_slice),
        "no_errors": all(
            "error" not in (value if isinstance(value, dict) else {})
            and "exception" not in (value if isinstance(value, dict) else {})
            for result in results.values()
            for value in result.values()
            if isinstance(value, dict)
        ),
    }
    return {"checks": checks, "pass": all(checks.values()), "results": results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    if not wheel.exists():
        raise SystemExit(f"wheel not found: {wheel}")
    result = asyncio.run(run(wheel))
    out = ROOT / "verify" / "results" / "mcp.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"checks": result["checks"], "pass": result["pass"]}, indent=2))


if __name__ == "__main__":
    main()
