"""Seeded synthetic scale benchmark (5k/20k/50k files).

    python benchmarks/scale/scale_bench.py --files 5000 --json
"""

from __future__ import annotations

import argparse
import json
import random
import resource
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402


def generate(root: Path, files: int, seed: int = 42) -> None:
    rng = random.Random(seed)
    per_package = 100
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    for index in range(files):
        package = root / f"pkg{index // per_package:04d}"
        package.mkdir(exist_ok=True)
        target = index + 1 if (index + 1) % per_package else index - per_package + 1
        suffix = rng.randrange(3)
        (package / f"mod{index:06d}.py").write_text(
            f"from pkg{target // per_package:04d}.mod{target:06d} import fn_{target}\n\n"
            f"def fn_{index}(x: int) -> int:\n"
            f"    return fn_{target}(x) + {suffix}\n"
        )


def run(files: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ast-scale-") as directory:
        root = Path(directory) / "repo"
        generate(root, files)
        engine = GraphEngine(root, cache_dir=Path(directory) / "cache")
        start = time.perf_counter()
        cold = engine.index_repository(force_rebuild=True)
        cold_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        warm = engine.index_repository(force_rebuild=False)
        warm_ms = (time.perf_counter() - start) * 1000

        source_files = engine.discover_source_files()
        rng = random.Random(7)
        latencies = []
        for path in rng.sample(source_files, min(20, len(source_files))):
            original = path.read_text()
            path.write_text(original + "\n# touch\n")
            latencies.append(engine.refresh_file(path)["elapsed_ms"])
            path.write_text(original)
            engine.refresh_file(path)
        latencies.sort()
        start = time.perf_counter()
        engine.get_ast_call_slice("fn_0", hops=2)
        query_ms = (time.perf_counter() - start) * 1000
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
            1e6 if sys.platform == "darwin" else 1e3
        )
        result = {
            "files": cold["files_indexed"],
            "symbols": cold["total_symbols"],
            "edges": cold["total_edges"],
            "cold_ms": round(cold_ms, 1),
            "warm_ms": round(warm_ms, 1),
            "warm_cache_hits": warm["cache_hits"],
            "query_ms": round(query_ms, 1),
            "refresh_p50_ms": round(statistics.median(latencies), 2),
            "refresh_p95_ms": round(latencies[int(len(latencies) * 0.95) - 1], 2),
            "rss_mb": round(rss_mb, 1),
        }
        engine.close()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=5000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run(args.files)
    if args.json:
        print(json.dumps(result))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
