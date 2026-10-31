"""End-to-end integration: the workflow from the README, on a bloated repository."""

from __future__ import annotations

import textwrap
from pathlib import Path

from polyglot_ast_slicer.graph_engine import GraphEngine


def _build_bloated_repo(root: Path) -> Path:
    noise = "\n\n".join(
        textwrap.dedent(
            f"""
            def legacy_report_{index}(rows: list[dict[str, str]]) -> str:
                buffer = []
                for row in rows:
                    buffer.append(f"{{row.get('id', 'none')}}")
                return "\\n".join(buffer)
            """
        ).strip()
        for index in range(60)
    )

    (root / "backend").mkdir(parents=True)
    (root / "backend" / "schemas.py").write_text(
        textwrap.dedent(
            """
            from dataclasses import dataclass

            @dataclass
            class PaymentRequest:
                order_id: str
                amount_cents: int
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (root / "backend" / "ledger.py").write_text(
        textwrap.dedent(
            """
            from backend.schemas import PaymentRequest

            def validate_fraud_rules(req: PaymentRequest) -> bool:
                return req.amount_cents > 0

            def compute_platform_fee(amount_cents: int) -> int:
                return int(amount_cents * 0.029) + 30
            """
        ).strip()
        + "\n\n"
        + noise
        + "\n",
        encoding="utf-8",
    )
    (root / "backend" / "service.py").write_text(
        textwrap.dedent(
            """
            from backend.schemas import PaymentRequest
            from backend.ledger import compute_platform_fee, validate_fraud_rules

            def process_payment(req: PaymentRequest) -> int:
                if not validate_fraud_rules(req):
                    raise ValueError("fraud")
                return compute_platform_fee(req.amount_cents)

            def webhook(order_id: str, amount_cents: int) -> int:
                return process_payment(PaymentRequest(order_id, amount_cents))
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return root


def test_realistic_slice_is_a_fraction_of_the_touched_files(tmp_path: Path) -> None:
    repo = _build_bloated_repo(tmp_path / "repo")
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)

    result = engine.get_ast_call_slice("process_payment", hops=1, include_metadata=True)
    metrics = result["token_metrics"]

    assert metrics["slice_tokens"] < metrics["touched_files_tokens"]
    assert metrics["reduction_vs_touched_files_pct"] > 80.0
    assert metrics["reduction_vs_full_repo_pct"] > 80.0

    # Everything needed to reason about the slice is present, and nothing else is.
    context = result["compiled_context"]
    assert "def process_payment(" in context
    assert "def compute_platform_fee(" in context
    assert "def validate_fraud_rules(" in context
    assert "legacy_report_0(" not in context
    engine.close()


def test_slice_covers_the_whole_call_chain_at_two_hops(tmp_path: Path) -> None:
    repo = _build_bloated_repo(tmp_path / "repo")
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)

    result = engine.get_ast_call_slice("webhook", hops=2, include_metadata=True)
    downstream = {item["qualified_name"] for item in result["downstream_callees"]}
    upstream = {item["qualified_name"] for item in result["upstream_callers"]}

    assert "process_payment" in downstream
    assert "compute_platform_fee" in downstream
    assert upstream == set()
    engine.close()
