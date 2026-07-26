"""Deterministic one-axis placement for temporally overlapping lyric pages.

The renderer owns pixel measurement.  This module deliberately knows nothing
about Qt, fonts or glyphs: it receives final visual bands and translates each
incoming page as one rigid block.  Horizontal lyrics use the Y axis and
vertical lyrics use the X axis.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Iterable, Literal, Sequence

PageAnchor = Literal["start", "center", "end"]


@dataclass(frozen=True)
class LineVisualBand:
    """One rendered line's time window and final one-axis pixel bounds."""

    line_id: Hashable
    page_id: Hashable
    display_start_ms: int
    display_end_ms: int
    axis_min: float
    axis_max: float

    def shifted(self, delta: float) -> "LineVisualBand":
        return LineVisualBand(
            line_id=self.line_id,
            page_id=self.page_id,
            display_start_ms=self.display_start_ms,
            display_end_ms=self.display_end_ms,
            axis_min=self.axis_min + delta,
            axis_max=self.axis_max + delta,
        )


@dataclass(frozen=True)
class PageVisualBands:
    """All line bands of one page before inter-page placement."""

    page_id: Hashable
    bands: tuple[LineVisualBand, ...]
    gap_px: float = 0.0
    anchor: PageAnchor = "end"


def time_windows_overlap(left: LineVisualBand, right: LineVisualBand) -> bool:
    """Return whether two half-open display windows intersect."""

    return max(left.display_start_ms, right.display_start_ms) < min(
        left.display_end_ms, right.display_end_ms
    )


def solve_page_axis_offsets(
    pages: Sequence[PageVisualBands],
    *,
    viewport_min: float,
    viewport_max: float,
) -> dict[Hashable, float]:
    """Place pages in order and return a stable scalar offset per page.

    Earlier pages are authoritative.  Each later page is tested against every
    earlier page whose line windows overlap.  The selected offset translates
    all lines on the page together.
    """

    if viewport_max < viewport_min:
        viewport_min, viewport_max = viewport_max, viewport_min

    offsets: dict[Hashable, float] = {}
    occupied: list[LineVisualBand] = []
    for page in pages:
        bands = tuple(_normalized_band(item) for item in page.bands)
        if not bands:
            offsets[page.page_id] = 0.0
            continue
        relevant_pairs = [
            (incoming, previous)
            for incoming in bands
            for previous in occupied
            if previous.page_id != page.page_id
            and time_windows_overlap(incoming, previous)
        ]
        offset = _choose_offset(
            bands,
            relevant_pairs,
            gap=max(float(page.gap_px), 0.0),
            anchor=page.anchor,
            viewport_min=float(viewport_min),
            viewport_max=float(viewport_max),
        )
        offsets[page.page_id] = offset
        occupied.extend(item.shifted(offset) for item in bands)
    return offsets


def _normalized_band(item: LineVisualBand) -> LineVisualBand:
    if item.axis_min <= item.axis_max:
        return item
    return LineVisualBand(
        line_id=item.line_id,
        page_id=item.page_id,
        display_start_ms=item.display_start_ms,
        display_end_ms=item.display_end_ms,
        axis_min=item.axis_max,
        axis_max=item.axis_min,
    )


def _choose_offset(
    bands: Sequence[LineVisualBand],
    pairs: Sequence[tuple[LineVisualBand, LineVisualBand]],
    *,
    gap: float,
    anchor: PageAnchor,
    viewport_min: float,
    viewport_max: float,
) -> float:
    if not pairs:
        return 0.0

    page_min = min(item.axis_min for item in bands)
    page_max = max(item.axis_max for item in bands)
    lower = viewport_min - page_min
    upper = viewport_max - page_max
    if lower > upper:
        # A page taller/wider than the viewport cannot fit.  Keep its authored
        # centre in the viewport and let the minimum-overlap scorer handle it.
        midpoint = ((viewport_min + viewport_max) - (page_min + page_max)) / 2.0
        lower = upper = midpoint

    candidates = {0.0, lower, upper}
    for incoming, previous in pairs:
        candidates.add(previous.axis_min - gap - incoming.axis_max)
        candidates.add(previous.axis_max + gap - incoming.axis_min)

    clamped = {_clamp(value, lower, upper) for value in candidates}
    valid = [
        value
        for value in clamped
        if all(_separation_deficit(incoming, previous, value, gap) <= 0.0
               for incoming, previous in pairs)
    ]
    pool = valid if valid else list(clamped)
    return min(
        pool,
        key=lambda value: _offset_score(
            value,
            pairs,
            gap=gap,
            anchor=anchor,
            require_overlap_score=not valid,
        ),
    )


def _offset_score(
    offset: float,
    pairs: Sequence[tuple[LineVisualBand, LineVisualBand]],
    *,
    gap: float,
    anchor: PageAnchor,
    require_overlap_score: bool,
) -> tuple[float, int, float, float, float]:
    deficits = [
        _separation_deficit(incoming, previous, offset, gap)
        for incoming, previous in pairs
    ]
    total = sum(value for value in deficits if value > 0.0)
    count = sum(value > 0.0 for value in deficits)
    direction_penalty = _direction_penalty(offset, anchor)
    if require_overlap_score:
        # When no perfect placement exists, preserve the product order:
        # overlap amount, conflicting pair count, distance, direction.
        return total, count, abs(offset), direction_penalty, offset
    # For valid placements the page anchor decides the preferred direction,
    # then the nearest placement wins.  The raw offset is a stable final tie.
    return 0.0, 0, direction_penalty, abs(offset), offset


def _direction_penalty(offset: float, anchor: PageAnchor) -> int:
    if anchor == "start":
        return 0 if offset >= 0.0 else 1
    if anchor == "end":
        return 0 if offset <= 0.0 else 1
    return 0


def _separation_deficit(
    incoming: LineVisualBand,
    previous: LineVisualBand,
    offset: float,
    gap: float,
) -> float:
    incoming_min = incoming.axis_min + offset
    incoming_max = incoming.axis_max + offset
    if incoming_max + gap <= previous.axis_min:
        return 0.0
    if incoming_min >= previous.axis_max + gap:
        return 0.0
    return min(
        incoming_max + gap - previous.axis_min,
        previous.axis_max + gap - incoming_min,
    )


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), lower), upper)


def shifted_bands(
    bands: Iterable[LineVisualBand], offset: float
) -> tuple[LineVisualBand, ...]:
    """Convenience helper used by renderer-side diagnostics and tests."""

    return tuple(item.shifted(offset) for item in bands)
