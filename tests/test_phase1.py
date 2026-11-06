"""Regression tests for the v0.3.0 correctness and safety contract."""

from __future__ import annotations

import json
import random
import textwrap
from pathlib import Path

import pytest

from polyglot_ast_slicer.graph_engine import GraphEngine


def _repo(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")
    return root


def test_no_name_only_edges(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "a.py": "def caller() -> int:\n    return helper()\n",
            "b.py": "def helper() -> int:\n    return 1\n",
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)

    slice_data = engine.get_ast_call_slice("caller", hops=1, include_metadata=True)
    assert slice_data["downstream_callees"] == []
    assert engine.graph.number_of_edges() == 0
    engine.close()


def test_self_recursion_is_an_edge_and_a_reported_cycle(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {"r.py": "def fact(n: int) -> int:\n    return n * fact(n - 1)\n"},
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)

    symbol = engine.resolve_strict("fact")[0]
    assert symbol is not None
    assert engine.graph.has_edge(symbol.symbol_id, symbol.symbol_id)

    cycles = engine.find_circular_dependencies()
    assert cycles["self_recursive_count"] == 1
    assert cycles["self_recursive"][0]["qualified_name"] == "fact"
    assert cycles["symbol_cycle_count"] == 0
    engine.close()


def test_default_budget_and_hub_fanout_caps(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    lines = ["def hub() -> int:", "    return 1", ""]
    for index in range(120):
        lines += [f"def caller_{index}() -> int:", "    return hub()", ""]
    (repo / "hub.py").write_text("\n".join(lines), encoding="utf-8")

    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    result = engine.get_ast_call_slice("hub", hops=1, include_metadata=True)

    assert result["token_metrics"]["payload_tokens"] <= 6000
    assert result["token_metrics"]["budget_tokens"] == 6000
    assert result["diagnostics"]["omitted"]["upstream"] > 0
    assert result["token_metrics"]["slice_tokens"] <= 6000
    engine.close()


def test_payload_tokens_match_exact_json(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "a.py": "from b import helper\n\ndef caller() -> int:\n    return helper()\n",
            "b.py": "def helper() -> int:\n    return 1\n",
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    payload = engine.get_ast_call_slice("caller", hops=1, include_metadata=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert payload["token_metrics"]["payload_tokens"] == engine.count_tokens(text)
    engine.close()


def test_slim_payload_is_default(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r", {"a.py": "def f() -> int:\n    return 1\n"})
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)

    slim = engine.get_ast_call_slice("f", hops=1)
    assert set(slim) == {"compiled_context", "token_metrics", "diagnostics"}
    full = engine.get_ast_call_slice("f", hops=1, include_metadata=True)
    assert "target" in full and "regions" in full
    engine.close()


def test_incremental_graph_equals_from_scratch_after_random_edits(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "a.py").write_text(
        "from pkg.b import helper\n\ndef use() -> int:\n    return helper()\n"
    )
    (repo / "pkg" / "b.py").write_text("def helper() -> int:\n    return 1\n")

    incremental = GraphEngine(repo)
    incremental.index_repository(force_rebuild=True)

    def edges(engine: GraphEngine) -> set[tuple[str, str, str, float]]:
        return {
            (u, v, data["relation"], data["confidence"])
            for u, v, data in engine.graph.edges(data=True)
        }

    # 1) new module that an existing file already imports (the historical drift case)
    (repo / "pkg" / "c.py").write_text("def thing() -> int:\n    return 2\n")
    (repo / "pkg" / "a.py").write_text(
        "from pkg.b import helper\nfrom pkg.c import thing\n\n"
        "def use() -> int:\n    return helper() + thing()\n"
    )
    incremental.refresh_file(repo / "pkg" / "c.py")
    incremental.refresh_file(repo / "pkg" / "a.py")

    # 2) rename a callee and add a new caller by random-ish sequence
    (repo / "pkg" / "b.py").write_text("def helper_renamed() -> int:\n    return 1\n")
    incremental.refresh_file(repo / "pkg" / "b.py")
    (repo / "pkg" / "a.py").write_text(
        "from pkg.b import helper_renamed\n\n"
        "def use() -> int:\n    return helper_renamed()\n\n"
        "def use_twice() -> int:\n    return use() + use()\n"
    )
    incremental.refresh_file(repo / "pkg" / "a.py")

    fresh = GraphEngine(repo)
    fresh.index_repository(force_rebuild=True)
    assert edges(incremental) == edges(fresh)

    incremental.close()
    fresh.close()


def test_symlink_escape_is_skipped_not_crashing(tmp_path: Path) -> None:
    inside = tmp_path / "repo"
    (inside / "pkg").mkdir(parents=True)
    (inside / "pkg" / "app.py").write_text("def public() -> int:\n    return 1\n")
    outside = tmp_path / "outside.py"
    outside.write_text("def secret() -> str:\n    return 'TOPSECRET'\n")
    (inside / "pkg" / "leak.py").symlink_to(outside)

    engine = GraphEngine(inside)
    stats = engine.index_repository(force_rebuild=True)
    assert stats["files_indexed"] == 1
    assert engine.resolve_strict("secret")[0] is None
    assert any("symlink escapes" in reason for reason in stats["skipped_files"].values())
    engine.close()


def test_cache_lives_outside_repo_and_read_only_checkouts_work(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "r", {"a.py": "def f() -> int:\n    return 1\n"})
    cache_dir = tmp_path / "cache"
    engine = GraphEngine(repo, cache_dir=cache_dir)
    engine.index_repository(force_rebuild=True)
    assert cache_dir in engine.cache.db_path.parents
    assert not any(p.name.startswith(".ast_slicer_cache") for p in repo.iterdir())
    engine.close()

    for path in repo.rglob("*"):
        path.chmod(0o555)
    repo.chmod(0o555)
    try:
        readonly = GraphEngine(repo, cache_dir=cache_dir)
        assert readonly.index_repository(force_rebuild=False)["files_indexed"] == 1
        readonly.close()
    finally:
        repo.chmod(0o755)
        for path in repo.rglob("*"):
            path.chmod(0o644)


def test_allowed_roots_reject_outside_repos(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    repo = _repo(tmp_path / "outside", {"a.py": "def f() -> int:\n    return 1\n"})
    with pytest.raises(PermissionError):
        GraphEngine(repo, allowed_roots=[allowed])


def test_binary_and_oversized_files_are_reported(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "ok.py").write_text("def ok() -> int:\n    return 1\n")
    (repo / "bin.py").write_bytes(b"def bin():\x00\n    return 1\n")
    (repo / "big.py").write_text("def big() -> int:\n    return 1\n" + "# pad\n" * 200)

    engine = GraphEngine(repo, max_file_bytes=300)
    stats = engine.index_repository(force_rebuild=True)
    reasons = " ".join(stats["skipped_files"].values())
    assert stats["files_indexed"] == 1
    assert "too large" in reasons
    assert "binary" in reasons
    engine.close()


def test_explain_edge_reports_provenance(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "a.py": "from b import helper\n\ndef caller() -> int:\n    return helper()\n",
            "b.py": "def helper() -> int:\n    return 1\n",
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    explained = engine.explain_edge("a.py::caller", "b.py::helper")
    assert explained["exists"] is True
    assert explained["relation"] == "calls"
    assert explained["resolution"].startswith("import:")
    assert explained["call_site"]["line"] == 4
    assert "helper()" in explained["call_site"]["text"]
    engine.close()


def test_ambiguous_query_returns_all_candidates(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "a.py": "class A:\n    def run(self) -> int:\n        return 1\n",
            "b.py": "class B:\n    def run(self) -> int:\n        return 2\n",
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    result = engine.get_ast_call_slice("run", hops=1)
    assert result["code"] == "ambiguous_symbol"
    assert {c["qualified_name"] for c in result["candidates"]} == {"A.run", "B.run"}

    qualified = engine.get_ast_call_slice("a.py::A.run", hops=1)
    assert "error" not in qualified
    engine.close()


def test_cache_dir_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_dir = tmp_path / "env-cache"
    monkeypatch.setenv("POLYGLOT_AST_CACHE_DIR", str(cache_dir))
    repo = _repo(tmp_path / "r", {"a.py": "def f() -> int:\n    return 1\n"})
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    assert engine.cache.db_path.exists()
    assert engine.cache.db_path.parent == cache_dir.resolve()
    engine.close()


def test_incremental_equals_rebuild_after_each_random_edit(tmp_path: Path) -> None:
    """Hypothesis-style differential test: random add/remove/rename sequences."""
    repo = tmp_path / "r"
    repo.mkdir()
    rng = random.Random(20261031)
    b_funcs: set[str] = {"alpha"}
    c_funcs: set[str] = {"beta"}

    def render(module: str, funcs: set[str]) -> str:
        return "\n\n".join(
            f"def {name}() -> int:\n    return {index}"
            for index, name in enumerate(sorted(funcs))
        )

    def write_project() -> list[Path]:
        changed = []
        for name, funcs in (("b", b_funcs), ("c", c_funcs)):
            path = repo / f"{name}.py"
            path.write_text(render(name, funcs) + "\n")
            changed.append(path)
        calls = " + ".join(sorted(b_funcs | c_funcs)) or "0"
        imports = []
        if b_funcs:
            imports.append(f"from b import {', '.join(sorted(b_funcs))}")
        if c_funcs:
            imports.append(f"from c import {', '.join(sorted(c_funcs))}")
        use = repo / "a.py"
        use.write_text("\n".join(imports) + "\n\ndef use() -> int:\n    return " + calls + "\n")
        changed.append(use)
        return changed

    incremental = GraphEngine(repo)
    incremental.index_repository(force_rebuild=True)

    def edges(engine: GraphEngine) -> set[tuple[str, str, str]]:
        return {(u, v, data["relation"]) for u, v, data in engine.graph.edges(data=True)}

    for _ in range(10):
        operation = rng.choice(["add", "remove", "rename"])
        pool = b_funcs if rng.random() < 0.5 else c_funcs
        if operation == "add" or not pool:
            pool.add(f"fn_{rng.randrange(1000)}")
        elif operation == "remove":
            pool.discard(sorted(pool)[rng.randrange(len(pool))])
        else:
            old = sorted(pool)[rng.randrange(len(pool))]
            pool.discard(old)
            pool.add(f"{old}_v{rng.randrange(100)}")

        for path in write_project():
            incremental.refresh_file(path)

        fresh = GraphEngine(repo)
        fresh.index_repository(force_rebuild=True)
        assert edges(incremental) == edges(fresh), f"diverged after {operation}: {sorted(pool)}"
        fresh.close()

    incremental.close()


def test_bare_call_never_binds_to_enclosing_method(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "a.py": (
                "class A:\n"
                "    def open(self) -> str:\n"
                "        return 'method'\n\n"
                "    def use(self) -> str:\n"
                "        return open('x')\n"
            )
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    use = engine.resolve_strict("a.py::A.use")[0]
    assert use is not None
    assert not list(engine.graph.successors(use.symbol_id))
    engine.close()


def test_nested_symbol_resolves_within_its_scope(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "t.py": (
                "def test_case() -> int:\n"
                "    class LocalError(Exception):\n"
                "        pass\n\n"
                "    def helper() -> int:\n"
                "        return 1\n\n"
                "    raise LocalError('boom')\n"
            )
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    caller = engine.resolve_strict("t.py::test_case")[0]
    assert caller is not None
    targets = {engine._symbols[v].qualified_name for v in engine.graph.successors(caller.symbol_id)}
    assert "test_case.LocalError" in targets
    engine.close()


def test_slice_includes_enclosing_class_and_imports(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "models.py": "class Payload:\n    pass\n",
            "service.py": (
                "from models import Payload\n\n"
                "class Engine:\n"
                "    def run(self, payload: Payload) -> str:\n"
                "        return 'ok'\n"
            ),
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    context = engine.get_ast_call_slice("service.py::Engine.run", hops=1)["compiled_context"]
    assert "# class Engine:" in context
    assert "# import: from models import Payload" in context
    assert context.count("def run(") == 1
    engine.close()


def test_impact_of_diff_on_real_commits(tmp_path: Path) -> None:
    import subprocess

    repo = _repo(
        tmp_path / "r",
        {
            "b.py": "def helper() -> int:\n    return 1\n",
            "a.py": "from b import helper\n\ndef use() -> int:\n    return helper()\n",
            "tests/test_a.py": "from a import use\n\ndef test_use():\n    assert use() == 1\n",
        },
    )

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "review@example.com")
    git("config", "user.name", "Review")
    git("add", "-A")
    git("commit", "-qm", "initial")
    (repo / "b.py").write_text("def helper() -> int:\n    return 2\n")
    git("commit", "-qam", "change helper")

    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    result = engine.impact_of_diff("HEAD~1", "HEAD")
    assert result["changed_symbols"][0]["qualified_name"] == "helper"
    assert {item["qualified_name"] for item in result["impacted_symbols"]} >= {"use"}
    assert result["total_impacted_symbols"] >= 1
    engine.close()

    engine2 = GraphEngine(repo)
    engine2.index_repository(force_rebuild=True)
    diff = (
        "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-def helper() -> int:\n+def helper() -> int:\n"
    )
    literal = engine2.impact_of_diff(diff=diff)
    assert literal["changed_files"] == ["b.py"]
    engine2.close()


def test_tsconfig_paths_and_barrel_reexports(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "tsconfig.json": (
                '{\n  // base for aliases\n  "compilerOptions": {\n'
                '    "baseUrl": ".",\n    "paths": {"@lib/*": ["src/lib/*"]}\n  }\n}\n'
            ),
            "src/lib/util.ts": "export function helper(x: number): number { return x; }\n",
            "src/lib/index.ts": 'export * from "./util";\n',
            "src/app.ts": (
                'import { helper } from "@lib/index";\n\n'
                "export function run(): number { return helper(1); }\n"
            ),
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    result = engine.get_ast_call_slice("src/app.ts::run", hops=1, include_metadata=True)
    assert "helper" in {item["qualified_name"] for item in result["downstream_callees"]}
    engine.close()


def test_tsx_and_js_grammars_when_available(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "widget.tsx": (
                "export interface Props { title: string }\n\n"
                "export function Widget({ title }: Props) {\n"
                '  return <div className="widget">{title}</div>;\n'
                "}\n"
            ),
            "legacy.js": "function oldSchool(x) { return x + 1; }\n",
        },
    )
    engine = GraphEngine(repo)
    stats = engine.index_repository(force_rebuild=True)
    if any("grammar" in reason.lower() for reason in stats["skipped_files"].values()):
        engine.close()
        pytest.skip("grammar download unavailable in this environment")
    assert engine.resolve_strict("Widget")[0] is not None
    assert engine.resolve_strict("oldSchool")[0] is not None
    engine.close()


def test_tsconfig_extends_relative_and_package(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "configs/base.json": (
                '{"compilerOptions": {"paths": {"@lib/*": ["src/lib/*"]}}}'
            ),
            "node_modules/@org/tsconfig/tsconfig.json": (
                '{"compilerOptions": {"baseUrl": "../../../"}}'
            ),
            "tsconfig.json": (
                '{"extends": ["./configs/base.json", "@org/tsconfig/tsconfig"],'
                ' "compilerOptions": {"strict": true}}'
            ),
            "src/lib/util.ts": "export function helper(x: number): number { return x; }\n",
            "src/app.ts": 'import { helper } from "@lib/util";\n'
            "export function run(): number { return helper(1); }\n",
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    result = engine.get_ast_call_slice("src/app.ts::run", hops=1, include_metadata=True)
    assert "helper" in {item["qualified_name"] for item in result["downstream_callees"]}
    engine.close()


def test_commonjs_exports_and_require_destructuring(tmp_path: Path) -> None:
    (tmp_path / "util.cjs").write_text(
        "exports.helper = function helper() { return 1; };\n", encoding="utf-8"
    )
    (tmp_path / "main.cjs").write_text(
        'const { helper } = require("./util");\n'
        "module.exports.run = function run() { return helper(); };\n",
        encoding="utf-8",
    )
    engine = GraphEngine(tmp_path)
    stats = engine.index_repository(force_rebuild=True)
    if any("grammar" in reason.lower() for reason in stats["skipped_files"].values()):
        engine.close()
        pytest.skip("JavaScript grammar unavailable")
    run = engine.resolve_strict("main.cjs::run")[0]
    assert run is not None and run.exported
    assert any(
        engine._symbols[v].qualified_name == "helper"
        for v in engine.graph.successors(run.symbol_id)
    )
    engine.close()


def test_refresh_relinks_module_add_delete_rename_from_importer(tmp_path: Path) -> None:
    """Adding/deleting/renaming a module must be visible when only its importer is refreshed."""
    repo = _repo(
        tmp_path / "r",
        {
            "a.py": "from b import thing\n\n\ndef use() -> int:\n    return thing()\n",
            "b.py": "def thing() -> int:\n    return 1\n",
        },
    )
    incremental = GraphEngine(repo)
    incremental.index_repository(force_rebuild=True)

    def edges(engine: GraphEngine) -> set[tuple[str, str, str]]:
        return {
            (u, v, data["relation"])
            for u, v, data in engine.graph.edges(data=True)
            if data["relation"] in ("calls", "constructs")
        }

    def assert_matches_rebuild() -> None:
        fresh = GraphEngine(repo)
        fresh.index_repository(force_rebuild=True)
        assert edges(incremental) == edges(fresh)
        fresh.close()

    # Add a module the importer is edited to use; refresh only the importer.
    (repo / "c.py").write_text("def other() -> int:\n    return 2\n", encoding="utf-8")
    (repo / "a.py").write_text(
        "from c import other\n\n\ndef use() -> int:\n    return other()\n", encoding="utf-8"
    )
    incremental.refresh_file(repo / "a.py")
    assert ("use", "calls", "other") in {
        (incremental._symbols[u].qualified_name, rel, incremental._symbols[v].qualified_name)
        for u, v, rel in edges(incremental)
    }
    assert_matches_rebuild()

    # Delete the module; refresh only the importer.
    (repo / "c.py").unlink()
    (repo / "a.py").write_text(
        "from b import thing\n\n\ndef use() -> int:\n    return thing()\n", encoding="utf-8"
    )
    incremental.refresh_file(repo / "a.py")
    assert all(
        incremental._symbols[v].qualified_name != "other"
        for _, v, _ in edges(incremental)
    )
    assert_matches_rebuild()

    # Rename the module; refresh only the importer.
    (repo / "b.py").rename(repo / "d.py")
    (repo / "d.py").write_text("def thing() -> int:\n    return 3\n", encoding="utf-8")
    (repo / "a.py").write_text(
        "from d import thing\n\n\ndef use() -> int:\n    return thing()\n", encoding="utf-8"
    )
    incremental.refresh_file(repo / "a.py")
    assert_matches_rebuild()
    incremental.close()


def test_call_and_type_edges_to_one_target_both_survive(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "m.py": (
                "class Foo:\n"
                "    pass\n\n\n"
                "def make() -> Foo:\n"
                "    return Foo()\n"
            )
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    make = engine.resolve_strict("make")[0]
    foo = engine.resolve_strict("Foo")[0]
    assert make is not None and foo is not None

    edge = engine.graph.get_edge_data(make.symbol_id, foo.symbol_id)
    assert edge is not None
    relations = set(edge.get("relations") or [edge["relation"]])
    assert {"constructs", "uses_type"} <= relations

    explained = engine.explain_edge("m.py::make", "m.py::Foo")
    assert explained["exists"] is True
    assert {"constructs", "uses_type"} <= set(explained["relations"])

    sliced = engine.get_ast_call_slice("make", hops=1, include_metadata=True)
    assert "Foo" in [item["qualified_name"] for item in sliced["downstream_callees"]]

    blast = engine.get_blast_radius("Foo")
    assert "make" in [item["qualified_name"] for item in blast["dependents_by_hop"][1]]

    cycles = engine.find_circular_dependencies()
    assert cycles["symbol_cycle_count"] == 0
    assert cycles["self_recursive_count"] == 0
    engine.close()


def test_cycle_detection_counts_each_pair_once_with_two_relations(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "m.py": (
                "class Ping:\n"
                "    pass\n\n\n"
                "class Pong:\n"
                "    pass\n\n\n"
                "def ping(n: int) -> Pong:\n"
                "    return pong(n)\n\n\n"
                "def pong(n: int) -> Ping:\n"
                "    return ping(n)\n"
            )
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    cycles = engine.find_circular_dependencies()
    assert cycles["symbol_cycle_count"] == 1
    assert sorted(item["qualified_name"] for item in cycles["symbol_cycles"][0]) == [
        "ping",
        "pong",
    ]
    engine.close()


def test_module_scope_calls_are_attributed_to_a_module_symbol(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "script.py": (
                "def work() -> int:\n"
                "    return 1\n\n\n"
                "if __name__ == \"__main__\":\n"
                "    work()\n"
            ),
            "only_defs.py": "def idle() -> int:\n    return 2\n",
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)

    module = engine.resolve_strict("script.py::<module>")[0]
    assert module is not None
    assert module.kind == "module"
    assert module.start_line == 5
    assert module.end_line == 6

    work = engine.resolve_strict("work")[0]
    assert work is not None
    assert engine.graph.has_edge(module.symbol_id, work.symbol_id)

    blast = engine.get_blast_radius("work")
    assert "<module>" in [item["qualified_name"] for item in blast["dependents_by_hop"][1]]

    sliced = engine.get_ast_call_slice("work", hops=1, include_metadata=True)
    assert "if __name__" in sliced["compiled_context"]
    assert "<module>" in [
        item["qualified_name"] for item in sliced["upstream_callers"]
    ]

    cycles = engine.find_circular_dependencies()
    assert cycles["symbol_cycle_count"] == 0
    assert cycles["self_recursive_count"] == 0
    assert engine.resolve_strict("only_defs.py::<module>")[0] is None
    engine.close()


def test_module_scope_call_outside_main_guard_is_attributed(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {"app.py": "def work() -> int:\n    return 1\n\n\nwork()\n"},
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    module = engine.resolve_strict("app.py::<module>")[0]
    work = engine.resolve_strict("work")[0]
    assert module is not None and work is not None
    assert engine.graph.has_edge(module.symbol_id, work.symbol_id)
    engine.close()


def test_property_getter_setter_and_overloads_keep_combined_spans(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "m.py": (
                "from typing import overload\n\n\n"
                "class C:\n"
                "    @property\n"
                "    def value(self) -> int:\n"
                "        return self._v\n\n"
                "    @value.setter\n"
                "    def value(self, v: int) -> None:\n"
                "        self._v = v\n\n"
                "    @overload\n"
                "    def parse(self, x: int) -> int: ...\n\n"
                "    @overload\n"
                "    def parse(self, x: str) -> str: ...\n\n"
                "    def parse(self, x):\n"
                "        return x\n"
            )
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)

    assert engine.resolve_strict("C.value")[0] is not None
    value = engine.resolve_strict("C.value")[0]
    assert value is not None
    assert value.start_line == 5
    assert value.end_line == 11

    value_slice = engine.get_ast_call_slice("C.value", hops=1)
    assert value_slice["compiled_context"].count("@property") == 1
    assert value_slice["compiled_context"].count("@value.setter") == 1

    parsed = engine.resolve_strict("C.parse")[0]
    assert parsed is not None
    assert parsed.start_line == 13
    assert parsed.end_line == 20
    parse_slice = engine.get_ast_call_slice("C.parse", hops=1)
    assert parse_slice["compiled_context"].count("@overload") == 2
    assert parse_slice["compiled_context"].count("def parse") == 3
    engine.close()


def test_super_init_and_constructed_receiver_calls_resolve(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "m.py": (
                "class A:\n"
                "    def __init__(self) -> None:\n"
                "        self.x = 1\n\n"
                "    def run(self) -> int:\n"
                "        return self.x\n\n\n"
                "class B(A):\n"
                "    def __init__(self) -> None:\n"
                "        super().__init__()\n\n\n"
                "class C:\n"
                "    def run(self) -> int:\n"
                "        return A().run()\n"
            )
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)

    b_init = engine.resolve_strict("B.__init__")[0]
    a_init = engine.resolve_strict("A.__init__")[0]
    assert b_init is not None and a_init is not None
    edge = engine.graph.get_edge_data(b_init.symbol_id, a_init.symbol_id)
    assert edge is not None
    assert edge["relation"] == "calls"
    assert edge["resolution"] == "super:A"

    c_run = engine.resolve_strict("C.run")[0]
    a_run = engine.resolve_strict("A.run")[0]
    assert c_run is not None and a_run is not None
    edge = engine.graph.get_edge_data(c_run.symbol_id, a_run.symbol_id)
    assert edge is not None
    assert edge["relation"] == "calls"
    assert edge["resolution"] == "constructed_receiver:A"

    cycles = engine.find_circular_dependencies()
    assert cycles["symbol_cycle_count"] == 0
    assert cycles["self_recursive_count"] == 0

    blast = engine.get_blast_radius("A.__init__")
    assert "B.__init__" in [item["qualified_name"] for item in blast["dependents_by_hop"][1]]
    explained = engine.explain_edge("B.__init__", "A.__init__")
    assert explained["exists"] is True
    assert explained["call_site"]["line"] == 11
    engine.close()


def test_imports_under_type_checking_resolve_annotated_receivers(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "a.py": (
                "from typing import TYPE_CHECKING\n\n"
                "if TYPE_CHECKING:\n"
                "    from b import Client\n\n\n"
                "def use(client: Client) -> int:\n"
                "    return client.run()\n"
            ),
            "b.py": (
                "class Client:\n"
                "    def run(self) -> int:\n"
                "        return 1\n"
            ),
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    use = engine.resolve_strict("use")[0]
    run = engine.resolve_strict("Client.run")[0]
    assert use is not None and run is not None
    edge = engine.graph.get_edge_data(use.symbol_id, run.symbol_id)
    assert edge is not None
    assert edge["resolution"].startswith("typed_receiver:client->Client")
    engine.close()


def test_imports_inside_functions_do_not_become_module_bindings(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "a.py": (
                "def use() -> int:\n"
                "    from b import helper\n\n"
                "    return helper()\n"
            ),
            "b.py": "def helper() -> int:\n    return 1\n",
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    use = engine.resolve_strict("use")[0]
    assert use is not None
    assert not list(engine.graph.successors(use.symbol_id))
    engine.close()


def _call_edges(engine: GraphEngine) -> set[tuple[str, str]]:
    return {
        (engine._symbols[source].qualified_name, engine._symbols[target].qualified_name)
        for source, target, data in engine.graph.edges(data=True)
        if data["relation"] in ("calls", "constructs")
    }


def test_ts_default_imports_resolve(tmp_path: Path) -> None:
    cases = {
        "function": (
            {
                "u.ts": "export default function main(): number { return 1; }\n",
                "i.ts": (
                    'import main from "./u";\n'
                    "export function run(): number { return main(); }\n"
                ),
            },
            ("run", "main"),
        ),
        "class": (
            {
                "u.ts": "export default class Widget { render(): number { return 1; } }\n",
                "i.ts": (
                    'import Widget from "./u";\n'
                    "export function run(): number {\n"
                    "  const widget = new Widget();\n"
                    "  return widget.render();\n"
                    "}\n"
                ),
            },
            ("run", "Widget"),
        ),
        "expression": (
            {
                "u.ts": "const handler = (): number => 1;\nexport default handler;\n",
                "i.ts": (
                    'import handler from "./u";\n'
                    "export function run(): number { return handler(); }\n"
                ),
            },
            ("run", "handler"),
        ),
        "module_exports": (
            {
                "u.js": "module.exports = function main() { return 1; };\n",
                "i.js": 'const main = require("./u");\nfunction run() { return main(); }\n',
            },
            ("run", "main"),
        ),
    }
    for name, (files, expected) in cases.items():
        repo = _repo(tmp_path / name, files)
        engine = GraphEngine(repo)
        engine.index_repository(force_rebuild=True)
        edges = _call_edges(engine)
        assert expected in edges, (name, sorted(edges))
        engine.close()


def test_ts_specifier_extension_mapping(tmp_path: Path) -> None:
    cases = {
        "js_to_ts": {
            "u.ts": "export function helper(): number { return 1; }\n",
            "e.mjs": (
                'import { helper } from "./u.js";\n'
                "export function esm() { return helper(); }\n"
            ),
        },
        "js_to_tsx": {
            "u.tsx": "export function helper(): number { return 1; }\n",
            "e.mjs": (
                'import { helper } from "./u.js";\n'
                "export function esm() { return helper(); }\n"
            ),
        },
        "mjs_to_mts": {
            "u.mts": "export function helper(): number { return 1; }\n",
            "e.mjs": (
                'import { helper } from "./u.mjs";\n'
                "export function esm() { return helper(); }\n"
            ),
        },
        "cjs_to_cts": {
            "u.cts": "export function helper(): number { return 1; }\n",
            "e.cjs": (
                'const { helper } = require("./u.cjs");\n'
                "module.exports.run = function run() { return helper(); };\n"
            ),
        },
        "jsx_to_tsx": {
            "u.tsx": "export function helper(): number { return 1; }\n",
            "e.jsx": (
                'import { helper } from "./u.jsx";\n'
                "export function run() { return helper(); }\n"
            ),
        },
        "directory_index": {
            "lib/index.ts": "export function helper(): number { return 1; }\n",
            "e.ts": (
                'import { helper } from "./lib";\n'
                "export function run(): number { return helper(); }\n"
            ),
        },
    }
    for name, files in cases.items():
        repo = _repo(tmp_path / name, files)
        engine = GraphEngine(repo)
        engine.index_repository(force_rebuild=True)
        edges = _call_edges(engine)
        source = "run" if name in ("cjs_to_cts", "jsx_to_tsx", "directory_index") else "esm"
        assert (source, "helper") in edges, (name, sorted(edges))
        engine.close()


def test_ts_super_resolves_against_base_before_enclosing_class(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "u.ts": (
                "export class A {\n"
                "  greet() { return 1; }\n"
                "}\n"
                "export class B extends A {\n"
                "  greet() { return super.greet() + 1; }\n"
                "}\n"
                "export class C extends B {\n"
                "  greet() { return super.greet() + 2; }\n"
                "  hi() { return this.greet(); }\n"
                "}\n"
            )
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    edges = _call_edges(engine)

    assert ("C.greet", "B.greet") in edges, sorted(edges)
    assert ("B.greet", "A.greet") in edges, sorted(edges)
    assert ("C.greet", "C.greet") not in edges
    assert ("B.greet", "B.greet") not in edges
    assert ("C.hi", "C.greet") in edges
    cycles = engine.find_circular_dependencies()
    assert cycles["self_recursive_count"] == 0, cycles["self_recursive"]
    engine.close()


def test_ts_call_forms_and_nested_imports_resolve(tmp_path: Path) -> None:
    cases = {
        "this_method": (
            {
                "c.ts": (
                    "export class C {\n"
                    "  m(): number { return 1; }\n"
                    "  n(): number { return this.m(); }\n"
                    "}\n"
                )
            },
            ("C.n", "C.m"),
        ),
        "this_field_method": (
            {
                "u.ts": "export class U { go(): number { return 1; } }\n",
                "c.ts": (
                    'import { U } from "./u";\n'
                    "export class C {\n"
                    "  u: U;\n"
                    "  n(): number { return this.u.go(); }\n"
                    "}\n"
                ),
            },
            ("C.n", "U.go"),
        ),
        "export_alias_reexport": (
            {
                "u.ts": "export function a(): number { return 1; }\n",
                "i.ts": 'export { a as b } from "./u";\n',
                "j.ts": (
                    'import { b } from "./i";\n'
                    "export function run(): number { return b(); }\n"
                ),
            },
            ("run", "a"),
        ),
        "import_type": (
            {
                "u.ts": "export class U { go(): number { return 1; } }\n",
                "c.ts": (
                    'import type { U } from "./u";\n'
                    "export class C {\n"
                    "  u: U;\n"
                    "  n(): number { return this.u.go(); }\n"
                    "}\n"
                ),
            },
            ("C.n", "U.go"),
        ),
        "optional_chain": (
            {
                "u.ts": "export class U { go(): number { return 1; } }\n",
                "c.ts": (
                    'import { U } from "./u";\n'
                    "export function run(u?: U): number { return u?.go() ?? 0; }\n"
                ),
            },
            ("run", "U.go"),
        ),
        "require_in_try": (
            {
                "u.ts": "export function helper(): number { return 1; }\n",
                "i.ts": (
                    "export function run(): number {\n"
                    "  try {\n"
                    '    const { helper } = require("./u");\n'
                    "    return helper();\n"
                    "  } catch {\n"
                    "    return 0;\n"
                    "  }\n"
                    "}\n"
                ),
            },
            ("run", "helper"),
        ),
        "require_in_conditional": (
            {
                "u.ts": "export function helper(): number { return 1; }\n",
                "i.ts": (
                    "export function run(flag: boolean): number {\n"
                    "  if (flag) {\n"
                    '    const { helper } = require("./u");\n'
                    "    return helper();\n"
                    "  }\n"
                    "  return 0;\n"
                    "}\n"
                ),
            },
            ("run", "helper"),
        ),
    }
    for name, (files, expected) in cases.items():
        repo = _repo(tmp_path / name, files)
        engine = GraphEngine(repo)
        engine.index_repository(force_rebuild=True)
        edges = _call_edges(engine)
        assert expected in edges, (name, sorted(edges))
        engine.close()


def test_ts_dotted_specifier_basenames_resolve(tmp_path: Path) -> None:
    cases = {
        "relative": (
            {
                "shared.utils.ts": "export function helper(): number { return 1; }\n",
                "i.ts": (
                    'import { helper } from "./shared.utils";\n'
                    "export function run(): number { return helper(); }\n"
                ),
            },
            ("run", "helper"),
        ),
        "alias": (
            {
                "tsconfig.json": (
                    '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./src/*"]}}}'
                ),
                "src/utils/format.util.ts": (
                    "export function helper(): number { return 1; }\n"
                ),
                "src/i.ts": (
                    'import { helper } from "@/utils/format.util";\n'
                    "export function run(): number { return helper(); }\n"
                ),
            },
            ("run", "helper"),
        ),
        "class_new": (
            {
                "validation.pipe.ts": "export class ValidationPipe {}\n",
                "i.ts": (
                    'import { ValidationPipe } from "./validation.pipe";\n'
                    "export function run() { return new ValidationPipe(); }\n"
                ),
            },
            ("run", "ValidationPipe"),
        ),
    }
    for name, (files, expected) in cases.items():
        repo = _repo(tmp_path / name, files)
        engine = GraphEngine(repo)
        engine.index_repository(force_rebuild=True)
        edges = _call_edges(engine)
        assert expected in edges, (name, sorted(edges))
        engine.close()


def test_js_property_assigned_functions_are_symbols(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "app.js": (
                "function helper() { return 1; }\n"
                "const app = {};\n"
                "app.set = function set() { return helper(); };\n"
                "module.exports = app;\n"
            ),
            "widget.js": (
                "function helper() { return 2; }\n"
                "function Widget() {}\n"
                "Widget.prototype.render = function render() { return helper(); };\n"
                "module.exports = Widget;\n"
            ),
        },
    )
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    edges = _call_edges(engine)
    assert ("app.set", "helper") in edges, sorted(edges)
    assert ("Widget.prototype.render", "helper") in edges, sorted(edges)
    engine.close()
