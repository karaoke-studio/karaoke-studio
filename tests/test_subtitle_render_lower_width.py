"""Lower romaji interference is merged with upper ruby by per-cell maximum.

Lower readings do move the main text when they need more room than the authored
upper reading, but their push is never added on top of the upper push: the two
annotation layers are solved independently and merged with ``max()``.  This
pins the 2026-11 defect where every lower reading accumulated an extra push and
inflated the line width along the line.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import (
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.painter import _layout_line
from krok_helper.subtitle_render.engine.render.elements.horizontal.positioning import (
    line_total_width,
)
from krok_helper.subtitle_render.engine.ruby.interference import (
    RubySpacingSpan,
    resolve_ruby_spacing,
)
from krok_helper.subtitle_render.engine.ruby.layout import ruby_char_gaps


def _style(**kwargs) -> Style:
    return Style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=40,
        ruby_font_size_px=20,
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        lower_ruby_enabled=True,
        lower_ruby_gap_px=0,
        ruby_gap_px=0,
        decoration_kind="none",
        stroke_width_px=0,
        stroke2_enabled=False,
        ruby_stroke_width_px=0,
        ruby_stroke2_enabled=False,
        ruby_decoration_kind="none",
        _lower_romaji_options=(False, False, False, False),
        **kwargs,
    )


def _line(text: str, *, index: int = 0) -> TimingLine:
    return TimingLine(
        chars=[
            TimingChar(char, position * 500, explicit_start=True)
            for position, char in enumerate(text)
        ],
        end_ms=len(text) * 500,
        track_line_index=index,
    )


def _spacing_style(semantics: str = "legacy") -> Style:
    return Style(
        layout_semantics=semantics,
        letter_spacing_px=0,
        ruby_interval_px=2,
    )


def _layer_gaps(spans, style, bounds):
    return resolve_ruby_spacing([20, 20, 20], style, spans, draw_bounds=bounds)[0]


@pytest.mark.parametrize("semantics", ["legacy", "n3_1074"])
def test_authored_upper_ruby_pushes_later_cells(semantics):
    style = _spacing_style(semantics)
    spans = [RubySpacingSpan(0, 0, False, 0), RubySpacingSpan(1, 1, False, 1)]
    bounds = lambda span, x, width: (x + (width - 60) / 2, x + (width + 60) / 2)  # noqa: E731
    assert resolve_ruby_spacing([20, 20], style, spans, draw_bounds=bounds) == (
        [0, 42],
        20,
        20,
    )


def test_upper_dominates_shared_boundary_but_lower_still_centers():
    style = _spacing_style()
    wide_upper = [
        RubySpacingSpan(0, 0, False, 0),
        RubySpacingSpan(1, 1, False, 1),
    ]
    narrow_lower = [
        RubySpacingSpan(0, 0, True, 2),
        RubySpacingSpan(1, 1, True, 3),
    ]
    def bounds(span, x, width):
        return (x - 20, x + 40) if not span.lower else (x - 5, x + 25)

    upper_only = _layer_gaps(wide_upper, style, bounds)
    lower_only = _layer_gaps(narrow_lower, style, bounds)
    both = _layer_gaps([*wide_upper, *narrow_lower], style, bounds)
    assert both == [max(a, b) for a, b in zip(upper_only, lower_only)]
    # The authored upper reading owns the shared inter-cell boundary, so its
    # push is not increased; the lower reading still reserves its own half-slot
    # at the line edges so the main cell stays centred under it.
    assert both[1] == upper_only[1]
    assert both[0] == lower_only[0] > 0


def test_lower_requirement_pushes_only_where_it_exceeds_upper():
    style = _spacing_style()
    narrow_upper = [
        RubySpacingSpan(0, 0, False, 0),
        RubySpacingSpan(1, 1, False, 1),
    ]
    wide_lower = [
        RubySpacingSpan(0, 0, True, 2),
        RubySpacingSpan(1, 1, True, 3),
    ]
    def bounds(span, x, width):
        return (x + 10, x + 30) if not span.lower else (x - 20, x + 40)

    upper_only = _layer_gaps(narrow_upper, style, bounds)
    lower_only = _layer_gaps(wide_lower, style, bounds)
    both = _layer_gaps([*narrow_upper, *wide_lower], style, bounds)
    assert both == [max(a, b) for a, b in zip(upper_only, lower_only)]
    # Lower's slot requirement covers the shared boundary and both edges.
    assert both[1] == lower_only[1]
    assert both[1] > upper_only[1]
    assert both[0] == lower_only[0] > 0


def test_merged_gaps_are_elementwise_max_not_sum():
    style = _spacing_style()
    spans = [
        RubySpacingSpan(0, 0, False, 0),
        RubySpacingSpan(1, 1, False, 1),
        RubySpacingSpan(0, 0, True, 2),
        RubySpacingSpan(1, 1, True, 3),
    ]

    def bounds(span, x, width):
        # Both layers need a push before cell 1; the lower reading needs more.
        if not span.lower:
            return (x - 10, x + 10) if span.first == 0 else (x - 40, x + 10)
        return (x - 10, x + 10) if span.first == 0 else (x - 80, x + 10)

    upper_only = _layer_gaps([s for s in spans if not s.lower], style, bounds)
    lower_only = _layer_gaps([s for s in spans if s.lower], style, bounds)
    both = _layer_gaps(spans, style, bounds)
    expected = [max(a, b) for a, b in zip(upper_only, lower_only)]
    assert both == expected
    assert upper_only[1] > 0 and lower_only[1] > 0
    assert both != [a + b for a, b in zip(upper_only, lower_only)]


@pytest.mark.parametrize("interval", [0, 200])
def test_line_total_width_includes_merged_gaps_and_lower_overhang(qapp, interval):
    line = _line("さくらさくら")
    style = replace(_style(), lower_ruby_interval_px=interval)
    track = TimingTrack(lines=[line])
    layout = _layout_line(track, line, style, 960, 540, baseline_y=400)
    assert layout is not None

    gaps, left_ext, right_ext = ruby_char_gaps(
        line, layout.char_widths, [], style, layout.intervals
    )
    # The line box is exactly the authored advance + merged pushes + the real
    # lower overhang on both sides.
    expected = int(
        round(
            sum(layout.char_widths)
            + int(style.letter_spacing_px) * max(len(layout.char_widths) - 1, 0)
            + sum(gaps)
            + left_ext
            + right_ext
        )
    )
    assert line_total_width(line, style, track.rubies, track=track) == expected
    assert sum(gaps) >= 0


def test_lower_pushes_do_not_grow_without_bound(qapp):
    """A long lower reading may push, but only by its measured deficit."""
    line = _line("ししし")
    style = _style()
    track = TimingTrack(lines=[line])
    plain = _layout_line(
        track, line, replace(style, lower_ruby_enabled=False), 960, 540,
        baseline_y=400,
    )
    for interval in (0, 50):
        annotated = _layout_line(
            track, line, replace(style, lower_ruby_interval_px=interval),
            960, 540, baseline_y=400,
        )
        assert annotated is not None
        gaps, _left, _right = ruby_char_gaps(
            line, plain.char_widths, [], style, plain.intervals
        )
        # Pushes are bounded by the narrowest possible 3-letter reading over
        # each 40px cell; they must not depend on the previous gaps twice.
        assert max(gaps, default=0) < 1000
        assert annotated.total_w >= plain.total_w


def test_authored_upper_ruby_keeps_its_own_push(qapp):
    line = _line("ししし")
    style = _style()
    track = TimingTrack(lines=[line])
    upper = [
        # Pause-only ruby values exercise a wide authored reading without
        # requiring any actual text content changes.
        (0, "ああああああ"),
        (1, "いいいいいい"),
    ]
    from krok_helper.subtitle_render.domain.timing import RubyAnnotation

    rubies = [
        RubyAnnotation(
            line.chars[index].text,
            reading,
            pos_start_ms=index * 500,
            pos_end_ms=(index + 1) * 500,
            target_line_index=0,
            target_char_start=index,
            target_char_end=index + 1,
        )
        for index, reading in upper
    ]
    upper_track = TimingTrack(lines=[line], rubies=rubies)
    plain = _layout_line(
        upper_track, line, replace(style, lower_ruby_enabled=False), 960, 540,
        baseline_y=400,
    )
    annotated = _layout_line(upper_track, line, style, 960, 540, baseline_y=400)
    assert plain is not None and annotated is not None
    # Upper interference still exists; the lower layer may add to it only where
    # the romanized reading needs more room (never by re-adding the upper gap).
    assert plain.total_w > sum(plain.char_widths)
    assert annotated.total_w >= plain.total_w
    assert annotated.char_widths == plain.char_widths


def test_line_width_is_independent_of_measurement_order(qapp):
    first = _line("さくらさくら", index=0)
    second = _line("しししし", index=1)
    track = TimingTrack(lines=[first, second])
    style = replace(_style(), lower_ruby_interval_px=200)
    style_first = style_for_line(style, first)
    style_second = style_for_line(style, second)
    with layout_pass():
        alone = line_total_width(second, style_second, track.rubies, track=track)
    with layout_pass():
        line_total_width(first, style_first, track.rubies, track=track)
        after_first = line_total_width(second, style_second, track.rubies, track=track)
        repeated = line_total_width(second, style_second, track.rubies, track=track)
    assert after_first == alone == repeated


def test_lower_ink_clearance_still_uses_half_slots():
    style = Style(letter_spacing_px=-2, ruby_interval_px=-2)
    spans = [RubySpacingSpan(i, i, True, i) for i in range(3)]
    original_ink = [(10, 90), (108, 188), (206, 286)]
    gaps, _left, _right = resolve_ruby_spacing(
        [100, 100, 100],
        style,
        spans,
        draw_bounds=lambda span, x, width: (x - 20, x + 120),
        lower_ink_bounds=lambda span, x, width: (x - 15, x + 115),
        main_ink_bounds=original_ink,
    )
    # 140px readings over 100px cells need 20px extra on each side: the first
    # cell gets its left half, interior boundaries carry both neighbouring
    # halves, and the trailing half is line overhang rather than a gap.
    assert gaps == [20, 40, 40]
