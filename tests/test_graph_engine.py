"""Graph engine: resolution accuracy, slicing, blast radius, cycles, and incrementality."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from polyglot_ast_slicer.graph_engine import GraphEngine
from polyglot_ast_slicer.parser import read_byte_slice


@pytest.fixture()
def engine(repo: Path) -> GraphEngine:
    instance = GraphEngine(repo)
    instance.index_repository(force_rebuild=True)
    yield instance
    instance.close()


def _callee_files(result: dict, name: str) -> set[str]:
    return {
        item["file_path"]
        for item in result["downstream_callees"]
        if item["qualified_name"] == name
    }


def test_import_aware_resolution_disambiguates_duplicate_names(engine: GraphEngine) -> None:
    result = engine.get_ast_call_slice(
        "BillingEngine.process_invoice", hops=1, include_metadata=True
    )
    assert _callee_files(result, "calculate_tax") == {"pkg/tax_us.py"}
    assert "pkg/tax_eu.py" not in _callee_files(result, "calculate_tax")


def test_self_method_and_constructor_edges(engine: GraphEngine) -> None:
    result = engine.get_ast_call_slice(
        "BillingEngine.process_invoice", hops=1, include_metadata=True
    )
    assert _callee_files(result, "BillingEngine.format_receipt") == {"pkg/billing.py"}

    api = engine.get_ast_call_slice("api_create_invoice", hops=1, include_metadata=True)
    assert "BillingEngine" in {item["qualified_name"] for item in api["downstream_callees"]}
    assert "BillingEngine.process_invoice" in {
        item["qualified_name"] for item in api["downstream_callees"]
    }


def test_no_repo_wide_false_edges(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "def helper() -> int:\n    return 1\n\ndef caller() -> int:\n    return helper()\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text("def helper() -> int:\n    return 2\n", encoding="utf-8")

    engine = GraphEngine(tmp_path)
    engine.index_repository(force_rebuild=True)
    result = engine.get_ast_call_slice("caller", hops=1, include_metadata=True)

    files = _callee_files(result, "helper")
    assert files == {"a.py"}, "unrelated same-named symbol in b.py must not be linked"
    engine.close()


def test_ambiguous_symbol_lists_candidates_and_never_guesses(engine: GraphEngine) -> None:
    result = engine.get_ast_call_slice("calculate_tax", hops=1)
    assert result["code"] == "ambiguous_symbol"
    assert len(result["candidates"]) == 2
    assert {c["file_path"] for c in result["candidates"]} == {"pkg/tax_us.py", "pkg/tax_eu.py"}

    qualified = engine.get_ast_call_slice(
        "pkg/tax_us.py::calculate_tax", hops=1, include_metadata=True
    )
    assert qualified["target"]["file_path"] == "pkg/tax_us.py"


def test_module_alias_receiver_resolves(tmp_path: Path) -> None:
    (tmp_path / "tax.py").write_text(
        "def compute(x: int) -> int:\n    return x * 2\n", encoding="utf-8"
    )
    (tmp_path / "main.py").write_text(
        "import tax as t\n\ndef run(x: int) -> int:\n    return t.compute(x)\n",
        encoding="utf-8",
    )
    engine = GraphEngine(tmp_path)
    engine.index_repository(force_rebuild=True)
    result = engine.get_ast_call_slice("run", hops=1, include_metadata=True)
    assert _callee_files(result, "compute") == {"tax.py"}
    engine.close()


def test_inherited_method_resolution(tmp_path: Path) -> None:
    (tmp_path / "shapes.py").write_text(
        textwrap.dedent(
            """
            class Base:
                def service(self) -> str:
                    return "base"

            class Child(Base):
                def run(self) -> str:
                    return self.service()
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    engine = GraphEngine(tmp_path)
    engine.index_repository(force_rebuild=True)
    result = engine.get_ast_call_slice("Child.run", hops=1, include_metadata=True)
    assert "Base.service" in {item["qualified_name"] for item in result["downstream_callees"]}
    engine.close()


def test_slice_merges_overlapping_regions(engine: GraphEngine) -> None:
    result = engine.get_ast_call_slice("api_create_invoice", hops=1, include_metadata=True)
    context = result["compiled_context"]

    assert context.count("def process_invoice(") == 1
    assert context.count("def format_receipt(") == 1
    billing_regions = [r for r in result["regions"] if r["file_path"] == "pkg/billing.py"]
    spans = [(r["byte_range"][0], r["byte_range"][1]) for r in billing_regions]
    assert spans, "expected at least one merged region"
    for (_, first_end), (second_start, _) in zip(spans, spans[1:], strict=False):
        assert first_end <= second_start


