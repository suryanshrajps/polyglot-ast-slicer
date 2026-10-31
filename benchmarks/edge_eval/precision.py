"""Samples call edges and checks each against Jedi (see README.md)."""

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
    sample_size = int(sys.argv[2]) if len(sys.argv) > 2 else 100
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

    edges = [
        (u, v, data)
        for u, v, data in engine.graph.edges(data=True)
        if data.get("relation") in ("calls", "constructs")
        and engine._symbols[u].language == "python"  # noqa: SLF001
    ]
    random.seed(42)
    sample = random.sample(edges, min(sample_size, len(edges)))
    text_cache: dict[str, list[str]] = {}

    def lines_of(path: str) -> list[str]:
        if path not in text_cache:
            text_cache[path] = Path(path).read_text(errors="replace").splitlines()
        return text_cache[path]

    strict = canonical = 0
    by_tier: dict[float, dict[str, int]] = {}
    failures: list[tuple[str, str, float]] = []
    for source_id, target_id, data in sample:
        source = engine._symbols[source_id]  # noqa: SLF001
        target = engine._symbols[target_id]  # noqa: SLF001
        tier = float(data.get("confidence", 0))
        bucket = by_tier.setdefault(tier, {"n": 0, "strict": 0, "canonical": 0})
        bucket["n"] += 1
        hit: str | None = None
        sites = [site for site in source.calls if site.name in (target.name, data.get("name"))]
        for site in sites:
            if not 1 <= site.line <= len(lines_of(source.file_path)):
                continue
            for column in positions(lines_of(source.file_path)[site.line - 1], target.name):
                try:
                    names = script_for(source.file_path).goto(site.line, column)
                except Exception:
                    continue
                for name in names:
                    if not name.module_path or not in_repo(name.module_path):
                        continue
                    if name.name not in (target.name, data.get("name")):
                        continue
                    resolved = str(Path(name.module_path).resolve())
                    if (
                        resolved == str(Path(target.file_path).resolve())
                        and target.start_line <= name.line <= target.end_line
                    ):
                        hit = "strict"
                    elif engine._symbol_in_file(resolved, target.name) == target.symbol_id:  # noqa: SLF001
                        hit = hit or "canonical"
                if hit:
                    break
            if hit:
                break
        if hit == "strict":
            strict += 1
            canonical += 1
            bucket["strict"] += 1
            bucket["canonical"] += 1
        elif hit == "canonical":
            canonical += 1
            bucket["canonical"] += 1
        else:
            failures.append((target.name, source.qualified_name, tier))

    total = len(sample)
    print(f"REPO {repo}")
    print(f"  edges sampled={total} strict={strict} canonical={canonical} "
          f"({canonical / total * 100:.1f}%)")
    for tier, bucket in sorted(by_tier.items(), reverse=True):
        n = bucket["n"]
        print(f"  tier {tier}: n={n} strict={bucket['strict']} "
              f"canonical={bucket['canonical']} ({bucket['canonical'] / n * 100:.0f}%)")
    print(f"  failures: {failures[:8]}")
    engine.close()


if __name__ == "__main__":
    main()
