"""Lower-annotation collision/placement envelope and cache-key tests.

The generated lower reading (``lower_ruby_enabled``) is painted outside the
main glyph box, so the static placement envelope must include its ink.  Upper
authored Ruby keeps the historical main-glyph-only collision treatment.
"""

from __future__ import annotations

import math
from dataclasses import replace

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import (
    RubyAnnotation,
    TimingChar,
    TimingLine,
    TimingTrack,
    TrackPage,
    TrackPagePlan,
    TrackSection,
)
from krok_helper.subtitle_render.engine import painter as subtitle_painter
from krok_helper.subtitle_render.engine.layout.page.placement import (
    bands_require_separation,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.layout import (
    annotated_line_geometry,
    lower_ruby_reserve,
)
from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine


def _style(**kwargs) -> Style:
    values = {
        "font_family": "Meiryo",
        "font_size_px": 80,
        "ruby_font_size_px": 40,
        "dual_line_layout": False,
        "lower_ruby_enabled": True,
        "lower_ruby_gap_px": 0,
        "lower_ruby_follow_upper": True,
        "_lower_romaji_options": (False, False, False, False),
        "decoration_kind": "none",
        "ruby_decoration_kind": "none",
        "entry_anim": "none",
        "exit_anim": "none",
    }
    values.update(kwargs)
    return Style(**values)


def _line(text: str, start_ms: int = 1_000, **kwargs) -> TimingLine:
    return TimingLine(
        chars=[TimingChar(char, start_ms + index * 500) for index, char in enumerate(text)],
        end_ms=start_ms + len(text) * 500,
        **kwargs,
    )


def _display(
    line: TimingLine,
    *,
    lane: int = 0,
    start_ms: int = 500,
    end_ms: int = 2_600,
    section_index: int = 0,
    page_index: int = 0,
) -> DisplayLine:
    return DisplayLine(
        line=line,
        lane=lane,
        display_start_ms=start_ms,
        display_end_ms=end_ms,
        section_index=section_index,
        page_index=page_index,
    )


def _lower_layouts(layout) -> list:
    return [
        ruby
        for ruby in layout.ruby_layouts
        if getattr(ruby.ruby, "render_style", None) is not None
    ]


def _upper_layouts(layout) -> list:
    return [
        ruby
        for ruby in layout.ruby_layouts
        if getattr(ruby.ruby, "render_style", None) is None
    ]


def _measured_line(
    track: TimingTrack,
    style: Style,
    line: TimingLine,
    *,
    width: int = 640,
    height: int = 720,
    lane: int = 0,
) -> tuple[tuple[int, int, int, int] | None, object]:
    """Return the collision rect and the exact line layout it came from."""

    display = [_display(line, lane=lane)]
    line_style = subtitle_painter._style_for_line(style, line)
    baselines = subtitle_painter._resolve_display_baselines(
        height, track, display, style
    )
    line_layouts = subtitle_painter._resolve_sayatoo_line_layouts(
        width, height, track, display, baselines, 0, style
    )
    cache_sig = subtitle_painter._layout_cache_sig(track, style)
    rect = subtitle_painter._display_line_horizontal_ink_rect(
        width,
        height,
        track,
        display[0],
        style,
        baselines,
        line_layouts,
        layout_cache_sig=cache_sig,
    )
    sayatoo_layout = line_layouts[id(line)]
    layout = subtitle_painter._layout_line(
        track,
        line,
        line_style,
        width,
        height,
        baseline_y=sayatoo_layout.baseline_y,
        line_x=sayatoo_layout.text_x,
        lane=lane if line_style.dual_line_layout else None,
        cache_sig=cache_sig,
    )
    assert layout is not None
    return rect, layout


def _page_displacement_track() -> tuple[TimingTrack, Style]:
    """Two-row page + one-row page whose stable windows overlap in time."""

    lines = [
        TimingLine(
            chars=[TimingChar(text, 1_000)],
            end_ms=2_000,
            display_start_override_ms=500,
            display_end_override_ms=2_600,
            track_line_index=index,
        )
        for index, text in enumerate(("あ", "い", "う"))
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan([TrackSection([TrackPage(2), TrackPage(1)])]),
    )
    style = _style(
        dual_line_layout=True,
        line_y_position="top",
        line_y_margin_px=20,
        line_gap_px=0,
        allow_inter_page_line_overlap=False,
        allow_entry_exit_animation_overlap=False,
        overlap_fallback_mode="lift",
        sync_entry=False,
        sync_ending=False,
        sync_each_page=False,
    )
    return track, style


def _window_offset(
    windows: dict[int, tuple[tuple[int, int, float, float], ...]], index: int
) -> float:
    items = windows.get(index, ())
    assert items
    return float(items[0][3])


def test_collision_envelope_with_lower_on_includes_lower_reading_ink(qapp):
    line = _line("かな")
    track = TimingTrack(lines=[line])
    style = _style()

    rect, layout = _measured_line(track, style, line)
    lower = _lower_layouts(layout)
    main = subtitle_painter._line_main_text_ink_rect(layout)

    assert rect is not None and main is not None
    assert [ruby.ruby.reading for ruby in lower] == ["ka", "na"]
    expected = (
        min(
            main[0],
            math.floor(min(float(r.gradient_rect.left()) for r in lower)),
        ),
        min(
            main[1],
            math.floor(min(float(r.gradient_rect.top()) for r in lower)),
        ),
        max(
            main[2],
            math.ceil(max(float(r.gradient_rect.right()) for r in lower)),
        ),
        max(
            main[3],
            math.ceil(max(float(r.gradient_rect.bottom()) for r in lower)),
        ),
    )
    assert rect == expected
    # The bottom comes from the lower reading, not from the main glyph ink.
    assert rect[3] == math.ceil(
        max(float(ruby.gradient_rect.bottom()) for ruby in lower)
    )
    assert rect[3] > main[3]
    # Lower sits below the baseline: it must not raise the envelope top.
    assert rect[1] == main[1]


def test_upper_authored_ruby_stays_main_only_but_lower_still_contributes(qapp):
    line = _line("歌")
    upper = RubyAnnotation(
        "歌",
        "うた",
        pos_start_ms=1_000,
        pos_end_ms=2_000,
        target_line_index=0,
        target_char_start=0,
        target_char_end=1,
    )
    track = TimingTrack(lines=[line], rubies=[upper])
    style = _style()

    rect, layout = _measured_line(track, style, line)
    main = subtitle_painter._line_main_text_ink_rect(layout)
    upper_layouts = _upper_layouts(layout)
    lower = _lower_layouts(layout)

    assert rect is not None and main is not None
    assert upper_layouts and lower
    assert upper_layouts[0].gradient_rect.top() < main[1]  # drawn above main
    # Authored upper Ruby is still excluded from the collision envelope.
    assert rect[1] == main[1]
    # The generated lower reading is included.
    assert rect[3] == math.ceil(
        max(float(ruby.gradient_rect.bottom()) for ruby in lower)
    )
    assert rect[3] > main[3]


def test_collision_envelope_with_lower_off_equals_main_glyph_rect(qapp):
    line = _line("歌")
    upper = RubyAnnotation(
        "歌",
        "うた",
        pos_start_ms=1_000,
        pos_end_ms=2_000,
        target_line_index=0,
        target_char_start=0,
        target_char_end=1,
    )
    track = TimingTrack(lines=[line], rubies=[upper])
    style = _style(lower_ruby_enabled=False)

    rect, layout = _measured_line(track, style, line)
    main = subtitle_painter._line_main_text_ink_rect(layout)

    assert rect is not None and main is not None
    assert _upper_layouts(layout), "authored upper ruby must still be laid out"
    assert not _lower_layouts(layout)
    assert rect == main


def test_lower_readings_force_page_displacement_clear_of_their_ink(qapp):
    """The lower-ink envelope, not just the main glyphs, drives page lifting."""

    track, base = _page_displacement_track()
    lower_on = replace(base, lower_ruby_enabled=True)
    lower_off = replace(base, lower_ruby_enabled=False)

    windows_on = subtitle_painter.resolved_page_offset_windows_for_style(
        1_280, 1_080, track, lower_on
    )
    windows_off = subtitle_painter.resolved_page_offset_windows_for_style(
        1_280, 1_080, track, lower_off
    )
    dy_on = _window_offset(windows_on, 2)
    dy_off = _window_offset(windows_off, 2)

    assert dy_off > 0.0
    assert dy_on > dy_off

    bands_on = {
        index: band
        for index, _page, band, _gap in subtitle_painter.measure_collision_bands(
            1_280, 1_080, track, lower_on,
            subtitle_painter.display_lines_for_style(
                track, lower_on, logical_w=1_280, logical_h=1_080
            ),
        )
    }
    bands_off = {
        index: band
        for index, _page, band, _gap in subtitle_painter.measure_collision_bands(
            1_280, 1_080, track, lower_off,
            subtitle_painter.display_lines_for_style(
                track, lower_off, logical_w=1_280, logical_h=1_080
            ),
        )
    }
    previous_on, incoming_on = bands_on[1], bands_on[2]
    previous_off, incoming_off = bands_off[1], bands_off[2]

    # With lower ink in the envelope the incoming row clears the reading.
    assert not bands_require_separation(previous_on, incoming_on.shifted(dy_on))
    # The main-only placement solution would paint the incoming row straight
    # over the previous row's lower reading.
    assert bands_require_separation(previous_on, incoming_on.shifted(dy_off))
    # The old envelope is self-consistent with its own (smaller) offset.
    assert not bands_require_separation(previous_off, incoming_off.shifted(dy_off))


def test_collision_geometry_cache_invalidates_on_lower_geometry_changes(qapp):
    line = _line("かな")
    track = TimingTrack(lines=[line])
    display = [_display(line)]
    base = _style(line_y_position="top", lower_ruby_follow_upper=False)
    cache = subtitle_painter.CollisionGeometryCache()

    first = subtitle_painter.measure_collision_bands(
        640, 720, track, base, display, geometry_cache=cache
    )
    first_band = first[0][2]
    base_baselines = subtitle_painter._resolve_display_baselines(
        720, track, display, base
    )

    variants = {
        "gap": replace(base, lower_ruby_gap_px=90),
        "interval": replace(base, lower_ruby_interval_px=60),
        "style": replace(
            base, lower_ruby_style={"ruby_latin_font_size_px": 80}
        ),
        "disabled": replace(base, lower_ruby_enabled=False),
    }
    changed_bands = {}
    for name, style in variants.items():
        cached = subtitle_painter.measure_collision_bands(
            640, 720, track, style, display, geometry_cache=cache
        )
        fresh = subtitle_painter.measure_collision_bands(
            640, 720, track, style, display
        )
        assert cached == fresh, f"stale collision geometry for {name}"
        changed_bands[name] = cached[0][2]
        if name != "disabled":
            # Top anchoring keeps the baseline identical, so only the logical
            # identity (lower style fields) can invalidate the cached rect.
            assert subtitle_painter._resolve_display_baselines(
                720, track, display, style
            ) == base_baselines

    assert changed_bands["gap"].axis_max > first_band.axis_max
    assert changed_bands["interval"].cross_max > first_band.cross_max
    assert changed_bands["style"].axis_max > first_band.axis_max
    assert changed_bands["disabled"].axis_max < first_band.axis_max


def test_row_box_reserve_is_position_independent_and_keeps_lower_inside_canvas(qapp):
    line = _line("かな")
    track = TimingTrack(lines=[line])
    canvas_h = 1_080
    geometry = {
        position: annotated_line_geometry(_style(line_y_position=position))
        for position in ("top", "center", "bottom")
    }
    assert geometry["top"] == geometry["center"] == geometry["bottom"]
    main_h, ascent, descent, _extra = geometry["bottom"]
    style = _style(line_y_position="bottom")
    assert lower_ruby_reserve(style) > 0
    assert descent > annotated_line_geometry(
        replace(style, lower_ruby_enabled=False)
    )[2]

    for position in ("top", "center", "bottom"):
        current = _style(line_y_position=position)
        baseline = subtitle_painter._resolve_display_baselines(
            canvas_h, track, [_display(line)], current
        )[0]
        if position == "top":
            assert baseline - ascent == current.line_y_margin_px
        elif position == "center":
            assert baseline - ascent == (canvas_h - main_h) // 2
        else:
            assert baseline + descent == canvas_h - current.line_y_margin_px
        _rect, layout = _measured_line(
            track, current, line, height=canvas_h
        )
        lower = _lower_layouts(layout)
        assert lower
        # The actual lower ink stays inside the reserved row box...
        assert max(float(r.gradient_rect.bottom()) for r in lower) <= (
            baseline + descent
        )
        # ...and the bottom row keeps the authored margin before the edge.
        if position == "bottom":
            assert max(float(r.gradient_rect.bottom()) for r in lower) <= (
                canvas_h - current.line_y_margin_px
            )


def test_render_ir_row_and_annotation_baselines_match_python_lower_layout(qapp):
    line = _line("かな")
    track = TimingTrack(lines=[line])
    style = _style(
        font_family="Arial", font_family_latin="Times New Roman", line_y_position="top"
    )
    display = [_display(line)]
    baselines = subtitle_painter._resolve_display_baselines(
        720, track, display, style
    )
    _rect, layout = _measured_line(track, style, line, height=720)

    ir = build_render_ir(track, style, width=640, height=720, fps=60)
    ir_line = ir["track"]["lines"][0]
    assert ir_line["resolved_baseline_y"] == baselines[0]
    assert ir_line["annotation_baseline_y"] == baselines[0]

    lower_entries = {
        entry["reading"]: entry
        for entry in ir["track"]["rubies"]
        if entry.get("lower")
    }
    lower = _lower_layouts(layout)
    assert [ruby.ruby.reading for ruby in lower] == ["ka", "na"]
    for ruby in lower:
        entry = lower_entries[ruby.ruby.reading]
        assert (
            ir_line["annotation_baseline_y"] + entry["lower_baseline_offset_y"]
            == ruby.baseline_y
        )


def test_render_ir_page_offset_windows_follow_lower_collision_envelope(qapp):
    track, style = _page_displacement_track()
    python_windows = subtitle_painter.resolved_page_offset_windows_for_style(
        1_280, 1_080, track, style
    )
    items = subtitle_painter.display_lines_for_style(
        track, style, logical_w=1_280, logical_h=1_080
    )
    expected_baselines = subtitle_painter._resolve_display_baselines(
        1_080, track, items, style
    )
    ir = build_render_ir(track, style, width=1_280, height=1_080, fps=60)

    for index, ir_line in enumerate(ir["track"]["lines"]):
        expected = [
            (int(start), int(end), float(offset_x), float(offset_y))
            for start, end, offset_x, offset_y in python_windows.get(index, ())
            if int(end) > int(start)
        ]
        actual = [
            (
                int(window["start_ms"]),
                int(window["end_ms"]),
                float(window["offset_x"]),
                float(window["offset_y"]),
            )
            for window in ir_line["layout_offset_windows"]
        ]
        assert actual == expected
        assert (
            ir_line["annotation_baseline_y"]
            == expected_baselines[ir_line["lane"]]
        )
