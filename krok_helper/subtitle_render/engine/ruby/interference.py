"""Resolve ruby interference using injected, already-measured reading bounds."""
from dataclasses import dataclass
import math

from krok_helper.subtitle_render.engine.text.cell_positions import char_left_positions


@dataclass(frozen=True)
class RubySpacingSpan:
    first: int
    last: int
    lower: bool
    key: object


def resolve_ruby_spacing(char_widths, style, spans, *, draw_bounds):
    """Keep upper/lower groups separate and push later targets as in N3.

    ``draw_bounds(span, left, target_width)`` returns reading box edges, before
    ink bearings/strokes. Main timing never depends on the inserted gaps.
    """
    if not char_widths or not spans:
        return [0] * len(char_widths), 0, 0
    spacing = int(style.letter_spacing_px)
    interval = int(style.ruby_interval_px)
    gaps = [0] * len(char_widths)
    previous_right = None
    min_left = max_right = 0.0
    previous_lower = False
    for span in sorted(spans, key=lambda s: (s.lower, s.first)):
        if not 0 <= span.first <= span.last < len(char_widths):
            raise ValueError('注音间距目标超出正文单元范围')
        if span.lower != previous_lower:
            previous_right = None
        previous_lower = span.lower
        lefts = char_left_positions(char_widths, 0, False, spacing, char_gaps=gaps,
                                    n3_no_backtracking=style.layout_semantics == 'n3_1074')
        left, right = lefts[span.first], lefts[span.last] + char_widths[span.last]
        ruby_left, ruby_right = draw_bounds(span, float(left), float(max(right - left, 1)))
        if not all(math.isfinite(v) for v in (ruby_left, ruby_right)) or ruby_right < ruby_left:
            raise ValueError('注音间距度量包含无效或非有限边界')
        if previous_right is not None and span.first > 0:
            deficit = previous_right + interval - ruby_left
            if deficit > 0:
                push = math.ceil(deficit)
                gaps[span.first] += push
                ruby_left += push
                ruby_right += push
        previous_right = ruby_right
        min_left = min(min_left, ruby_left)
        max_right = max(max_right, ruby_right)
    if style.layout_semantics == 'n3_1074':
        lefts = char_left_positions(char_widths, 0, False, spacing, char_gaps=gaps, n3_no_backtracking=True)
        text_width = lefts[-1] + char_widths[-1]
    else:
        text_width = sum(char_widths) + spacing * max(len(char_widths) - 1, 0) + sum(gaps)
    return gaps, max(0, math.ceil(-min_left)), max(0, math.ceil(max_right - text_width))
