"""Watcher end-to-end: edit, query, survive an exception, survive a deleted file.

    python verify/watcher_probe.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from polyglot_ast_slicer import server  # noqa: E402


def has_edge(engine: object, source: str, target: str) -> bool:
    return engine.explain_edge(source, target).get("exists") is True  # type: ignore[attr-defined]


def wait_for(
    engine: object, source: str, target: str, present: bool, timeout: float = 10.0
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if has_edge(engine, source, target) is present:
            return True
        time.sleep(0.1)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.parse_args()

    repo = Path(tempfile.mkdtemp(prefix="ast-watch-")) / "repo"
    repo.mkdir(parents=True)
    (repo / "b.py").write_text("def helper() -> int:\n    return 1\n", encoding="utf-8")
    (repo / "a.py").write_text(
        "from b import helper\n\n\ndef run() -> int:\n    return helper()\n", encoding="utf-8"
    )

    os.environ["POLYGLOT_AST_WATCH"] = "1"
    os.environ["POLYGLOT_AST_CACHE_DIR"] = str(repo.parent / "cache")
    engine = server._get_engine(str(repo), watch_enabled=True)
    result: dict[str, object] = {"initial": has_edge(engine, "run", "helper")}

    (repo / "b.py").write_text(
        "def helper2() -> int:\n    return 2\n", encoding="utf-8"
    )
    (repo / "a.py").write_text(
        "from b import helper2\n\n\ndef run() -> int:\n    return helper2()\n", encoding="utf-8"
    )
    result["after_edit"] = wait_for(engine, "run", "helper2", True)

    original = engine.refresh_file
    calls = {"count": 0}

    def flaky(path: str | Path) -> dict[str, object]:
        if calls["count"] == 0:
            calls["count"] += 1
            raise RuntimeError("injected watcher failure")
        return original(path)

    engine.refresh_file = flaky  # type: ignore[method-assign]
    (repo / "b.py").write_text(
        "def helper2() -> int:\n    return 2\n\n\ndef third() -> int:\n    return 3\n",
        encoding="utf-8",
    )
    time.sleep(1.5)
    engine.refresh_file = original  # type: ignore[method-assign]
    # Re-save b.py after the failure so the watcher can index ``third``.
    with (repo / "b.py").open("a", encoding="utf-8") as handle:
        handle.write("\n# recovered\n")
    (repo / "a.py").write_text(
        "from b import third\n\n\ndef run() -> int:\n    return third()\n", encoding="utf-8"
    )
    result["after_exception"] = wait_for(engine, "run", "third", True)
    result["exception_seen"] = calls["count"] == 1

    (repo / "b.py").unlink()
    result["after_delete"] = wait_for(engine, "run", "third", False)
    result["post_delete_stats_ok"] = "error" not in engine.get_index_stats()

    server._ENGINES.pop(str(repo.resolve()), None)
    server._WATCHERS.pop(str(repo.resolve()), None)
    engine.close()
    result["pass"] = all(
        result[key]
        for key in ("initial", "after_edit", "after_exception", "after_delete")
    ) and result["exception_seen"] and result["post_delete_stats_ok"]

    out = ROOT / "verify" / "results" / "watcher.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
