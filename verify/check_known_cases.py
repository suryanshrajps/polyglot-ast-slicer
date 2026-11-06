"""Known-failure and robustness checks for polyglot-ast-slicer.

Not part of `make gate`. Each case builds a tiny fixture, states the expected result and
prints PASS/FAIL with the raw observation used for the verdict.

Run: uv run python verify/check_known_cases.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from polyglot_ast_slicer.graph_engine import GraphEngine
from polyglot_ast_slicer.parser import GrammarUnavailable

WORK = Path(tempfile.mkdtemp(prefix="pas-verify-"))
CACHE = Path(tempfile.mkdtemp(prefix="pas-verify-cache-"))
os.environ["POLYGLOT_AST_CACHE_DIR"] = str(CACHE)

RESULTS: list[tuple[str, str, str, str]] = []


def record(case: str, expected: str, ok: bool, actual: str) -> None:
    RESULTS.append((case, "PASS" if ok else "FAIL", expected, actual))


def blocked(case: str, expected: str, error: BaseException | str) -> None:
    RESULTS.append((case, "BLOCKED", expected, str(error)))


def build(name: str, files: dict[str, str | bytes]) -> Path:
    root = WORK / name
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
    return root


def engine(root: Path, name: str, **kwargs: Any) -> GraphEngine:
    return GraphEngine(root, cache_dir=CACHE / name, **kwargs)


def edges(instance: GraphEngine) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for source, target, data in instance.graph.edges(data=True):
        left = instance._symbols.get(source)
        right = instance._symbols.get(target)
        if left is not None and right is not None:
            found.add((left.qualified_name, str(data.get("relation")), right.qualified_name))
    return found


def names(instance: GraphEngine) -> set[str]:
    return {symbol.qualified_name for symbol in instance._symbols.values()}


def self_recursive(instance: GraphEngine) -> list[str]:
    return [
        item["qualified_name"]
        for item in instance.find_circular_dependencies()["self_recursive"]
    ]


# ------------------------------------------------------------------ Python cases


def python_cases() -> None:
    root = build(
        "py-a",
        {
            "m.py": """
def request():
    return "module"


class Client:
    def request(self):
        return "method"

    def send(self):
        return self.request()
""",
        },
    )
    instance = engine(root, "py-a")
    instance.index_repository()
    got = edges(instance)
    expected = {("Client.send", "calls", "Client.request")}
    record(
        "py-a_self_request",
        "Client.send -> Client.request; no Client.send -> request",
        expected <= got and ("Client.send", "calls", "request") not in got,
        f"edges={sorted(got)}",
    )

    root = build(
        "py-b",
        {
            "m.py": """
class A:
    def __init__(self):
        self.x = 1


class B(A):
    def __init__(self):
        super().__init__()


class C(A):
    def __init__(self):
        super().__init__()
""",
        },
    )
    instance = engine(root, "py-b")
    instance.index_repository()
    cycles = instance.find_circular_dependencies()
    record(
        "py-b_super_no_cycle",
        "symbol_cycle_count=0 and no self-recursive symbol",
        cycles["symbol_cycle_count"] == 0 and not self_recursive(instance),
        f"symbol_cycles={cycles['symbol_cycle_count']} self={self_recursive(instance)}",
    )

    root = build(
        "py-c",
        {
            "m.py": """
class A:
    def run(self):
        return 1


class B:
    def run(self):
        return A().run()
""",
        },
    )
    instance = engine(root, "py-c")
    instance.index_repository()
    got = edges(instance)
    record(
        "py-c_no_false_recursion",
        "B.run is not a self-edge",
        ("B.run", "calls", "B.run") not in got,
        f"edges={sorted(got)} (note: A().run() has no A.run edge)",
    )

    root = build(
        "py-d",
        {
            "m.py": """
def fact(n):
    return 1 if n <= 0 else n * fact(n - 1)
""",
        },
    )
    instance = engine(root, "py-d")
    instance.index_repository()
    got = edges(instance)
    recursive = self_recursive(instance)
    record(
        "py-d_real_recursion",
        "fact -> fact self edge and fact reported self-recursive",
        ("fact", "calls", "fact") in got and "fact" in recursive,
        f"edges={sorted(got)} self={recursive}",
    )

    root = build(
        "py-e",
        {
            "m.py": """
