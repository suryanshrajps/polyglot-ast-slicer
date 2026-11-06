"""Call-graph construction, surgical slicing, and static analysis.

Resolution model
----------------
Every call site is resolved with an explicit *confidence* tier instead of a repo-wide
name match:

======  ==============================================================
Tier    Evidence
======  ==============================================================
0.98    same-file definition of the bare name
0.95    imported binding (``from pkg.tax import calculate_tax``)
0.95    receiver is ``self``/``this``/``cls`` and the member exists
0.90    receiver type known from an annotation / constructor assignment
0.90    receiver is a module alias from the import table
0.85    member inherited from a resolved base class
0.50    unique repo-wide symbol with that exact name
======  ==============================================================

Anything weaker than that produces *no edge*. A missing edge is survivable; a wrong edge
silently poisons the slice and the blast radius, which is exactly the failure mode this tool
exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from collections import Counter, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

import networkx as nx
import tiktoken

from polyglot_ast_slicer.cache import ASTSQLiteCache, BinaryFileError, default_cache_dir
from polyglot_ast_slicer.parser import (
    EXTENSION_TO_LANGUAGE,
    PRIMITIVE_TYPES,
    GrammarUnavailable,
    ParsedFile,
    PolyglotASTParser,
    SymbolNode,
)

IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".eggs", "dist", "build",
        ".next", ".nuxt", "coverage", "htmlcov", ".idea", ".vscode", "target", "vendor",
    }
)

IGNORED_FILE_SUFFIXES: tuple[str, ...] = (".min.js", ".min.ts", ".bundle.js", ".d.ts.map")

CALL_RELATIONS: frozenset[str] = frozenset({"calls", "constructs"})
TYPE_RELATIONS: frozenset[str] = frozenset({"uses_type", "inherits"})
ALL_RELATIONS: frozenset[str] = frozenset(CALL_RELATIONS | TYPE_RELATIONS)


def _edge_relations(edge: dict[str, Any]) -> set[str]:
    """All relations carried by one graph edge (older edges have only ``relation``)."""
    stored = edge.get("relations")
    if stored:
        return set(stored)
    relation = edge.get("relation")
    return {relation} if relation else set()

MAX_SOURCE_BYTES = 1_500_000

#: Default cap on the JSON payload returned to an agent. Overridable per query.
DEFAULT_MAX_TOKENS = 6000
#: Fan-out caps keep hub symbols (decorators, base classes) from exploding a slice.
MAX_CALLERS_PER_HOP = 10
MAX_CALLEES_PER_HOP = 10
MAX_TOTAL_SNIPPETS = 40
#: A non-target symbol with this total degree is treated as a hub and omitted.
HUB_DEGREE = 150

#: Calls to these names are language builtins, not project symbols. They are kept in the
#: parsed call list (a project *may* define ``log``) but excluded from "unresolved" noise.
BUILTIN_CALL_NAMES: frozenset[str] = PRIMITIVE_TYPES | frozenset(
    {
        "print", "len", "range", "enumerate", "zip", "map", "filter", "isinstance",
        "issubclass", "getattr", "setattr", "hasattr", "delattr", "super", "open",
        "max", "min", "sum", "abs", "round", "sorted", "reversed", "any", "all",
        "iter", "next", "id", "hash", "repr", "format", "input", "vars", "dir",
        "callable", "exec", "eval", "type", "int", "float", "str", "bool", "bytes",
        "list", "dict", "set", "tuple", "frozenset", "complex", "object", "property",
        "staticmethod", "classmethod", "dataclass", "field", "Enum", "partial",
        "console", "log", "warn", "error", "debug", "info", "parseInt", "parseFloat",
        "setTimeout", "clearTimeout", "setInterval", "clearInterval", "fetch",
        "require", "String", "Number", "Boolean", "Array", "Object", "Promise",
        "JSON", "Math", "Date", "RegExp", "Error", "Symbol", "Map", "Set",
        "structlog", "logger", "logging", "throws", "assert",
    }
)


@dataclass(slots=True)
class SliceOptions:
    """Knobs for :meth:`GraphEngine.get_ast_call_slice`."""

    hops: int = 2
    include_types: bool = True
    include_code: bool = True
    max_tokens: int | None = None
    include_docs: bool = True


@dataclass(slots=True)
class _Snippet:
    symbol: SymbolNode
    role: Literal["target", "upstream_caller", "downstream_callee", "data_schema"]
    hop: int
    priority: int


@dataclass(slots=True)
class _Region:
    file_path: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    symbols: list[str] = field(default_factory=list)
    symbol_ids: list[str] = field(default_factory=list)
    roles: set[str] = field(default_factory=set)


def _strip_jsonc(text: str) -> str:
    """Removes comments and trailing commas from a tsconfig-style JSON file."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return re.sub(r",\s*([}\]])", r"\1", text)


