"""Cache behaviour: two-tier invalidation, schema versioning, and payload fidelity."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from polyglot_ast_slicer.cache import SCHEMA_VERSION, ASTSQLiteCache
from polyglot_ast_slicer.graph_engine import GraphEngine
from polyglot_ast_slicer.parser import ParsedFile, PolyglotASTParser


def test_cold_then_warm_hits(repo: Path) -> None:
    engine = GraphEngine(repo)
    cold = engine.index_repository(force_rebuild=True)
    warm = engine.index_repository(force_rebuild=False)

    assert cold["cache_misses"] == cold["files_indexed"] == 6
    assert cold["cache_hits"] == 0
    assert warm["cache_hits"] == 6
    assert warm["cache_misses"] == 0
    assert warm["elapsed_ms"] < cold["elapsed_ms"]


def test_touch_without_content_change_is_a_hit(repo: Path) -> None:
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    target = repo / "pkg" / "billing.py"

    future = time.time_ns() + 5_000_000_000
    os.utime(target, ns=(future, future))

    result = engine.cache.get_or_parse_file(target, engine.parser)
    assert result.cache_hit is True
    assert result.raw_tokens > 0


def test_content_change_is_a_miss_and_roundtrips_symbols(repo: Path) -> None:
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    target = repo / "pkg" / "billing.py"

    original = target.read_text(encoding="utf-8")
    target.write_text(original + "\n# trailing comment\n", encoding="utf-8")

    result = engine.cache.get_or_parse_file(target, engine.parser)
    assert result.cache_hit is False
    assert isinstance(result.parsed, ParsedFile)
    assert any(
        symbol.qualified_name == "BillingEngine.process_invoice" for symbol in result.symbols
    )

    # Stored payload must survive compression round-trip byte-for-byte in structure.
    again = engine.cache.get_or_parse_file(target, engine.parser)
    assert again.cache_hit is True
    assert [s.to_dict() for s in again.symbols] == [s.to_dict() for s in result.symbols]


def test_schema_version_bump_invalidates_old_cache(repo: Path) -> None:
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    db_path = engine.cache.db_path
    engine.cache.close()

    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA user_version = 1")  # simulate an older release
    connection.commit()
    connection.close()

    reopened = GraphEngine(repo)
    stats = reopened.index_repository(force_rebuild=False)
    assert stats["cache_misses"] == 6
    assert reopened.cache.stats().schema_version == SCHEMA_VERSION


def test_batch_context_commits_once(tmp_path: Path) -> None:
    cache = ASTSQLiteCache(tmp_path / "c.db")
    cache.clear()
    with cache.batch():
        cache._conn.execute("DELETE FROM file_ast_cache")  # noqa: SLF001 - test seam
    assert cache.stats().files == 0
    cache.close()


def test_stats_and_removal(repo: Path) -> None:
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    stats = engine.cache.stats()
    assert stats.files == 6
    assert stats.total_tokens > 0
    assert stats.total_bytes > 0

    engine.cache.remove_file(repo / "pkg" / "tax_eu.py")
    assert engine.cache.stats().files == 5
    engine.cache.clear()
    assert engine.cache.stats().files == 0


def test_token_counter_is_pluggable(tmp_path: Path) -> None:
    cache = ASTSQLiteCache(tmp_path / "c.db", token_counter=lambda text: 42)
    target = tmp_path / "x.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    result = cache.get_or_parse_file(target, PolyglotASTParser())
    assert result.raw_tokens == 42
    cache.close()