def work():
    return 1


if __name__ == "__main__":
    work()
""",
        },
    )
    instance = engine(root, "py-e")
    instance.index_repository()
    got = edges(instance)
    record(
        "py-e_main_guard_call",
        "work has at least one caller from the __main__ block",
        any(target == "work" for _, _, target in got),
        f"edges={sorted(got)}",
    )

    root = build(
        "py-f",
        {
            "m.py": """
def dup():
    return 1


class A:
    def dup(self):
        return 2
""",
        },
    )
    instance = engine(root, "py-f")
    instance.index_repository()
    response = instance.get_ast_call_slice("dup")
    candidates = sorted(item["qualified_name"] for item in response.get("candidates", []))
    record(
        "py-f_ambiguous_candidates",
        "code=ambiguous_symbol and candidates={A.dup,dup}",
        response.get("code") == "ambiguous_symbol"
        and candidates == ["A.dup", "dup"]
        and "compiled_context" not in response,
        f"code={response.get('code')} candidates={candidates}",
    )

    root = build(
        "py-g",
        {
            "m.py": """
def parse(x):
    return x


def log(x):
    return x


def open(x):
    return x


def error(x):
    return x


def caller():
    parse(1)
    log(2)
    open(3)
    error(4)
""",
        },
    )
    instance = engine(root, "py-g")
    instance.index_repository()
    got = edges(instance)
    targets = {target for source, _, target in got if source == "caller"}
    record(
        "py-g_builtin_names",
        "caller -> parse, log, open, error",
        targets == {"parse", "log", "open", "error"},
        f"caller targets={sorted(targets)}",
    )

    root = build(
        "py-h",
        {
            "m.py": """
class Foo:
    pass


def make() -> Foo:
    return Foo()
""",
        },
    )
    instance = engine(root, "py-h")
    instance.index_repository()
    relations = {
        relation for source, relation, target in edges(instance)
        if source == "make" and target == "Foo"
    }
    record(
        "py-h_call_and_type_edges",
        "make -> Foo keeps both constructs and uses_type",
        {"constructs", "uses_type"} <= relations,
        f"make->Foo relations={sorted(relations)}",
    )

    root = build(
        "py-i",
        {
            "pkg/__init__.py": "from .core import x\n",
            "pkg/core.py": "def x():\n    return 1\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/mod.py": """
from ..core import x as renamed
from pkg import x


def use_rel():
    return renamed()


def use_init():
    return x()
""",
        },
    )
    instance = engine(root, "py-i")
    instance.index_repository()
    got = edges(instance)
    record(
        "py-i_relative_alias_reexport",
        "use_rel -> x and use_init -> x",
        ("use_rel", "calls", "x") in got and ("use_init", "calls", "x") in got,
        f"edges={sorted(got)}",
    )

    root = build(
        "py-j",
        {
            "m.py": """
class C:
    @property
    def value(self):
        return self._v

    @value.setter
    def value(self, v):
        self._v = v

    @staticmethod
    def s():
        return 1

    @classmethod
    def c(cls):
        return 2

    def use(self):
        return self.value + self.s() + self.c()
""",
        },
    )
    instance = engine(root, "py-j")
    instance.index_repository()
    parsed = next(iter(instance._parsed.values()))
    value_defs = [item for item in parsed.symbols if item.qualified_name == "C.value"]
    graph_value = instance._symbols.get(f"{parsed.file_path}::C.value")
    decorated = {"C.s", "C.c"} <= names(instance)
    both = len(value_defs) == 2 and graph_value is not None
    value_lines = None if graph_value is None else (graph_value.start_line, graph_value.end_line)
    record(
        "py-j_property_decorators",
        "getter and setter both represented; static/class methods present",
        both and decorated and graph_value is not None
        and graph_value.start_line == value_defs[0].start_line,
        f"parsed_value_defs={[(s.start_line, s.signature) for s in value_defs]} "
        f"graph_value_lines={value_lines} "
        f"decorated_present={decorated}",
    )

    root = build(
        "py-k",
        {
            "m.py": """
def dynamic(obj, name):
    return getattr(obj, name)()


def computed(obj, name):
    return obj[name]()
