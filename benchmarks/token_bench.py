# benchmarks/token_bench.py
from __future__ import annotations

import argparse
import sys
import tempfile
import textwrap
from pathlib import Path

# Ensure root package is importable when running `uv run python benchmarks/token_bench.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.graph_engine import GraphEngine


def _populate_synthetic_polyglot_repo(root: Path) -> None:
    """Creates a realistic multi-module Python + TypeScript repository for deterministic benchmarking."""
    py_dir = root / "backend"
    ts_dir = root / "frontend"
    py_dir.mkdir(parents=True, exist_ok=True)
    ts_dir.mkdir(parents=True, exist_ok=True)

    # 1. Python Schemas & Models + unrelated domain classes (simulating large file bloat)
    noise_classes = "\n\n".join(
        textwrap.dedent(
            f"""
            class LegacyAuditRecord_{i}:
                \"\"\"Unrelated domain model #{i} that bloats full-file context windows.\"\"\"
                def __init__(self, record_id: str, payload: dict[str, str]) -> None:
                    self.record_id = record_id
                    self.payload = payload

                def serialize_record_{i}(self) -> str:
                    return f"{{self.record_id}}:{{len(self.payload)}}:{{','.join(self.payload.keys())}}"
            """
        ).strip()
        for i in range(1, 25)
    )

    (py_dir / "schemas.py").write_text(
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
        + noise_classes
        + "\n",
        encoding="utf-8",
    )

    # 2. Python Downstream Helpers + Circular SCC loop + noise functions
    noise_helpers = "\n\n".join(
        textwrap.dedent(
            f"""
            def generate_monthly_report_chunk_{i}(rows: list[dict[str, str]]) -> str:
                \"\"\"Unrelated batch reporting function #{i} that should never enter payment slice.\"\"\"
                buffer = []
                for idx, row in enumerate(rows):
                    buffer.append(f"row_{{idx}}={{row.get('id', 'none')}}")
                return "\\n".join(buffer)
            """
        ).strip()
        for i in range(1, 25)
    )

    (py_dir / "ledger.py").write_text(
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

            # Intentional mutual recursion cycle for Tarjan's SCC verification
            def reconcile_node_alpha(batch_id: str) -> str:
                return reconcile_node_beta(batch_id)

            def reconcile_node_beta(batch_id: str) -> str:
                return reconcile_node_alpha(batch_id)
            """
        ).strip()
        + "\n\n"
        + noise_helpers
        + "\n",
        encoding="utf-8",
    )

    # 3. Python Core Service + Upstream API Controller
    (py_dir / "payment_service.py").write_text(
        textwrap.dedent(
            """
            from backend.schemas import PaymentRequest, PaymentReceipt
            from backend.ledger import validate_fraud_rules, compute_platform_fee, persist_ledger_entry

            def process_checkout_payment(req: PaymentRequest) -> PaymentReceipt:
                if not validate_fraud_rules(req):
                    raise ValueError("Fraud check failed for request")
                fee = compute_platform_fee(req.amount_cents)
                return persist_ledger_entry(req, fee)

            def handle_checkout_webhook(order_id: str, user_id: str, amount_cents: int) -> PaymentReceipt:
                request = PaymentRequest(
                    order_id=order_id,
                    user_id=user_id,
                    amount_cents=amount_cents,
                    currency="USD",
                )
                return process_checkout_payment(request)
            """
        ).strip()
        + "\n\n"
        + noise_helpers
        + "\n",
        encoding="utf-8",
    )

    # 4. TypeScript Frontend Checkout Module
    ts_noise = "\n\n".join(
        textwrap.dedent(
            f"""
            export function renderAnalyticsChartWidget_{i}(dataPoints: number[]): string {{
                const total = dataPoints.reduce((acc, v) => acc + v, 0);
                return `<div class="chart-{i}">Total: ${{total}}</div>`;
            }}
            """
        ).strip()
        for i in range(1, 20)
    )

    (ts_dir / "checkout_client.ts").write_text(
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

            export async function submitOrderCheckout(payload: CheckoutPayload): Promise<string> {
                const clean = sanitizeCheckoutPayload(payload);
                return `submitted:${clean.orderId}:${clean.amountCents}`;
            }

            export async function onCheckoutButtonClick(orderId: string, userId: string): Promise<string> {
                return await submitOrderCheckout({ orderId, userId, amountCents: 4999 });
            }
            """
        ).strip()
        + "\n\n"
        + ts_noise
        + "\n",
        encoding="utf-8",
    )