def test_slice_hop_distance_limits_expansion(engine: GraphEngine) -> None:
    one_hop = engine.get_ast_call_slice(
        "api_create_invoice", hops=1, include_types=False, include_metadata=True
    )
    two_hops = engine.get_ast_call_slice(
        "api_create_invoice", hops=2, include_types=False, include_metadata=True
    )

    assert {item["hop_distance"] for item in one_hop["downstream_callees"]} == {1}
    assert max(item["hop_distance"] for item in two_hops["downstream_callees"]) == 2


def test_slice_type_schemas_are_attached_and_toggleable(engine: GraphEngine) -> None:
    with_types = engine.get_ast_call_slice(
        "BillingEngine.process_invoice", hops=1, include_metadata=True
    )
    without = engine.get_ast_call_slice(
        "BillingEngine.process_invoice", hops=1, include_types=False, include_metadata=True)
    assert "InvoicePayload" in {
        item["qualified_name"] for item in with_types["relevant_types_and_schemas"]
    }
    assert without["relevant_types_and_schemas"] == []


def test_default_payload_budget_is_enforced(engine: GraphEngine) -> None:
    default = engine.get_ast_call_slice("api_create_invoice", hops=2)
    assert default["token_metrics"]["payload_tokens"] <= 6000
    assert default["token_metrics"]["budget_tokens"] == 6000
    assert set(default) == {"compiled_context", "token_metrics", "diagnostics"}

    tight = engine.get_ast_call_slice("api_create_invoice", hops=2, max_tokens=400)
    assert tight["token_metrics"]["payload_tokens"] <= 400
    assert tight["token_metrics"]["slice_tokens"] <= 400


def test_relative_symbol_ids_are_accepted(engine: GraphEngine) -> None:
    result = engine.get_ast_call_slice(
        "pkg/tax_us.py::calculate_tax", hops=1, include_metadata=True
    )
    assert result["target"]["qualified_name"] == "calculate_tax"
    assert result["target"]["file_path"] == "pkg/tax_us.py"


def test_symbol_snippet_is_byte_exact(engine: GraphEngine, repo: Path) -> None:
    symbol = engine.resolve_query_symbol("BillingEngine.process_invoice")
    assert symbol is not None
    code = read_byte_slice(symbol.file_path, symbol.start_byte, symbol.end_byte)
    assert code.startswith("def process_invoice(")
    assert code.rstrip().endswith("subtotal_cents + tax)")
    assert symbol.end_line - symbol.start_line + 1 == len(code.rstrip("\n").splitlines())


def test_slice_metadata_only_mode(engine: GraphEngine) -> None:
    result = engine.get_ast_call_slice(
        "api_create_invoice", hops=1, include_code=False, include_metadata=True
    )
    assert result["compiled_context"] == ""
    assert result["token_metrics"]["slice_tokens"] == 0
    assert result["regions"]
    assert result["downstream_callees"]


def test_slice_token_metrics_are_internally_consistent(engine: GraphEngine) -> None:
    result = engine.get_ast_call_slice(
        "BillingEngine.process_invoice", hops=2, include_metadata=True
    )
    metrics = result["token_metrics"]
    assert metrics["slice_tokens"] > 0
    assert metrics["full_repo_tokens"] == engine.get_index_stats()["total_source_tokens"]
    assert 0 <= metrics["reduction_vs_full_repo_pct"] <= 100
    assert metrics["files_in_slice"] >= 1


def test_missing_symbol_returns_guidance(engine: GraphEngine) -> None:
    result = engine.get_ast_call_slice("does_not_exist", hops=1, include_metadata=True)
    assert "error" in result
    assert "hint" in result


def test_blast_radius_groups_by_hop(engine: GraphEngine) -> None:
    result = engine.get_blast_radius("pkg/tax_us.py::calculate_tax", max_depth=3)
    assert result["total_impacted_symbols"] == 2
    assert [item["qualified_name"] for item in result["dependents_by_hop"][1]] == [
        "BillingEngine.process_invoice"
    ]
    assert [item["qualified_name"] for item in result["dependents_by_hop"][2]] == [
        "api_create_invoice"
    ]
    assert result["risk_level"] in {"LOW", "MODERATE", "HIGH", "CRITICAL"}


