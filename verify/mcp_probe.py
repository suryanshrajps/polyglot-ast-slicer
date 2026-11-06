"""Run every MCP tool once over a real stdio session.

Usage: python verify/mcp_probe.py <repo_root>
Prints one JSON object with tools/list metadata and per-tool results.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


def _payload(result: Any) -> Any:
    if getattr(result, "data", None) is not None:
        return result.data
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    blocks = getattr(result, "content", None) or []
    texts = [getattr(block, "text", "") for block in blocks]
    raw = "\n".join(texts)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


async def main(repo: str, command: str, extra: list[str], cache: str) -> None:
    env = dict(os.environ)
    env["POLYGLOT_AST_WATCH"] = "0"
    env["POLYGLOT_AST_CACHE_DIR"] = cache
    transport = StdioTransport(
        command=command,
        args=[*(extra or ["-m", "polyglot_ast_slicer"]), "serve", "--repo", repo],
        env=env,
        cwd=repo,
    )
    out: dict[str, Any] = {"repo": repo, "tools": [], "calls": {}}
    async with Client(transport) as client:
        tools = await client.list_tools()
        out["tools"] = [
            {"name": tool.name, "description": (tool.description or "").splitlines()[0]}
            for tool in tools
        ]
        calls: list[tuple[str, dict[str, Any]]] = [
            ("index_codebase", {"repo_path": repo}),
            ("search_symbols", {"repo_path": repo, "query": "run"}),
            ("get_ast_call_slice", {"repo_path": repo, "symbol_name": "run", "hops": 1}),
            ("get_blast_radius", {"repo_path": repo, "symbol_name": "run"}),
            ("find_circular_dependencies", {"repo_path": repo}),
            (
                "explain_edge",
                {
                    "repo_path": repo,
                    "source": "src/index.ts::run" if "ts" in repo else "pkg/app.py::extra",
                    "target": "src/util.ts::helper" if "ts" in repo else "pkg/app.py::entry",
                },
            ),
            ("impact_of_diff", {"repo_path": repo, "base": "HEAD~1", "head": "HEAD"}),
            ("get_index_stats", {"repo_path": repo}),
        ]
        for name, arguments in calls:
            try:
                result = await client.call_tool(name, arguments)
                value = _payload(result)
                if isinstance(value, dict):
                    summary = {
                        "keys": sorted(value.keys()),
                        "error": value.get("error"),
                        "count": value.get("count"),
                        "total_symbols": value.get("total_symbols"),
                        "total_edges": value.get("total_edges"),
                        "exists": value.get("exists"),
                        "risk_level": value.get("risk_level"),
                        "total_changed_symbols": value.get("total_changed_symbols"),
                        "total_impacted_symbols": value.get("total_impacted_symbols"),
                        "symbol_cycle_count": value.get("symbol_cycle_count"),
                        "self_recursive_count": value.get("self_recursive_count"),
                        "file_cycle_count": value.get("file_cycle_count"),
                        "compiled_context_chars": len(value.get("compiled_context") or ""),
                        "slice_tokens": (value.get("token_metrics") or {}).get("slice_tokens"),
                    }
                else:
                    summary = {"type": type(value).__name__, "preview": str(value)[:120]}
                out["calls"][name] = summary
            except Exception as error:  # noqa: BLE001 - probe reports, never raises
                out["calls"][name] = {"exception": f"{type(error).__name__}: {error}"}
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--command", default=sys.executable)
    parser.add_argument("--cache", default="/tmp/pas-mcp-cache")
    parser.add_argument("extra", nargs=argparse.REMAINDER, help="command arguments before 'serve'")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.repo, arguments.command, arguments.extra, arguments.cache))