def run_benchmark(repo_path: Path, target_symbols: list[str], hops: int = 1) -> None:
    engine = GraphEngine(repo_path)
    idx_stats = engine.index_repository(force_rebuild=True)

    print("=" * 84)
    print("POLYGLOT AST CALL-GRAPH SLICER - BENCHMARK REPORT")
    print("=" * 84)
    print(f"Repository Path : {idx_stats['repo_path']}")
    print(
        f"Files Indexed   : {idx_stats['files_indexed']} | "
        f"Symbols: {idx_stats['total_symbols']} | "
        f"Call/Type Edges: {idx_stats['total_edges']} | "
        f"Cold Index: {idx_stats['elapsed_ms']} ms"
    )

    # Incremental single-file modification benchmark
    sample_file = engine.discover_source_files()[0]
    original_content = sample_file.read_text(encoding="utf-8")
    sample_file.write_text(original_content + "\n# incremental save trigger\n", encoding="utf-8")
    splice_stats = engine.refresh_file(sample_file)
    sample_file.write_text(original_content, encoding="utf-8")
    engine.refresh_file(sample_file)

    print(
        f"Incremental Splice Latency (1 modified file): {splice_stats['elapsed_ms']} ms "
        f"(Target SLA: under 15.0 ms)"
    )
    print("-" * 84)
    print(
        f"{'Target Symbol':<28} | {'Full Files':>12} | {'Full Repo':>11} | "
        f"{f'{hops}-Hop Slice':>12} | {'Saved vs Files':>14}"
    )
    print("-" * 84)

    for sym in target_symbols:
        res = engine.get_ast_call_slice(sym, hops=hops, include_types=True)
        if "error" in res:
            print(f"{sym:<28} | ERROR: {res['error']}")
            continue
        metrics = res["token_metrics"]
        print(
            f"{sym:<28} | "
            f"{metrics['touched_full_files_tokens']:>9} tk | "
            f"{metrics['full_repo_tokens']:>8} tk | "
            f"{metrics['slice_tokens']:>9} tk | "
            f"{metrics['reduction_vs_touched_files_pct']:>12.2f}%"
        )

    print("-" * 84)
    cycles = engine.find_circular_dependencies()
    print(f"Circular Dependency SCCs Detected: {cycles['symbol_cycle_count']}")
    for idx, cyc in enumerate(cycles["symbol_cycles"], start=1):
        names = " <-> ".join(item["qualified_name"] for item in cyc)
        print(f"  Cycle #{idx}: {names}")
    print("=" * 84)


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Polyglot AST Call-Graph Slicer")
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Path to an existing repo (if omitted, runs on a synthetic polyglot repo)",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Symbol name(s) to slice (can be passed multiple times)",
    )
    parser.add_argument("--hops", type=int, default=1, help="BFS hop radius (default: 1)")
    args = parser.parse_args()

    if args.repo:
        repo_path = Path(args.repo).resolve()
        symbols = args.symbol or ["main"]
        run_benchmark(repo_path, symbols, hops=args.hops)
    else:
        with tempfile.TemporaryDirectory(prefix="polyglot_ast_bench_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            _populate_synthetic_polyglot_repo(tmp_path)
            default_symbols = ["process_checkout_payment", "submitOrderCheckout"]
            run_benchmark(tmp_path, args.symbol or default_symbols, hops=args.hops)


if __name__ == "__main__":
    main()