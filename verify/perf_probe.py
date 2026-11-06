"""Cold/warm/refresh/query latency and peak RSS for one real repository.

Usage: python verify/perf_probe.py <repo> <symbol> [cache_dir]
"""

from __future__ import annotations

import json
import random
import resource
import statistics
import sys
import time
from pathlib import Path

from polyglot_ast_slicer.graph_engine import GraphEngine


def main() -> None:
    repo = Path(sys.argv[1]).resolve()
    symbol = sys.argv[2]
    cache_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("/tmp/pas-perf-cache")

    engine = GraphEngine(repo, cache_dir=cache_dir)
    started = time.perf_counter()
    cold = engine.index_repository(force_rebuild=True)
    cold_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    warm = engine.index_repository(force_rebuild=False)
    warm_ms = (time.perf_counter() - started) * 1000

    files = engine.discover_source_files()
    rng = random.Random(11)
    latencies: list[float] = []
    for path in rng.sample(files, min(20, len(files))):
        original = path.read_text(encoding="utf-8", errors="replace")
        path.write_text(original + "\n# verify touch\n", encoding="utf-8")
        latencies.append(engine.refresh_file(path)["elapsed_ms"])
        path.write_text(original, encoding="utf-8")
        engine.refresh_file(path)
    latencies.sort()

    started = time.perf_counter()
    payload = engine.get_ast_call_slice(symbol, hops=2)
    query_ms = (time.perf_counter() - started) * 1000

    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
        1e6 if sys.platform == "darwin" else 1e3
    )
    result = {
        "repo": str(repo),
        "symbol": symbol,
        "files": cold["files_indexed"],
        "symbols": cold["total_symbols"],
        "edges": cold["total_edges"],
        "cold_ms": round(cold_ms, 1),
        "warm_ms": round(warm_ms, 1),
        "warm_cache_hits": warm["cache_hits"],
        "refresh_p50_ms": round(statistics.median(latencies), 2),
        "refresh_p95_ms": round(latencies[int(len(latencies) * 0.95) - 1], 2),
        "query_ms": round(query_ms, 1),
        "query_error": payload.get("error"),
        "query_tokens": payload.get("token_metrics", {}).get("payload_tokens"),
        "rss_mb": round(rss_mb, 1),
    }
    engine.close()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
