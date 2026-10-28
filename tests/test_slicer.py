# tests/test_slicer.py
from __future__ import annotations

import os
import textwrap
import time
from pathlib import Path

import pytest

from src.graph_engine import GraphEngine
from src.parser import read_byte_slice


@pytest.fixture()
def polyglot_repo(tmp_path: Path) -> Path:
    """Builds an isolated Python + TypeScript repository for deterministic AST testing."""
    pkg = tmp_path / "pkg"
    web = tmp_path / "web"
    pkg.mkdir(parents=True)
    web.mkdir(parents=True)

    # 1. Shared Data Schema
    (pkg / "models.py").write_text(
        textwrap.dedent(
            """
            from dataclasses import dataclass

            @dataclass
            class InvoicePayload:
                invoice_id: str
                subtotal_cents: int
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    # 2. Two modules exporting identically named functions (`calculate_tax`)
    (pkg / "tax_us.py").write_text(
        textwrap.dedent(
            """
            from pkg.models import InvoicePayload

            def calculate_tax(payload: InvoicePayload) -> int:
                return int(payload.subtotal_cents * 0.08)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    (pkg / "tax_eu.py").write_text(
        textwrap.dedent(
            """
            from pkg.models import InvoicePayload

            def calculate_tax(payload: InvoicePayload) -> int:
                return int(payload.subtotal_cents * 0.20)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    # 3. Caller explicitly importing `pkg.tax_us` (tests Tier 2 import-aware resolution)
    (pkg / "billing.py").write_text(
        textwrap.dedent(
            """
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
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    # 4. Mutual recursion cycle for Tarjan's SCC verification
    (pkg / "cycles.py").write_text(
        textwrap.dedent(
            """
            def ping_alpha(n: int) -> int:
                return pong_beta(n - 1) if n > 0 else 0

            def pong_beta(n: int) -> int:
                return ping_alpha(n - 1)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    # 5. TypeScript module with interface, arrow function, and exported function
    (web / "cart.ts").write_text(
        textwrap.dedent(
            """
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
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    return tmp_path


def test_polyglot_parsing_and_exact_byte_ranges(polyglot_repo: Path) -> None:
    """Verifies Python and TypeScript AST symbols map to exact byte offsets on disk."""
    engine = GraphEngine(polyglot_repo)
    stats = engine.index_repository(force_rebuild=True)

    assert stats["files_indexed"] == 6
    assert stats["total_symbols"] >= 10

    # Verify Python method byte slice starts with 'def process_invoice'
    py_sym = engine.resolve_query_symbol("BillingEngine.process_invoice")
    assert py_sym is not None
    py_code = read_byte_slice(py_sym.file_path, py_sym.start_byte, py_sym.end_byte)
    assert py_code.startswith("def process_invoice(")
    assert "calculate_tax(payload)" in py_code

    # Verify TypeScript arrow function and interface extraction
    ts_fn = engine.resolve_query_symbol("sanitizeItem")
    ts_iface = engine.resolve_query_symbol("CartItem")
    assert ts_fn is not None and ts_fn.language == "typescript"
    assert ts_iface is not None and ts_iface.kind == "interface"


def test_import_aware_call_resolution_disambiguates_duplicate_names(
    polyglot_repo: Path,
) -> None:
    """
    Ensures `BillingEngine.process_invoice` resolves `calculate_tax` to `pkg/tax_us.py`
    (which is imported) and NOT `pkg/tax_eu.py`.
    """
    engine = GraphEngine(polyglot_repo)
    engine.index_repository(force_rebuild=True)

    slice_data = engine.get_ast_call_slice("BillingEngine.process_invoice", hops=1)
    callees = slice_data["downstream_callees"]
    callee_files = {c["file_path"] for c in callees if c["qualified_name"] == "calculate_tax"}

    assert callee_files == {str(Path("pkg/tax_us.py"))}
    assert str(Path("pkg/tax_eu.py")) not in callee_files

    # Verify data schema `InvoicePayload` is automatically attached via `uses_type` edge
    schema_names = {s["qualified_name"] for s in slice_data["relevant_types_and_schemas"]}
    assert "InvoicePayload" in schema_names


def test_byte_range_overlap_deduplication(polyglot_repo: Path) -> None:
    """
    Verifies that when both an enclosing class (`BillingEngine`) and its nested methods
    (`BillingEngine.process_invoice`, `BillingEngine.format_receipt`) are present in a slice,
    the compiled context deduplicates the nested byte ranges so code is never emitted twice.
    """
    engine = GraphEngine(polyglot_repo)
    engine.index_repository(force_rebuild=True)

    # `api_create_invoice` instantiates `BillingEngine` (class span) AND calls `process_invoice` (method span)
    slice_data = engine.get_ast_call_slice("api_create_invoice", hops=1)
    compiled_context = slice_data["compiled_context"]

    # `def process_invoice` is inside `class BillingEngine:` byte range; it must appear exactly once
    assert compiled_context.count("def process_invoice(") == 1
    assert compiled_context.count("def format_receipt(") == 1


def test_two_tier_sqlite_cache_and_sub_15ms_incremental_splicing(
    polyglot_repo: Path,
) -> None:
    """
    Tests:
    1. Cold index -> cache miss, warm index -> 100% cache hit.
    2. Tier-2 SHA-256 fast-path: touching mtime without changing file bytes yields a cache hit.
    3. Modifying an import dynamically splices the new call edge into NetworkX in < 15ms.
    4. Deleting a file removes its symbols from both SQLite and the DiGraph.
    """
    engine = GraphEngine(polyglot_repo)

    # 1. Cold build vs Warm build
    cold_stats = engine.index_repository(force_rebuild=True)
    assert cold_stats["cache_misses"] == 6
    assert cold_stats["cache_hits"] == 0

    warm_stats = engine.index_repository(force_rebuild=False)
    assert warm_stats["cache_hits"] == 6
    assert warm_stats["cache_misses"] == 0

    # 2. Bump mtime_ns on billing.py without modifying content -> Tier-2 SHA-256 cache hit
    billing_file = polyglot_repo / "pkg" / "billing.py"
    future_ns = time.time_ns() + 5_000_000_000
    os.utime(billing_file, ns=(future_ns, future_ns))

    touch_res = engine.cache.get_or_parse_file(billing_file, engine.parser, force_reparse=False)
    assert touch_res.cache_hit is True

    # 3. Live edit: switch billing.py import from `pkg.tax_us` to `pkg.tax_eu`
    updated_code = billing_file.read_text(encoding="utf-8").replace(
        "from pkg.tax_us import calculate_tax",
        "from pkg.tax_eu import calculate_tax",
    )
    billing_file.write_text(updated_code, encoding="utf-8")

    splice_res = engine.refresh_file(billing_file)
    assert splice_res["status"] == "updated"
    assert splice_res["cache_hit"] is False
    assert splice_res["elapsed_ms"] < 15.0, f"Expected < 15ms, got {splice_res['elapsed_ms']}ms"

    # Verify the call graph dynamically re-pointed to `pkg/tax_eu.py`
    new_slice = engine.get_ast_call_slice("BillingEngine.process_invoice", hops=1)
    new_callee_files = {
        c["file_path"] for c in new_slice["downstream_callees"] if c["qualified_name"] == "calculate_tax"
    }
    assert new_callee_files == {str(Path("pkg/tax_eu.py"))}

    # 4. File deletion pruning
    cycles_file = polyglot_repo / "pkg" / "cycles.py"
    cycles_file.unlink()
    del_res = engine.refresh_file(cycles_file)
    assert del_res["status"] == "deleted"
    assert engine.resolve_query_symbol("ping_alpha") is None


def test_blast_radius_and_tarjan_circular_dependencies(polyglot_repo: Path) -> None:
    """Verifies multi-hop upstream blast radius and Tarjan's SCC cycle detection."""
    engine = GraphEngine(polyglot_repo)
    engine.index_repository(force_rebuild=True)

    # 1. Upstream blast radius for `pkg/tax_us.py::calculate_tax`
    us_tax_id = f"{(polyglot_repo / 'pkg' / 'tax_us.py').resolve()}::calculate_tax"
    blast = engine.get_blast_radius(us_tax_id, max_depth=3)

    assert blast["total_impacted_symbols"] == 2
    hop_1_names = {d["qualified_name"] for d in blast["dependents_by_hop"][1]}
    hop_2_names = {d["qualified_name"] for d in blast["dependents_by_hop"][2]}
    assert "BillingEngine.process_invoice" in hop_1_names
    assert "api_create_invoice" in hop_2_names

    # 2. Tarjan's Strongly Connected Components (SCC) cycle detection
    cycles = engine.find_circular_dependencies()
    assert cycles["symbol_cycle_count"] == 1
    cycle_members = {node["qualified_name"] for node in cycles["symbol_cycles"][0]}
    assert cycle_members == {"ping_alpha", "pong_beta"}