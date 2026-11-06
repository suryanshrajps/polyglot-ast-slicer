"""Writes the worked examples shown in README.md from real engine output."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

EXAMPLES = {
    "python": (
        ROOT / "verify" / "fixtures" / "readme" / "python",
        "process_payment",
    ),
    "typescript": (
        ROOT / "verify" / "fixtures" / "readme" / "typescript",
        "processPayment",
    ),
}


def main() -> None:
    results = {}
    for language, (repo, symbol) in EXAMPLES.items():
        engine = GraphEngine(repo, cache_dir=Path("/tmp/pas-readme-cache") / language)
        engine.index_repository(force_rebuild=True)
        payload = engine.get_ast_call_slice(symbol, hops=1)
        engine.close()
        if "error" in payload:
            raise SystemExit(f"example failed: {language}: {payload['error']}")
        results[language] = {
            "symbol": symbol,
            "compiled_context": payload["compiled_context"],
            "token_metrics": payload["token_metrics"],
        }
    out = ROOT / "metrics" / "examples.json"
    out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({name: row["token_metrics"] for name, row in results.items()}, indent=2))


if __name__ == "__main__":
    main()
