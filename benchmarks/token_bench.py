"""Benchmark: slice size vs. naive full-file context, plus indexing and refresh latency.

Run without arguments to build a synthetic polyglot repository and measure it:

    uv run python benchmarks/token_bench.py

Or point it at a real checkout:

    uv run python benchmarks/token_bench.py --repo ~/code/my-app --symbol handle_request --hops 2
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402


def _noise_functions(prefix: str, count: int, body: str) -> str:
    blocks = []
    for index in range(count):
        blocks.append(
            textwrap.dedent(
                f"""
                def {prefix}_{index}(rows: list[dict[str, str]]) -> str:
                    \"\"\"Unrelated helper #{index}; must never enter a payment slice.\"\"\"
                {body}
                """
            ).rstrip()
        )
    return "\n\n".join(blocks)


def populate_synthetic_repo(root: Path) -> None:
    backend = root / "backend"
    frontend = root / "frontend"
    backend.mkdir(parents=True, exist_ok=True)
    frontend.mkdir(parents=True, exist_ok=True)

    (backend / "schemas.py").write_text(
        textwrap.dedent(
            """
            from dataclasses import dataclass

            @dataclass
            class PaymentRequest:
                order_id: str
                user_id: str
                amount_cents: int
                currency: str

            @dataclass
            class PaymentReceipt:
                transaction_id: str
                status: str
                net_amount_cents: int
            """
        ).strip()
        + "\n\n"
        + _noise_functions(
            "legacy_audit_record",
            40,
            "    return f\"{len(rows)}:{rows[0].get('id', 'none')}\"",
        )
        + "\n",
        encoding="utf-8",
    )

    (backend / "ledger.py").write_text(
        textwrap.dedent(
            """
            from backend.schemas import PaymentRequest, PaymentReceipt

            def validate_fraud_rules(req: PaymentRequest) -> bool:
                return req.amount_cents > 0 and req.amount_cents < 10_000_000

            def compute_platform_fee(amount_cents: int) -> int:
                return int(amount_cents * 0.029) + 30

            def persist_ledger_entry(req: PaymentRequest, fee_cents: int) -> PaymentReceipt:
                net = req.amount_cents - fee_cents
                return PaymentReceipt(
                    transaction_id=f"tx_{req.order_id}",
                    status="SETTLED",
                    net_amount_cents=net,
                )

            def reconcile_node_alpha(batch_id: str) -> str:
                return reconcile_node_beta(batch_id)

            def reconcile_node_beta(batch_id: str) -> str:
                return reconcile_node_alpha(batch_id)
            """
        ).strip()
        + "\n\n"
        + _noise_functions(
            "generate_monthly_report_chunk",
            140,
            "    return \"\\n\".join(f\"{row.get('id', 'none')}\" for row in rows)",
        )
        + "\n",
        encoding="utf-8",
    )

    (backend / "service.py").write_text(
        textwrap.dedent(
            """
            from backend.schemas import PaymentRequest, PaymentReceipt
            from backend.ledger import (
                compute_platform_fee,
                persist_ledger_entry,
                validate_fraud_rules,
            )

            def process_payment(req: PaymentRequest) -> PaymentReceipt:
                if not validate_fraud_rules(req):
                    raise ValueError("fraud check failed")
                fee = compute_platform_fee(req.amount_cents)
                return persist_ledger_entry(req, fee)

            def handle_checkout_webhook(
                order_id: str, user_id: str, amount_cents: int
            ) -> PaymentReceipt:
                request = PaymentRequest(
                    order_id=order_id,
                    user_id=user_id,
                    amount_cents=amount_cents,
                    currency="USD",
                )
                return process_payment(request)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    (frontend / "checkout.ts").write_text(
        textwrap.dedent(
            """
            export interface CheckoutPayload {
              orderId: string;
              userId: string;
              amountCents: number;
            }

            export function sanitizeCheckoutPayload(payload: CheckoutPayload): CheckoutPayload {
              return {
                orderId: payload.orderId.trim(),
                userId: payload.userId.trim(),
                amountCents: Math.max(0, payload.amountCents),
              };
            }

            export async function submitOrderCheckout(
              payload: CheckoutPayload
            ): Promise<string> {
              const clean = sanitizeCheckoutPayload(payload);
              return `submitted:${clean.orderId}:${clean.amountCents}`;
            }
            """
        ).strip()
        + "\n\n"
        + "\n\n".join(
            f"export function renderAnalyticsWidget_{index}(points: number[]): string {{\n"
            f"  return `<div>{index}:${{points.length}}</div>`;\n}}"
            for index in range(60)
        )
        + "\n",
        encoding="utf-8",
    )