def test_symbol_and_import_cycle_detection(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "from b import thing_b\n\ndef thing_a() -> int:\n    return thing_b()\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "from a import thing_a\n\ndef thing_b() -> int:\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "rec.py").write_text(
        "def ping(n: int) -> int:\n    return pong(n - 1)\n\n"
        "def pong(n: int) -> int:\n    return ping(n - 1)\n",
        encoding="utf-8",
    )

    engine = GraphEngine(tmp_path)
    engine.index_repository(force_rebuild=True)
    cycles = engine.find_circular_dependencies()

    symbol_members = {node["qualified_name"] for cycle in cycles["symbol_cycles"] for node in cycle}
    assert {"ping", "pong"} <= symbol_members
    assert any(set(cycle) == {"a.py", "b.py"} for cycle in cycles["file_cycles"])
    engine.close()


def test_search_symbols_ranks_exact_matches_first(engine: GraphEngine) -> None:
    result = engine.search_symbols("calculate_tax")
    names = [item["qualified_name"] for item in result["results"]]
    assert names[:2] == ["calculate_tax", "calculate_tax"]

    classes = engine.search_symbols("engine", kind="class")
    assert [item["qualified_name"] for item in classes["results"]] == ["BillingEngine"]


def test_gitignore_and_size_limits_are_honoured(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("def kept() -> int:\n    return 1\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("generated/\n*.gen.py\n", encoding="utf-8")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "skip.py").write_text("def skipped() -> int:\n    return 1\n")
    (tmp_path / "thing.gen.py").write_text("def generated() -> int:\n    return 1\n")
    (tmp_path / "huge.py").write_text("def huge() -> int:\n    return 1\n" + "# pad\n" * 100)

    engine = GraphEngine(tmp_path, max_file_bytes=400)
    stats = engine.index_repository(force_rebuild=True)
    assert stats["files_indexed"] == 1
    assert engine.resolve_query_symbol("kept") is not None
    assert engine.resolve_query_symbol("skipped") is None
    engine.close()


def test_incremental_refresh_repoints_edges_under_15ms(engine: GraphEngine, repo: Path) -> None:
    target = repo / "pkg" / "billing.py"
    original = target.read_text(encoding="utf-8")
    target.write_text(
        original.replace("from pkg.tax_us import", "from pkg.tax_eu import"), encoding="utf-8"
    )

    result = engine.refresh_file(target)
    assert result["status"] == "updated"
    assert result["cache_hit"] is False
    assert result["elapsed_ms"] < 15.0, f"refresh took {result['elapsed_ms']}ms"

    slice_data = engine.get_ast_call_slice(
        "BillingEngine.process_invoice", hops=1, include_metadata=True
    )
    assert _callee_files(slice_data, "calculate_tax") == {"pkg/tax_eu.py"}

    target.write_text(original, encoding="utf-8")
    engine.refresh_file(target)


def test_deleted_file_is_pruned(engine: GraphEngine, repo: Path) -> None:
    target = repo / "pkg" / "cycles.py"
    target.unlink()
    result = engine.refresh_file(target)
    assert result["status"] == "deleted"
    assert engine.resolve_query_symbol("ping_alpha") is None


def test_reindex_drops_stale_files(engine: GraphEngine, repo: Path) -> None:
    (repo / "pkg" / "cycles.py").unlink()
    stats = engine.index_repository(force_rebuild=False)
    assert stats["files_indexed"] == 5
    assert engine.resolve_query_symbol("ping_alpha") is None


def test_indexing_is_deterministic(repo: Path) -> None:
    first = GraphEngine(repo)
    first.index_repository(force_rebuild=True)
    payload_one = json.dumps(
        first.get_ast_call_slice("api_create_invoice", hops=2, include_metadata=True),
        sort_keys=True,
        default=str,
    )
    first.close()

    second = GraphEngine(repo)
    second.index_repository(force_rebuild=True)
    payload_two = json.dumps(
        second.get_ast_call_slice("api_create_invoice", hops=2, include_metadata=True),
        sort_keys=True,
        default=str,
    )
    second.close()

    assert payload_one == payload_two


def test_stats_report_languages_and_cache(engine: GraphEngine) -> None:
    stats = engine.get_index_stats()
    assert stats["files_indexed"] == 6
    assert stats["languages"] == {"python": 9, "typescript": 3}
    assert stats["cache"]["schema_version"] == 2
    assert stats["total_source_tokens"] > 0
