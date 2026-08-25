"""Layout-facing line geometry semantics independent from a paint backend."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from krok_helper.subtitle_render.engine.guide import (
    guide_symbol_is_bitmap,
    render_line_with_guide_symbols,
)
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.timing.timeline import compute_char_intervals
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingLine, TimingTrack


CharWidthResolver = Callable[[TimingLine, Style], Sequence[int]]
GuideAnchorMeasurer = Callable[
    [TimingTrack, TimingLine, Style],
    tuple[float, float] | None,
]


def line_has_role_labels(line: TimingLine) -> bool:
    return any(bool(char.role_label) for char in line.chars)


def resolve_char_intervals(
    line: TimingLine,
    style: Style,
    char_widths_for: CharWidthResolver,
) -> list[tuple[int, int]]:
    """Resolve final character intervals using backend-provided glyph widths."""
    line_style = style_for_line(style, line)
    render_line = render_line_with_guide_symbols(line)
    if line_style.vertical:
        return compute_char_intervals(render_line)
    return compute_char_intervals(
        render_line,
        list(char_widths_for(render_line, line_style)),
    )


def resolve_guide_anchor_bounds(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    measure_anchor: GuideAnchorMeasurer,
) -> tuple[float, float] | None:
    """Resolve whether and how a source line contributes a guide anchor box."""
    line_style = style_for_line(style, line)
    guide_symbols = [line.guide_symbol, *line.inline_guide_symbols.values()]
    if (
        line_style.vertical
        or line_has_role_labels(line)
        or (line.guide_symbol is None and not line.inline_guide_symbols)
        or any(guide_symbol_is_bitmap(symbol) for symbol in guide_symbols)
    ):
        return None
    return measure_anchor(track, line, line_style)


__all__ = [
    "CharWidthResolver",
    "GuideAnchorMeasurer",
    "line_has_role_labels",
    "resolve_char_intervals",
    "resolve_guide_anchor_bounds",
]
