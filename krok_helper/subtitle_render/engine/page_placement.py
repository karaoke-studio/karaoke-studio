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
CollisionPair = tuple["LineVisualBand", "LineVisualBand", float]


@dataclass(frozen=True)
class LineVisualBand:
    """One rendered line's stable window and final two-axis ink bounds.

    ``axis_*`` is the only direction the placement solver may translate:
    horizontal lyrics use Y, vertical lyrics use X.  ``cross_*`` is the
    orthogonal read-only interval used to reject false rectangle collisions.
    """

    line_id: Hashable
    page_id: Hashable
    display_start_ms: int
    display_end_ms: int
    axis_min: float
    axis_max: float
    entry_start_ms: int | None = None
    """Full display start including entry animation, when known."""
    axis_anchor: float | None = None
    """Authored baseline/column centre used to snap an avoided page to a row."""
    cross_min: float = float("-inf")
    cross_max: float = float("inf")
    """Static ink bounds on the non-displacement axis.

    Legacy/unit-test bands omit these fields and therefore conservatively
    overlap on the cross axis.  Measured renderer bands always provide them.
    """

    def shifted(self, delta: float) -> "LineVisualBand":
        return LineVisualBand(
            line_id=self.line_id,
            page_id=self.page_id,
            display_start_ms=self.display_start_ms,
            display_end_ms=self.display_end_ms,
            axis_min=self.axis_min + delta,
            axis_max=self.axis_max + delta,
            entry_start_ms=self.entry_start_ms,
            axis_anchor=(
                None if self.axis_anchor is None else self.axis_anchor + delta
            ),
            cross_min=self.cross_min,
            cross_max=self.cross_max,
        )


@dataclass(frozen=True)
class PageVisualBands:
    """All line bands of one page before inter-page placement.

    ``gap_px`` belongs to this page's layout.  A later page that overlaps this
    page must preserve this value around the already occupied bands.
    ``layout_key`` is retained in the transport model for compatibility and
    diagnostics; animation intervals never expand placement collision windows.
    """

    page_id: Hashable
    bands: tuple[LineVisualBand, ...]
    gap_px: float = 0.0
    anchor: PageAnchor = "end"
    layout_key: Hashable | None = None


@dataclass(frozen=True)
class AxisOffsetWindow:
    """One half-open display interval with a resolved page translation."""

    start_ms: int
    end_ms: int
    offset: float


def time_windows_overlap(
    left: LineVisualBand,
    right: LineVisualBand,
) -> bool:
    """Return whether two half-open *stable ink* windows intersect.

    Entry and exit animation intervals are deliberately excluded for every
    layout.  ``entry_start_ms`` only controls how long an already selected page
    translation remains visible; it never expands the collision lifetime.
    """

    return max(left.display_start_ms, right.display_start_ms) < min(
        left.display_end_ms, right.display_end_ms
    )


def bands_require_separation(
    incoming: LineVisualBand,
    previous: LineVisualBand,
    gap: float = 0.0,
) -> bool:
    """Whether two static ink rectangles violate axis/gap separation."""

    incoming = _normalized_band(incoming)
    previous = _normalized_band(previous)
    if (
        incoming.cross_max <= previous.cross_min
        or incoming.cross_min >= previous.cross_max
    ):
        return False

    return _separation_deficit(
        incoming,
        previous,
        0.0,
        max(float(gap), 0.0),
    ) > 0.0


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
    occupied: list[tuple[LineVisualBand, float, Hashable | None]] = []
    for page in pages:
        bands = tuple(_normalized_band(item) for item in page.bands)
        if not bands:
            offsets[page.page_id] = 0.0
            continue
        time_pairs = [
            (incoming, previous, max(float(previous_gap), 0.0))
            for incoming in bands
            for previous, previous_gap, _previous_layout_key in occupied
            if previous.page_id != page.page_id
            and time_windows_overlap(incoming, previous)
        ]
        overlapping_pairs = {
            (incoming, previous)
            for incoming, previous, _previous_gap in time_pairs
            if _pixel_overlap(incoming, previous, 0.0) > 0.0
        }
        # A requested line gap is a placement target after a real painted
        # collision, not a collision trigger by itself.  Other temporally
        # active bands remain zero-gap constraints so moving the rigid page
        # cannot create a new painted overlap elsewhere.
        relevant_pairs = [
            (
                incoming,
                previous,
                (
                    previous_gap
                    if (incoming, previous) in overlapping_pairs
                    else 0.0
                ),
            )
            for incoming, previous, previous_gap in time_pairs
        ]
        snap_offsets = {
            float(previous.axis_anchor) - float(incoming.axis_anchor)
            for incoming in bands
            for previous, _previous_gap, _previous_layout_key in occupied
            if previous.page_id != page.page_id
            and incoming.axis_anchor is not None
            and previous.axis_anchor is not None
        }
        offset = (
            _choose_offset(
                bands,
                relevant_pairs,
                snap_offsets=snap_offsets,
                anchor=page.anchor,
                viewport_min=float(viewport_min),
                viewport_max=float(viewport_max),
            )
            if overlapping_pairs
            else 0.0
        )
        offsets[page.page_id] = offset
        occupied.extend(
            (
                item.shifted(offset),
                max(float(page.gap_px), 0.0),
                page.layout_key,
            )
            for item in bands
        )
    return offsets


