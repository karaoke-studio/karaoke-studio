"""Frame-independent timing semantics for subtitle guide signals."""

from __future__ import annotations

from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
from krok_helper.subtitle_render.engine.layout.page_plan import (
    page_plan_signature,
    section_head_line_indices,
)
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingTrack


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


__all__ = [
    "display_style_for_signal_window",
    "lit_signal_active",
    "signal_head_context",
    "signal_lead_in_ms",
]