""",
        },
    )
    instance = engine(root, "py-k")
    instance.index_repository()
    got = {item for item in edges(instance) if item[0] in {"dynamic", "computed"}}
    record(
        "py-k_dynamic_no_edge",
        "dynamic and computed have no outgoing edges",
        not got,
        f"outgoing={sorted(got)}",
    )

    root = build(
        "py-l",
        {
            "a.py": "from b import thing\n\n\ndef use():\n    return thing()\n",
        },
    )
    incremental = engine(root, "py-l")
    incremental.index_repository()
    (root / "b.py").write_text("def thing():\n    return 1\n")
    incremental.refresh_file(root / "a.py")
    incremental_edges = edges(incremental)
    from_scratch = engine(root, "py-l-full")
    from_scratch.index_repository(force_rebuild=True)
    rebuild_edges = edges(from_scratch)
    record(
        "py-l_incremental_new_module",
        "refresh(a.py) edge set equals full rebuild",
        incremental_edges == rebuild_edges,
        f"incremental={sorted(incremental_edges)} rebuild={sorted(rebuild_edges)}",
    )


# ------------------------------------------------------------------ TS/JS cases

TS_CASES: list[tuple[str, dict[str, str], str, tuple[str, str, str] | None]] = [
    (
        "ts-paths_alias",
        {
            "tsconfig.json": '{"compilerOptions":{"baseUrl":".","paths":{"@/*":["src/*"]}}}',
            "src/u.ts": "export function helper(){return 1;}\n",
            "src/i.ts": 'import {helper as h} from "@/u";\nexport function run(){return h();}\n',
        },
        "run -> helper through @/ alias",
        ("run", "calls", "helper"),
    ),
    (
        "ts-barrel",
        {
            "src/u.ts": "export function helper(){return 1;}\n",
            "src/b.ts": 'export * from "./u";\n',
            "src/i.ts": 'import {helper} from "./b";\nexport function run(){return helper();}\n',
        },
        "run -> helper through export * barrel",
        ("run", "calls", "helper"),
    ),
    (
        "ts-default_import",
        {
            "u.ts": "export default function main(){return 1;}\n",
            "i.ts": 'import main from "./u";\nexport function run(){return main();}\n',
        },
        "run -> main through default import",
        ("run", "calls", "main"),
    ),
    (
        "ts-named_import",
        {
            "u.ts": "export function helper(){return 1;}\n",
            "i.ts": 'import {helper} from "./u";\nexport function run(){return helper();}\n',
        },
        "run -> helper through named import",
        ("run", "calls", "helper"),
    ),
    (
        "ts-namespace_import",
        {
            "u.ts": "export function helper(){return 1;}\n",
            "i.ts": 'import * as util from "./u";\nexport function run(){return util.helper();}\n',
        },
        "run -> helper through namespace import",
        ("run", "calls", "helper"),
    ),
    (
        "tsx-jsx",
        {
            "c.tsx": """
export function Button() {
  return <div>button</div>;
}

export function App() {
  return <Button />;
}
""",
        },
        "App -> Button through <Button />",
        ("App", "calls", "Button"),
    ),
    (
        "js-require_destructure",
        {
            "u.js": "exports.helper = function helper(){return 1;};\n",
            "l.js": 'const {helper} = require("./u");\nfunction legacy(){return helper();}\n',
        },
        "legacy -> helper through require destructuring",
        ("legacy", "calls", "helper"),
    ),
    (
        "js-commonjs_exports",
        {
            "u.js": "module.exports.helper = function helper(){return 1;};\n",
            "l.js": """