def solve_page_axis_offset_windows(
    pages: Sequence[PageVisualBands],
    *,
    viewport_min: float,
    viewport_max: float,
) -> dict[Hashable, tuple[AxisOffsetWindow, ...]]:
    """Resolve one persistent translation for each page's full display life.

    Once a page moves, changing overlap later in its lifetime must not pull it
    back to the authored position and force the viewer's gaze to jump.  The
    scalar solver still checks every temporally overlapping line pair and all
    previously shifted pages; this wrapper only attaches that stable result to
    the page's half-open lifetime for CPU/native consumers.
    """

    offsets = solve_page_axis_offsets(
        pages,
        viewport_min=viewport_min,
        viewport_max=viewport_max,
    )
    resolved: dict[Hashable, tuple[AxisOffsetWindow, ...]] = {}
    for page in pages:
        valid_bands = tuple(
            band
            for band in page.bands
            if int(band.display_end_ms)
            > (
                int(band.entry_start_ms)
                if band.entry_start_ms is not None
                else int(band.display_start_ms)
            )
        )
        if not valid_bands:
            resolved[page.page_id] = ()
            continue
        resolved[page.page_id] = (
            AxisOffsetWindow(
                start_ms=min(
                    int(band.entry_start_ms)
                    if band.entry_start_ms is not None
                    else int(band.display_start_ms)
                    for band in valid_bands
                ),
                end_ms=max(int(band.display_end_ms) for band in valid_bands),
                offset=float(offsets.get(page.page_id, 0.0)),
            ),
        )
    return resolved


def _normalized_band(item: LineVisualBand) -> LineVisualBand:
    if item.axis_min <= item.axis_max and item.cross_min <= item.cross_max:
        return item
    return LineVisualBand(
        line_id=item.line_id,
        page_id=item.page_id,
        display_start_ms=item.display_start_ms,
        display_end_ms=item.display_end_ms,
        axis_min=min(item.axis_min, item.axis_max),
        axis_max=max(item.axis_min, item.axis_max),
        entry_start_ms=item.entry_start_ms,
        axis_anchor=item.axis_anchor,
        cross_min=min(item.cross_min, item.cross_max),
        cross_max=max(item.cross_min, item.cross_max),
    )


