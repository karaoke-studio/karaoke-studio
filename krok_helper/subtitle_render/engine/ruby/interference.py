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


def _measure_span(char_widths, style, gaps, span, draw_bounds):
    lefts = char_left_positions(
        char_widths, 0, False, int(style.letter_spacing_px), char_gaps=gaps,
        n3_no_backtracking=style.layout_semantics == 'n3_1074',
    )
    left, right = lefts[span.first], lefts[span.last] + char_widths[span.last]
    target_width = float(max(right - left, 1))
    ruby_left, ruby_right = draw_bounds(span, float(left), target_width)
    if not all(math.isfinite(v) for v in (ruby_left, ruby_right)) or ruby_right < ruby_left:
        raise ValueError('注音间距度量包含无效或非有限边界')
    return float(left), target_width, ruby_left, ruby_right


def _apply_upper_pushes(char_widths, style, gaps, spans, *, draw_bounds):
    """Grow ``gaps`` with N3's left-to-right authored-upper interference."""
    if not spans:
        return
    interval = int(style.ruby_interval_px)
    previous_right = None
    for span in sorted(spans, key=lambda item: item.first):
        if not 0 <= span.first <= span.last < len(char_widths):
            raise ValueError('注音间距目标超出正文单元范围')
        _left, _target, ruby_left, ruby_right = _measure_span(
            char_widths, style, gaps, span, draw_bounds
        )
        if previous_right is not None and span.first > 0:
            deficit = previous_right + interval - ruby_left
            if deficit > 0:
                gaps[span.first] += math.ceil(deficit)
                _left, _target, ruby_left, ruby_right = _measure_span(
                    char_widths, style, gaps, span, draw_bounds
                )
        previous_right = ruby_right


def _apply_lower_slots(
    char_widths,
    style,
    gaps,
    spans,
    *,
    draw_bounds,
):
    """Widen each lower reading symmetrically around its own main cells.

    A reading wider than its cell opens half of the missing room at each side
    boundary, so the main glyph stays at the centre of its romanization slot
    instead of being pushed to the slot's left edge (the reported blank-gap /
    shifted-centre defect).  Boundary requirements from neighbouring readings
    are added on their own sides, then merged with the authored upper gaps via
    ``max`` so the lower layer never stacks a second push on top of upper's.
    """
    if not spans:
        return
    base_gaps = list(gaps)
    boundary = [0.0] * len(char_widths)
    for span in sorted(spans, key=lambda item: item.first):
        if not 0 <= span.first <= span.last < len(char_widths):
            raise ValueError('注音间距目标超出正文单元范围')
        _left, target_width, ruby_left, ruby_right = _measure_span(
            char_widths, style, base_gaps, span, draw_bounds
        )
        extra = max(0.0, (ruby_right - ruby_left) - target_width)
        half = extra / 2.0
        boundary[span.first] += half
        if span.last + 1 < len(char_widths):
            boundary[span.last + 1] += half
    for index, required in enumerate(boundary):
        if required > gaps[index]:
            gaps[index] = math.ceil(required)


def resolve_ruby_spacing(char_widths, style, spans, *, draw_bounds,
                         main_ink_bounds=None, lower_ink_bounds=None):
    """Push later targets as in N3 and centre lower readings in their slots.

    ``draw_bounds(span, left, target_width)`` returns reading box edges, before
    ink bearings/strokes.  Authored upper groups keep N3's left-to-right push.
    Generated lower groups do not add a second push on top of it: each reading
    is measured against the room the upper layer already opened, and only the
    remaining deficit is added — half on each side of its main cell, so the
    moved glyph's body stays centred under its reading.  ``main_ink_bounds`` /
    ``lower_ink_bounds`` are accepted for the historical clearance contract
    and keep the lower reading's visible ink inside the slot; main timing never
    depends on the gaps.
    """
    if not char_widths or not spans:
        return [0] * len(char_widths), 0, 0
    upper = [span for span in spans if not span.lower]
    lower = [span for span in spans if span.lower]
    gaps = [0] * len(char_widths)
    if upper:
        _apply_upper_pushes(char_widths, style, gaps, upper, draw_bounds=draw_bounds)
    if lower:
        _apply_lower_slots(char_widths, style, gaps, lower, draw_bounds=draw_bounds)

    spacing = int(style.letter_spacing_px)
    min_left = max_right = 0.0
    for span in sorted(spans, key=lambda item: (item.lower, item.first)):
        _left, _target, ruby_left, ruby_right = _measure_span(
            char_widths, style, gaps, span, draw_bounds
        )
        if lower_ink_bounds is not None and span.lower:
            ink_left, ink_right = lower_ink_bounds(span, _left, _target)
            min_left = min(min_left, ink_left)
            max_right = max(max_right, ink_right)
        else:
            min_left = min(min_left, ruby_left)
            max_right = max(max_right, ruby_right)

    if style.layout_semantics == 'n3_1074':
        lefts = char_left_positions(char_widths, 0, False, spacing, char_gaps=gaps,
                                    n3_no_backtracking=True)
        text_width = lefts[-1] + char_widths[-1]
    else:
        text_width = sum(char_widths) + spacing * max(len(char_widths) - 1, 0) + sum(gaps)
    return gaps, max(0, math.ceil(-min_left)), max(0, math.ceil(max_right - text_width))