const { helper } = require("./u");
function legacy() { return helper(); }
module.exports.legacy = legacy;
exports.other = function other() { return legacy(); };
""",
        },
        "other -> legacy and both symbols exported",
        ("other", "calls", "legacy"),
    ),
    (
        "mjs-import",
        {
            "u.ts": "export function helper(){return 1;}\n",
            "e.mjs": 'import { helper } from "./u.js";\nexport function esm(){return helper();}\n',
        },
        "esm -> helper through .mjs import",
        ("esm", "calls", "helper"),
    ),
    (
        "cjs-require",
        {
            "u.ts": "export function helper(){return 1;}\n",
            "c.cjs": (
                'const util = require("./u");\n'
                "module.exports.cjs = function cjs(){return util.helper();};\n"
            ),
        },
        "cjs -> helper through .cjs require",
        ("cjs", "calls", "helper"),
    ),
]


def ts_cases() -> None:
    for case, files, expected_text, wanted in TS_CASES:
        try:
            root = build(case, files)
            instance = engine(root, case)
            instance.index_repository()
        except GrammarUnavailable as error:
            blocked(case, expected_text, error)
            continue
        except Exception as error:  # noqa: BLE001 - report, never abort the suite
            blocked(case, expected_text, f"{type(error).__name__}: {error}")
            continue
        got = edges(instance)
        record(case, expected_text, wanted in got if wanted else not got, f"edges={sorted(got)}")

    root = build(
        "ts-super",
        {
            "u.ts": """
export class Base {
  greet() { return 1; }
}
export class Child extends Base {
  greet() { return super.greet(); }
  hi() { return this.greet(); }
}
""",
        },
    )
    instance = engine(root, "ts-super")
    instance.index_repository()
    got = edges(instance)
    recursive = self_recursive(instance)
    record(
        "ts-super_this",
        "Child.hi -> Child.greet and Child.greet -> Base.greet; no false self edge",
        ("Child.hi", "calls", "Child.greet") in got
        and ("Child.greet", "calls", "Base.greet") in got
        and ("Child.greet", "calls", "Child.greet") not in got,
        f"edges={sorted(got)} self={recursive}",
    )


# ------------------------------------------------------------------ safety


def safety_cases() -> None:
    outside = WORK / "outside.py"
    outside.write_text("def escaped():\n    return 1\n")
    root = build("safe-symlink", {"ok.py": "def ok():\n    return 1\n"})
    link = root / "evil.py"
    try:
        link.symlink_to(outside)
        instance = engine(root, "safe-symlink")
        stats = instance.index_repository()
        skipped = stats["skipped_files"]
        record(
            "safe-symlink_escape",
            "escaping symlink is skipped, no crash, escaped symbol absent",
            any(reason == "symlink escapes repository" for reason in skipped.values())
            and "escaped" not in names(instance),
            f"skipped={skipped} symbols={sorted(names(instance))}",
        )
    except OSError as error:
        blocked("safe-symlink_escape", "symlink skipped", f"{type(error).__name__}: {error}")

    root = build("safe-readonly", {"m.py": "def f():\n    return 1\n"})
    modes: list[tuple[Path, int]] = []
    try:
        for path in [root, *root.rglob("*")]:
            modes.append((path, stat.S_IMODE(path.stat().st_mode)))
            path.chmod(0o444 if path.is_file() else 0o555)
        instance = engine(root, "safe-readonly")
        stats = instance.index_repository()
        record(
            "safe-readonly_checkout",
            "read-only checkout indexes",
            stats["files_indexed"] == 1 and stats["total_symbols"] == 1,
            f"files={stats['files_indexed']} symbols={stats['total_symbols']}",
        )
    except Exception as error:  # noqa: BLE001 - report, never abort the suite
        record("safe-readonly_checkout", "read-only checkout indexes", False,
               f"{type(error).__name__}: {error}")
    finally:
        for path, mode in reversed(modes):
            path.chmod(mode)

    root = build("safe-cache", {"m.py": "def f():\n    return 1\n"})
    previous_cache = os.environ.pop("POLYGLOT_AST_CACHE_DIR", None)
    previous_home = os.environ.get("HOME")
    os.environ["HOME"] = str(WORK / "home")
    try:
        instance = GraphEngine(root)
        instance.index_repository()
        db_path = instance.cache.db_path.resolve()
        try:
            outside_repo = not db_path.is_relative_to(root.resolve())
        except ValueError:
            outside_repo = True
        stray = sorted(path.name for path in root.glob("*.db*"))
        detail = f"db={db_path} stray={stray}"
    finally:
        if previous_cache is not None:
            os.environ["POLYGLOT_AST_CACHE_DIR"] = previous_cache
        if previous_home is not None:
            os.environ["HOME"] = previous_home
        else:
            os.environ.pop("HOME", None)
    record(
        "safe-cache_outside_repo",
        "default cache database is outside the repo and repo has no db file",
        outside_repo and not stray,
        detail,
    )

    allowed = WORK / "allowed"
    allowed.mkdir()
    root = build("safe-denied", {"m.py": "def f():\n    return 1\n"})
    try:
        GraphEngine(root, allowed_roots=[allowed])
        denied = False
        detail = "no exception"
    except PermissionError as error:
        denied = "outside the allowed roots" in str(error)
        detail = str(error)
    record(
        "safe-allowed_roots",
        "repo outside allowed roots raises PermissionError",
        denied,
        detail,
    )

    binary = b"def bad():\x00\n"
    latin1 = "# -*- coding: latin-1 -*-\ndef f():\n    return 'caf\xe9'\n".encode("latin-1")
    big = b"# pad\n" * (5 * 1024 * 1024 // 6)
    root = build(
        "safe-weird",
        {
            "empty.py": "",
            "broken.py": "def broken(:\n    return\n",
            "binary.py": binary,
            "latin1.py": latin1,
            "big.py": big,
        },
    )
    try:
        instance = engine(root, "safe-weird")
        stats = instance.index_repository()
        skipped = stats["skipped_files"]
        parse_errors = instance.get_index_stats()["files_with_parse_errors"]
        binary_skipped = any("binary" in reason for reason in skipped.values())
        big_skipped = any("file too large" in reason for reason in skipped.values())
        record(
            "safe-weird_files",
            "empty/broken/latin1 index; binary and 5MB skipped; no crash",
            stats["files_indexed"] == 3
            and binary_skipped
            and big_skipped
            and "broken.py" in parse_errors,
            f"files={stats['files_indexed']} skipped={skipped} parse_errors={parse_errors}",
        )
    except Exception as error:  # noqa: BLE001 - report, never abort the suite
        record("safe-weird_files", "weird files do not crash", False,
               f"{type(error).__name__}: {error}")


# ------------------------------------------------------------------ payload


def payload_cases() -> None:
    import tiktoken

    root = build(
        "payload",
        {
            "m.py": """
