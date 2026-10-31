"""Samples call sites and checks whether the graph contains the edge Jedi implies."""

from __future__ import annotations

import random
import re
import sys
from pathlib import Path

import jedi

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from polyglot_ast_slicer.graph_engine import GraphEngine  # noqa: E402


def positions(line: str, name: str) -> list[int]:
    return [m.start() for m in re.finditer(rf"(?<![\w$]){re.escape(name)}(?![\w$])", line)]


def main() -> None:
    repo = str(Path(sys.argv[1]).resolve())
    sample_size = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    engine = GraphEngine(repo)
    engine.index_repository(force_rebuild=True)
    project = jedi.Project(
        repo,
        sys_path=[repo]
        + [
            str(path)
            for path in Path(repo).glob("*")
            if path.is_dir() and path.name in ("src", "lib")
        ],
        smart_sys_path=False,
    )
    scripts: dict[str, jedi.Script] = {}

    def script_for(path: str) -> jedi.Script:
        if path not in scripts:
            scripts[path] = jedi.Script(path=path, project=project)
        return scripts[path]

    def in_repo(path: str) -> bool:
        try:
            return Path(path).resolve().is_relative_to(Path(repo))
        except (OSError, ValueError):
            return False

    sites: list[tuple[str, object]] = []
    for symbol_id, symbol in engine._symbols.items():  # noqa: SLF001
        if symbol.language != "python":
            continue
        for site in symbol.calls:
            if site.line:
                sites.append((symbol_id, site))
    random.seed(7)
    sample = random.sample(sites, min(sample_size, len(sites)))
    text_cache: dict[str, list[str]] = {}

    def lines_of(path: str) -> list[str]:
        if path not in text_cache:
            text_cache[path] = Path(path).read_text(errors="replace").splitlines()
        return text_cache[path]

    resolved = hits = unresolved = 0
    misses: list[tuple[str, str]] = []
    for symbol_id, site in sample:
        symbol = engine._symbols[symbol_id]  # noqa: SLF001
        lines = lines_of(symbol.file_path)
        if not 1 <= site.line <= len(lines):
            continue
        target: str | None = None
        for column in positions(lines[site.line - 1], site.name):
            try:
                names = script_for(symbol.file_path).goto(site.line, column)
            except Exception:
                continue
            for name in names:
                if name.module_path and in_repo(name.module_path) and name.name == site.name:
                    target = engine._symbol_in_file(  # noqa: SLF001
                        str(Path(name.module_path).resolve()), site.name
                    )
                    break
            if target:
                break
        if not target:
            unresolved += 1
            continue
        resolved += 1
        if engine.graph.has_edge(symbol.symbol_id, target):
            hits += 1
        else:
            misses.append((symbol.qualified_name, site.name))

    print(f"REPO {repo} sites_sampled={len(sample)} jedi_project_targets={resolved} "
          f"tool_edges_present={hits} recall={hits / resolved * 100:.1f}% "
          f"unresolved_by_jedi={unresolved}")
    print(f"  misses: {misses[:8]}")
    engine.close()


if __name__ == "__main__":
    main()
