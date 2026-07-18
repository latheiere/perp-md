from __future__ import annotations

from collections.abc import Iterable


def find_resume_time(
    timestamps_ms: Iterable[int],
    *,
    floor_ms: int,
    interval_ms: int,
) -> int:
    """Return the leading missing boundary, first sparse gap, or newest row."""
    if floor_ms < 0:
        raise ValueError("floor_ms must not be negative")
    if interval_ms <= 0:
        raise ValueError("interval_ms must be positive")
    ordered = sorted({int(value) for value in timestamps_ms if int(value) >= floor_ms})
    if not ordered or ordered[0] - floor_ms > interval_ms:
        return floor_ms
    for previous, current in zip(ordered, ordered[1:]):
        if current - previous > interval_ms:
            return previous
    return ordered[-1]