class Service:
    def target(self):
        return "UNIQUE_TARGET_MARKER_9f3a"

    def caller(self):
        return self.target()
""",
        },
    )
    instance = engine(root, "payload")
    instance.index_repository()
    response = instance.get_ast_call_slice("Service.target")
    keys = sorted(response)
    record(
        "payload-default_keys",
        "default keys are compiled_context, diagnostics, token_metrics",
        keys == ["compiled_context", "diagnostics", "token_metrics"],
        f"keys={keys}",
    )
    dumped = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    count = dumped.count("UNIQUE_TARGET_MARKER_9f3a")
    record(
        "payload-snippet_once",
        "target snippet body appears once in returned JSON",
        count == 1,
        f"occurrences={count}",
    )
    try:
        encoding = tiktoken.get_encoding("cl100k_base")
    except Exception as error:  # noqa: BLE001 - tokenizer is optional
        RESULTS.append(
            ("payload-tokens", "UNVERIFIED", "tiktoken count equals payload_tokens",
             f"{type(error).__name__}: {error}")
        )
    else:
        counted = len(encoding.encode(dumped, disallowed_special=()))
        claimed = response["token_metrics"]["payload_tokens"]
        record(
            "payload-tokens",
            "tiktoken count of exact JSON equals token_metrics.payload_tokens",
            counted == claimed,
            f"tiktoken={counted} payload_tokens={claimed}",
        )


def main() -> int:
    try:
        python_cases()
        ts_cases()
        safety_cases()
        payload_cases()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
        shutil.rmtree(CACHE, ignore_errors=True)

    for case, status, expected, actual in RESULTS:
        print(f"{case:<32} {status:<10} expected: {expected}")
        print(f"{'':<32} {'':<10} actual:   {actual}")
    counts = {key: sum(1 for item in RESULTS if item[1] == key) for key in
              ("PASS", "FAIL", "BLOCKED", "UNVERIFIED")}
    print("SUMMARY " + " ".join(f"{key}={value}" for key, value in counts.items())
          + f" TOTAL={len(RESULTS)}")
    return 0 if counts["FAIL"] == 0 and counts["BLOCKED"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
