"""Shared fixtures: a small but deliberately tricky polyglot repository."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

FILES: dict[str, str] = {
    "pkg/models.py": """
        from dataclasses import dataclass


        @dataclass
        class InvoicePayload:
            invoice_id: str
            subtotal_cents: int
    """,
    "pkg/tax_us.py": """
        from pkg.models import InvoicePayload


        def calculate_tax(payload: InvoicePayload) -> int:
            return int(payload.subtotal_cents * 0.08)
    """,
    "pkg/tax_eu.py": """
        from pkg.models import InvoicePayload


        def calculate_tax(payload: InvoicePayload) -> int:
            return int(payload.subtotal_cents * 0.20)
    """,
    "pkg/billing.py": """
        from pkg.models import InvoicePayload
        from pkg.tax_us import calculate_tax


        class BillingEngine:
            def format_receipt(self, total_cents: int) -> str:
                return f"TOTAL:{total_cents}"

            def process_invoice(self, payload: InvoicePayload) -> str:
                tax = calculate_tax(payload)
                return self.format_receipt(payload.subtotal_cents + tax)


        def api_create_invoice(invoice_id: str, subtotal_cents: int) -> str:
            engine = BillingEngine()
            payload = InvoicePayload(invoice_id=invoice_id, subtotal_cents=subtotal_cents)
            return engine.process_invoice(payload)
    """,
    "pkg/cycles.py": """
        def ping_alpha(n: int) -> int:
            return pong_beta(n - 1) if n > 0 else 0


        def pong_beta(n: int) -> int:
            return ping_alpha(n - 1)
    """,
    "web/cart.ts": """
        export interface CartItem {
            sku: string;
            priceCents: number;
        }

        export const sanitizeItem = (item: CartItem): CartItem => {
            return { sku: item.sku.trim(), priceCents: Math.max(0, item.priceCents) };
        };

        export function checkoutCart(item: CartItem): CartItem {
            return sanitizeItem(item);
        }
    """,
}


def write_repo(root: Path, files: dict[str, str] | None = None) -> Path:
    for relative, content in (files or FILES).items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")
    return root


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return write_repo(tmp_path / "sample")
