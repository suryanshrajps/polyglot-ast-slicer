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
    started: dict[str, str] = {}

    def fake_run(*, transport: str) -> None:
        started["transport"] = transport

    monkeypatch.setattr(server.mcp, "run", fake_run)
    monkeypatch.chdir(repo)
    server.main([])
    assert started == {"transport": "stdio"}
