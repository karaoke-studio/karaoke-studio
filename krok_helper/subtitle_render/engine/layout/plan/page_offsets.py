"""Assemble measured subtitle line boxes into persistent page-offset windows."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from krok_helper.subtitle_render.engine.layout.plan.model import LayoutOffsetWindow
from krok_helper.subtitle_render.engine.layout.page.placement import (
    AxisOffsetWindow,
    LineVisualBand,
    PageVisualBands,
    solve_page_axis_offsets,
)
from krok_helper.subtitle_render.engine.value_signature import value_signature
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.models import LYRICS_LAYOUT_FIELDS, Style
from krok_helper.subtitle_render.domain.timing import TimingTrack


PageId = tuple[int, int]
_PAGE_OFFSET_CACHE_MAX = 24
_PAGE_OFFSET_CACHE: OrderedDict[
    tuple,
    dict[int, tuple[LayoutOffsetWindow, ...]],
] = OrderedDict()


@dataclass(frozen=True)
class MeasuredPageLine:
    """One resolved display line and its optional static collision envelope."""

    track_index: int
    page_id: PageId
    display_start_ms: int
    display_end_ms: int
    page_style: Style
    collision_start_ms: int | None = None
    collision_end_ms: int | None = None
    axis_bounds: tuple[float, float] | None = None
    cross_bounds: tuple[float, float] | None = None
    axis_anchor: float | None = None


class DisplayLinesResolver(Protocol):
    def __call__(
        self,
        track: TimingTrack,
        style: Style,
        *,
        logical_w: int | None = None,
        logical_h: int | None = None,
    ) -> list[DisplayLine]: ...


class PageLineMeasurer(Protocol):
    def __call__(
        self,
        logical_w: int,
        logical_h: int,
        track: TimingTrack,
        style: Style,
        display_lines: list[DisplayLine],
    ) -> list[MeasuredPageLine]: ...


@dataclass(frozen=True)
class PageOffsetResolvers:
    """Backend capabilities needed by the page-offset layout policy."""

    display_lines: DisplayLinesResolver
    measure_lines: PageLineMeasurer


def _page_offset_cache_key(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
) -> tuple:
    return (
        max(int(logical_w), 1),
        max(int(logical_h), 1),
        value_signature(track),
        value_signature(style),
    )


def cached_page_offset_windows(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
) -> dict[int, tuple[LayoutOffsetWindow, ...]] | None:
    key = _page_offset_cache_key(logical_w, logical_h, track, style)
    cached = _PAGE_OFFSET_CACHE.get(key)
    if cached is None:
        return None
    _PAGE_OFFSET_CACHE.move_to_end(key)
    return {track_index: tuple(windows) for track_index, windows in cached.items()}


def store_page_offset_windows(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    resolved: dict[int, tuple[LayoutOffsetWindow, ...]],
) -> None:
    key = _page_offset_cache_key(logical_w, logical_h, track, style)
    _PAGE_OFFSET_CACHE[key] = {
        track_index: tuple(windows) for track_index, windows in resolved.items()
    }
    _PAGE_OFFSET_CACHE.move_to_end(key)
    while len(_PAGE_OFFSET_CACHE) > _PAGE_OFFSET_CACHE_MAX:
        _PAGE_OFFSET_CACHE.popitem(last=False)


def clear_page_offset_cache() -> None:
    _PAGE_OFFSET_CACHE.clear()


def resolve_page_offset_windows(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    resolvers: PageOffsetResolvers,
) -> dict[int, tuple[LayoutOffsetWindow, ...]]:
    """Resolve and cache page translations through backend measurement ports."""

    if style.allow_inter_page_line_overlap or not style.dual_line_layout:
        return {}
    cached = cached_page_offset_windows(logical_w, logical_h, track, style)
    if cached is not None:
        return cached
    display_lines = resolvers.display_lines(
        track,
        style,
        logical_w=logical_w,
        logical_h=logical_h,
    )
    if not display_lines:
        return {}
    measurements = resolvers.measure_lines(
        logical_w,
        logical_h,
        track,
        style,
        display_lines,
    )
    resolved = build_page_offset_windows(
        logical_w,
        logical_h,
        style,
        measurements,
    )
    store_page_offset_windows(logical_w, logical_h, track, style, resolved)
    return {key: tuple(value) for key, value in resolved.items()}


def page_offsets_at_time(
    windows: Mapping[int, Sequence[LayoutOffsetWindow]],
    *,
    t_ms: int | None = None,
) -> dict[int, tuple[float, float]]:
    """Select the active translation from each line's offset windows."""

    resolved: dict[int, tuple[float, float]] = {}
    for track_index, items in windows.items():
        selected: LayoutOffsetWindow | None = None
        if t_ms is None:
            selected = items[0] if items else None
        else:
            selected = next(
                (
                    item
                    for item in items
                    if item[0] <= int(t_ms) < item[1]
                ),
                None,
            )
        if selected is not None:
            resolved[track_index] = (selected[2], selected[3])
    return resolved


