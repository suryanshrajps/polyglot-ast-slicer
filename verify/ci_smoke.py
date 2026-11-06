"""Index and slice smoke test used by CI after installing the built wheel."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {
    "python": ROOT / "verify" / "fixtures" / "smoke" / "python",
    "typescript": ROOT / "verify" / "fixtures" / "smoke" / "typescript",
}


def cli(*args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "polyglot_ast_slicer", *args, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(args)}\n{completed.stderr[-1000:]}")
    return json.loads(completed.stdout)


def main() -> None:
    for language, repo in FIXTURES.items():
        stats = cli("index", "--repo", str(repo), "--force")
        assert stats["files_indexed"] >= 1, (language, stats)
        assert stats["total_symbols"] >= 2, (language, stats)
        assert stats["total_edges"] >= 1, (language, stats)
        result = cli("slice", "--repo", str(repo), "--symbol", "run", "--metadata")
        assert "error" not in result, (language, result)
        assert "helper" in result["compiled_context"], (language, result.keys())
        assert any(
            item["qualified_name"] == "helper" for item in result["downstream_callees"]
        ), (language, result["downstream_callees"])
        print(f"{language}: {stats['total_symbols']} symbols, {stats['total_edges']} edges, OK")


if __name__ == "__main__":
    main()
