"""Agent-outcome benchmark harness (dry-run by default; no model calls embedded).

A real run needs a model adapter that can: read a task, call the MCP tools, edit files and
run the task's test command. That adapter is deliberately *not* implemented here because this
environment has no model endpoint. With ``--dry-run`` the harness only measures the context
the tool would hand the agent, and reports ``status: "UNMEASURED"`` for resolve rate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402


def load_tasks(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("tasks file must contain a JSON list")
    return data


def run_task(task: dict[str, object]) -> dict[str, object]:
    repo = Path(str(task["repo"])).expanduser().resolve()
    symbols = task.get("symbols", [])
    if not isinstance(symbols, list) or not symbols:
        return {"id": task.get("id"), "error": "task needs a non-empty 'symbols' list"}
    if not repo.is_dir():
        return {"id": task.get("id"), "error": f"repo not found locally: {repo}"}

    engine = GraphEngine(repo)
    stats = engine.index_repository(force_rebuild=False)
    slices = []
    for symbol in symbols:
        payload = engine.get_ast_call_slice(str(symbol), hops=2)
        if "error" in payload:
            slices.append({"symbol": symbol, "error": payload["error"]})
            continue
        slices.append(
            {
                "symbol": symbol,
                "payload_tokens": payload["token_metrics"]["payload_tokens"],
                "slice_tokens": payload["token_metrics"]["slice_tokens"],
                "touched_files_tokens": payload["token_metrics"]["touched_files_tokens"],
                "truncated": payload["diagnostics"]["truncated"],
                "omitted": payload["diagnostics"]["omitted"],
            }
        )
    engine.close()
    return {
        "id": task.get("id"),
        "repo": str(repo),
        "files_indexed": stats["files_indexed"],
        "symbols": slices,
        "status": "UNMEASURED",
        "note": "No model adapter; resolve rate and tool calls cannot be measured here.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=Path("agent_eval_results.json"))
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true", help="Requires a model adapter (absent)")
    args = parser.parse_args()
    if args.execute:
        raise SystemExit(
            "No model adapter is installed. Mark this run UNMEASURED; see README in this folder."
        )

    results = [run_task(task) for task in load_tasks(args.tasks)]
    report = {
        "status": "UNMEASURED",
        "reason": "dry-run: no model endpoint or adapter in this environment",
        "tasks": results,
    }
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "tasks": len(results), "out": str(args.out)}))


if __name__ == "__main__":
    main()
