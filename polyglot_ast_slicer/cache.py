"""Embedded SQLite cache for parsed files.

Two-tier invalidation keeps re-indexing cheap:

1. ``mtime_ns`` + ``size_bytes`` match  -> reuse the stored AST without touching the file.
2. metadata changed                     -> hash the bytes; reuse the AST if the SHA-256 matches.

Parsed payloads are zlib-compressed JSON. A ``PRAGMA user_version`` guard means an upgraded
schema silently discards stale rows instead of raising on deserialisation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import zlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from polyglot_ast_slicer.parser import ParsedFile, PolyglotASTParser, SymbolNode

SCHEMA_VERSION = 2

TokenCounter = Callable[[str], int]


class BinaryFileError(ValueError):
    """Raised when a source file contains NUL bytes and is not text."""


def _cache_candidates(
    platform: str, env: Mapping[str, str], home: Path
) -> list[Path]:
    """Ordered cache locations for a platform, without touching the filesystem."""
    override = env.get("POLYGLOT_AST_CACHE_DIR")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())
    elif platform == "win32":
        local = env.get("LOCALAPPDATA")
        base = Path(local) if local else home / "AppData" / "Local"
        candidates.append(base / "polyglot-ast-slicer")
    elif platform == "darwin":
        candidates.append(home / "Library" / "Caches" / "polyglot-ast-slicer")
    elif env.get("XDG_CACHE_HOME"):
        candidates.append(Path(env["XDG_CACHE_HOME"]) / "polyglot-ast-slicer")
    else:
        candidates.append(home / ".cache" / "polyglot-ast-slicer")
    uid = getattr(os, "getuid", lambda: 0)()
    candidates.append(Path(tempfile.gettempdir()) / f"polyglot-ast-slicer-{uid}")
    return candidates


def default_cache_dir() -> Path:
    """OS cache directory for index databases, never inside the user's repository."""
    for candidate in _cache_candidates(sys.platform, os.environ, Path.home()):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix="polyglot-ast-slicer-"))


@dataclass(slots=True)
class CacheLookupResult:
    """Result of a cache lookup, including the parsed payload and its cost."""

    file_path: str
    sha256: str
    parsed: ParsedFile
    cache_hit: bool
    raw_tokens: int = 0
    elapsed_ms: float = 0.0

    @property
    def symbols(self) -> list[SymbolNode]:
        return self.parsed.symbols


@dataclass(slots=True)
class CacheStats:
    files: int = 0
    total_bytes: int = 0
    total_tokens: int = 0
    last_updated: float = 0.0
    schema_version: int = 0


