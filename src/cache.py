# src/cache.py
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from src.parser import PolyglotASTParser, SymbolNode


@dataclass(slots=True)
class CacheLookupResult:
    file_path: str
    sha256: str
    symbols: list[SymbolNode]
    cache_hit: bool
    elapsed_ms: float


class ASTSQLiteCache:
    """Embedded SQLite cache with two-tier (mtime_ns -> SHA-256) invalidation."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_ast_cache (
                    file_path TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    symbols_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    @staticmethod
    def compute_sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def get_or_parse_file(
        self,
        file_path: str | Path,
        parser: PolyglotASTParser,
        force_reparse: bool = False,
    ) -> CacheLookupResult:
        t0 = time.perf_counter()
        resolved = str(Path(file_path).resolve())
        path_obj = Path(resolved)
        stat = path_obj.stat()

        if not force_reparse:
            row = self._conn.execute(
                "SELECT sha256, mtime_ns, symbols_json FROM file_ast_cache WHERE file_path = ?",
                (resolved,),
            ).fetchone()

            if row is not None:
                cached_sha, cached_mtime_ns, symbols_json = row
                # Fast-path Tier 1: mtime_ns is identical -> skip disk read & hashing
                if cached_mtime_ns == stat.st_mtime_ns:
                    symbols = [SymbolNode.from_dict(item) for item in json.loads(symbols_json)]
                    return CacheLookupResult(
                        file_path=resolved,
                        sha256=cached_sha,
                        symbols=symbols,
                        cache_hit=True,
                        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
                    )

                # Fast-path Tier 2: mtime_ns changed, check if SHA-256 content actually changed
                raw_bytes = path_obj.read_bytes()
                current_sha = self.compute_sha256(raw_bytes)
                if current_sha == cached_sha:
                    with self._conn:
                        self._conn.execute(
                            "UPDATE file_ast_cache SET mtime_ns = ?, updated_at = ? WHERE file_path = ?",
                            (stat.st_mtime_ns, time.time(), resolved),
                        )
                    symbols = [SymbolNode.from_dict(item) for item in json.loads(symbols_json)]
                    return CacheLookupResult(
                        file_path=resolved,
                        sha256=current_sha,
                        symbols=symbols,
                        cache_hit=True,
                        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
                    )
            else:
                raw_bytes = path_obj.read_bytes()
                current_sha = self.compute_sha256(raw_bytes)
        else:
            raw_bytes = path_obj.read_bytes()
            current_sha = self.compute_sha256(raw_bytes)

        # Cache miss or content modified -> re-parse via Tree-sitter
        symbols = parser.parse_file(resolved, source_bytes=raw_bytes)
        serialized = json.dumps([s.to_dict() for s in symbols])

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO file_ast_cache (file_path, sha256, mtime_ns, symbols_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    sha256 = excluded.sha256,
                    mtime_ns = excluded.mtime_ns,
                    symbols_json = excluded.symbols_json,
                    updated_at = excluded.updated_at
                """,
                (resolved, current_sha, stat.st_mtime_ns, serialized, time.time()),
            )

        return CacheLookupResult(
            file_path=resolved,
            sha256=current_sha,
            symbols=symbols,
            cache_hit=False,
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def remove_file(self, file_path: str | Path) -> None:
        resolved = str(Path(file_path).resolve())
        with self._conn:
            self._conn.execute("DELETE FROM file_ast_cache WHERE file_path = ?", (resolved,))

    def clear(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM file_ast_cache")

    def close(self) -> None:
        self._conn.close()