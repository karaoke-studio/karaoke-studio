"""Layout-facing line geometry semantics independent from a paint backend."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from krok_helper.subtitle_render.engine.guide_semantics import (
    render_line_with_guide_symbols,
)
from krok_helper.subtitle_render.engine.line_style import style_for_line
from krok_helper.subtitle_render.engine.timeline import compute_char_intervals
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingLine


CharWidthResolver = Callable[[TimingLine, Style], Sequence[int]]


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


__all__ = [
    "CharWidthResolver",
    "line_has_role_labels",
    "resolve_char_intervals",
]