def _page_collision_layout_key(
    page_style: Style,
    *,
    line_count: int,
    vertical: bool,
) -> tuple:
    return (
        bool(vertical),
        max(int(line_count), 0),
        tuple(
            (name, value_signature(getattr(page_style, name)))
            for name in LYRICS_LAYOUT_FIELDS
        ),
    )


def build_page_offset_windows(
    logical_w: int,
    logical_h: int,
    style: Style,
    measurements: list[MeasuredPageLine],
) -> dict[int, tuple[LayoutOffsetWindow, ...]]:
    """Resolve page translations from backend-measured static line envelopes."""
    page_order: list[PageId] = []
    page_bands: dict[PageId, list[LineVisualBand]] = {}
    page_lifetimes: dict[PageId, list[tuple[int, int]]] = {}
    page_styles: dict[PageId, Style] = {}
    line_to_page: dict[int, PageId] = {}

    for item in measurements:
        if item.page_id not in page_bands:
            page_order.append(item.page_id)
            page_bands[item.page_id] = []
            page_lifetimes[item.page_id] = []
        page_styles.setdefault(item.page_id, item.page_style)
        line_to_page[item.track_index] = item.page_id
        page_lifetimes[item.page_id].append(
            (item.display_start_ms, item.display_end_ms)
        )
        if (
            item.axis_bounds is None
            or item.cross_bounds is None
            or item.collision_start_ms is None
            or item.collision_end_ms is None
            or item.collision_end_ms <= item.display_start_ms
        ):
            continue
        page_bands[item.page_id].append(
            LineVisualBand(
                line_id=item.track_index,
                page_id=item.page_id,
                display_start_ms=item.collision_start_ms,
                display_end_ms=item.collision_end_ms,
                axis_min=float(item.axis_bounds[0]),
                axis_max=float(item.axis_bounds[1]),
                entry_start_ms=item.display_start_ms,
                axis_anchor=item.axis_anchor,
                cross_min=float(item.cross_bounds[0]),
                cross_max=float(item.cross_bounds[1]),
            )
        )

    pages: list[PageVisualBands] = []
    for page_id in page_order:
        page_style = page_styles[page_id]
        position = page_style.line_y_position
        anchor = (
            "start"
            if position == "top"
            else "center"
            if position == "center"
            else "end"
        )
        if style.vertical:
            anchor = "end"
        pages.append(
            PageVisualBands(
                page_id=page_id,
                bands=tuple(page_bands[page_id]),
                gap_px=max(int(page_style.line_gap_px), 0),
                anchor=anchor,
                layout_key=_page_collision_layout_key(
                    page_style,
                    line_count=len(page_lifetimes.get(page_id, ())),
                    vertical=style.vertical,
                ),
            )
        )
    axis_offsets = solve_page_axis_offsets(
        pages,
        viewport_min=0.0,
        viewport_max=float(logical_w if style.vertical else logical_h),
    )
    axis_windows: dict[PageId, tuple[AxisOffsetWindow, ...]] = {}
    for page_id in page_order:
        lifetimes = [
            (start, end)
            for start, end in page_lifetimes.get(page_id, ())
            if end > start
        ]
        axis_windows[page_id] = (
            (
                AxisOffsetWindow(
                    start_ms=min(start for start, _end in lifetimes),
                    end_ms=max(end for _start, end in lifetimes),
                    offset=float(axis_offsets.get(page_id, 0.0)),
                ),
            )
            if lifetimes
            else ()
        )
    return {
        track_index: tuple(
            (
                int(window.start_ms),
                int(window.end_ms),
                float(window.offset) if style.vertical else 0.0,
                0.0 if style.vertical else float(window.offset),
            )
            for window in axis_windows.get(page_id, ())
        )
        for track_index, page_id in line_to_page.items()
    }


__all__ = [
    "MeasuredPageLine",
    "PageOffsetResolvers",
    "build_page_offset_windows",
    "cached_page_offset_windows",
    "clear_page_offset_cache",
    "page_offsets_at_time",
    "resolve_page_offset_windows",
    "store_page_offset_windows",
]
