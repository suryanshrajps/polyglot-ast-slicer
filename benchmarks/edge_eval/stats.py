"""Small statistics helpers shared by the evaluation harness."""

from __future__ import annotations

import math

Z_95 = 1.959963984540054


def wilson(successes: int, total: int, z: float = Z_95) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion (no continuity correction)."""
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1.0 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def as_percent(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"


def interval_text(successes: int, total: int) -> str:
    low, high = wilson(successes, total)
    point = as_percent(successes / total if total else 0)
    return f"{point} (95% CI {as_percent(low)}–{as_percent(high)})"
