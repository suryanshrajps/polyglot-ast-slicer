"""Scale probe: synthetic 5k/20k/50k plus one real repo per language.

Each measurement runs in a fresh subprocess so peak RSS is per-corpus.

    python verify/scale_probe.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "benchmarks" / "scale"))

from scale_bench import generate  # noqa: E402

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

EVAL_REPOS = Path(os.environ.get("POLYGLOT_AST_EVAL_REPOS", "/tmp/pas-eval-repos"))
REAL = {
    "python-requests": EVAL_REPOS / "requests",
    "ts-zod": EVAL_REPOS / "zod",
}


def rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(value / (1e6 if sys.platform == "darwin" else 1e3), 1)


def measure(root: Path, cache: Path) -> dict[str, object]:
    engine = GraphEngine(root, cache_dir=cache)
    start = time.perf_counter()
    cold = engine.index_repository(force_rebuild=True)
    cold_ms = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    warm = engine.index_repository(force_rebuild=False)
    warm_ms = (time.perf_counter() - start) * 1000

    files = engine.discover_source_files()
    rng = random.Random(7)
    latencies: list[float] = []
    for path in rng.sample(files, min(20, len(files))):
        original = path.read_bytes()
        path.write_bytes(original + b"\n# touch\n")
        latencies.append(float(engine.refresh_file(path)["elapsed_ms"]))
        path.write_bytes(original)
        engine.refresh_file(path)
    latencies.sort()
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]

    target = next(
        (s.qualified_name for s in sorted(engine._symbols.values(), key=lambda s: s.symbol_id)
         if s.kind in ("function", "method") and s.qualified_name != "<module>"),
        "",
    )
    start = time.perf_counter()
    result = engine.get_ast_call_slice(target, hops=2) if target else {"error": "no symbol"}
    query_ms = (time.perf_counter() - start) * 1000
    out = {
        "files": cold["files_indexed"],
        "symbols": cold["total_symbols"],
        "edges": cold["total_edges"],
        "cold_ms": round(cold_ms, 1),
        "warm_ms": round(warm_ms, 1),
        "warm_cache_hits": warm["cache_hits"],
        "incremental_p95_ms": round(p95, 2),
        "query_ms": round(query_ms, 1),
        "query_symbol": target,
        "query_ok": "error" not in result,
        "peak_rss_mb": rss_mb(),
    }
    engine.close()
    return out


def child(kind: str, name: str) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__)), "--one", kind, name, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr[-2000:])
    return json.loads(completed.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--one", nargs=2, metavar=("KIND", "NAME"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.one:
        kind, name = args.one
        if kind == "synthetic":
            with tempfile.TemporaryDirectory(prefix="ast-scale-") as directory:
                root = Path(directory) / "repo"
                generate(root, int(name))
                print(json.dumps(measure(root, Path(directory) / "cache")))
        else:
            print(json.dumps(measure(REAL[name], Path(tempfile.mkdtemp(prefix="ast-real-")))))
        return

    rows: dict[str, dict[str, object]] = {}
    for files in (5000, 20000, 50000):
        rows[f"synthetic-{files}"] = child("synthetic", str(files))
    for name in REAL:
        rows[name] = child("real", name)
    payload = {"results": rows}
    out = ROOT / "verify" / "results" / "scale.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