def _choose_offset(
    bands: Sequence[LineVisualBand],
    pairs: Sequence[CollisionPair],
    *,
    snap_offsets: Iterable[float] = (),
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

    snap_candidates = {float(value) for value in snap_offsets}
    candidates = {0.0, lower, upper, *snap_candidates}
    for incoming, previous, previous_gap in pairs:
        candidates.add(previous.axis_min - previous_gap - incoming.axis_max)
        candidates.add(previous.axis_max + previous_gap - incoming.axis_min)
        candidates.add(previous.axis_min - incoming.axis_max)
        candidates.add(previous.axis_max - incoming.axis_min)

    in_viewport = {
        float(value)
        for value in candidates
        if _viewport_overflow(
            bands,
            float(value),
            viewport_min=viewport_min,
            viewport_max=viewport_max,
        )
        <= 0.0
    }
    preferred = {
        value for value in in_viewport if _direction_penalty(value, anchor) == 0
    }
    opposite = in_viewport - preferred
    snap_in_viewport = in_viewport.intersection(snap_candidates)

    # Preserve the authored top/bottom direction when it has enough room, then
    # search the opposite side.  Requested line gap has priority over merely
    # non-overlapping pixels.  Every accepted candidate must keep the complete
    # static ink envelope inside the logical canvas.
    for placement_level in ("gap", "pixel"):
        for directional in (preferred, opposite):
            directional_groups = (
                directional.intersection(snap_in_viewport),
                directional,
            )
            for candidate_group in directional_groups:
                if not candidate_group:
                    continue
                if placement_level == "gap":
                    valid = [
                        value
                        for value in candidate_group
                        if all(
                            _separation_deficit(
                                incoming, previous, value, previous_gap
                            )
                            <= 0.0
                            for incoming, previous, previous_gap in pairs
                        )
                    ]
                else:
                    valid = [
                        value
                        for value in candidate_group
                        if all(
                            _pixel_overlap(incoming, previous, value) <= 0.0
                            for incoming, previous, _previous_gap in pairs
                        )
                    ]
                if valid:
                    return min(
                        valid,
                        key=lambda value: _offset_score(
                            value,
                            pairs,
                            bands=bands,
                            anchor=anchor,
                            viewport_min=viewport_min,
                            viewport_max=viewport_max,
                            placement_level=placement_level,
                        ),
                    )

    # There is no complete collision-free position on either side.  Keeping
    # the authored position preserves the existing page/draw priority and is
    # safer than moving part of the subtitle outside the canvas.
    return 0.0


def _offset_score(
    offset: float,
    pairs: Sequence[CollisionPair],
    *,
    bands: Sequence[LineVisualBand],
    anchor: PageAnchor,
    viewport_min: float,
    viewport_max: float,
    placement_level: Literal["gap", "pixel", "overlap"],
) -> tuple[float, ...]:
    deficits = [
        _separation_deficit(incoming, previous, offset, previous_gap)
        for incoming, previous, previous_gap in pairs
    ]
    pixel_overlaps = [
        _pixel_overlap(incoming, previous, offset)
        for incoming, previous, _previous_gap in pairs
    ]
    gap_total = sum(value for value in deficits if value > 0.0)
    gap_count = sum(value > 0.0 for value in deficits)
    pixel_total = sum(value for value in pixel_overlaps if value > 0.0)
    pixel_count = sum(value > 0.0 for value in pixel_overlaps)
    direction_penalty = _direction_penalty(offset, anchor)
    overflow = _viewport_overflow(
        bands, offset, viewport_min=viewport_min, viewport_max=viewport_max
    )
    if placement_level == "overlap":
        # If the viewport cannot contain all active ink, minimize actual painted
        # intersection first.  Missing the requested gap is less severe than
        # drawing two glyph/effect envelopes over each other.
        return (
            pixel_total,
            pixel_count,
            gap_total,
            gap_count,
            overflow,
            abs(offset),
            direction_penalty,
            offset,
        )
    if placement_level == "pixel":
        return (
            gap_total,
            gap_count,
            direction_penalty,
            overflow,
            abs(offset),
            offset,
        )
    return direction_penalty, overflow, abs(offset), offset


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
    if (
        incoming.cross_max <= previous.cross_min
        or incoming.cross_min >= previous.cross_max
    ):
        return 0.0
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


def _pixel_overlap(
    incoming: LineVisualBand,
    previous: LineVisualBand,
    offset: float,
) -> float:
    if (
        incoming.cross_max <= previous.cross_min
        or incoming.cross_min >= previous.cross_max
    ):
        return 0.0
    return max(
        min(incoming.axis_max + offset, previous.axis_max)
        - max(incoming.axis_min + offset, previous.axis_min),
        0.0,
    )


def _viewport_overflow(
    bands: Sequence[LineVisualBand],
    offset: float,
    *,
    viewport_min: float,
    viewport_max: float,
) -> float:
    page_min = min(item.axis_min for item in bands) + offset
    page_max = max(item.axis_max for item in bands) + offset
    return max(viewport_min - page_min, 0.0) + max(page_max - viewport_max, 0.0)


def shifted_bands(
    bands: Iterable[LineVisualBand], offset: float
) -> tuple[LineVisualBand, ...]:
    """Convenience helper used by renderer-side diagnostics and tests."""

    return tuple(item.shifted(offset) for item in bands)
