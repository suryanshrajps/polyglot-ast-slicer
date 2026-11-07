"""MCP tool surface and CLI behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from polyglot_ast_slicer import server


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYGLOT_AST_WATCH", "0")
    server._ENGINES.clear()  # noqa: SLF001 - test isolation
    server._WATCHERS.clear()  # noqa: SLF001 - test isolation


def test_index_and_slice_tools(repo: Path) -> None:
    stats = server.index_codebase(str(repo))
    assert stats["files_indexed"] == 6
    assert stats["total_symbols"] > 0

    result = server.get_ast_call_slice(
        str(repo), "BillingEngine.process_invoice", hops=1
    )
    assert "error" not in result
    assert "def process_invoice(" in result["compiled_context"]
    assert result["token_metrics"]["slice_tokens"] > 0


def test_search_and_stats_tools(repo: Path) -> None:
    found = server.search_symbols(str(repo), "invoice", limit=5)
    assert found["count"] >= 1
    assert all("invoice" in item["qualified_name"].lower() for item in found["results"])

    stats = server.get_index_stats(str(repo))
    assert stats["files_indexed"] == 6
    assert stats["languages"]["python"] >= 1


def test_blast_and_cycles_tools(repo: Path) -> None:
    blast = server.get_blast_radius(str(repo), "pkg/tax_us.py::calculate_tax", max_depth=3)
    assert blast["total_impacted_symbols"] == 2

    cycles = server.find_circular_dependencies(str(repo))
    assert cycles["symbol_cycle_count"] >= 1


def test_tools_return_errors_instead_of_raising(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    result = server.index_codebase(str(missing))
    assert "error" in result


def test_cli_index_and_slice(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        server.main(["index", "--repo", str(repo), "--json"])
    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files_indexed"] == 6

    with pytest.raises(SystemExit) as exc:
        server.main(
            ["slice", "--repo", str(repo), "--symbol", "BillingEngine.process_invoice"]
        )
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "def process_invoice(" in captured.out
    assert "slice tokens" in captured.err


def test_cli_unknown_symbol_exits_nonzero(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        server.main(["slice", "--repo", str(repo), "--symbol", "nope"])
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_cli_search_and_cycles(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        server.main(["search", "--repo", str(repo), "tax", "--json"])
    assert json.loads(capsys.readouterr().out)["count"] >= 2

    with pytest.raises(SystemExit):
        server.main(["cycles", "--repo", str(repo)])
    assert "Symbol cycles" in capsys.readouterr().out


def test_cli_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        server.main(["--version"])
    assert exc.value.code == 0
    assert "polyglot-ast-slicer" in capsys.readouterr().out


def test_no_subcommand_starts_mcp(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    started: dict[str, object] = {}

    def fake_run(*, transport: str, show_banner: bool) -> None:
        started["transport"] = transport
        started["show_banner"] = show_banner

    monkeypatch.setattr(server.mcp, "run", fake_run)
    monkeypatch.chdir(repo)
    server.main([])
    assert started == {"transport": "stdio", "show_banner": False}


def _payload(result: object) -> dict:
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    return {}


def test_serve_without_repo_uses_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import os
    import sys

    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "def helper() -> int:\n    return 1\n\n\ndef run() -> int:\n    return helper()\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("POLYGLOT_AST_REPO", raising=False)
    env = dict(
        os.environ,
        POLYGLOT_AST_WATCH="0",
        POLYGLOT_AST_CACHE_DIR=str(tmp_path / "cache"),
    )

    async def probe() -> tuple[dict, dict, dict]:
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "polyglot_ast_slicer", "serve"],
            env=env,
            cwd=str(repo),
        )
        async with Client(transport) as client:
            indexed = _payload(await client.call_tool("index_codebase", {}))
            sliced = _payload(
                await client.call_tool(
                    "get_ast_call_slice", {"symbol_name": "run", "hops": 1}
                )
            )
            blast = _payload(
                await client.call_tool("get_blast_radius", {"symbol_name": "helper"})
            )
        return indexed, sliced, blast

    indexed, sliced, blast = asyncio.run(probe())
    assert indexed["files_indexed"] == 1
    assert "error" not in sliced and "helper" in sliced["compiled_context"]
    assert blast["total_impacted_symbols"] >= 1


def test_serve_refuses_implicit_home_or_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("POLYGLOT_AST_REPO", raising=False)
    for location in (Path.home(), Path("/")):
        monkeypatch.chdir(location)
        with pytest.raises(SystemExit) as exc:
            server.main(["serve"])
        assert exc.value.code == 2
        assert "--repo" in capsys.readouterr().err


def test_repo_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit"
    from_env = tmp_path / "from_env"
    cwd = tmp_path / "cwd"
    for path in (explicit, from_env, cwd):
        path.mkdir()
    monkeypatch.setenv("POLYGLOT_AST_REPO", str(from_env))
    assert server._resolve_repo(str(explicit)) == explicit.resolve()
    assert server._resolve_repo(None) == from_env.resolve()

    monkeypatch.delenv("POLYGLOT_AST_REPO")
    monkeypatch.chdir(cwd)
    assert server._resolve_repo(None) == cwd.resolve()
    assert server._resolve_repo(str(explicit)) == explicit.resolve()

    # An explicit path is honoured even when it is the home directory.
    assert server._resolve_repo(str(Path.home())) == Path.home().resolve()
