"""Pure planning logic for capped, cursor-less history endpoints.

The API returns newest-first, at most ``limit`` rows, with no page cursor. So a time range is
only known to be complete when it returns FEWER than ``limit`` rows. A full page means
"possibly truncated": the range is bisected and both halves are queried again.

Range boundary semantics (inclusive vs exclusive) are not documented, so halves overlap by one
millisecond and results are de-duplicated by stable id. A saturated range too small to bisect
is queried one millisecond at a time, but only after an in-situ check proves that a
``from == to`` query returns rows (i.e. bounds are inclusive); otherwise, or when a single
millisecond is itself saturated, completeness cannot be established and the range is recorded
as an explicit coverage gap rather than silently treated as done.
"""
from __future__ import annotations

HISTORY_LIMIT = 500
ATOMIC_SPAN_MS = 2  # a range this small cannot be split into two strictly smaller overlapping halves

COMPLETE, SPLIT, POINTS, GAP = "complete", "split", "points", "gap"


def evaluate(range_from: int, range_to: int, returned: int, limit: int = HISTORY_LIMIT) -> str:
    """COMPLETE: fewer rows than the cap. SPLIT: bisect. POINTS: too small to bisect, query each
    millisecond on its own (only valid once inclusive bounds are verified). GAP: one single
    millisecond is itself saturated, so completeness cannot be established."""
    if range_to < range_from:
        raise ValueError("range_to must be >= range_from")
    if returned < limit:
        return COMPLETE
    if range_to == range_from:
        return GAP
    return POINTS if range_to - range_from <= ATOMIC_SPAN_MS else SPLIT


def points(range_from: int, range_to: int) -> list[tuple[int, int]]:
    """Single-millisecond ranges covering a tiny saturated range."""
    if range_to - range_from > ATOMIC_SPAN_MS:
        raise ValueError("range is too large for point queries")
    return [(t, t) for t in range(range_from, range_to + 1)]


def split(range_from: int, range_to: int) -> list[tuple[int, int]]:
    """Two strictly smaller sub-ranges that together cover the parent, overlapping by 1 ms."""
    if range_to - range_from <= ATOMIC_SPAN_MS:
        raise ValueError("range is atomic and cannot be split")
    mid = (range_from + range_to) // 2
    return [(range_from, mid + 1), (mid, range_to)]