def measure_refresh(engine: GraphEngine, files: list[Path]) -> dict[str, float]:
    latencies: list[float] = []
    for path in files:
        original = path.read_text(encoding="utf-8")
        try:
            path.write_text(original + "\n# benchmark touch\n", encoding="utf-8")
            latencies.append(engine.refresh_file(path)["elapsed_ms"])
        finally:
            path.write_text(original, encoding="utf-8")
            engine.refresh_file(path)
    latencies.sort()
    return {
        "samples": len(latencies),
        "p50_ms": round(statistics.median(latencies), 3),
        "p95_ms": round(latencies[max(0, int(len(latencies) * 0.95) - 1)], 3),
        "max_ms": round(latencies[-1], 3),
    }


def run(repo: Path, symbols: list[str], hops: int, as_json: bool) -> dict[str, object]:
    engine = GraphEngine(repo)

    cold = engine.index_repository(force_rebuild=True)
    warm = engine.index_repository(force_rebuild=False)

    files = engine.discover_source_files()
    refresh = measure_refresh(engine, files[:10])

    rows = []
    for symbol in symbols:
        result = engine.get_ast_call_slice(symbol, hops=hops, include_metadata=True)
        if "error" in result:
            rows.append({"symbol": symbol, "error": result["error"]})
            continue
        metrics = result["token_metrics"]
        rows.append(
            {
                "symbol": symbol,
                "qualified_name": result["target"]["qualified_name"],
                "touched_files_tokens": metrics["touched_files_tokens"],
                "full_repo_tokens": metrics["full_repo_tokens"],
                "slice_tokens": metrics["slice_tokens"],
                "saved_vs_files_pct": metrics["reduction_vs_touched_files_pct"],
                "saved_vs_repo_pct": metrics["reduction_vs_full_repo_pct"],
                "regions": len(result["regions"]),
                "files_in_slice": metrics["files_in_slice"],
            }
        )

    valid = [row for row in rows if "slice_tokens" in row and row["touched_files_tokens"]]
    aggregate = {
        "mean_saved_vs_files_pct": round(
            statistics.mean(row["saved_vs_files_pct"] for row in valid), 2
        )
        if valid
        else 0.0,
        "mean_saved_vs_repo_pct": round(
            statistics.mean(row["saved_vs_repo_pct"] for row in valid), 2
        )
        if valid
        else 0.0,
    }

    report = {
        "repo": str(repo),
        "index": {"cold": cold, "warm": warm},
        "refresh": refresh,
        "slices": rows,
        "aggregate": aggregate,
        "cycles": engine.find_circular_dependencies()["symbol_cycle_count"],
    }
    engine.close()

    if as_json:
        print(json.dumps(report, indent=2))
        return report

    print("=" * 92)
    print("POLYGLOT AST CALL-GRAPH SLICER — BENCHMARK")
    print("=" * 92)
    print(f"Repository      : {repo}")
    print(
        f"Cold index      : {cold['elapsed_ms']:>8.1f} ms  "
        f"({cold['files_indexed']} files, {cold['total_symbols']} symbols, "
        f"{cold['total_edges']} edges)"
    )
    print(
        f"Warm index      : {warm['elapsed_ms']:>8.1f} ms  "
        f"({warm['cache_hits']} cache hits, {warm['cache_misses']} misses)"
    )
    print(
        f"Refresh latency : {refresh['p50_ms']:>8.2f} ms p50  "
        f"{refresh['p95_ms']:>6.2f} ms p95  over {refresh['samples']} files"
    )
    print("-" * 92)
    print(
        f"{'Symbol':<28}{'Files':>10}{'Repo':>10}{'Slice':>10}"
        f"{'vs Files':>12}{'vs Repo':>10}"
    )
    print("-" * 92)
    for row in rows:
        if "error" in row:
            print(f"{row['symbol']:<28}  ERROR: {row['error']}")
            continue
        print(
            f"{row['qualified_name'][:27]:<28}"
            f"{row['touched_files_tokens']:>10}"
            f"{row['full_repo_tokens']:>10}"
            f"{row['slice_tokens']:>10}"
            f"{row['saved_vs_files_pct']:>11.1f}%"
            f"{row['saved_vs_repo_pct']:>9.1f}%"
        )
    print("-" * 92)
    print(
        f"Mean reduction  : {aggregate['mean_saved_vs_files_pct']:.1f}% vs touched files, "
        f"{aggregate['mean_saved_vs_repo_pct']:.1f}% vs whole repo"
    )
    print(f"Mutual recursion cycles detected: {report['cycles']}")
    print("=" * 92)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark polyglot-ast-slicer")
    parser.add_argument("--repo", default=None, help="Existing checkout (default: synthetic)")
    parser.add_argument("--symbol", action="append", default=[], help="Symbol to slice")
    parser.add_argument("--hops", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.repo:
        run(Path(args.repo).resolve(), args.symbol or ["main"], args.hops, args.json)
        return

    with tempfile.TemporaryDirectory(prefix="ast-slicer-bench-") as directory:
        root = Path(directory)
        populate_synthetic_repo(root)
        run(
            root,
            args.symbol or ["process_payment", "handle_checkout_webhook", "submitOrderCheckout"],
            args.hops,
            args.json,
        )


if __name__ == "__main__":
    main()