class GraphEngine:
    """AST-aware call-graph engine with incremental SQLite caching."""

    def __init__(
        self,
        repo_path: str | Path,
        db_path: str | Path | None = None,
        *,
        cache_dir: str | Path | None = None,
        allowed_roots: Sequence[str | Path] | None = None,
        respect_gitignore: bool = True,
        max_file_bytes: int = MAX_SOURCE_BYTES,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.repo_path = Path(repo_path).expanduser().resolve()
        if not self.repo_path.is_dir():
            raise NotADirectoryError(f"Repository path is not a directory: {self.repo_path}")

        roots: Sequence[str | Path] = allowed_roots or ()
        if not roots:
            env_roots = os.environ.get("POLYGLOT_AST_ALLOWED_ROOTS", "")
            roots = [part for part in env_roots.split(os.pathsep) if part]
        self.allowed_roots = [Path(root).expanduser().resolve() for root in roots]
        if self.allowed_roots and not any(
            self.repo_path == root or self.repo_path.is_relative_to(root)
            for root in self.allowed_roots
        ):
            raise PermissionError(
                f"Repository {self.repo_path} is outside the allowed roots "
                f"{[str(root) for root in self.allowed_roots]}"
            )

        self.parser = PolyglotASTParser()
        self._encoding = self._load_encoding()
        if db_path is not None:
            database = Path(db_path).expanduser()
        else:
            base = Path(cache_dir).expanduser().resolve() if cache_dir else default_cache_dir()
            digest = hashlib.sha256(str(self.repo_path).encode()).hexdigest()[:16]
            database = base / f"{digest}.db"
        self.cache = ASTSQLiteCache(database, token_counter=self.count_tokens)
        self.graph: nx.DiGraph = nx.DiGraph()
        self.lock = threading.RLock()
        self.default_max_tokens = default_max_tokens

        self._symbols: dict[str, SymbolNode] = {}
        self._by_name: dict[str, set[str]] = {}
        self._by_qualified: dict[str, set[str]] = {}
        self._by_file: dict[str, set[str]] = {}
        self._parsed: dict[str, ParsedFile] = {}
        self._file_tokens: dict[str, int] = {}
        self._total_tokens = 0

        self._module_to_file: dict[str, str] = {}
        self._file_modules: dict[str, set[str]] = {}
        self._bindings: dict[str, dict[str, tuple[str | None, str | None]]] = {}
        self._wildcards: dict[str, set[str]] = {}
        self._import_targets: dict[str, set[str]] = {}
        self._imported_by: dict[str, set[str]] = {}

        self._ignore_patterns = (
            self._load_gitignore() if respect_gitignore else []
        )
        self._tsconfig_base, self._tsconfig_paths = self._load_tsconfig()
        self._max_file_bytes = max_file_bytes
        self._indexed_at: float = 0.0
        self._last_index_stats: dict[str, Any] = {}
        self._unresolved_calls = 0
        self._skipped: dict[str, str] = {}
        # Reverse index for incremental relinking: callee name -> unresolved/ambiguous callers.
        self._pending: dict[str, set[str]] = {}
        self._pending_by_caller: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ tokens

    @staticmethod
    def _load_encoding() -> Any:
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:  # pragma: no cover - tiktoken ships its vocab; defensive only
            return None

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is not None:
            return len(self._encoding.encode(text, disallowed_special=()))
        return max(1, len(text) // 4)

    # ------------------------------------------------------------------ discovery

    def _load_gitignore(self) -> list[tuple[str, bool, bool]]:
        """Parses the root ``.gitignore`` into ``(pattern, negated, dir_only)`` tuples."""
        path = self.repo_path / ".gitignore"
        if not path.is_file():
            return []
        patterns: list[tuple[str, bool, bool]] = []
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            if negated:
                line = line[1:].strip()
                if not line:
                    continue
            dir_only = line.endswith("/")
            patterns.append((line.rstrip("/"), negated, dir_only))
        return patterns

    def _is_ignored(self, rel_path: str) -> bool:
        rel = rel_path.replace(os.sep, "/")
        segments = rel.split("/")
        ignored = False
        for pattern, negated, dir_only in self._ignore_patterns:
            anchored = pattern.startswith("/")
            body = pattern.lstrip("/")
            matched = False
            if dir_only:
                prefix_match = any(
                    fnmatch(segment, body) for segment in segments[:-1]
                ) or fnmatch(segments[-1], body)
                matched = prefix_match
            elif "/" in body or anchored:
                matched = fnmatch(rel, body) or rel.startswith(body + "/")
            else:
                matched = any(fnmatch(segment, body) for segment in segments)
            if matched:
                ignored = not negated
        return ignored

    def discover_source_files(self) -> list[Path]:
        """Walks the repo and returns candidate source files in deterministic order."""
        found: list[Path] = []
        for root, dirs, filenames in os.walk(self.repo_path):
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in IGNORED_DIRS
                and not d.startswith(".")
                and not self._is_ignored(self._rel(Path(root) / d) + "/")
            )
            for filename in sorted(filenames):
                suffix = Path(filename).suffix.lower()
                if suffix not in EXTENSION_TO_LANGUAGE:
                    continue
                if filename.endswith(IGNORED_FILE_SUFFIXES):
                    continue
                path = Path(root) / filename
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size > self._max_file_bytes:
                    self._skipped[str(path)] = (
                        f"file too large ({size} bytes > {self._max_file_bytes})"
                    )
                    continue
                rel = self._rel(path)
                if self._is_ignored(rel):
                    continue
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                try:
                    resolved.relative_to(self.repo_path)
                except ValueError:
                    # A symlink pointing outside the repository is never indexed.
                    self._skipped[str(resolved)] = "symlink escapes repository"
                    continue
                found.append(resolved)
        return sorted(found)

    def _rel(self, path: str | Path) -> str:
        try:
            return Path(path).resolve().relative_to(self.repo_path).as_posix()
        except ValueError:
            return Path(path).as_posix()

    # ------------------------------------------------------------------ indexing

    def index_repository(self, force_rebuild: bool = False) -> dict[str, Any]:
        with self.lock:
            started = time.perf_counter()
            if force_rebuild:
                self._reset()

            source_files = self.discover_source_files()
            current = {str(p) for p in source_files}

            for stale in sorted(set(self._by_file) - current):
                self._remove_file(stale)
            for orphan in sorted(self.cache.known_files() - current):
                self.cache.remove_file(orphan)

            hits = misses = 0
            with self.cache.batch():
                for file_path in source_files:
                    try:
                        result = self.cache.get_or_parse_file(file_path, self.parser)
                    except (GrammarUnavailable, OSError, BinaryFileError) as error:
                        self._skipped[str(file_path)] = f"{type(error).__name__}: {error}"
                        continue
                    self._skipped.pop(str(file_path), None)
                    hits += int(result.cache_hit)
                    misses += int(not result.cache_hit)
                    self._register_file(result.parsed, result.raw_tokens)

            self._rebuild_module_index()
            self._rebuild_bindings()
            self._rebuild_all_edges()
            self._indexed_at = time.time()

            stats = self._index_stats(
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                cache_hits=hits,
                cache_misses=misses,
            )
            self._last_index_stats = stats
            return stats

    def _reset(self) -> None:
        self.cache.clear()
        self.graph.clear()
        self._symbols.clear()
        self._by_name.clear()
        self._by_qualified.clear()
        self._by_file.clear()
        self._parsed.clear()
        self._file_tokens.clear()
        self._total_tokens = 0
        self._module_to_file.clear()
        self._file_modules.clear()
        self._bindings.clear()
        self._wildcards.clear()
        self._import_targets.clear()
        self._imported_by.clear()
        self._unresolved_calls = 0
        self._pending.clear()
        self._pending_by_caller.clear()
        self._skipped.clear()

    def refresh_file(self, file_path: str | Path) -> dict[str, Any]:
        """Re-parses one saved file and relinks exactly the callers it can affect."""
        with self.lock:
            started = time.perf_counter()
            resolved = str(Path(file_path).resolve())
            # A single-file refresh is only sound if the set of modules is unchanged:
            # imports resolve through ``_module_to_file``, so a module added, deleted or
            # renamed since the last index must be reconciled before relinking.
            current_files = {str(path) for path in self.discover_source_files()}
            if current_files != set(self._by_file):
                self.index_repository(force_rebuild=False)
            if resolved not in self._by_file and not Path(resolved).exists():
                self.cache.remove_file(resolved)
                return {"file_path": resolved, "status": "deleted", "elapsed_ms": 0.0}

            old_symbols = [self._symbols[sid] for sid in self._by_file.get(resolved, set())]
            changed_names = {symbol.name for symbol in old_symbols}
            affected = self._dependents_of(resolved) | self._imported_by.get(resolved, set())
            for old in old_symbols:
                if old.symbol_id in self.graph:
                    affected.update(self.graph.predecessors(old.symbol_id))

            self._remove_file(resolved)

            if not Path(resolved).exists():
                relink = affected | self._pending_callers(changed_names)
                self._relink(relink)
                elapsed = (time.perf_counter() - started) * 1000.0
                return {
                    "file_path": resolved,
                    "status": "deleted",
                    "elapsed_ms": round(elapsed, 3),
                }

            try:
                result = self.cache.get_or_parse_file(resolved, self.parser)
            except (GrammarUnavailable, OSError, BinaryFileError) as error:
                self._skipped[resolved] = f"{type(error).__name__}: {error}"
                return {
                    "file_path": resolved,
                    "status": "skipped",
                    "error": self._skipped[resolved],
                    "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
                }
            self._skipped.pop(resolved, None)
            self._register_file(result.parsed, result.raw_tokens)
            self._rebuild_bindings_for({resolved, *self._imported_by.get(resolved, set())})

            changed_names |= {symbol.name for symbol in result.parsed.symbols}
            relink = (
                set(self._by_file.get(resolved, set()))
                | affected
                | self._pending_callers(changed_names)
            )
            self._relink(relink)

            elapsed = (time.perf_counter() - started) * 1000.0
            return {
                "file_path": resolved,
                "status": "updated",
                "cache_hit": result.cache_hit,
                "symbols_in_file": len(result.parsed.symbols),
                "elapsed_ms": round(elapsed, 3),
            }

    def _pending_callers(self, names: Iterable[str]) -> set[str]:
        callers: set[str] = set()
        for name in names:
            callers.update(self._pending.get(name, set()))
        return callers

    def _relink(self, symbol_ids: Iterable[str]) -> None:
        for symbol_id in sorted(set(symbol_ids)):
            symbol = self._symbols.get(symbol_id)
            if symbol is not None:
                self._link_symbol_edges(symbol)

    def _dependents_of(self, file_path: str) -> set[str]:
        return set(self._imported_by.get(file_path, set()))

    # -- registration ---------------------------------------------------

    def _register_file(self, parsed: ParsedFile, raw_tokens: int) -> None:
        file_path = parsed.file_path
        self._remove_file(file_path)

        self._parsed[file_path] = parsed
        self._file_tokens[file_path] = raw_tokens
        self._total_tokens += raw_tokens
        ids: set[str] = set()
        for symbol in parsed.symbols:
            ids.add(symbol.symbol_id)
            self._symbols[symbol.symbol_id] = symbol
            self._by_name.setdefault(symbol.name, set()).add(symbol.symbol_id)
            self._by_qualified.setdefault(symbol.qualified_name, set()).add(symbol.symbol_id)
            self.graph.add_node(
                symbol.symbol_id,
                name=symbol.name,
                qualified_name=symbol.qualified_name,
                kind=symbol.kind,
                file_path=symbol.file_path,
                language=symbol.language,
            )
        self._by_file[file_path] = ids
        self._register_modules(parsed)

    def _remove_file(self, file_path: str) -> None:
        self._unregister_modules(file_path)
        for symbol_id in self._by_file.pop(file_path, set()):
            symbol = self._symbols.pop(symbol_id, None)
            if symbol is not None:
                name_bucket = self._by_name.get(symbol.name)
                if name_bucket is not None:
                    name_bucket.discard(symbol_id)
                    if not name_bucket:
                        del self._by_name[symbol.name]
                qualified_bucket = self._by_qualified.get(symbol.qualified_name)
                if qualified_bucket is not None:
                    qualified_bucket.discard(symbol_id)
                    if not qualified_bucket:
                        del self._by_qualified[symbol.qualified_name]
            if self.graph.has_node(symbol_id):
                self.graph.remove_node(symbol_id)
            self._clear_pending(symbol_id)
        self._parsed.pop(file_path, None)
        self._total_tokens -= self._file_tokens.pop(file_path, 0)
        self._bindings.pop(file_path, None)
        self._wildcards.pop(file_path, None)
        self._skipped.pop(file_path, None)
        for target in self._import_targets.pop(file_path, set()):
            importers = self._imported_by.get(target)
            if importers is not None:
                importers.discard(file_path)
                if not importers:
                    del self._imported_by[target]

    # -- module index ---------------------------------------------------

    def _register_modules(self, parsed: ParsedFile) -> None:
        modules = self._modules_for(parsed)
        self._file_modules[parsed.file_path] = modules
        for module in modules:
            self._module_to_file.setdefault(module, parsed.file_path)

    def _unregister_modules(self, file_path: str) -> None:
        for module in self._file_modules.pop(file_path, set()):
            if self._module_to_file.get(module) == file_path:
                del self._module_to_file[module]

    def _rebuild_module_index(self) -> None:
        self._module_to_file.clear()
        self._file_modules.clear()
        for parsed in self._parsed.values():
            self._register_modules(parsed)

    def _modules_for(self, parsed: ParsedFile) -> set[str]:
        if parsed.language != "python":
            return set()
        relative = Path(parsed.file_path).relative_to(self.repo_path)
        parts = list(relative.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            return set()
        modules = {".".join(parts)}
        for prefix in ("src", "lib"):
            if len(parts) > 1 and parts[0] == prefix:
                modules.add(".".join(parts[1:]))
        return modules

    # -- import bindings ------------------------------------------------

    def _rebuild_bindings(self) -> None:
        self._bindings.clear()
        self._wildcards.clear()
        for parsed in self._parsed.values():
            self._compute_bindings(parsed)

    def _rebuild_bindings_for(self, files: Iterable[str]) -> None:
        for file_path in files:
            parsed = self._parsed.get(file_path)
            if parsed is not None:
                self._compute_bindings(parsed)

    def _compute_bindings(self, parsed: ParsedFile) -> None:
        bindings: dict[str, tuple[str | None, str | None]] = {}
        wildcards: set[str] = set()
        targets: set[str] = set()

        for ref in parsed.imports:
            resolved = self._resolve_import_module(ref.module, parsed)
            if resolved is not None:
                targets.add(resolved)

            if ref.is_module:
                binding_name = ref.alias or ref.module.split(".")[-1]
                bindings.setdefault(binding_name, (resolved, None))
                continue

            base_module = self._module_name_of(resolved) if resolved else ref.module.lstrip(".")
            for local, original in ref.names:
                submodule: str | None = None
                if parsed.language == "python" and base_module:
                    submodule = self._module_to_file.get(f"{base_module}.{original}")
                if submodule is not None:
                    bindings.setdefault(local, (submodule, None))
                    targets.add(submodule)
                    continue
                bindings.setdefault(local, (resolved, original))

            if ref.wildcard and resolved is not None:
                wildcards.add(resolved)

        self._bindings[parsed.file_path] = bindings
        self._wildcards[parsed.file_path] = wildcards
        previous = self._import_targets.get(parsed.file_path, set())
        for stale in previous - targets:
            self._imported_by.get(stale, set()).discard(parsed.file_path)
        for target in targets:
            self._imported_by.setdefault(target, set()).add(parsed.file_path)
        self._import_targets[parsed.file_path] = targets

    def _module_name_of(self, file_path: str | None) -> str | None:
        if file_path is None:
            return None
        modules = self._file_modules.get(file_path, set())
        return min(modules, key=len) if modules else None

    def _resolve_import_module(self, module: str, parsed: ParsedFile) -> str | None:
        if parsed.language == "python":
            return self._resolve_python_module(module, parsed.file_path)
        return self._resolve_ts_module(module, parsed.file_path)

    def _resolve_python_module(self, module: str, caller_file: str) -> str | None:
        if not module:
            return None
        if module.startswith("."):
            level = len(module) - len(module.lstrip("."))
            rest = module.lstrip(".")
            base = self._package_parts(caller_file)
            trim = max(0, level - 1)
            if trim:
                base = base[: max(0, len(base) - trim)]
            target = ".".join([*base, rest]) if rest else ".".join(base)
        else:
            target = module
        file_path = self._module_to_file.get(target)
        if file_path is not None:
            return file_path
        # ``from a.b import c`` where ``a.b`` is a package: accept the package directory.
        while "." in target:
            target = target.rsplit(".", 1)[0]
            file_path = self._module_to_file.get(target)
            if file_path is not None:
                return file_path
        return None

    def _package_parts(self, caller_file: str) -> list[str]:
        relative = Path(caller_file).relative_to(self.repo_path)
        parts = list(relative.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            return parts[:-1]
        return parts[:-1]

    def _resolve_ts_module(self, spec: str, caller_file: str) -> str | None:
        if spec.startswith("."):
            base = os.path.normpath(os.path.join(os.path.dirname(caller_file), spec))
            return self._first_ts_file(base)

        for pattern, targets in self._tsconfig_paths.items():
            if "*" in pattern:
                prefix, _, suffix = pattern.partition("*")
                if not (spec.startswith(prefix) and spec.endswith(suffix)):
                    continue
                middle = spec[len(prefix) : len(spec) - len(suffix) if suffix else None]
                for target in targets:
                    found = self._first_ts_file(
                        os.path.join(self._tsconfig_root(), target.replace("*", middle))
                    )
                    if found is not None:
                        return found
            elif spec == pattern:
                for target in targets:
                    found = self._first_ts_file(os.path.join(self._tsconfig_root(), target))
                    if found is not None:
                        return found

        if self._tsconfig_base is not None:
            return self._first_ts_file(os.path.join(str(self._tsconfig_base), spec))
        return None

    def _tsconfig_root(self) -> str:
        return str(self._tsconfig_base or self.repo_path)

    def _first_ts_file(self, base: str) -> str | None:
        extensions = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
        # TypeScript resolves an emitted ``.js``/``.mjs``/``.cjs``/``.jsx`` specifier back to
        # its source extension (``./u.js`` -> ``u.ts``/``u.tsx``).
        rewritten = {
            ".js": (".ts", ".tsx"),
            ".jsx": (".tsx",),
            ".mjs": (".mts",),
            ".cjs": (".cts",),
        }
        suffix = Path(base).suffix.lower()
        candidates: list[str] = []
        if suffix in rewritten:
            stem = base[: -len(suffix)]
            candidates += [stem + extension for extension in rewritten[suffix]]
            candidates.append(base)
        elif suffix in extensions:
            candidates.append(base)
        else:
            # A dot in the basename (``shared.utils``, ``validation.pipe``) is not a file
            # extension; without this branch those imports never try ``.ts``.
            candidates += [base + extension for extension in extensions]
            candidates += [os.path.join(base, f"index{ext}") for ext in extensions]
        for candidate in candidates:
            normalised = os.path.normpath(candidate)
            if normalised in self._by_file:
                return normalised
        return None

    def _load_tsconfig(self) -> tuple[Path | None, dict[str, list[str]]]:
        path = self.repo_path / "tsconfig.json"
        if not path.is_file():
            return None, {}
        return self._load_tsconfig_file(path)

    def _load_tsconfig_file(
        self, path: Path, depth: int = 0
    ) -> tuple[Path | None, dict[str, list[str]]]:
        """Loads a tsconfig, following ``extends`` (relative, package, chained)."""
        if depth > 6 or not path.is_file():
            return None, {}
        try:
            data = json.loads(_strip_jsonc(path.read_text(encoding="utf-8", errors="replace")))
        except (OSError, json.JSONDecodeError):
            return None, {}
        if not isinstance(data, dict):
            return None, {}

        base: Path | None = None
        patterns: dict[str, list[str]] = {}
        parents = data.get("extends")
        if isinstance(parents, str):
            parents = [parents]
        if isinstance(parents, list):
            for spec in parents:
                if not isinstance(spec, str):
                    continue
                parent_path = self._resolve_extends(spec, path.parent)
                if parent_path is None:
                    continue
                parent_base, parent_patterns = self._load_tsconfig_file(parent_path, depth + 1)
                if parent_base is not None:
                    base = parent_base
                patterns.update(parent_patterns)

        options = data.get("compilerOptions", {})
        if not isinstance(options, dict):
            options = {}
        base_url = options.get("baseUrl")
        if isinstance(base_url, str):
            base = (path.parent / base_url).resolve()
        raw_paths = options.get("paths")
        if isinstance(raw_paths, dict):
            patterns = {
                str(alias): [str(item) for item in targets if isinstance(item, str)]
                for alias, targets in raw_paths.items()
                if isinstance(targets, list)
            }
        return base, patterns

    def _resolve_extends(self, spec: str, config_dir: Path) -> Path | None:
        candidates: list[Path] = []
        if spec.startswith(".") or spec.startswith("/"):
            raw = (config_dir / spec).resolve()
            candidates.append(raw if raw.suffix == ".json" else raw.with_suffix(".json"))
        else:
            for root in (config_dir, self.repo_path):
                package = root / "node_modules" / spec
                candidates.extend(
                    [package, package.with_suffix(".json"), package / "tsconfig.json"]
                )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    # -- edges ----------------------------------------------------------

    def _rebuild_all_edges(self) -> None:
        self.graph.remove_edges_from(list(self.graph.edges()))
        self._unresolved_calls = 0
        self._pending.clear()
        self._pending_by_caller.clear()
        for symbol in self._symbols.values():
            self._link_symbol_edges(symbol)

    def _clear_pending(self, symbol_id: str) -> None:
        for name in self._pending_by_caller.pop(symbol_id, set()):
            bucket = self._pending.get(name)
            if bucket is not None:
                bucket.discard(symbol_id)
                if not bucket:
                    del self._pending[name]

    def _track_pending(self, symbol_id: str, name: str) -> None:
        self._pending.setdefault(name, set()).add(symbol_id)
        self._pending_by_caller.setdefault(symbol_id, set()).add(name)

    def _add_relation(
        self,
        source: str,
        target: str,
        *,
        relation: str,
        confidence: float,
        resolution: str,
        line: int | None,
        receiver: str | None,
        name: str,
    ) -> None:
        """Adds an edge, merging a call and a type reference between the same pair.

        ``DiGraph`` stores one edge per ordered pair. A call and a ``-> Foo`` annotation
        are independent facts, so the edge carries every relation it has; the call
        relation stays primary because traversal, blast radius and explain_edge need its
        call-site metadata.
        """
        existing = self.graph.get_edge_data(source, target)
        if existing is None:
            self.graph.add_edge(
                source,
                target,
                relation=relation,
                relations=(relation,),
                confidence=confidence,
                resolution=resolution,
                line=line,
                receiver=receiver,
                name=name,
            )
            return

        relations = _edge_relations(existing)
        relations.add(relation)
        existing["relations"] = tuple(sorted(relations))
        current = existing.get("relation")
        replace = False
        if relation in CALL_RELATIONS and current not in CALL_RELATIONS:
            replace = True
        elif (relation in CALL_RELATIONS) == (current in CALL_RELATIONS):
            replace = confidence > float(existing.get("confidence", 0.0))
        if replace:
            existing.update(
                relation=relation,
                confidence=confidence,
                resolution=resolution,
                line=line,
                receiver=receiver,
                name=name,
            )

    def _link_symbol_edges(self, symbol: SymbolNode) -> None:
        if symbol.symbol_id not in self.graph:
            return
        self.graph.remove_edges_from(list(self.graph.out_edges(symbol.symbol_id)))
        self._clear_pending(symbol.symbol_id)

        for site in symbol.calls:
            resolutions = self._resolve_call_site(symbol, site)
            if not resolutions:
                if site.name in BUILTIN_CALL_NAMES:
                    continue
                if site.name in self._by_name:
                    self._unresolved_calls += 1
                # Track bare names (and project collisions) so a later definition, import,
                # rename or deletion relinks exactly the affected callers.
                if site.receiver is None or site.name in self._by_name:
                    self._track_pending(symbol.symbol_id, site.name)
                continue
            for target_id, confidence, path in resolutions:
                target = self._symbols.get(target_id)
                if target is None:
                    continue
                relation = "constructs" if target.is_type else "calls"
                # Self edges are kept: direct recursion must be visible to cycle detection.
                self._add_relation(
                    symbol.symbol_id,
                    target_id,
                    relation=relation,
                    confidence=confidence,
                    resolution=path,
                    line=site.line,
                    receiver=site.receiver,
                    name=site.name,
                )

        for type_name in symbol.type_refs:
            for target_id, confidence, path in self._resolve_type_candidates(type_name, symbol):
                if target_id != symbol.symbol_id:
                    self._add_relation(
                        symbol.symbol_id,
                        target_id,
                        relation="uses_type",
                        confidence=confidence,
                        resolution=path,
                        line=None,
                        receiver=None,
                        name=type_name,
                    )

        for base_name in symbol.bases:
            for target_id, confidence, path in self._resolve_type_candidates(base_name, symbol):
                if target_id != symbol.symbol_id:
                    self._add_relation(
                        symbol.symbol_id,
                        target_id,
                        relation="inherits",
                        confidence=confidence,
                        resolution=path,
                        line=None,
                        receiver=None,
                        name=base_name,
                    )

    # -- resolution -----------------------------------------------------

    def _resolve_call_site(
        self, symbol: SymbolNode, site: Any
    ) -> list[tuple[str, float, str]]:
        """Resolves one call site to ``(target_id, confidence, resolution_path)``.

        There is deliberately **no name-only fallback**: an unimported, ambiguous name
        produces no edge. A missing edge is survivable; a fabricated one poisons every
        downstream slice and blast-radius report.
        """
        name: str = site.name
        receiver: str | None = site.receiver
        if not name:
            return []

        if receiver in ("super", "super()"):
            # ``super.m()`` must bind to a base member, never to the enclosing class:
            # checking the enclosing class first made ``Child.greet -> Child.greet``.
            for enclosing in self._enclosing_classes(symbol):
                for base in enclosing.bases:
                    for base_id, _, _ in self._resolve_type_candidates(base, symbol):
                        inherited = self._find_member(self._symbols[base_id], name)
                        if inherited is not None:
                            return [
                                (
                                    inherited,
                                    0.85,
                                    f"super:{self._symbols[base_id].qualified_name}",
                                )
                            ]
            return []

        if receiver in ("self", "this", "cls"):
            for enclosing in self._enclosing_classes(symbol):
                found = self._find_member(enclosing, name)
                if found is not None:
                    return [(found, 0.95, f"enclosing_member:{enclosing.qualified_name}")]

        if receiver and symbol.language == "python" and receiver.endswith(")"):
            # ``A().run()``: the receiver is a constructor call, not a binding.
            head = str(receiver).split("(", 1)[0].strip()
            if head[:1].isupper() and "." not in head:
                for type_id, confidence, _ in self._resolve_type_candidates(head, symbol):
                    found = self._find_member(self._symbols[type_id], name)
                    if found is not None:
                        return [
                            (
                                found,
                                min(confidence, 0.90),
                                f"constructed_receiver:{head}",
                            )
                        ]

        if receiver:
            resolved_type = self._resolve_receiver_type(receiver, symbol)
            if resolved_type is not None:
                found = self._find_member(self._symbols[resolved_type], name)
                if found is not None:
                    return [
                        (
                            found,
                            0.90,
                            f"typed_receiver:{receiver}->{self._symbols[resolved_type].qualified_name}",
                        )
                    ]
            for module_file in self._resolve_receiver_modules(receiver, symbol):
                found = self._symbol_in_file(module_file, name)
                if found is not None:
                    return [(found, 0.90, f"module:{self._rel(module_file)}")]
            if receiver[:1].isupper():
                for type_id, confidence, _ in self._resolve_type_candidates(receiver, symbol):
                    found = self._find_member(self._symbols[type_id], name)
                    if found is not None:
                        return [
                            (found, min(confidence, 0.90), f"class_receiver:{receiver}")
                        ]
        else:
            same_file = f"{symbol.file_path}::{name}"
            if same_file in self._symbols:
                return [(same_file, 0.98, "same_file")]

            binding = self._bindings.get(symbol.file_path, {}).get(name)
            if binding is not None:
                target_file, original = binding
                if target_file is not None:
                    found = self._symbol_in_file(target_file, original or name)
                    if found is None and original is None:
                        # ``const x = require("./m"); x()`` calls the CommonJS default export.
                        found = self._symbol_in_file(target_file, "default")
                    if found is not None:
                        return [(found, 0.95, f"import:{self._rel(target_file)}")]

            scoped = self._same_file_scoped(symbol, name)
            if scoped is not None:
                return [(scoped, 0.90, "same_file_scope")]

            # NOTE: an unqualified call never binds to a method of the enclosing class.
            # ``open(...)`` inside a method is the builtin/module function, not ``self.open``.
            for wildcard_file in self._wildcards.get(symbol.file_path, set()):
                found = self._symbol_in_file(wildcard_file, name)
                if found is not None:
                    return [(found, 0.85, f"wildcard:{self._rel(wildcard_file)}")]

        return []

    def _same_file_scoped(self, symbol: SymbolNode, name: str) -> str | None:
        """Resolves a nested/local symbol by short name **within its lexical scope only**.

        ``test_x.cli`` calling ``CLIError()`` may legitimately bind to
        ``test_x.CLIError`` (a class defined inside the same test function). This is
        scope-checked, not a repo-wide name guess.
        """
        candidates: list[str] = []
        for symbol_id in sorted(self._by_file.get(symbol.file_path, set())):
            candidate = self._symbols[symbol_id]
            if candidate.name != name or candidate.kind == "method":
                continue
            if "." not in candidate.qualified_name:
                continue
            parent = candidate.qualified_name.rsplit(".", 1)[0]
            if symbol.qualified_name == parent or symbol.qualified_name.startswith(parent + "."):
                candidates.append(symbol_id)
        return candidates[0] if len(candidates) == 1 else None

    def _resolve_receiver_type(self, receiver: str, symbol: SymbolNode) -> str | None:
        attribute = receiver.split(".")[-1]
        if receiver.split(".")[0] in ("self", "this"):
            for enclosing in self._enclosing_classes(symbol):
                declared = enclosing.attribute_types.get(attribute)
                if declared:
                    resolved = self._resolve_type_candidates(declared, symbol)
                    if resolved:
                        return resolved[0][0]
        declared = symbol.local_types.get(receiver) or symbol.local_types.get(attribute)
        if declared:
            resolved = self._resolve_type_candidates(declared, symbol)
            if resolved:
                return resolved[0][0]
        return None

    def _resolve_receiver_modules(self, receiver: str, symbol: SymbolNode) -> list[str]:
        bindings = self._bindings.get(symbol.file_path, {})
        head = receiver.split(".")[0]
        binding = bindings.get(head)
        found: list[str] = []
        if binding is not None and binding[1] is None and binding[0] is not None:
            found.append(binding[0])
        direct = self._module_to_file.get(receiver)
        if direct is not None and direct not in found:
            found.append(direct)
        return found

    def _resolve_type_candidates(
        self, name: str, caller: SymbolNode
    ) -> list[tuple[str, float, str]]:
        if not name:
            return []
        same_file = f"{caller.file_path}::{name}"
        symbol = self._symbols.get(same_file)
        if symbol is not None and symbol.is_type:
            return [(same_file, 0.98, "same_file_type")]

        binding = self._bindings.get(caller.file_path, {}).get(name)
        if binding is not None and binding[0] is not None:
            found = self._symbol_in_file(binding[0], binding[1] or name)
            if found is not None and self._symbols[found].is_type:
                return [(found, 0.95, f"import:{self._rel(binding[0])}")]
        return []

    def _symbol_in_file(
        self, file_path: str, name: str, depth: int = 0, mode: Literal["call", "export"] = "call"
    ) -> str | None:
        """Finds ``name`` in ``file_path``, following re-exports (``pkg/__init__.py``).

        Package ``__init__`` files typically re-export symbols imported from submodules;
        without this walk, ``import networkx as nx; nx.shortest_path(...)`` would not link.

        ``mode="export"`` is used while walking a ``from x import *`` chain: a private alias
        (``from y import _impl as shortest_path``) must not shadow a module that actually
        defines ``shortest_path``.
        """
        exact = f"{file_path}::{name}"
        if exact in self._symbols:
            return exact
        if name == "default":
            for symbol_id in sorted(self._by_file.get(file_path, set())):
                if self._symbols[symbol_id].is_default:
                    return symbol_id
        if depth >= 6 or file_path not in self._by_file:
            return None

        binding = self._bindings.get(file_path, {}).get(name)
        if binding is not None and binding[0] is not None and binding[0] != file_path:
            original = binding[1]
            if mode == "call" or original in (None, name):
                found = self._symbol_in_file(binding[0], original or name, depth + 1, mode)
                if found is not None:
                    return found

        for wildcard_file in sorted(self._wildcards.get(file_path, set())):
            found = self._symbol_in_file(wildcard_file, name, depth + 1, "export")
            if found is not None:
                return found

        for symbol_id in sorted(self._by_file.get(file_path, set())):
            if self._symbols[symbol_id].name == name:
                return symbol_id
        return None

    def _enclosing_classes(self, symbol: SymbolNode) -> list[SymbolNode]:
        parts = symbol.qualified_name.split(".")
        found: list[SymbolNode] = []
        for index in range(len(parts) - 1, 0, -1):
            candidate_id = f"{symbol.file_path}::{'.'.join(parts[:index])}"
            candidate = self._symbols.get(candidate_id)
            if candidate is not None and candidate.is_type:
                found.append(candidate)
        return found

    def _find_member(self, cls: SymbolNode, name: str, depth: int = 0) -> str | None:
        member_id = f"{cls.file_path}::{cls.qualified_name}.{name}"
        if member_id in self._symbols:
            return member_id
        if depth >= 3:
            return None
        for base in cls.bases:
            for base_id, _, _ in self._resolve_type_candidates(base, cls):
                found = self._find_member(self._symbols[base_id], name, depth + 1)
                if found is not None:
                    return found
        return None

    # ------------------------------------------------------------------ queries

    def resolve_query_symbol(self, symbol_name: str) -> SymbolNode | None:
        symbol, _ = self.resolve_symbol_with_alternatives(symbol_name)
        return symbol

    def resolve_symbol_with_alternatives(
        self, symbol_name: str
    ) -> tuple[SymbolNode | None, list[SymbolNode]]:
        """Resolves a user-supplied name, preferring symbols that are actually wired up.

        When several symbols share a name (``calculate_tax`` in ``tax_us`` and ``tax_eu``),
        the most-depended-upon one wins, but the alternatives are returned so callers can
        surface the ambiguity instead of silently guessing.
        """
        if symbol_name in self._symbols:
            return self._symbols[symbol_name], []

        # Accept repo-relative ids such as ``pkg/billing.py::BillingEngine.process_invoice``.
        if "::" in symbol_name:
            path_part, _, qualified_part = symbol_name.partition("::")
            if path_part and qualified_part:
                absolute = f"{(self.repo_path / path_part).resolve()}::{qualified_part}"
                if absolute in self._symbols:
                    return self._symbols[absolute], []

        qualified = self._by_qualified.get(symbol_name)
        if qualified:
            ranked = self._rank_candidates(qualified)
            return ranked[0], ranked[1:]

        short = self._by_name.get(symbol_name)
        if short:
            ranked = self._rank_candidates(short)
            return ranked[0], ranked[1:]

        suffix = f".{symbol_name}"
        matches = {
            symbol_id
            for qualified_name, ids in self._by_qualified.items()
            if qualified_name.endswith(suffix)
            for symbol_id in ids
        }
        if matches:
            ranked = self._rank_candidates(matches)
            return ranked[0], ranked[1:]
        return None, []

    def _rank_candidates(self, ids: Iterable[str]) -> list[SymbolNode]:
        def sort_key(symbol_id: str) -> tuple[int, int, str]:
            in_degree = self.graph.in_degree(symbol_id) if self.graph.has_node(symbol_id) else 0
            out_degree = self.graph.out_degree(symbol_id) if self.graph.has_node(symbol_id) else 0
            return (-in_degree, -out_degree, symbol_id)

        return [self._symbols[symbol_id] for symbol_id in sorted(ids, key=sort_key)]

    def search_symbols(
        self, query: str, kind: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        with self.lock:
            if not self._symbols:
                self.index_repository()
            needle = query.strip().lower()
            if not needle:
                return {"query": query, "count": 0, "results": []}

            scored: list[tuple[int, str]] = []
            for symbol_id, symbol in self._symbols.items():
                if kind and symbol.kind != kind:
                    continue
                haystack = symbol.qualified_name.lower()
                if needle not in haystack:
                    continue
                if haystack == needle:
                    score = 0
                elif symbol.name.lower() == needle:
                    score = 1
                elif haystack.startswith(needle):
                    score = 2
                elif symbol.name.lower().startswith(needle):
                    score = 3
                else:
                    score = 4
                scored.append((score, symbol_id))

            scored.sort(key=lambda item: (item[0], item[1]))
            results = [
                self._symbol_meta(self._symbols[symbol_id]) for _, symbol_id in scored[:limit]
            ]
            return {"query": query, "count": len(results), "results": results}

    def resolve_strict(self, symbol_name: str) -> tuple[SymbolNode | None, list[SymbolNode]]:
        """Resolves a query name without guessing.

        Returns ``(symbol, [])`` on success and ``(None, candidates)`` when several symbols
        share the name. Tools must surface the candidates instead of silently picking one.
        """
        if "::" in symbol_name:
            path_part, _, qualified_part = symbol_name.partition("::")
            absolute = f"{(self.repo_path / path_part).resolve()}::{qualified_part}"
            symbol = self._symbols.get(absolute)
            return (symbol, []) if symbol is not None else (None, [])

        if symbol_name in self._symbols:
            return self._symbols[symbol_name], []

        candidates: set[str] = set(self._by_qualified.get(symbol_name, set()))
        candidates |= set(self._by_name.get(symbol_name, set()))
        suffix = f".{symbol_name}"
        for qualified_name, ids in self._by_qualified.items():
            if qualified_name.endswith(suffix):
                candidates |= ids

        symbols = [self._symbols[symbol_id] for symbol_id in sorted(candidates)]
        if len(symbols) == 1:
            return symbols[0], []
        return None, symbols

    def _ambiguous_response(
        self, symbol_name: str, candidates: Sequence[SymbolNode]
    ) -> dict[str, Any]:
        return {
            "error": f"Ambiguous symbol '{symbol_name}': {len(candidates)} candidates.",
            "code": "ambiguous_symbol",
            "candidates": [self._symbol_meta(symbol) for symbol in candidates[:20]],
            "hint": "Re-query with '<repo-relative path>::<qualified_name>'.",
        }

    def get_ast_call_slice(
        self,
        symbol_name: str,
        hops: int = 2,
        include_types: bool = True,
        max_tokens: int | None = None,
        include_code: bool = True,
        include_metadata: bool = False,
    ) -> dict[str, Any]:
        """Returns a budgeted slice; the JSON payload itself is capped by ``max_tokens``."""
        with self.lock:
            if not self._symbols:
                self.index_repository()

            target, candidates = self.resolve_strict(symbol_name)
            if target is None:
                if candidates:
                    return self._ambiguous_response(symbol_name, candidates)
                return self._not_found(symbol_name)

            hops = max(1, min(int(hops), 5))
            budget = self.default_max_tokens if max_tokens is None else max(1, int(max_tokens))

            upstream, upstream_omitted = self._cap_neighbours(
                self._bfs(target.symbol_id, hops, direction="up"), MAX_CALLERS_PER_HOP
            )
            downstream, downstream_omitted = self._cap_neighbours(
                self._bfs(target.symbol_id, hops, direction="down"), MAX_CALLEES_PER_HOP
            )

            type_ids: set[str] = set()
            if include_types:
                core = {target.symbol_id, *upstream, *downstream}
                for node_id in sorted(core):
                    for successor in sorted(self.graph.successors(node_id)):
                        edge = self.graph.get_edge_data(node_id, successor) or {}
                        if _edge_relations(edge) & TYPE_RELATIONS and successor not in core:
                            type_ids.add(successor)
            type_omitted = max(0, len(type_ids) - 10)
            for node_id in sorted(type_ids)[10:]:
                type_ids.discard(node_id)

            snippets: list[_Snippet] = [_Snippet(target, "target", 0, 0)]
            snippets += [
                _Snippet(self._symbols[node_id], "upstream_caller", dist, 1 + dist)
                for node_id, dist in sorted(upstream.items(), key=lambda kv: (kv[1], kv[0]))
            ]
            snippets += [
                _Snippet(self._symbols[node_id], "downstream_callee", dist, 1 + dist)
                for node_id, dist in sorted(downstream.items(), key=lambda kv: (kv[1], kv[0]))
            ]
            snippets += [
                _Snippet(self._symbols[node_id], "data_schema", 1, 9)
                for node_id in sorted(type_ids)
            ]
            snippets = self._dedupe_snippets(snippets)

            # Leave room for the envelope; the exact payload is re-checked below.
            selected, truncated = self._select_within_budget(snippets, max(1, budget - 400))
            omitted = {
                "upstream": upstream_omitted,
                "downstream": downstream_omitted,
                "types": type_omitted,
            }

            payload: dict[str, Any] = {}
            while True:
                regions = self._build_regions(selected)
                compiled = self._render_context(regions) if include_code else ""
                payload = self._compose_payload(
                    target=target,
                    hops=hops,
                    selected=selected,
                    regions=regions,
                    compiled=compiled,
                    budget=budget,
                    omitted=omitted,
                    truncated=truncated,
                    include_metadata=include_metadata,
                    include_code=include_code,
                )
                payload = self._finalize_payload(payload)
                if payload["token_metrics"]["payload_tokens"] <= budget or len(selected) <= 1:
                    break
                selected = selected[:-1]
                truncated = True

            return payload

    def _cap_neighbours(
        self, distances: dict[str, int], per_hop: int
    ) -> tuple[dict[str, int], int]:
        kept: dict[str, int] = {}
        omitted = 0
        for hop in sorted(set(distances.values())):
            node_ids = [node_id for node_id, dist in distances.items() if dist == hop]
            node_ids.sort(key=lambda node_id: (-self.graph.degree(node_id), node_id))
            taken = 0
            for node_id in node_ids:
                if (
                    taken >= per_hop
                    or len(kept) >= MAX_TOTAL_SNIPPETS
                    or self.graph.degree(node_id) >= HUB_DEGREE
                ):
                    omitted += 1
                    continue
                kept[node_id] = hop
                taken += 1
        return kept, omitted

    def _compose_payload(
        self,
        *,
        target: SymbolNode,
        hops: int,
        selected: Sequence[_Snippet],
        regions: Sequence[_Region],
        compiled: str,
        budget: int,
        omitted: dict[str, int],
        truncated: bool,
        include_metadata: bool,
        include_code: bool,
    ) -> dict[str, Any]:
        slice_files = {snippet.symbol.file_path for snippet in selected}
        touched_tokens = sum(self._file_tokens.get(path, 0) for path in slice_files)
        slice_tokens = self.count_tokens(compiled) if include_code else 0
        metrics: dict[str, Any] = {
            "slice_tokens": slice_tokens,
            "payload_tokens": 0,
            "budget_tokens": budget,
            "touched_files_tokens": touched_tokens,
            "full_repo_tokens": self._total_tokens,
            "reduction_vs_touched_files_pct": self._reduction(slice_tokens, touched_tokens),
            "reduction_vs_full_repo_pct": self._reduction(slice_tokens, self._total_tokens),
            "files_in_slice": len(slice_files),
        }
        diagnostics: dict[str, Any] = {
            "truncated": truncated,
            "omitted": omitted,
            "expand_hint": (
                "Omitted items are the lowest-ranked by hop and degree. Raise max_tokens "
                "or hops, or add include_metadata=true to see what was left out."
            ),
            "unresolved_project_calls_in_slice": self._unresolved_in(selected),
            "parse_errors": sorted(
                self._rel(path)
                for path, parsed in self._parsed.items()
                if parsed.has_parse_errors
            ),
        }
        payload: dict[str, Any] = {
            "compiled_context": compiled,
            "token_metrics": metrics,
            "diagnostics": diagnostics,
        }
        if include_metadata:
            metadata: dict[str, list[dict[str, Any]]] = {
                "upstream_callers": [],
                "downstream_callees": [],
                "relevant_types_and_schemas": [],
            }
            for snippet in selected:
                if snippet.role == "target":
                    continue
                metadata[
                    {
                        "upstream_caller": "upstream_callers",
                        "downstream_callee": "downstream_callees",
                        "data_schema": "relevant_types_and_schemas",
                    }[snippet.role]
                ].append(self._snippet_meta(snippet))
            payload["target"] = self._symbol_meta(target)
            payload["hops"] = hops
            payload.update(metadata)
            payload["regions"] = [
                {
                    "file_path": self._rel(region.file_path),
                    "lines": f"{region.start_line}-{region.end_line}",
                    "byte_range": [region.start_byte, region.end_byte],
                    "symbols": region.symbols,
                    "symbol_ids": region.symbol_ids,
                    "roles": sorted(region.roles),
                }
                for region in regions
            ]
        return payload

    def _finalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Computes ``payload_tokens`` on the exact compact JSON string returned."""
        metrics = payload["token_metrics"]
        for _ in range(4):
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            count = self.count_tokens(text)
            if metrics.get("payload_tokens") == count:
                break
            metrics["payload_tokens"] = count
        return payload

    def _not_found(self, symbol_name: str) -> dict[str, Any]:
        return {
            "error": f"Symbol '{symbol_name}' not found in the indexed graph.",
            "hint": "Call index_codebase first, or use search_symbols to find exact names.",
            "available_symbols_sample": sorted(self._by_qualified)[:40],
        }

    def _ambiguity_hint(
        self, target: SymbolNode, alternatives: Sequence[SymbolNode]
    ) -> dict[str, Any] | None:
        if not alternatives:
            return None
        return {
            "note": (
                f"{len(alternatives) + 1} symbols are named '{target.name}'; "
                f"'{target.qualified_name}' was chosen because it has the most dependents."
            ),
            "chosen": f"{self._rel(target.file_path)}::{target.qualified_name}",
            "alternatives": [
                f"{self._rel(symbol.file_path)}::{symbol.qualified_name}"
                for symbol in alternatives[:10]
            ],
        }

    def _unresolved_in(self, snippets: Sequence[_Snippet]) -> int:
        unresolved = 0
        for snippet in self._dedupe_snippets(snippets):
            for site in snippet.symbol.calls:
                if site.name not in self._by_name or site.name in BUILTIN_CALL_NAMES:
                    continue
                if not self._resolve_call_site(snippet.symbol, site):
                    unresolved += 1
        return unresolved

    @staticmethod
    def _dedupe_snippets(snippets: Sequence[_Snippet]) -> list[_Snippet]:
        seen: set[str] = set()
        unique: list[_Snippet] = []
        for snippet in snippets:
            if snippet.symbol.symbol_id in seen:
                continue
            seen.add(snippet.symbol.symbol_id)
            unique.append(snippet)
        return unique

    def _bfs(self, start: str, hops: int, direction: Literal["up", "down"]) -> dict[str, int]:
        distances: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        visited = {start}
        while queue:
            current, depth = queue.popleft()
            if depth >= hops:
                continue
            neighbours = (
                self.graph.predecessors(current)
                if direction == "up"
                else self.graph.successors(current)
            )
            for neighbour in sorted(neighbours):
                edge = (
                    self.graph.get_edge_data(neighbour, current)
                    if direction == "up"
                    else self.graph.get_edge_data(current, neighbour)
                ) or {}
                if not (_edge_relations(edge) & CALL_RELATIONS) or neighbour in visited:
                    continue
                visited.add(neighbour)
                distances[neighbour] = depth + 1
                queue.append((neighbour, depth + 1))
        return distances

    def _select_within_budget(
        self, snippets: list[_Snippet], max_tokens: int | None
    ) -> tuple[list[_Snippet], bool]:
        if max_tokens is None:
            return snippets, False
        selected: list[_Snippet] = []
        budget = max(0, int(max_tokens))
        spent = 0
        for snippet in sorted(snippets, key=lambda s: (s.priority, s.hop, s.symbol.symbol_id)):
            cost = self._snippet_tokens(snippet)
            if spent + cost > budget and snippet.priority > 0:
                return selected, True
            selected.append(snippet)
            spent += cost
        return selected, False

    def _snippet_tokens(self, snippet: _Snippet) -> int:
        # Cheap upper-ish estimate used only for budget selection; the authoritative count is
        # computed from the rendered context afterwards.
        return max(1, (snippet.symbol.end_byte - snippet.symbol.start_byte) // 4)

    def _build_regions(self, snippets: Sequence[_Snippet]) -> list[_Region]:
        grouped: dict[str, list[_Snippet]] = {}
        for snippet in snippets:
            grouped.setdefault(snippet.symbol.file_path, []).append(snippet)

        regions: list[_Region] = []
        for file_path in sorted(grouped):
            ordered = sorted(
                grouped[file_path], key=lambda item: (item.symbol.start_byte, item.symbol.end_byte)
            )
            current: _Region | None = None
            for snippet in ordered:
                symbol = snippet.symbol
                if current is not None and symbol.start_byte <= current.end_byte:
                    current.end_byte = max(current.end_byte, symbol.end_byte)
                    current.end_line = max(current.end_line, symbol.end_line)
                    if symbol.qualified_name not in current.symbols:
                        current.symbols.append(symbol.qualified_name)
                        current.symbol_ids.append(snippet.symbol.symbol_id)
                    current.roles.add(snippet.role)
                    continue
                if current is not None:
                    regions.append(current)
                current = _Region(
                    file_path=file_path,
                    start_byte=symbol.start_byte,
                    end_byte=symbol.end_byte,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    symbols=[symbol.qualified_name],
                    symbol_ids=[snippet.symbol.symbol_id],
                    roles={snippet.role},
                )
            if current is not None:
                regions.append(current)
        return regions

    def _render_context(self, regions: Sequence[_Region]) -> str:
        chunks: list[str] = []
        cache: dict[str, bytes] = {}
        for region in regions:
            data = cache.get(region.file_path)
            if data is None:
                try:
                    data = Path(region.file_path).read_bytes()
                except OSError:
                    continue
                cache[region.file_path] = data
            code = data[region.start_byte : region.end_byte].decode("utf-8", errors="replace")
            symbols = ", ".join(region.symbols)
            roles = ", ".join(sorted(region.roles))
            context = self._context_lines(region, code)
            header = (
                f"### {self._rel(region.file_path)}:{region.start_line}-{region.end_line} "
                f"[{symbols}] ({roles})"
            )
            chunks.append("\n".join([header, *context, code.rstrip()]))
        return "\n\n".join(chunks)

    def _context_lines(self, region: _Region, code: str) -> list[str]:
        """Adds enclosing-class and import context without repeating the sliced code."""
        if not region.symbol_ids:
            return []
        symbol = self._symbols.get(region.symbol_ids[0])
        if symbol is None:
            return []
        lines: list[str] = []
        for enclosing in self._enclosing_classes(symbol):
            if enclosing.qualified_name in region.symbols:
                break
            lines.append(
                f"# class {enclosing.qualified_name}: "
                f"({self._rel(enclosing.file_path)}:{enclosing.start_line}-{enclosing.end_line})"
            )
            break
        parsed = self._parsed.get(region.file_path)
        if parsed is not None:
            rendered: list[str] = []
            for ref in parsed.imports:
                text = self._format_import(ref)
                if not text or len(rendered) >= 8:
                    continue
                names = [original for _, original in ref.names] or [ref.alias or ref.module]
                if any(name and name in code for name in names) or ref.wildcard:
                    rendered.append(f"# import: {text}")
            if rendered:
                lines.extend(rendered)
        return lines

    def _format_import(self, ref: Any) -> str:
        if ref.is_module:
            alias = f" as {ref.alias}" if ref.alias and ref.alias != ref.module else ""
            return f"import {ref.module}{alias}"
        if ref.names:
            names = ", ".join(
                original if local == original else f"{original} as {local}"
                for local, original in ref.names
            )
            return f"from {ref.module} import {names}"
        return ""

    @staticmethod
    def _reduction(part: int, whole: int) -> float:
        if whole <= 0:
            return 0.0
        return round((1.0 - (part / whole)) * 100.0, 2)

    def _symbol_meta(self, symbol: SymbolNode, *, include_doc: bool = True) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "symbol_id": symbol.symbol_id,
            "qualified_name": symbol.qualified_name,
            "kind": symbol.kind,
            "file_path": self._rel(symbol.file_path),
            "lines": f"{symbol.start_line}-{symbol.end_line}",
            "language": symbol.language,
            "signature": symbol.signature,
            "exported": symbol.exported,
        }
        if include_doc and symbol.doc:
            meta["doc"] = symbol.doc
        return meta

    def _snippet_meta(self, snippet: _Snippet) -> dict[str, Any]:
        meta = self._symbol_meta(snippet.symbol)
        meta["role"] = snippet.role
        meta["hop_distance"] = snippet.hop
        return meta

    # ------------------------------------------------------------------ analysis

    def get_blast_radius(
        self, symbol_name: str, max_depth: int = 5, include_types: bool = False
    ) -> dict[str, Any]:
        with self.lock:
            if not self._symbols:
                self.index_repository()
            target, candidates = self.resolve_strict(symbol_name)
            if target is None:
                if candidates:
                    return self._ambiguous_response(symbol_name, candidates)
                return self._not_found(symbol_name)

            relations = ALL_RELATIONS if include_types else CALL_RELATIONS
            max_depth = max(1, min(int(max_depth), 20))
            by_hop: dict[int, list[dict[str, Any]]] = {}
            impacted_files: set[str] = set()
            queue: deque[tuple[str, int]] = deque([(target.symbol_id, 0)])
            visited = {target.symbol_id}

            while queue:
                current, depth = queue.popleft()
                if depth >= max_depth:
                    continue
                for caller in sorted(self.graph.predecessors(current)):
                    edge = self.graph.get_edge_data(caller, current) or {}
                    if not (_edge_relations(edge) & relations) or caller in visited:
                        continue
                    visited.add(caller)
                    symbol = self._symbols[caller]
                    relative = self._rel(symbol.file_path)
                    impacted_files.add(relative)
                    by_hop.setdefault(depth + 1, []).append(
                        {
                            **self._symbol_meta(symbol, include_doc=False),
                            "relation": edge.get("relation", "calls"),
                            "relations": sorted(_edge_relations(edge)),
                            "confidence": edge.get("confidence", 0.5),
                        }
                    )
                    queue.append((caller, depth + 1))

            total = sum(len(group) for group in by_hop.values())
            return {
                "target": self._symbol_meta(target, include_doc=False),
                "risk_level": self._risk_level(total),
                "total_impacted_symbols": total,
                "impacted_files_count": len(impacted_files),
                "impacted_files": sorted(impacted_files),
                "dependents_by_hop": {hop: by_hop[hop] for hop in sorted(by_hop)},
            }

    @staticmethod
    def _risk_level(total: int) -> str:
        if total >= 25:
            return "CRITICAL"
        if total >= 10:
            return "HIGH"
        if total >= 3:
            return "MODERATE"
        if total >= 1:
            return "LOW"
        return "NONE"

    def find_circular_dependencies(self) -> dict[str, Any]:
        """Reports multi-node cycles and direct self-recursion separately."""
        with self.lock:
            if not self._symbols:
                self.index_repository()

            symbol_cycles: list[list[dict[str, str]]] = []
            self_recursive: list[dict[str, str]] = []
            for component in nx.strongly_connected_components(self.graph):
                if len(component) > 1:
                    symbol_cycles.append(
                        [
                            {
                                "qualified_name": self._symbols[node_id].qualified_name,
                                "file_path": self._rel(self._symbols[node_id].file_path),
                            }
                            for node_id in sorted(component)
                        ]
                    )
                else:
                    node_id = next(iter(component))
                    if self.graph.has_edge(node_id, node_id):
                        symbol = self._symbols[node_id]
                        self_recursive.append(
                            {
                                "qualified_name": symbol.qualified_name,
                                "file_path": self._rel(symbol.file_path),
                                "lines": f"{symbol.start_line}-{symbol.end_line}",
                            }
                        )

            file_graph = nx.DiGraph()
            for file_path, targets in self._import_targets.items():
                for target in targets:
                    if target != file_path:
                        file_graph.add_edge(self._rel(file_path), self._rel(target))

            file_cycles = [
                sorted(component)
                for component in nx.strongly_connected_components(file_graph)
                if len(component) > 1
            ]
            file_cycles.sort()

            return {
                "symbol_cycle_count": len(symbol_cycles),
                "symbol_cycles": sorted(symbol_cycles, key=lambda c: c[0]["qualified_name"]),
                "self_recursive_count": len(self_recursive),
                "self_recursive": sorted(self_recursive, key=lambda item: item["qualified_name"]),
                "file_cycle_count": len(file_cycles),
                "file_cycles": file_cycles,
                "note": "file_cycles are derived from import statements, not call edges.",
            }

    def explain_edge(self, source: str, target: str) -> dict[str, Any]:
        """Explains why an edge exists: call site, confidence and resolution path."""
        with self.lock:
            if not self._symbols:
                self.index_repository()
            source_symbol, source_candidates = self.resolve_strict(source)
            if source_symbol is None:
                if source_candidates:
                    return self._ambiguous_response(source, source_candidates)
                return self._not_found(source)
            target_symbol, target_candidates = self.resolve_strict(target)
            if target_symbol is None:
                if target_candidates:
                    return self._ambiguous_response(target, target_candidates)
                return self._not_found(target)

            edge = self.graph.get_edge_data(source_symbol.symbol_id, target_symbol.symbol_id)
            if edge is None:
                return {
                    "exists": False,
                    "source": self._symbol_meta(source_symbol, include_doc=False),
                    "target": self._symbol_meta(target_symbol, include_doc=False),
                }
            call_site: dict[str, Any] | None = None
            if edge.get("line"):
                try:
                    line_text = Path(source_symbol.file_path).read_text(
                        errors="replace"
                    ).splitlines()[int(edge["line"]) - 1]
                except (OSError, IndexError):
                    line_text = ""
                call_site = {
                    "line": edge["line"],
                    "text": line_text.strip()[:200],
                    "receiver": edge.get("receiver"),
                    "name": edge.get("name"),
                }
            return {
                "exists": True,
                "source": self._symbol_meta(source_symbol, include_doc=False),
                "target": self._symbol_meta(target_symbol, include_doc=False),
                "relation": edge.get("relation"),
                "relations": sorted(_edge_relations(edge)),
                "confidence": edge.get("confidence"),
                "resolution": edge.get("resolution"),
                "call_site": call_site,
            }

    def impact_of_diff(
        self,
        base: str = "HEAD~1",
        head: str = "HEAD",
        diff: str | None = None,
        max_depth: int = 3,
    ) -> dict[str, Any]:
        """Maps a git diff to changed symbols and impacted symbols."""
        with self.lock:
            if not self._symbols:
                self.index_repository()

            if diff is None:
                diff_text, error = self._git_diff(base, head)
                if error is not None:
                    return {"error": error, "base": base, "head": head}
                source = f"git diff {base}..{head}"
            else:
                diff_text, source = diff, "provided diff"

            changed_lines = self._parse_unified_diff(diff_text)
            changed_symbols: list[SymbolNode] = []
            for relative, lines in sorted(changed_lines.items()):
                absolute = str((self.repo_path / relative).resolve())
                for symbol_id in sorted(self._by_file.get(absolute, set())):
                    symbol = self._symbols[symbol_id]
                    if any(symbol.start_line <= line <= symbol.end_line for line in lines):
                        changed_symbols.append(symbol)

            changed_ids = {symbol.symbol_id for symbol in changed_symbols}
            impacted: dict[str, int] = {}
            for symbol in changed_symbols:
                for caller_id, hop in self._upstream_tags(symbol.symbol_id, max_depth).items():
                    if caller_id in changed_ids:
                        continue
                    impacted[caller_id] = min(hop, impacted.get(caller_id, hop))

            return {
                "source": source,
                "base": base,
                "head": head,
                "changed_files": sorted(changed_lines),
                "changed_symbols": [
                    self._symbol_meta(symbol, include_doc=False) for symbol in changed_symbols
                ],
                "impacted_symbols": [
                    {
                        **self._symbol_meta(self._symbols[symbol_id], include_doc=False),
                        "hop_distance": hop,
                    }
                    for symbol_id, hop in sorted(
                        impacted.items(), key=lambda item: (item[1], item[0])
                    )
                ],
                "total_changed_symbols": len(changed_symbols),
                "total_impacted_symbols": len(impacted),
                "risk_level": self._risk_level(len(impacted)),
                "note": "Impact is upstream call-graph reachability over calls/constructs.",
            }

    def _git_diff(self, base: str, head: str) -> tuple[str, str | None]:
        if base.startswith("-") or head.startswith("-"):
            return "", "invalid git revision"
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.repo_path), "diff", "-U0", base, head, "--"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return "", f"git diff failed: {error}"
        if completed.returncode != 0:
            return "", (completed.stderr.strip()[:500] or "git diff failed")
        return completed.stdout, None

    @staticmethod
    def _parse_unified_diff(diff_text: str) -> dict[str, set[int]]:
        changed: dict[str, set[int]] = {}
        current: str | None = None
        for line in diff_text.splitlines():
            if line.startswith("+++ "):
                target = line[4:].split("\t")[0].strip()
                current = target[2:] if target.startswith("b/") else None
                if current is not None:
                    changed.setdefault(current, set())
            elif line.startswith("@@") and current is not None:
                match = re.search(r"\+(\d+)(?:,(\d+))?", line)
                if match:
                    start = int(match.group(1))
                    count = int(match.group(2) or 1)
                    changed[current].update(range(start, start + max(count, 1)))
        return {path: lines for path, lines in changed.items() if lines}

    def _upstream_tags(self, start: str, max_depth: int) -> dict[str, int]:
        found: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        visited = {start}
        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for caller in sorted(self.graph.predecessors(current)):
                edge = self.graph.get_edge_data(caller, current) or {}
                if not (_edge_relations(edge) & CALL_RELATIONS) or caller in visited:
                    continue
                visited.add(caller)
                found[caller] = depth + 1
                queue.append((caller, depth + 1))
        return found

    def get_index_stats(self) -> dict[str, Any]:
        with self.lock:
            if not self._symbols:
                self.index_repository()
            stats = dict(self._last_index_stats) or self._index_stats(0.0, 0, 0)
            cache_stats = self.cache.stats()
            languages = Counter(symbol.language for symbol in self._symbols.values())
            stats.update(
                {
                    "languages": dict(sorted(languages.items())),
                    "total_source_tokens": self._total_tokens,
                    "unresolved_call_sites": self._unresolved_calls,
                    "files_with_parse_errors": sorted(
                        self._rel(path)
                        for path, parsed in self._parsed.items()
                        if parsed.has_parse_errors
                    ),
                    "skipped_files": {
                        self._rel(path): reason for path, reason in sorted(self._skipped.items())
                    },
                    "cache": {
                        "path": self._rel(self.cache.db_path),
                        "files": cache_stats.files,
                        "schema_version": cache_stats.schema_version,
                    },
                    "indexed_at": self._indexed_at,
                }
            )
            return stats

    def _index_stats(
        self, elapsed_ms: float, cache_hits: int, cache_misses: int
    ) -> dict[str, Any]:
        return {
            "repo_path": str(self.repo_path),
            "files_indexed": len(self._by_file),
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "total_symbols": self.graph.number_of_nodes(),
            "total_edges": self.graph.number_of_edges(),
            "skipped_files": {
                self._rel(path): reason for path, reason in sorted(self._skipped.items())
            },
            "elapsed_ms": round(elapsed_ms, 2),
        }

    def close(self) -> None:
        self.cache.close()

    def __enter__(self) -> GraphEngine:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
