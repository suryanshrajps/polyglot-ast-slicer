"""Robustness: malformed, huge, non-UTF-8, nested, symlinked and read-only inputs.

    python verify/robustness_probe.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402


def build() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="ast-robust-")) / "repo"
    repo.mkdir(parents=True)
    (repo / "empty.py").write_bytes(b"")
    (repo / "broken.py").write_text("def broken(:\n    return\n", encoding="utf-8")
    (repo / "binary.py").write_bytes(b"def bad():\x00\n")
    (repo / "latin1.py").write_bytes(
        b"# -*- coding: latin-1 -*-\ndef f():\n    return 'caf\xe9'\n"
    )
    (repo / "big.py").write_bytes(b"# pad\n" * (5 * 1024 * 1024 // 6))
    (repo / "huge_line.py").write_text('x = "' + "a" * 1_000_000 + '"\n', encoding="utf-8")
    lines = ["def deep() -> int:"]
    for depth in range(200):
        lines.append("    " * (depth + 1) + "if True:")
    lines.append("    " * 201 + "return 1")
    (repo / "deep.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (repo / "ok.py").write_text("def ok() -> int:\n    return 1\n", encoding="utf-8")
    loop = repo / "loop.py"
    loop.symlink_to(loop)
    return repo


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.parse_args()

    repo = build()
    engine = GraphEngine(repo, cache_dir=repo.parent / "cache")
    stats = engine.index_repository(force_rebuild=True)
    skipped = stats["skipped_files"]
    parse_errors = engine.get_index_stats()["files_with_parse_errors"]
    result = {
        "files_indexed": stats["files_indexed"],
        "binary_skipped": any("binary" in reason for reason in skipped.values()),
        "big_skipped": any("file too large" in reason for reason in skipped.values()),
        "broken_recorded": "broken.py" in parse_errors,
        "deep_indexed": any(
            symbol.name == "deep" for symbol in engine._symbols.values()
        ),
        "slice_ok": "compiled_context" in engine.get_ast_call_slice("ok", hops=0),
    }
    engine.close()

    os.chmod(repo, stat.S_IRUSR | stat.S_IXUSR)
    try:
        readonly_engine = GraphEngine(repo, cache_dir=repo.parent / "cache2")
        readonly_stats = readonly_engine.index_repository(force_rebuild=True)
        result["readonly_indexed"] = readonly_stats["files_indexed"] >= 1
        readonly_engine.close()
    finally:
        os.chmod(repo, stat.S_IRWXU)

    result["pass"] = all(
        bool(result[key])
        for key in (
            "binary_skipped",
            "big_skipped",
            "broken_recorded",
            "deep_indexed",
            "slice_ok",
            "readonly_indexed",
        )
    )
    out = ROOT / "verify" / "results" / "robustness.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