class ASTSQLiteCache:
    """Versioned, self-healing SQLite cache of parsed files."""

    def __init__(
        self,
        db_path: str | Path,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_counter = token_counter
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._batch_depth = 0
        self._init_schema()

    # ------------------------------------------------------------------ schema

    def _init_schema(self) -> None:
        with self._conn:
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            # ``user_version == 0`` is ambiguous: it means either a brand-new database or a
            # pre-versioning table. Verify the columns rather than guessing.
            if version != SCHEMA_VERSION or not self._columns_match():
                self._conn.execute("DROP TABLE IF EXISTS file_ast_cache")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_ast_cache (
                    file_path   TEXT PRIMARY KEY,
                    sha256      TEXT NOT NULL,
                    mtime_ns    INTEGER NOT NULL,
                    size_bytes  INTEGER NOT NULL,
                    raw_tokens  INTEGER NOT NULL DEFAULT 0,
                    payload     BLOB NOT NULL,
                    updated_at  REAL NOT NULL
                )
                """
            )
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _columns_match(self) -> bool:
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(file_ast_cache)").fetchall()
        }
        return columns == {
            "file_path",
            "sha256",
            "mtime_ns",
            "size_bytes",
            "raw_tokens",
            "payload",
            "updated_at",
        }

    # ------------------------------------------------------------------ plumbing

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Defers commits so indexing N files costs one fsync instead of N."""
        self._batch_depth += 1
        try:
            yield
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                self._conn.commit()

    def _commit(self) -> None:
        if self._batch_depth == 0:
            self._conn.commit()

    @staticmethod
    def compute_sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _encode(self, parsed: ParsedFile) -> bytes:
        raw = json.dumps(parsed.to_dict(), separators=(",", ":")).encode("utf-8")
        return zlib.compress(raw, level=6)

    @staticmethod
    def _decode(blob: bytes) -> ParsedFile:
        return ParsedFile.from_dict(json.loads(zlib.decompress(blob)))

    def _count_tokens(self, text: str) -> int:
        if self._token_counter is None:
            return max(1, len(text) // 4)
        return self._token_counter(text)

    # ------------------------------------------------------------------ lookups

    def get_or_parse_file(
        self,
        file_path: str | Path,
        parser: PolyglotASTParser,
        force_reparse: bool = False,
    ) -> CacheLookupResult:
        started = time.perf_counter()
        resolved = str(Path(file_path).resolve())
        stat = Path(resolved).stat()

        row: tuple[str, int, int, int, bytes] | None = None
        if not force_reparse:
            row = self._conn.execute(
                "SELECT sha256, mtime_ns, size_bytes, raw_tokens, payload "
                "FROM file_ast_cache WHERE file_path = ?",
                (resolved,),
            ).fetchone()

        if row is not None:
            cached_sha, cached_mtime, cached_size, cached_tokens, payload = row

            # Tier 1: metadata identical -> trust the stored AST.
            if (
                cached_mtime == stat.st_mtime_ns
                and cached_size == stat.st_size
                and self._mtime_is_stable(stat.st_mtime_ns)
            ):
                parsed = self._decode(payload)
                return CacheLookupResult(
                    file_path=resolved,
                    sha256=cached_sha,
                    parsed=parsed,
                    cache_hit=True,
                    raw_tokens=cached_tokens,
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )

            # Tier 2: metadata moved, but the content may be identical (touch/checkout).
            raw_bytes = Path(resolved).read_bytes()
            current_sha = self.compute_sha256(raw_bytes)
            if current_sha == cached_sha and cached_size == len(raw_bytes):
                with self._conn:
                    self._conn.execute(
                        "UPDATE file_ast_cache "
                        "SET mtime_ns = ?, updated_at = ? WHERE file_path = ?",
                        (stat.st_mtime_ns, time.time(), resolved),
                    )
                parsed = self._decode(payload)
                return CacheLookupResult(
                    file_path=resolved,
                    sha256=current_sha,
                    parsed=parsed,
                    cache_hit=True,
                    raw_tokens=cached_tokens,
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )
        else:
            raw_bytes = Path(resolved).read_bytes()
            current_sha = self.compute_sha256(raw_bytes)

        if b"\x00" in raw_bytes[:8192]:
            raise BinaryFileError(f"binary content (NUL byte) in {resolved}")

        parsed = parser.parse_file(resolved, source_bytes=raw_bytes)
        raw_tokens = self._count_tokens(raw_bytes.decode("utf-8", errors="replace"))

        self._conn.execute(
            """
            INSERT INTO file_ast_cache
                (file_path, sha256, mtime_ns, size_bytes, raw_tokens, payload, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET
                sha256 = excluded.sha256,
                mtime_ns = excluded.mtime_ns,
                size_bytes = excluded.size_bytes,
                raw_tokens = excluded.raw_tokens,
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (
                resolved,
                current_sha,
                stat.st_mtime_ns,
                len(raw_bytes),
                raw_tokens,
                self._encode(parsed),
                time.time(),
            ),
        )
        self._commit()

        return CacheLookupResult(
            file_path=resolved,
            sha256=current_sha,
            parsed=parsed,
            cache_hit=False,
            raw_tokens=raw_tokens,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    @staticmethod
    def _mtime_is_stable(mtime_ns: int) -> bool:
        """Guards against timestamp granularity equal to the current clock tick."""
        return abs(time.time_ns() - mtime_ns) > 1_000_000  # 1 ms

    # ------------------------------------------------------------------ maintenance

    def remove_file(self, file_path: str | Path) -> None:
        resolved = str(Path(file_path).resolve())
        self._conn.execute("DELETE FROM file_ast_cache WHERE file_path = ?", (resolved,))
        self._commit()

    def known_files(self) -> set[str]:
        rows = self._conn.execute("SELECT file_path FROM file_ast_cache").fetchall()
        return {row[0] for row in rows}

    def stats(self) -> CacheStats:
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0), COALESCE(SUM(raw_tokens), 0), "
            "COALESCE(MAX(updated_at), 0) FROM file_ast_cache"
        ).fetchone()
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        return CacheStats(
            files=int(row[0]),
            total_bytes=int(row[1]),
            total_tokens=int(row[2]),
            last_updated=float(row[3]),
            schema_version=version,
        )

    def clear(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM file_ast_cache")

    def vacuum(self) -> None:
        self._conn.execute("VACUUM")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ASTSQLiteCache:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
