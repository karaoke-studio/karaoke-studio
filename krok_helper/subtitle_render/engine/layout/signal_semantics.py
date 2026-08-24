"""Frame-independent timing semantics for subtitle guide signals."""

from __future__ import annotations

from typing import Protocol

from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
from krok_helper.subtitle_render.engine.layout.page_plan import (
    page_plan_signature,
    section_head_line_indices,
)
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingTrack


class VisibleLinesResolver(Protocol):
    def __call__(
        self,
        track: TimingTrack,
        t_ms: int,
        style: Style,
        *,
        logical_w: int | None = None,
        logical_h: int | None = None,
    ) -> list[DisplayLine]: ...


def display_style_for_signal_window(style: Style) -> Style:
    """Return the style used to resolve signal-aware display windows."""
    # Signal lead is applied only to section heads by ``signal_head_context``;
    # no whole-style transformation is currently required.
    return style


def lit_signal_active(style: Style) -> bool:
    """Return whether a horizontal guide signal participates in layout."""
    return bool(style.lit_enabled) and not style.vertical


def signal_head_context(
    track: TimingTrack,
    style: Style,
) -> frozenset[int] | None:
    """Return track indexes that own a signal, or ``None`` when disabled."""
    if not lit_signal_active(style):
        return None
    cache = getattr(_LAYOUT_PASS, "signal_heads", None)
    key = None
    if cache is not None:
        key = (id(track), page_plan_signature(track), max(style.section_gap_ms, 0))
        hit = cache.get(key)
        if hit is not None:
            return hit
    heads = section_head_line_indices(
        track,
        style,
        section_gap_ms=max(style.section_gap_ms, 0),
    )
    if cache is not None:
        cache[key] = heads
        # The key contains id(track), so retain the owner for the pass.
        _LAYOUT_PASS.tracks.append(track)
    return heads


def signal_lead_in_ms(style: Style) -> int:
    """Return how far before singing a configured signal must become visible."""
    duration = max(int(style.signals_duration_ms), 0)
    if duration <= 0:
        return 0
    return max(
        0,
        duration
        + max(int(style.lit_waiting_time_ms), 0)
        - int(style.lit_time_offset_ms),
    )


def resolve_signal_display_lines(
    track: TimingTrack,
    t_ms: int,
    style: Style,
    visible_lines: VisibleLinesResolver,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> list[DisplayLine]:
    """Filter visible candidates to section heads that own a guide signal."""

    if not lit_signal_active(style) or signal_lead_in_ms(style) <= 0:
        return []
    signal_heads = signal_head_context(track, style)
    if signal_heads is None:
        return []
    index_of = {id(line): index for index, line in enumerate(track.lines)}
    return [
        item
        for item in visible_lines(
            track,
            t_ms,
            style,
            logical_w=logical_w,
            logical_h=logical_h,
        )
        if index_of.get(id(item.line)) in signal_heads
    ]


__all__ = [
    "display_style_for_signal_window",
    "lit_signal_active",
    "resolve_signal_display_lines",
    "signal_head_context",
    "signal_lead_in_ms",
]
