"""Synthetic tests for the label scoring script (no repo, no network)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from score_labels import compare, score, wilson  # noqa: E402


def test_wilson_known_value() -> None:
    low, high = wilson(8, 10)
    assert 0.49 < low < 0.50
    assert 0.94 < high < 0.95


def test_precision_unsure_and_kinds() -> None:
    key = {f"e{i}": {"kind": "module" if i < 5 else "same_file"} for i in range(10)}
    labels = {f"e{i}": "correct" for i in range(8)}
    labels["e8"] = "incorrect"
    labels["e9"] = "unsure"
    result = score(labels, key)
    assert result["hard_edges"]["n"] == 5
    assert result["hard_edges"]["correct"] == 3
    assert result["hard_edges"]["unsure"] == 1
    assert abs(result["hard_edges"]["precision_unsure_incorrect"] - 0.6) < 1e-9
    assert abs(result["hard_edges"]["precision_unsure_excluded"] - 0.75) < 1e-9


def test_canary_rates() -> None:
    key = {"r1": {"kind": "import"}, "r2": {"kind": "import"}}
    labels = {"r1": "correct", "r2": "incorrect"}
    canary = {
        "r1": {"seeded": False},
        "r2": {"seeded": False},
        "c1": {"seeded": True},
        "c2": {"seeded": True},
    }
    labels.update({"c1": "correct", "c2": "incorrect"})
    result = score(labels, key, canary)
    assert result["canary"]["false_accepts"] == 1
    assert result["canary"]["false_rejects"] == 1
    assert result["canary"]["false_accept_rate"] == 0.5


def test_kappa_and_degenerate_case() -> None:
    a = {"x": "correct", "y": "correct", "z": "incorrect"}
    b = {"x": "correct", "y": "incorrect", "z": "incorrect"}
    result = compare(a, b)
    assert result["agreements"] == 2
    assert result["kappa"] is not None
    assert -1.0 <= result["kappa"] <= 1.0

    degenerate = compare({"x": "correct"}, {"x": "correct"})
    assert degenerate["kappa"] is None
    assert degenerate["kappa_status"].startswith("undefined")
