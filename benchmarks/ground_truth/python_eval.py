"""Exhaustive Python call-site ground truth for the pinned evaluation repos.

Every raw tree-sitter ``call`` node under ``src/`` is resolved with Jedi
(``goto(..., follow_imports=True)``). Sites whose target is a project-defined symbol are
compared with the engine's ``calls``/``constructs`` edges.

A site is *correct* when the engine reports the Jedi target, *wrong* when it reports a
different project target for the same call, and *missed* when it reports nothing. Ambiguous
sites (Jedi returns more than one project definition) are counted separately and excluded
from the headline precision/recall denominators.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import jedi
from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge_eval"))

from stats import interval_text, wilson  # noqa: E402

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402

FORMS = (
    "bare",
    "module_attr",
    "self_cls",
    "super",
    "constructor",
    "aliased_import",
    "chained",
    "callback",
)

jedi.settings.cache_directory = tempfile.mkdtemp(prefix="jedi-gt-")


@dataclass
class Site:
    source_id: str | None
    source_name: str
    file: str
    rel_file: str
    line: int
    column: int
    name: str
    receiver: str | None
    form: str
    text: str
    truth: set[str] = field(default_factory=set)
    truth_names: set[str] = field(default_factory=set)
    ambiguous: bool = False
    reported: set[str] = field(default_factory=set)
    verdict: str = "unjudged"

    def key(self) -> tuple[str | None, str, str | None]:
        return (self.source_id, self.name, self.receiver)


def _first_identifier(node: Node) -> Node | None:
    for child in node.named_children:
        if child.type == "identifier":
            return child
        found = _first_identifier(child)
        if found is not None:
            return found
    return None


def _param_names(function: Node) -> set[str]:
    parameters = function.child_by_field_name("parameters")
    if parameters is None:
        return set()
    names: set[str] = set()
    for child in parameters.named_children:
        if child.type == "identifier":
            names.add(child.text.decode("utf-8", "replace"))
            continue
        if child.type in ("typed_parameter", "typed_default_parameter", "default_parameter"):
            name = child.child_by_field_name("name")
            if name is None:
                name = _first_identifier(child)
        elif child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            name = _first_identifier(child)
        else:
            continue
        if name is not None:
            names.add(name.text.decode("utf-8", "replace"))
    return names


def _callee(node: Node) -> tuple[Node | None, Node | None]:
    function = node.child_by_field_name("function")
    if function is None:
        return None, None
    if function.type == "identifier":
        return function, function
    if function.type == "attribute":
        attribute = function.child_by_field_name("attribute")
        return function, attribute or function
    return function, function


def _receiver_text(callee: Node, source: bytes) -> tuple[str | None, Node | None]:
    if callee.type != "attribute":
        return None, None
    obj = callee.child_by_field_name("object")
    if obj is None:
        return None, None
    return obj.text.decode("utf-8", "replace"), obj


def _import_sets(engine: GraphEngine, file: str) -> tuple[set[str], set[str]]:
    """Returns (aliased local names, module-bound local names) for one file."""
    aliased: set[str] = set()
    parsed = engine._parsed.get(file)  # noqa: SLF001
    if parsed is not None:
        for ref in parsed.imports:
            if ref.is_module:
                local = ref.alias or ref.module.split(".")[-1]
                if ref.alias:
                    aliased.add(local)
            for local, original in ref.names:
                if local != original:
                    aliased.add(local)
    bindings = engine._bindings.get(file, {})  # noqa: SLF001
    modules = {
        local for local, (target, original) in bindings.items() if target and original is None
    }
    return aliased, modules


def _enclosing_symbol(engine: GraphEngine, file: str, start_byte: int) -> object | None:
    best = None
    best_span = None
    for symbol_id in engine._by_file.get(file, set()):  # noqa: SLF001
        symbol = engine._symbols[symbol_id]  # noqa: SLF001
        if not (symbol.start_byte <= start_byte <= symbol.end_byte):
            continue
        span = symbol.end_byte - symbol.start_byte
        if best_span is None or span < best_span:
            best, best_span = symbol, span
    return best


def _target_symbol(
    engine: GraphEngine, module_path: str, line: int, name: str
) -> tuple[str, str] | None:
    candidates = []
    for symbol_id in engine._by_file.get(module_path, set()):  # noqa: SLF001
        symbol = engine._symbols[symbol_id]  # noqa: SLF001
        if symbol.start_line <= line <= symbol.end_line:
            span = symbol.end_line - symbol.start_line
            candidates.append((span, symbol.name != name, symbol_id, symbol))
    if not candidates:
        return None
    _, _, symbol_id, symbol = min(candidates)
    return symbol_id, symbol.qualified_name


def _classify(
    callee: Node,
    receiver: str | None,
    target_ids: set[str],
    engine: GraphEngine,
    aliased: set[str],
    modules: set[str],
    params: set[str],
) -> str:
    head = receiver.split(".")[0] if receiver else None
    if callee.type == "identifier" and callee.text == b"super":
        return "super"
    if receiver and head in ("self", "cls"):
        return "self_cls"
    if receiver and receiver.startswith("super("):
        return "super"
    if callee.type == "attribute":
        obj = callee.child_by_field_name("object")
        if obj is not None and obj.type == "call":
            return "chained"
    if any(engine._symbols[t].kind == "class" for t in target_ids):  # noqa: SLF001
        return "constructor"
    if callee.type == "identifier" and callee.text.decode("utf-8", "replace") in params:
        return "callback"
    if head in aliased or (callee.type == "identifier" and callee.text.decode() in aliased):
        return "aliased_import"
    if head in modules:
        return "module_attr"
    if callee.type == "identifier":
        return "bare"
    return "other"


def _line_text(lines: list[str], line: int) -> str:
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()
    return ""


def _def_location(engine: GraphEngine, repo: Path, symbol_id: str) -> dict[str, object]:
    symbol = engine._symbols[symbol_id]  # noqa: SLF001
    try:
        rel = Path(symbol.file_path).resolve().relative_to(repo).as_posix()
    except ValueError:
        rel = symbol.file_path
    return {
        "qualified_name": symbol.qualified_name,
        "file": rel,
        "line": symbol.start_line,
        "signature": symbol.signature,
        "kind": symbol.kind,
    }


def evaluate(
    repo_arg: str, seed: int, unjudged: int, details: Path | None, label: str | None = None
) -> dict[str, object]:
    repo = Path(repo_arg).resolve()
    src = repo / "src"
    if not src.is_dir():
        raise SystemExit(f"{repo} has no src/ directory")
    engine = GraphEngine(str(repo))
    engine.index_repository(force_rebuild=True)
    project = jedi.Project(
        str(repo), sys_path=[str(repo), str(src)], smart_sys_path=False
    )
    scripts: dict[str, jedi.Script] = {}

    def script_for(file: str) -> jedi.Script:
        if file not in scripts:
            scripts[file] = jedi.Script(path=file, project=project)
        return scripts[file]

    def in_src(path: str | None) -> bool:
        if not path:
            return False
        try:
            return Path(path).resolve().is_relative_to(src)
        except (OSError, ValueError):
            return False

    parser = engine.parser
    grammar = parser._get_parser("python")  # noqa: SLF001
    text_cache: dict[str, list[str]] = {}

    def lines_of(file: str) -> list[str]:
        if file not in text_cache:
            text_cache[file] = Path(file).read_text(errors="replace").splitlines()
        return text_cache[file]

    sites: list[Site] = []
    unsupported = 0
    for path in sorted(src.rglob("*.py")):
        file = str(path.resolve())
        source = path.read_bytes()
        tree = grammar.parse(source)
        aliased, modules = _import_sets(engine, file)
        stack: list[Node] = [tree.root_node]
        calls: list[Node] = []
        while stack:
            node = stack.pop()
            if node.type == "call":
                calls.append(node)
            stack.extend(node.children)
        for node in sorted(calls, key=lambda item: item.start_byte):
            callee, position = _callee(node)
            if callee is None or position is None:
                unsupported += 1
                continue
            receiver, _ = _receiver_text(callee, source)
            name = callee.text.decode("utf-8", "replace") if callee.type == "identifier" else ""
            if callee.type == "attribute":
                attribute = callee.child_by_field_name("attribute")
                name = attribute.text.decode("utf-8", "replace") if attribute else ""
            source_symbol = _enclosing_symbol(engine, file, node.start_byte)
            source_id = source_symbol.symbol_id if source_symbol is not None else None
            source_name = (
                source_symbol.qualified_name if source_symbol is not None else "<module>"
            )
            try:
                rel_file = path.relative_to(repo).as_posix()
            except ValueError:
                rel_file = str(path)
            site = Site(
                source_id=source_id,
                source_name=source_name,
                file=file,
                rel_file=rel_file,
                line=node.start_point[0] + 1,
                column=node.start_point[1],
                name=name,
                receiver=receiver,
                form="other",
                text=_line_text(lines_of(file), node.start_point[0] + 1)[:160],
            )
            params: set[str] = set()
            ancestor = node.parent
            while ancestor is not None:
                if ancestor.type in ("function_definition", "lambda"):
                    params = _param_names(ancestor)
                    break
                ancestor = ancestor.parent
            try:
                names = script_for(file).goto(
                    position.start_point[0] + 1,
                    position.start_point[1],
                    follow_imports=True,
                )
            except Exception:
                names = []
            for jedi_name in names:
                module_path = jedi_name.module_path
                if not in_src(str(module_path) if module_path else None):
                    continue
                if jedi_name.line is None:
                    continue
                target = _target_symbol(
                    engine, str(Path(str(module_path)).resolve()), jedi_name.line, jedi_name.name
                )
                if target is not None:
                    site.truth.add(target[0])
                    site.truth_names.add(target[1])
            if len(site.truth) > 1:
                site.ambiguous = True
            site.form = _classify(callee, receiver, site.truth, engine, aliased, modules, params)
            sites.append(site)

    reported: dict[tuple[str | None, str, str | None], set[str]] = {}
    edges: list[tuple[str, str, dict[str, object]]] = []
    for source_id, target_id, data in engine.graph.edges(data=True):
        if data.get("relation") not in ("calls", "constructs"):
            continue
        if data.get("line") is None:
            continue
        edges.append((source_id, target_id, data))
        key = (source_id, str(data.get("name")), data.get("receiver"))
        reported.setdefault(key, set()).add(target_id)

    for site in sites:
        ids = reported.get(site.key(), set())
        site.reported = ids
        if site.ambiguous:
            site.verdict = "ambiguous"
        elif not site.truth:
            site.verdict = "unjudged"
        elif not ids:
            site.verdict = "missed"
        elif next(iter(site.truth)) in ids:
            site.verdict = "correct"
        else:
            site.verdict = "wrong"

    project_sites = [site for site in sites if site.truth and not site.ambiguous]
    ambiguous_sites = [site for site in sites if site.ambiguous]
    correct = sum(1 for site in project_sites if site.verdict == "correct")
    wrong = sum(1 for site in project_sites if site.verdict == "wrong")
    missed = sum(1 for site in project_sites if site.verdict == "missed")
    ambiguous_reported = sum(1 for site in ambiguous_sites if site.reported)
    module_level = sum(1 for site in project_sites if site.source_id is None)
    total_sites = len(project_sites)
    precision_low, precision_high = wilson(correct, correct + wrong)
    recall_low, recall_high = wilson(correct, total_sites)

    by_form: dict[str, dict[str, object]] = {}
    for form in (*FORMS, "other"):
        group = [site for site in project_sites if site.form == form]
        hit = sum(1 for site in group if site.verdict == "correct")
        if not group and form == "other":
            continue
        low, high = wilson(hit, len(group))
        by_form[form] = {
            "sites": len(group),
            "correct": hit,
            "wrong": sum(1 for site in group if site.verdict == "wrong"),
            "missed": len(group) - hit - sum(1 for site in group if site.verdict == "wrong"),
            "recall": hit / len(group) if group else 0.0,
            "recall_ci95": [low, high],
        }

    detail_rows = []
    for site in sites:
        if site.verdict not in ("wrong", "missed"):
            continue
        truth = sorted(site.truth)[0] if site.truth else None
        truth_def = _def_location(engine, repo, truth) if truth else None
        reported_defs = [
            _def_location(engine, repo, target) for target in sorted(site.reported)
        ]
        detail_rows.append(
            {
                "verdict": site.verdict,
                "form": site.form,
                "source": site.source_name,
                "file": site.rel_file,
                "line": site.line,
                "call": site.text,
                "name": site.name,
                "receiver": site.receiver,
                "truth": truth_def,
                "reported": reported_defs,
            }
        )
    if details is not None:
        details.write_text(json.dumps(detail_rows, indent=2) + "\n", encoding="utf-8")

    unjudged_rows = []
    if unjudged:
        by_call = {
            (site.source_id, site.line, site.name, site.receiver): site for site in sites
        }
        candidates = []
        for source_id, target_id, data in edges:
            key = (source_id, int(data["line"]), str(data.get("name")), data.get("receiver"))
            site = by_call.get(key)
            if site is not None and (site.truth or site.ambiguous):
                continue
            candidates.append((source_id, target_id, data, site))
        random.seed(seed)
        for source_id, target_id, data, site in random.sample(
            candidates, min(unjudged, len(candidates))
        ):
            source = engine._symbols[source_id]  # noqa: SLF001
            lines = lines_of(source.file_path)
            line = int(data["line"])
            unjudged_rows.append(
                {
                    "source": source.qualified_name,
                    "file": engine._rel(source.file_path),  # noqa: SLF001
                    "line": line,
                    "call": _line_text(lines, line)[:160],
                    "name": data.get("name"),
                    "receiver": data.get("receiver"),
                    "resolution": data.get("resolution"),
                    "confidence": data.get("confidence"),
                    "jedi": "unresolved" if site is None or not site.truth else site.verdict,
                    "target": _def_location(engine, repo, target_id),
                }
            )

    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    summary: dict[str, object] = {
        "repo": label or repo.name,
        "sha": sha,
        "call_sites": len(sites) + unsupported,
        "unsupported_callees": unsupported,
        "unresolved_by_jedi": sum(1 for site in sites if not site.truth),
        "project_sites": total_sites,
        "ambiguous": len(ambiguous_sites),
        "ambiguous_but_reported": ambiguous_reported,
        "module_level_sites": module_level,
        "correct": correct,
        "wrong": wrong,
        "missed": missed,
        "precision": correct / (correct + wrong) if correct + wrong else 0.0,
        "precision_ci95": [precision_low, precision_high],
        "recall": correct / total_sites if total_sites else 0.0,
        "recall_ci95": [recall_low, recall_high],
        "by_form": by_form,
        "unjudged_sample": unjudged_rows,
    }
    engine.close()
    return summary


def print_summary(summary: dict[str, object]) -> None:
    print(
        f"REPO {summary['repo']} sha={str(summary['sha'])[:7]} "
        f"call_sites={summary['call_sites']} project_sites={summary['project_sites']} "
        f"correct={summary['correct']} wrong={summary['wrong']} missed={summary['missed']} "
        f"ambiguous={summary['ambiguous']} (reported={summary['ambiguous_but_reported']}) "
        f"module_level={summary['module_level_sites']} "
        f"unresolved={summary['unresolved_by_jedi']}"
    )
    correct, wrong = int(summary["correct"]), int(summary["wrong"])  # type: ignore[arg-type]
    print(
        f"  precision={interval_text(correct, correct + wrong)} "
        f"recall={interval_text(correct, int(summary['project_sites']))}"  # type: ignore[arg-type]
    )
    for form, row in summary["by_form"].items():  # type: ignore[union-attr]
        print(
            f"  {form}: sites={row['sites']} correct={row['correct']} "
            f"missed={row['missed']} recall={interval_text(row['correct'], row['sites'])}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--details", type=Path)
    parser.add_argument("--unjudged", type=int, default=0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--name", help="repo name override (defaults to directory name)")
    args = parser.parse_args()
    summary = evaluate(args.repo, args.seed, args.unjudged, args.details, args.name)
    if args.json:
        print(json.dumps(summary))
    else:
        print_summary(summary)


if __name__ == "__main__":
    main()
