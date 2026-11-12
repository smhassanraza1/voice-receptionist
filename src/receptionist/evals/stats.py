"""Summary statistics shared by the reports, probes and Langfuse export."""

from __future__ import annotations

import math


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile: the smallest observed value with at least `q` of the data at or
    below it (index ceil(q * n) - 1 of the sorted values), rounded to 0.1.

    It always returns a measured value, never an interpolated one, and works for any n >= 1:
    with 10 samples p90 is the 9th, not the maximum, and p50 of an even-sized sample is the
    lower middle value. Returns None for no data.
    """
    if not values:
        return None
    ordered = sorted(values)
    # rounded first, so a float product a hair above an integer doesn't bump the rank
    rank = max(1, math.ceil(round(q * len(ordered), 9)))
    return round(ordered[min(rank, len(ordered)) - 1], 1)
