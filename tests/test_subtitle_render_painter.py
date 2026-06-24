"""Tests for ``krok_helper.subtitle_render.engine.painter``.

像素级断言不可移植（字形 / 字体可用性平台差异大），所以本测试聚焦：

- 函数能在不同时刻正常完成不抛
- 各阶段（未唱 / 半唱 / 全唱）画面像素与"完全空白"对比都有差异
- 空 track 不画任何东西
"""

from __future__ import annotations

import os
from dataclasses import replace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRectF  # noqa: E402
from PyQt6.QtGui import QColor, QFontMetrics, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.engine.painter import (  # noqa: E402
    _IMAGE_BRUSH_CACHE,
    _IMAGE_FILL_CACHE,
    _FillSegment,
    _LineCharTransition,
    _RubyTextLayer,
    _GlyphRunLayer,
    _GlyphRunAfterGlowLayer,
    _active_lit_indices,
    _active_rubies_for_line,
    _apply_character_transform,
    _brush_for_fill,
    _build_font,
    _build_ruby_font,
    _char_transition_layer_stack,
    _char_fade_opacity,
    _char_left_positions,
    _character_fill_ratio,
    _character_transform,
    _spin_flip_skew,
    _fill_clip_band,
    _fill_extent_end,
    _layout_vertical_line,
    _layout_rubies,
    _layout_line,
    _ruby_layer_stack,
    _resolve_vertical_columns,
    _ruby_utopia_visual_units,
    _vertical_fill_band,
    _vertical_orientation,
    _karaoke_fill_segments,
    _paint_ruby_text,
    _paint_ruby_text_units_with_transition,
    _resolve_display_baselines,
    _resolve_line_x,
    _resolve_sayatoo_line_layouts,
    _signal_layout_metrics,
    _signal_lit_groups,
    _signal_lit_y,
    _signal_local_x,
    _signal_stroke_extent,
    _volume_flash_alpha,
    _volume_signal_column_rects,
    _volume_signal_geometry,
    _ruby_progress_ratio,
    _ruby_reading_intervals,
    _ruby_layout_units,
    _ruby_target_indices,
    _ruby_target_x_range,
    _ruby_utopia_reading_units_and_intervals,
    _transition_char_state,
    _utopia_main_group_for_index,
    _utopia_transition_scope_layers,
    _visual_text_padding,
    _display_style_for_signal_window,
    _effective_ruby_for_target,
    _visible_lines_for_style,
    _resolve_title_text,
    _title_overlay_opacity,
    paint_frame,
    frame_vertical_bounds,
    clear_before_layer_cache,
    _RUN_GLOW_CACHE,
)
from krok_helper.subtitle_render.engine.layers import LayerCompositor, LayerContext, SCOPE_GROUP  # noqa: E402
from krok_helper.subtitle_render.engine.timeline import DisplayLine  # noqa: E402
from krok_helper.subtitle_render.models import (  # noqa: E402
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
    RubyAnnotation,
    SubtitleStyleScheme,
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
    TimingTrackMeta,
    TitleOverlay,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _blank(w=800, h=450) -> QImage:
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#101010"))
    return img


def _pixel_hash(img: QImage) -> int:
    """Bits 的 hash 近似可比对，足够做 diff 断言。"""
    bits = img.constBits()
    bits.setsize(img.sizeInBytes())
    return hash(bytes(bits))


def _ink_bounds(img: QImage, bg: QColor = QColor("#101010")) -> tuple[int, int, int, int]:
    left = img.width()
    top = img.height()
    right = -1
    bottom = -1
    bg_rgb = bg.rgb()
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixel(x, y) == bg_rgb:
                continue
            left = min(left, x)
            top = min(top, y)
            right = max(right, x)
            bottom = max(bottom, y)
    return left, top, right, bottom


def _bounds_size(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    left, top, right, bottom = bounds
    if right < left or bottom < top:
        return 0, 0
    return right - left + 1, bottom - top + 1


def _solid_fill(color: str) -> PaintFill:
    return PaintFill(
        mode="solid",
        color=color,
        start_color=color,
        end_color=color,
        gradient_stops=[(0, color), (100, color)],
        split_top_color=color,
        split_bottom_color=color,
    )


def _dominant_bounds(
    img: QImage,
    *,
    channel: str,
    left: int = 0,
    right: int | None = None,
    margin: int = 25,
) -> tuple[int, int, int, int]:
    channel_index = {"red": 0, "green": 1, "blue": 2}[channel]
    right = img.width() - 1 if right is None else right
    bounds = [img.width(), img.height(), -1, -1]
    for y in range(img.height()):
        for x in range(max(left, 0), min(right, img.width() - 1) + 1):
            color = QColor(img.pixel(x, y))
            values = (color.red(), color.green(), color.blue())
            value = values[channel_index]
            if value < 80 or any(
                value <= other + margin
                for index, other in enumerate(values)
                if index != channel_index
            ):
                continue
            bounds[0] = min(bounds[0], x)
            bounds[1] = min(bounds[1], y)
            bounds[2] = max(bounds[2], x)
            bounds[3] = max(bounds[3], y)
    return tuple(bounds)  # type: ignore[return-value]


def _track() -> TimingTrack:
    line = TimingLine(
        chars=[
            TimingChar(text="あ", start_ms=1000),
            TimingChar(text="い", start_ms=1500),
            TimingChar(text="う", start_ms=2000),
        ],
        end_ms=2500,
    )
    return TimingTrack(lines=[line])


def _two_line_track() -> TimingTrack:
    line1 = TimingLine(
        chars=[
            TimingChar(text="あ", start_ms=1000),
            TimingChar(text="い", start_ms=1500),
        ],
        end_ms=2000,
    )
    line2 = TimingLine(
        chars=[
            TimingChar(text="う", start_ms=3000),
            TimingChar(text="え", start_ms=3500),
        ],
        end_ms=4000,
    )
    return TimingTrack(lines=[line1, line2])


def _track_with_ruby() -> TimingTrack:
    line = TimingLine(
        chars=[
            TimingChar(text="漢", start_ms=1000),
            TimingChar(text="字", start_ms=1500),
        ],
        end_ms=2000,
    )
    return TimingTrack(
        lines=[line],
        rubies=[
            RubyAnnotation(
                kanji="漢字",
                reading="かんじ",
                pos_start_ms=1000,
                pos_end_ms=2000,
            )
        ],
    )


def _track_with_timed_ruby() -> TimingTrack:
    line = TimingLine(
        chars=[
            TimingChar(text="漢", start_ms=1000),
            TimingChar(text="字", start_ms=1500),
        ],
        end_ms=2500,
    )
    return TimingTrack(
        lines=[line],
        rubies=[
            RubyAnnotation(
                kanji="漢字",
                reading="かんじ",
                reading_part_ms=[500, 1000],
                pos_start_ms=1000,
                pos_end_ms=2500,
            )
        ],
    )


def _singer_track(singer_id: int = 1) -> TimingTrack:
    line = TimingLine(
        chars=[TimingChar(text="A", start_ms=1000)],
        end_ms=2000,
        singer_label=f"S{singer_id}",
        singer_id=singer_id,
    )
    return TimingTrack(lines=[line])


def _sayatoo_layout_for(
    track: TimingTrack,
    style: Style,
    t_ms: int,
    *,
    w: int = 160,
    h: int = 90,
):
    display_style = _display_style_for_signal_window(style)
    display_lines = _visible_lines_for_style(track, t_ms, display_style)
    baselines = _resolve_display_baselines(h, track, display_lines, display_style)
    return _resolve_sayatoo_line_layouts(
        w,
        h,
        track,
        display_lines,
        baselines,
        t_ms,
        display_style,
    )[0]


def _default_text_x(track: TimingTrack, style: Style, w: int = 160) -> int:
    line = track.lines[0]
    metrics = QFontMetrics(_build_font(style))
    text_w = sum(metrics.horizontalAdvance(c.text) for c in line.chars)
    visual_pad = _visual_text_padding(style)
    return _resolve_line_x(w, text_w + visual_pad * 2, style, 0) + visual_pad


def test_layout_plain_line_is_pure_t_independent_geometry(qapp):
    # P1.a：layout 段是纯几何函数，不接收 t_ms；字符几何/基线/fill_segments 与帧无关。
    from krok_helper.subtitle_render.engine.painter import _layout_plain_line

    track = _track()
    style = Style(line_y_position="center")
    layout = _layout_plain_line(track, track.lines[0], style, 800, 450)

    assert layout.total_w > 0
    assert layout.baseline_y > 0
    assert len(layout.char_x_ranges) == len(track.lines[0].chars)
    assert len(layout.char_widths) == len(track.lines[0].chars)
    assert len(layout.fill_segments) >= 1
    # fill_segments 携带的是时序(start/end_ms) + x 范围，而非"当前帧已填多少"。
    seg = layout.fill_segments[0]
    assert hasattr(seg, "start_ms") and hasattr(seg, "end_ms")
    assert hasattr(seg, "left") and hasattr(seg, "right")
    # 同一行同样式两次 layout 的几何一致（可缓存的前提）。
    again = _layout_plain_line(track, track.lines[0], style, 800, 450)
    assert again.char_x_ranges == layout.char_x_ranges
    assert again.baseline_y == layout.baseline_y


def test_layout_role_line_is_pure_geometry_with_per_glyph_fonts(qapp):
    # P1.a.2：分色行也走纯几何 layout 段，glyph 列表逐段带自身 font（句内混排的地基）。
    from krok_helper.subtitle_render.engine.painter import _layout_role_line

    line = TimingLine(
        chars=[
            TimingChar(text="A", start_ms=1000, role_label="大"),
            TimingChar(text="B", start_ms=2000, role_label="小"),
        ],
        end_ms=3000,
    )
    track = TimingTrack(lines=[line])
    style = Style(
        font_family="Arial", font_family_latin="Arial", font_size_px=48,
        line_y_position="center",
        custom_style_schemes={
            "大": SubtitleStyleScheme(font_size_px=72),
            "小": SubtitleStyleScheme(font_size_px=36),
        },
    )
    layout = _layout_role_line(track, line, style, 400, 220)

    assert layout is not None
    assert len(layout.text_layout.glyphs) == 2
    # 逐段不同字号 → glyph 各自字体不同（普通行做不到的句内混排）
    assert layout.text_layout.glyphs[0].font.pixelSize() != layout.text_layout.glyphs[1].font.pixelSize()
    assert len(layout.fill_segments) >= 1
    again = _layout_role_line(track, line, style, 400, 220)
    assert again.char_x_ranges == layout.char_x_ranges
    assert again.baseline_y == layout.baseline_y


def test_paint_frame_with_no_track_leaves_image_unchanged(qapp):
    img = _blank()
    baseline = _pixel_hash(img)
    paint_frame(img, None, 1000, Style())
    assert _pixel_hash(img) == baseline


def test_paint_frame_outside_any_line_leaves_image_unchanged(qapp):
    img = _blank()
    baseline = _pixel_hash(img)
    paint_frame(img, _track(), 500, Style(line_lead_in_ms=0))  # 早于行起点
    assert _pixel_hash(img) == baseline


def test_paint_frame_uses_default_line_lead_in(qapp):
    img = _blank()
    baseline = _pixel_hash(img)
    paint_frame(img, _track(), 500, Style())  # 默认提前 1800ms 显示
    assert _pixel_hash(img) != baseline


def test_signal_lits_default_off_leaves_early_frame_unchanged(qapp):
    img = _blank(120, 80)
    baseline = _pixel_hash(img)

    paint_frame(img, _track(), 900, Style(line_lead_in_ms=0))

    assert _pixel_hash(img) == baseline


def test_signal_lits_render_during_signal_window(qapp):
    img = _blank(120, 80)
    style = Style(
        font_size_px=20,
        line_y_margin_px=10,
        dual_line_layout=False,
        line_lead_in_ms=0,
        lit_enabled=True,
        lit_style="circle",
        lit_size=10,
        lit_offset_x=-20,
        lit_offset_y=0,
        lit_tracking=2,
        lit_stroke_width=0,
        lit_shadow=False,
        lit_transition_mode="none",
        signals_duration_ms=1000,
    )

    paint_frame(img, _singer_track(singer_id=0), 50, style)

    layout = _sayatoo_layout_for(_singer_track(singer_id=0), style, 50, w=120, h=80)
    bounds = _ink_bounds(img)
    assert bounds[0] == int(layout.signal_x)
    assert layout.text_x > layout.signal_x
    assert QColor(img.pixel(int(layout.signal_x) + 2, 36)).name(QColor.NameFormat.HexRgb).upper() == "#0000FF"
    assert QColor(img.pixel(layout.text_x, 56)).name(QColor.NameFormat.HexRgb).upper() == "#FFFFFF"


def test_frame_vertical_bounds_cover_signal_only_window(qapp):
    track = _singer_track(singer_id=0)
    style = Style(
        font_size_px=20,
        line_y_margin_px=10,
        dual_line_layout=False,
        line_lead_in_ms=0,
        lit_enabled=True,
        lit_style="circle",
        lit_size=10,
        lit_offset_x=-20,
        lit_offset_y=0,
        lit_tracking=2,
        lit_stroke_width=0,
        lit_shadow=False,
        lit_transition_mode="none",
        signals_duration_ms=1000,
    )
    img = _blank(120, 80)
    paint_frame(img, track, 50, style)
    ink = _ink_bounds(img)
    bounds = frame_vertical_bounds(120, 80, track, 50, style)

    assert ink is not None
    assert bounds is not None
    assert bounds[0] <= ink[1]
    assert bounds[1] >= ink[3]


def test_signal_lits_extend_the_lyric_text_window(qapp):
    img = _blank(120, 80)
    style = Style(
        font_size_px=20,
        line_y_margin_px=10,
        dual_line_layout=False,
        line_lead_in_ms=0,
        lit_enabled=True,
        lit_style="circle",
        lit_size=10,
        lit_offset_x=-20,
        lit_offset_y=0,
        lit_tracking=2,
        lit_stroke_width=0,
        lit_shadow=False,
        lit_transition_mode="none",
        signals_duration_ms=1000,
    )

    paint_frame(img, _singer_track(singer_id=0), 50, style)

    layout = _sayatoo_layout_for(_singer_track(singer_id=0), style, 50, w=120, h=80)
    bounds = _ink_bounds(img)
    assert bounds[0] == int(layout.signal_x)
    assert bounds[2] >= layout.text_x
    assert QColor(img.pixel(layout.text_x, 56)).name(QColor.NameFormat.HexRgb).upper() == "#FFFFFF"


def test_signal_lits_are_line_countdown_not_singer_lamps(qapp):
    img = _blank(120, 80)
    style = Style(
        font_size_px=20,
        line_y_margin_px=10,
        dual_line_layout=False,
        line_lead_in_ms=0,
        lit_enabled=True,
        lit_style="circle",
        lit_size=10,
        lit_offset_x=-20,
        lit_offset_y=0,
        lit_tracking=2,
        lit_stroke_width=0,
        lit_shadow=False,
        lit_transition_mode="none",
        signals_duration_ms=1000,
    )

    paint_frame(img, _singer_track(singer_id=1), 100, style)

    layout = _sayatoo_layout_for(_singer_track(singer_id=1), style, 100, w=120, h=80)
    assert QColor(img.pixel(int(layout.signal_x) + 2, 36)).name(QColor.NameFormat.HexRgb).upper() == "#0000FF"
    assert layout.text_x > layout.signal_x


def test_signal_volume_uses_sayatoo_default_shape_and_line_anchor(qapp):
    img = _blank(160, 90)
    style = Style(
        font_size_px=20,
        line_y_margin_px=10,
        dual_line_layout=False,
        line_lead_in_ms=0,
        lit_enabled=True,
        lit_shadow=False,
        signals_duration_ms=1000,
    )

    paint_frame(img, _singer_track(singer_id=0), 800, style)

    assert style.lit_style == "volume"
    assert style.volume_size == 48
    assert style.volume_column_width == 12
    assert style.volume_column_count == 4
    bounds = _ink_bounds(img)
    assert bounds is not None
    layout = _sayatoo_layout_for(_singer_track(singer_id=0), style, 800)
    geometry = _volume_signal_geometry(style)
    first_column = _volume_signal_column_rects(layout.signal_x, 0.0, geometry)[0]
    assert first_column.left() < float(layout.text_x)
    assert float(layout.text_x) - first_column.left() == pytest.approx(
        geometry.group_width - geometry.stroke_extent
    )
    assert layout.text_x > layout.signal_x
    assert QColor(img.pixel(int(layout.signal_x) + 6, 65)).name(QColor.NameFormat.HexRgb).upper() == "#0000FF"
    assert bounds[2] >= layout.text_x


def test_signal_volume_local_bounds_match_sayatoo_offset_origin(qapp):
    style = Style(
        lit_enabled=True,
        lit_shadow=False,
        lit_stroke_width=2,
        volume_offset_x=0,
        volume_column_count=4,
        volume_column_width=12,
        volume_column_spacing=0,
    )

    geometry = _volume_signal_geometry(style)
    metrics = _signal_layout_metrics(style)
    rects = _volume_signal_column_rects(geometry.local_left, 0.0, geometry)

    assert geometry.stroke_extent == 2.0
    assert geometry.local_left == pytest.approx(-2.0)
    assert _signal_local_x(metrics, style) == pytest.approx(-geometry.group_width)
    assert rects[0].left() == pytest.approx(0.0)
    assert rects[-1].left() == pytest.approx(48.0)
    assert geometry.local_left + geometry.group_width == pytest.approx(62.0)


def test_signal_volume_layout_does_not_jump_between_flash_and_fill(qapp):
    track = _singer_track(singer_id=0)
    style = Style(
        font_size_px=20,
        line_y_margin_px=10,
        dual_line_layout=False,
        line_lead_in_ms=0,
        lit_enabled=True,
        lit_shadow=False,
        signals_duration_ms=1000,
        volume_flash_times=1,
        volume_flash_duration_ratio=0.25,
        volume_transition_ratio_pct=0,
    )

    flash_layout = _sayatoo_layout_for(track, style, 100)
    fill_layout = _sayatoo_layout_for(track, style, 500)

    assert flash_layout.signal_x == pytest.approx(fill_layout.signal_x)
    assert flash_layout.text_x == fill_layout.text_x


def test_signal_volume_widens_line_and_shifts_text(qapp):
    track = _singer_track(singer_id=0)
    style = Style(
        font_size_px=20,
        line_y_margin_px=10,
        dual_line_layout=False,
        line_lead_in_ms=0,
        lit_enabled=True,
        lit_shadow=False,
        signals_duration_ms=1000,
    )

    layout = _sayatoo_layout_for(track, style, 800)
    geometry = _volume_signal_geometry(style)
    rects = _volume_signal_column_rects(layout.signal_x, 0.0, geometry)

    # Sayatoo aligns the union of the text box and the signal bounds, so under
    # centre alignment the lyric text is pushed right to reserve room for the
    # bars on its left (it no longer stays at the no-signal anchor).
    assert layout.text_x > _default_text_x(track, style)
    assert rects[0].left() < float(layout.text_x)
    assert float(layout.text_x) - rects[0].left() == pytest.approx(
        geometry.group_width - geometry.stroke_extent
    )

    # The union (bars' left edge .. text's right edge) stays centred on the frame.
    metrics = QFontMetrics(_build_font(style))
    text_w = sum(metrics.horizontalAdvance(c.text) for c in track.lines[0].chars)
    visual_pad = _visual_text_padding(style)
    union_mid = (layout.signal_x + (layout.text_x + text_w + visual_pad)) / 2
    assert union_mid == pytest.approx(160 / 2, abs=1.0)


def test_signal_volume_union_alignment_left_vs_right(qapp):
    track = _singer_track(singer_id=0)
    common = dict(
        font_size_px=20,
        line_y_margin_px=10,
        dual_line_layout=False,
        line_lead_in_ms=2000,  # keep the line visible at t=800 with or without bars
        lit_shadow=False,
        signals_duration_ms=1000,
        line_horizontal_layout="per_row",
    )

    # Left-aligned row (Sayatoo row1, align==0): the union's left edge sits at the
    # row offset, so the bars take the anchor and the lyric text shifts right.
    left_off = _sayatoo_layout_for(
        track, Style(**common, row1_align="left", row1_offset_x=20, lit_enabled=False), 800
    )
    left_on = _sayatoo_layout_for(
        track, Style(**common, row1_align="left", row1_offset_x=20, lit_enabled=True), 800
    )
    assert left_on.signal_x == pytest.approx(20.0)
    assert left_on.text_x > left_off.text_x

    # Right-aligned row (Sayatoo row2, align==2): the union's right edge is the
    # text's right edge, so the text stays put and the bars extend further left.
    right_off = _sayatoo_layout_for(
        track, Style(**common, row1_align="right", row1_offset_x=0, lit_enabled=False), 800
    )
    right_on = _sayatoo_layout_for(
        track, Style(**common, row1_align="right", row1_offset_x=0, lit_enabled=True), 800
    )
    assert right_on.text_x == right_off.text_x
    assert right_on.signal_x is not None and right_on.signal_x < right_on.text_x


def test_signal_volume_offset_x_moves_bars_not_text(qapp):
    track = _singer_track(singer_id=0)

    def layout_for(offset_x: int):
        style = Style(
            font_size_px=20,
            line_y_margin_px=10,
            dual_line_layout=False,
            line_lead_in_ms=0,
            lit_enabled=True,
            lit_shadow=False,
            signals_duration_ms=1000,
            line_horizontal_layout="per_row",
            row1_align="left",
            row1_offset_x=20,
            volume_offset_x=offset_x,
        )
        return _sayatoo_layout_for(track, style, 800)

    base = layout_for(0)
    shifted = layout_for(-10)
    # The X offset nudges only the bars; the lyric text layout is unchanged.
    assert shifted.text_x == base.text_x
    assert shifted.signal_x == pytest.approx(base.signal_x - 10)


def test_signal_volume_stays_visible_after_the_line_starts(qapp):
    track = _singer_track(singer_id=0)
    style = Style(
        font_size_px=20,
        line_y_margin_px=10,
        dual_line_layout=False,
        line_lead_in_ms=0,
        lit_enabled=True,
        lit_shadow=False,
        signals_duration_ms=1000,
        volume_flash_times=1,
        volume_flash_duration_ratio=0.25,
        volume_transition_ratio_pct=0,
    )

    layout = _sayatoo_layout_for(track, style, 1200)
    img = _blank(160, 90)
    paint_frame(img, track, 1200, style)

    assert layout.signal_x is not None
    assert QColor(img.pixel(int(layout.signal_x) + 6, 65)).name(QColor.NameFormat.HexRgb).upper() == "#0000FF"


def test_signal_shape_tracks_top_of_subtitle_line_box(qapp):
    track = _track_with_ruby()
    style = Style(
        font_size_px=48,
        ruby_font_size_px=16,
        line_y_margin_px=20,
        dual_line_layout=False,
        line_lead_in_ms=0,
        lit_enabled=True,
        lit_style="circle",
        lit_size=16,
        lit_offset_y=-24,
        lit_stroke_width=0,
        lit_shadow=False,
        signals_duration_ms=1000,
    )
    display_lines = [DisplayLine(track.lines[0], lane=0, display_start_ms=0, display_end_ms=2000)]
    baselines = _resolve_display_baselines(180, track, display_lines, style)
    font = _build_font(style)
    metrics = QFontMetrics(font)

    groups = _signal_lit_groups(
        track,
        display_lines,
        baselines,
        320,
        180,
        500,
        style,
        4,
        style.lit_size,
        style.lit_size,
        style.lit_tracking,
    )

    assert groups
    main_text_top = baselines[0] - metrics.ascent()
    layout = _resolve_sayatoo_line_layouts(
        320,
        180,
        track,
        display_lines,
        baselines,
        500,
        style,
    )[0]
    assert layout.signal_y == pytest.approx(groups[0].y)
    assert groups[0].y + style.lit_size <= main_text_top


def test_signal_volume_flash_off_phase_is_transparent(qapp):
    style = Style(
        font_size_px=20,
        line_y_margin_px=10,
        dual_line_layout=False,
        line_lead_in_ms=0,
        lit_enabled=True,
        lit_shadow=False,
        signals_duration_ms=1000,
        volume_flash_times=1,
        volume_flash_duration_ratio=0.25,
        volume_transition_ratio_pct=0,
    )

    assert _volume_flash_alpha(100, 200, style) == 0.0


def test_signal_volume_flash_on_phase_keeps_all_columns_visible(qapp):
    img = _blank(160, 90)
    style = Style(
        font_size_px=20,
        line_y_margin_px=10,
        dual_line_layout=False,
        line_lead_in_ms=0,
        lit_enabled=True,
        lit_shadow=False,
        signals_duration_ms=1000,
        volume_flash_times=1,
        volume_flash_duration_ratio=0.25,
        volume_transition_ratio_pct=0,
    )

    track = _singer_track(singer_id=0)
    paint_frame(img, track, 50, style)

    layout = _sayatoo_layout_for(track, style, 50)
    metrics = QFontMetrics(_build_font(style))
    base_y = _signal_lit_y(
        layout.baseline_y, metrics, style.volume_size, style,
        _signal_stroke_extent(style, is_volume=True),
    )
    rects = _volume_signal_column_rects(layout.signal_x, base_y, _volume_signal_geometry(style))
    # Flash-on phase: every column is painted (white fill), first and last alike.
    for rect in (rects[0], rects[-1]):
        cx, cy = int(rect.center().x()), int(rect.center().y())
        assert QColor(img.pixel(cx, cy)).name(QColor.NameFormat.HexRgb).upper() == "#FFFFFF"


def test_signal_shape_fade_makes_the_whole_shape_transparent(qapp):
    img = _blank(140, 90)
    style = Style(
        font_size_px=20,
        line_y_margin_px=10,
        dual_line_layout=False,
        line_lead_in_ms=0,
        lit_enabled=True,
        lit_style="circle",
        lit_number=2,
        lit_size=20,
        lit_offset_x=-20,
        lit_offset_y=0,
        lit_tracking=0,
        lit_stroke_width=4,
        lit_shadow=False,
        lit_transition_mode="fade",
        lit_transition_ratio_pct=100,
        signals_duration_ms=1000,
    )

    paint_frame(img, _singer_track(singer_id=0), 500, style)

    layout = _sayatoo_layout_for(_singer_track(singer_id=0), style, 500, w=140, h=90)
    assert QColor(img.pixel(int(layout.signal_x) + 10, 45)).name(QColor.NameFormat.HexRgb).upper() == "#0000FF"
    assert QColor(img.pixel(int(layout.signal_x) + 40, 45)).name(QColor.NameFormat.HexRgb).upper() == "#101010"
    assert QColor(img.pixel(int(layout.signal_x) + 50, 45)).name(QColor.NameFormat.HexRgb).upper() == "#101010"


def test_shape_active_lit_indices_extinguish_from_right_to_left(qapp):
    track = _singer_track(singer_id=2)
    style = Style(lit_enabled=True, lit_style="circle", signals_duration_ms=300)
    display_lines = [DisplayLine(track.lines[0], lane=0, display_start_ms=700, display_end_ms=2000)]

    assert _active_lit_indices(track, display_lines, 699, style, 3) == set()
    assert _active_lit_indices(track, display_lines, 700, style, 3) == {2}
    assert _active_lit_indices(track, display_lines, 850, style, 3) == {1}
    assert _active_lit_indices(track, display_lines, 975, style, 3) == {0}
    assert _active_lit_indices(track, display_lines, 1000, style, 3) == set()
    assert _active_lit_indices(track, display_lines, 1001, style, 3) == set()


def test_volume_active_lit_indices_flash_then_count_up_to_the_line_start(qapp):
    track = _singer_track(singer_id=2)
    style = Style(lit_enabled=True, lit_style="volume", signals_duration_ms=300)
    display_lines = [DisplayLine(track.lines[0], lane=0, display_start_ms=700, display_end_ms=2000)]

    assert _active_lit_indices(track, display_lines, 699, style, 3) == set()
    assert _active_lit_indices(track, display_lines, 700, style, 3) == set()
    assert _active_lit_indices(track, display_lines, 890, style, 3) == set()
    assert _active_lit_indices(track, display_lines, 940, style, 3) == {0}
    assert _active_lit_indices(track, display_lines, 975, style, 3) == {2}
    assert _active_lit_indices(track, display_lines, 1001, style, 3) == {2}


def test_paint_frame_applies_style_timing_offset(qapp):
    img = _blank()
    baseline = _pixel_hash(img)

    paint_frame(img, _track(), 500, Style(timing_offset_ms=1000))

    assert _pixel_hash(img) == baseline


def test_paint_frame_applies_track_meta_offset(qapp):
    img = _blank()
    baseline = _pixel_hash(img)
    track = _track()
    track.meta = TimingTrackMeta(offset_ms=1000)

    paint_frame(img, track, 500, Style())

    assert _pixel_hash(img) == baseline


def test_viewport_align_alone_does_not_change_render(qapp):
    """仅改对齐锚点（缩放 100%、旋转 0、无位移）不应改变画面。"""
    base = _blank()
    aligned = _blank()
    paint_frame(base, _track(), 1700, Style())
    paint_frame(aligned, _track(), 1700, Style(viewport_align="top_left"))
    assert _pixel_hash(base) == _pixel_hash(aligned)


def test_viewport_offset_translates_ink_bounds(qapp):
    base = _blank()
    shifted = _blank()
    style = Style(line_y_position="center")
    paint_frame(base, _track(), 1700, style)
    paint_frame(shifted, _track(), 1700, replace(style, viewport_offset_x=90, viewport_offset_y=40))

    base_bounds = _ink_bounds(base)
    shifted_bounds = _ink_bounds(shifted)
    assert shifted_bounds[:2] != base_bounds[:2]
    # 纯平移：墨迹尺寸不变，左上角整体偏移。
    assert _bounds_size(shifted_bounds) == _bounds_size(base_bounds)
    assert shifted_bounds[0] == base_bounds[0] + 90
    assert shifted_bounds[1] == base_bounds[1] + 40


def test_viewport_scale_enlarges_ink_bounds(qapp):
    base = _blank()
    scaled = _blank()
    style = Style(line_y_position="center")
    paint_frame(base, _track(), 1700, style)
    paint_frame(scaled, _track(), 1700, replace(style, viewport_scale_pct=150))

    base_w, base_h = _bounds_size(_ink_bounds(base))
    scaled_w, scaled_h = _bounds_size(_ink_bounds(scaled))
    assert scaled_w > base_w
    assert scaled_h > base_h


def test_viewport_rotation_changes_render(qapp):
    base = _blank()
    rotated = _blank()
    style = Style(line_y_position="center")
    paint_frame(base, _track(), 1700, style)
    paint_frame(rotated, _track(), 1700, replace(style, viewport_rotation_deg=30))
    assert _pixel_hash(base) != _pixel_hash(rotated)


def test_resolve_line_x_per_row_aligns_each_row(qapp):
    style = Style(
        line_horizontal_layout="per_row",
        row1_align="left",
        row1_offset_x=40,
        row2_align="right",
        row2_offset_x=-30,
    )
    # 第一行：贴左 (0) + 40
    assert _resolve_line_x(1000, 200, style, 0) == 40
    # 第二行：贴右 (1000-200=800) + (-30)
    assert _resolve_line_x(1000, 200, style, 1) == 770
    # 居中锚点
    centered = replace(style, row1_align="center", row1_offset_x=0)
    assert _resolve_line_x(1000, 200, centered, 0) == (1000 - 200) // 2


def test_per_row_offset_y_shifts_each_baseline(qapp):
    track = _two_line_track()
    display = [
        DisplayLine(track.lines[0], 0, 0, 5000),
        DisplayLine(track.lines[1], 1, 0, 5000),
    ]
    base = _resolve_display_baselines(1080, track, display, Style())
    shifted = _resolve_display_baselines(
        1080,
        track,
        display,
        Style(line_horizontal_layout="per_row", row1_offset_y=-25, row2_offset_y=40),
    )
    assert shifted[0] == base[0] - 25
    assert shifted[1] == base[1] + 40


def test_char_left_positions_rtl_reverses_order():
    assert _char_left_positions([10, 20, 30], 100, rtl=False) == [100, 110, 130]
    # rtl：首字符排最右，依次向左；总宽 60，base 100 → 区间 [100,160]
    assert _char_left_positions([10, 20, 30], 100, rtl=True) == [150, 130, 100]


def test_fill_clip_band_ltr_grows_from_left(qapp):
    segments = [
        _FillSegment(0, 100, 0, 1000),
        _FillSegment(100, 200, 1000, 2000),
    ]
    # t=500：第一字填一半 → 带 [0, 50]
    assert _fill_clip_band(segments, 500, rtl=False) == (0, 50)
    # t=1500：第一字满 + 第二字一半 → 带 [0, 150]
    assert _fill_clip_band(segments, 1500, rtl=False) == (0, 150)
    # 起唱前无带
    assert _fill_clip_band(segments, 0, rtl=False) is None


def test_fill_clip_band_rtl_grows_from_right(qapp):
    # rtl 下 segments 仍按演唱顺序，但位置反转：首字符在最右 [100,200]
    segments = [
        _FillSegment(100, 200, 0, 1000),
        _FillSegment(0, 100, 1000, 2000),
    ]
    # t=500：首字符（右侧）填一半，从右缘向左 → 带 [150, 200]
    assert _fill_clip_band(segments, 500, rtl=True) == (150, 200)
    # t=1500：首字符满 + 第二字一半 → 左缘移到 50 → 带 [50, 200]
    assert _fill_clip_band(segments, 1500, rtl=True) == (50, 200)
    assert _fill_clip_band(segments, 0, rtl=True) is None


def test_rtl_changes_render_vs_ltr(qapp):
    style = Style(line_y_position="center", line_horizontal_layout="center")
    img_ltr = _blank()
    img_rtl = _blank()
    paint_frame(img_ltr, _track(), 1700, style)
    paint_frame(img_rtl, _track(), 1700, replace(style, right_to_left=True))
    # 字序反转 → 像素不同；居中布局下整体横向 span 不变
    assert _pixel_hash(img_ltr) != _pixel_hash(img_rtl)
    ltr_l, _, ltr_r, _ = _ink_bounds(img_ltr)
    rtl_l, _, rtl_r, _ = _ink_bounds(img_rtl)
    center_ltr = (ltr_l + ltr_r) / 2
    center_rtl = (rtl_l + rtl_r) / 2
    assert abs(center_ltr - center_rtl) <= 4


def test_ruby_reading_rtl_reverse_flips_small_kana_keeps_dakuten():
    # RTL 反转按可见字形：小书き假名也独立反转（純粋=じゅんすい → いすんゅじ）
    assert "".join(reversed(_ruby_utopia_visual_units("じゅんすい"))) == "いすんゅじ"
    assert "".join(reversed(_ruby_utopia_visual_units("おも"))) == "もお"
    # 零宽浊点(゙)跟随基字、不被拆开
    assert _ruby_utopia_visual_units("が") == ["が"]
    assert "".join(reversed(_ruby_utopia_visual_units("がき"))) == "きが"


def test_rtl_ruby_render_differs_from_ltr(qapp):
    track = _track_with_timed_ruby()  # 漢字 + ruby かんじ
    style = Style(line_y_position="center", line_horizontal_layout="center")
    img_ltr = _blank()
    img_rtl = _blank()
    paint_frame(img_ltr, track, 1700, style)
    paint_frame(img_rtl, track, 1700, replace(style, right_to_left=True))
    assert _pixel_hash(img_ltr) != _pixel_hash(img_rtl)


def test_rtl_default_off_matches_plain(qapp):
    style = Style(line_y_position="center")
    img_a = _blank()
    img_b = _blank()
    paint_frame(img_a, _track(), 1700, style)
    paint_frame(img_b, _track(), 1700, replace(style, right_to_left=False))
    assert _pixel_hash(img_a) == _pixel_hash(img_b)


def test_resolve_vertical_columns_right_to_left(qapp):
    track = _two_line_track()
    display = [
        DisplayLine(track.lines[0], 0, 0, 5000),
        DisplayLine(track.lines[1], 1, 0, 5000),
    ]
    cols = _resolve_vertical_columns(1920, track, display, Style(line_gap_px=40))
    # 当前句在右列、下一句在左列
    assert cols[0] > cols[1]
    # 右列靠近右边缘
    assert cols[0] > 1920 * 0.7


def test_vertical_fill_band_grows_downward(qapp):
    cells = [(100, 200), (200, 300)]
    intervals = [(0, 1000), (1000, 2000)]
    # 起唱前无带
    assert _vertical_fill_band(cells, intervals, 0) is None
    # t=500：第一字填一半 → 扫到 150
    assert _vertical_fill_band(cells, intervals, 500) == (100, 150)
    # t=1500：第一字满 + 第二字一半 → 扫到 250
    assert _vertical_fill_band(cells, intervals, 1500) == (100, 250)


def test_layout_vertical_line_is_pure_t_independent_geometry(qapp):
    track = _track()
    style = Style(vertical=True, line_y_position="center")
    line = track.lines[0]

    layout = _layout_vertical_line(track, line, style, 320, 180, column_x=None)

    assert layout is not None
    assert layout.column_x > 0
    assert layout.y_top >= 0
    assert len(layout.cells) == len(line.chars)
    assert len(layout.intervals) == len(line.chars)
    assert not layout.text_path.isEmpty()
    again = _layout_vertical_line(track, line, style, 320, 180, column_x=None)
    assert again is not None
    assert again.column_x == layout.column_x
    assert again.cells == layout.cells
    assert again.line_rect == layout.line_rect


def test_vertical_render_is_taller_than_wide_and_differs(qapp):
    style = Style(line_y_position="center", line_horizontal_layout="center")
    img_h = _blank()
    img_v = _blank()
    paint_frame(img_h, _track(), 1700, style)
    paint_frame(img_v, _track(), 1700, replace(style, vertical=True))
    assert _pixel_hash(img_h) != _pixel_hash(img_v)
    # 竖排：墨迹纵向分布（高 > 宽）；横排相反
    hl, ht, hr, hb = _ink_bounds(img_h)
    vl, vt, vr, vb = _ink_bounds(img_v)
    assert (hr - hl) > (hb - ht)  # 横排更宽
    assert (vb - vt) > (vr - vl)  # 竖排更高


def test_vertical_orientation_classification():
    # 直立：汉字、平假/片假名、数字
    for ch in "永あアА1漢":
        assert _vertical_orientation(ch) == "U"
    # 旋转：长音、破折号、波浪、横向括号、横箭头
    for ch in "ー—〜（）「」〈〉→←":
        assert _vertical_orientation(ch) == "R"


def test_vertical_render_with_rotated_and_punct_chars(qapp):
    # 含长音/括号/标点的竖排行能正常渲染且改变画面
    line = TimingLine(
        chars=[
            TimingChar(text="ス", start_ms=1000),
            TimingChar(text="ー", start_ms=1300),
            TimingChar(text="、", start_ms=1600),
            TimingChar(text="ゃ", start_ms=1900),
        ],
        end_ms=2200,
    )
    track = TimingTrack(lines=[line])
    img = _blank()
    baseline = _pixel_hash(img)
    paint_frame(img, track, 1700, Style(vertical=True, line_y_position="center"))
    assert _pixel_hash(img) != baseline


def test_vertical_ruby_renders_to_right_of_base(qapp):
    track = _track_with_timed_ruby()  # 漢字 + ruby かんじ
    style = Style(vertical=True, line_y_position="center")
    img = _blank()
    paint_frame(img, track, 1700, style)
    cols = _resolve_vertical_columns(
        img.width(), track, [DisplayLine(track.lines[0], 0, 0, 5000)], style
    )
    base_col_x = cols[0]
    left, _, right, _ = _ink_bounds(img)
    # 注音排在基字列右侧 → 墨迹右缘超出列中心；基字本身在列中心左侧
    assert right > base_col_x
    assert left < base_col_x


def test_vertical_default_off_matches_plain(qapp):
    style = Style(line_y_position="center")
    img_a = _blank()
    img_b = _blank()
    paint_frame(img_a, _track(), 1700, style)
    paint_frame(img_b, _track(), 1700, replace(style, vertical=False))
    assert _pixel_hash(img_a) == _pixel_hash(img_b)


def test_paint_frame_during_line_modifies_image(qapp):
    img = _blank()
    baseline = _pixel_hash(img)
    paint_frame(img, _track(), 1700, Style())  # 第二字进行中
    assert _pixel_hash(img) != baseline


def test_paint_frame_progress_changes_between_timestamps(qapp):
    """同行不同时刻渲染像素应该不同（fill 比例不同）。"""
    img1 = _blank()
    img2 = _blank()
    track = _track()
    style = Style()
    paint_frame(img1, track, 1100, style)  # 第一字刚开始唱
    paint_frame(img2, track, 2400, style)  # 接近行尾，全部唱完
    assert _pixel_hash(img1) != _pixel_hash(img2)


def test_paint_frame_fill_gradient_changes_rendered_frame(qapp):
    img_solid = _blank()
    img_gradient = _blank()
    track = _track()
    solid = Style(fill_color="#FF5A6F", line_y_position="center")
    gradient = Style(
        fill_color="#FF5A6F",
        fill_gradient_enabled=True,
        fill_gradient_start_color="#FF5A6F",
        fill_gradient_end_color="#0055FF",
        fill_gradient_angle_deg=0,
        line_y_position="center",
    )

    paint_frame(img_solid, track, 2400, solid)
    paint_frame(img_gradient, track, 2400, gradient)

    assert _pixel_hash(img_solid) != _pixel_hash(img_gradient)


def test_paint_frame_gradient_stops_change_rendered_frame(qapp):
    img_two_stops = _blank()
    img_three_stops = _blank()
    track = _track()
    two_stops = PaintFill(
        mode="gradient_horizontal",
        color="#FF0000",
        start_color="#FF0000",
        end_color="#0000FF",
        gradient_stops=[(0, "#FF0000"), (100, "#0000FF")],
    )
    three_stops = replace(two_stops, gradient_stops=[(0, "#FF0000"), (50, "#00FF00"), (100, "#0000FF")])
    style_two = Style(
        karaoke_colors=KaraokeColors(after=KaraokeColorState(text=two_stops)),
        line_y_position="center",
    )
    style_three = Style(
        karaoke_colors=KaraokeColors(after=KaraokeColorState(text=three_stops)),
        line_y_position="center",
    )

    paint_frame(img_two_stops, track, 2400, style_two)
    paint_frame(img_three_stops, track, 2400, style_three)

    assert _pixel_hash(img_two_stops) != _pixel_hash(img_three_stops)


def test_paint_frame_applies_singer_style_scheme(qapp):
    img_global = _blank()
    img_singer = _blank()
    track = _track()
    track.lines[0].singer_id = 1
    style_global = Style(fill_color="#FFFFFF", line_y_position="center")
    style_singer = Style(
        fill_color="#FFFFFF",
        line_y_position="center",
        singer_style_overrides={
            1: SubtitleStyleScheme(
                font_size_px=80,
                fill_color="#00FF00",
                ruby_color="#00FF00",
                shadow_offset_x=5,
                shadow_offset_y=4,
            )
        },
    )

    paint_frame(img_global, track, 1700, style_global)
    paint_frame(img_singer, track, 1700, style_singer)

    assert _pixel_hash(img_global) != _pixel_hash(img_singer)


def test_paint_frame_applies_singer_gradient_scheme(qapp):
    img_global = _blank()
    img_singer = _blank()
    track = _track()
    track.lines[0].singer_id = 1
    style = Style(
        fill_color="#FF5A6F",
        line_y_position="center",
        singer_style_overrides={
            1: SubtitleStyleScheme(
                fill_color="#FF5A6F",
                fill_gradient_enabled=True,
                fill_gradient_start_color="#FF5A6F",
                fill_gradient_end_color="#0055FF",
                fill_gradient_angle_deg=0,
            )
        },
    )

    paint_frame(img_global, track, 2400, Style(fill_color="#FF5A6F", line_y_position="center"))
    paint_frame(img_singer, track, 2400, style)

    assert _pixel_hash(img_global) != _pixel_hash(img_singer)


def test_paint_frame_applies_inline_role_styles_with_mixed_font_sizes(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="A", start_ms=1000, role_label="1配色"),
            TimingChar(text="B", start_ms=2000, role_label="2配色"),
        ],
        end_ms=3000,
    )
    track = TimingTrack(lines=[line])
    style = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=48,
        line_y_position="center",
        stroke_width_px=0,
        stroke2_width_px=0,
        shadow_offset_x=0,
        shadow_offset_y=0,
        custom_style_schemes={
            "1配色": SubtitleStyleScheme(
                font_size_px=96,
                karaoke_colors=KaraokeColors(
                    before=KaraokeColorState(text=_solid_fill("#00FF00")),
                    after=KaraokeColorState(text=_solid_fill("#FF0000")),
                ),
            ),
            "2配色": SubtitleStyleScheme(
                font_size_px=48,
                karaoke_colors=KaraokeColors(
                    before=KaraokeColorState(text=_solid_fill("#0000FF")),
                    after=KaraokeColorState(text=_solid_fill("#FFFF00")),
                ),
            ),
        },
    )

    before = _blank(420, 220)
    paint_frame(before, track, 500, style)
    green_bounds = _dominant_bounds(before, channel="green")
    blue_bounds = _dominant_bounds(before, channel="blue", left=green_bounds[2] + 1)

    assert _bounds_size(green_bounds)[1] > _bounds_size(blue_bounds)[1] + 10
    assert _bounds_size(green_bounds)[0] > 10
    assert _bounds_size(blue_bounds)[0] > 10

    during = _blank(420, 220)
    paint_frame(during, track, 1750, style)
    red_bounds = _dominant_bounds(during, channel="red")
    blue_during_bounds = _dominant_bounds(during, channel="blue", left=red_bounds[2] + 1)

    assert _bounds_size(red_bounds)[0] > 10
    assert _bounds_size(blue_during_bounds)[0] > 10


def test_inline_role_line_uses_character_transition_path(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="A", start_ms=1000, role_label="lead"),
            TimingChar(text="B", start_ms=2000, role_label="back"),
        ],
        end_ms=3000,
    )
    track = TimingTrack(lines=[line])
    base = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=72,
        line_y_position="center",
        stroke_width_px=0,
        shadow_offset_x=0,
        shadow_offset_y=0,
        custom_style_schemes={
            "lead": SubtitleStyleScheme(karaoke_colors=KaraokeColors(after=KaraokeColorState(text=_solid_fill("#FF0000")))),
            "back": SubtitleStyleScheme(karaoke_colors=KaraokeColors(after=KaraokeColorState(text=_solid_fill("#00FF00")))),
        },
    )
    static = _blank(360, 180)
    animated = _blank(360, 180)

    paint_frame(static, track, 200, base)
    paint_frame(animated, track, 200, replace(base, entry_anim="char_fade", entry_lead_ms=1000))

    assert _pixel_hash(static) != _pixel_hash(animated)


def test_inline_role_utopia_exit_handles_multi_kanji_ruby_group(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="A", start_ms=1000, role_label="lead"),
            TimingChar(text="B", start_ms=1500, role_label="back"),
            TimingChar(text="C", start_ms=2000, role_label="back"),
        ],
        end_ms=2500,
    )
    track = TimingTrack(
        lines=[line],
        rubies=[
            RubyAnnotation(
                kanji="AB",
                reading="ab",
                reading_part_ms=[300],
                pos_start_ms=1000,
                pos_end_ms=2000,
            )
        ],
    )
    base = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=72,
        line_y_position="center",
        line_tail_ms=1000,
        exit_fade_ms=1000,
        stroke_width_px=0,
        shadow_offset_x=0,
        shadow_offset_y=0,
        custom_style_schemes={
            "lead": SubtitleStyleScheme(karaoke_colors=KaraokeColors(after=KaraokeColorState(text=_solid_fill("#FF0000")))),
            "back": SubtitleStyleScheme(karaoke_colors=KaraokeColors(after=KaraokeColorState(text=_solid_fill("#00FF00")))),
        },
    )
    char_fade = _blank(420, 220)
    utopia = _blank(420, 220)

    paint_frame(char_fade, track, 2300, replace(base, exit_anim="char_fade"))
    paint_frame(utopia, track, 2300, replace(base, exit_anim="utopia"))

    assert _pixel_hash(char_fade) != _pixel_hash(utopia)


def _solid_color_pixel_count(img: QImage, *, r: int, g: int, b: int) -> int:
    """统计接近指定纯色（且不透明）的像素数，用于区分 before/after 着色层。"""
    rgba = img.convertToFormat(QImage.Format.Format_RGBA8888)
    bits = rgba.constBits()
    bits.setsize(rgba.sizeInBytes())
    arr = np.frombuffer(bytes(bits), dtype=np.uint8).reshape(rgba.height(), rgba.width(), 4)
    mask = (
        (np.abs(arr[:, :, 0].astype(int) - r) < 50)
        & (np.abs(arr[:, :, 1].astype(int) - g) < 50)
        & (np.abs(arr[:, :, 2].astype(int) - b) < 50)
        & (arr[:, :, 3] > 180)
    )
    return int(np.count_nonzero(mask))


def test_utopia_exit_keeps_full_fill_when_ruby_progress_lags(qapp):
    """退场阶段整词应作为「已唱」整体淡出：不得因卡拉ok扫光 ratio<1 把部分着色裁掉。

    复现 bug：ruby 读音时长比正文字符区间长，使退场起点处 _ruby_progress_ratio<1.0；
    修复前 _paint_char_karaoke_stack 会对已被退场变换旋转的字形按设备空间水平带裁切
    「已唱(after)层」，露出 before 底色 → 着色被褪掉一部分。修复后退场强制 ratio=1.0。
    """
    line = TimingLine(
        chars=[
            TimingChar(text="A", start_ms=1000),
            TimingChar(text="B", start_ms=1500),
        ],
        end_ms=2000,
    )
    track = TimingTrack(
        lines=[line],
        rubies=[
            RubyAnnotation(
                kanji="AB",
                reading="ab",
                # 读音区间远长于正文（pos_end 远在未来）→ 退场起点处 ruby 进度仍 <1.0
                pos_start_ms=1000,
                pos_end_ms=6000,
            )
        ],
    )
    style = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=96,
        line_y_position="center",
        line_tail_ms=2000,  # tail_delay=2000-750=1250 → group_done=2000+1250=3250
        exit_anim="utopia",
        stroke_width_px=0,
        stroke2_width_px=0,
        shadow_offset_x=0,
        shadow_offset_y=0,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(text=_solid_fill("#0000FF")),  # 未唱=蓝
            after=KaraokeColorState(text=_solid_fill("#FF0000")),  # 已唱=红
        ),
    )

    # t=3300 处于退场窗口 (group_done=3250, display_end=line_end+tail=4000)，
    # 此时 ruby 进度 = (3300-1000)/(6000-1000) ≈ 0.46（修复前会触发水平裁切）。
    img = _blank(520, 260)
    paint_frame(img, track, 3300, style)

    red = _solid_color_pixel_count(img, r=255, g=0, b=0)
    blue = _solid_color_pixel_count(img, r=0, g=0, b=255)
    assert red > 0, "退场词应当被渲染（已唱红色）"
    assert blue == 0, f"退场词不应残留未唱(蓝)底色，却有 {blue} 像素被裁出 before 层"


def test_static_wipe_segments_use_ink_bounds_not_advance(qapp):
    """走字（扫光）按字形墨水包围盒推进，而非 advance 框。

    advance 含两侧 side bearing 与字间空隙，纯按 advance 走会让扫光锋面与字形墨水
    错位（字头偏慢、字尾悬空）。与 SUG karaoke_preview.py 的 _ink_bounds 同口径。
    回退到 advance 会使 fill_segment 等于 advance 框 → 本测试失败。
    """
    import math  # noqa: PLC0415

    from PyQt6.QtGui import QPainterPath  # noqa: E402,PLC0415

    line = TimingLine(
        chars=[
            TimingChar(text="W", start_ms=1000),
            TimingChar(text="A", start_ms=1500),
        ],
        end_ms=2000,
    )
    track = TimingTrack(lines=[line])
    style = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=96,
        line_y_position="center",
        letter_spacing_px=40,  # 显式字间距 → advance/排版框明显宽于墨水
        stroke_width_px=0,
        stroke2_width_px=0,
        shadow_offset_x=0,
        shadow_offset_y=0,
    )
    layout = _layout_line(track, line, style, 600, 240)
    assert layout is not None
    assert len(layout.fill_segments) == len(line.chars)

    font = _build_font(style)
    any_strictly_narrower = False
    for idx, ch in enumerate(line.chars):
        seg = layout.fill_segments[idx]
        adv_left, adv_right = layout.char_x_ranges[idx]
        # 墨水段必须落在 advance 框内
        assert adv_left <= seg.left <= seg.right <= adv_right
        # 且与该字形的矢量墨水包围盒（与 fillPath 同源）一致
        path = QPainterPath()
        path.addText(float(adv_left), 0.0, font, ch.text)
        br = path.boundingRect()
        assert seg.left == int(math.floor(br.left()))
        assert seg.right == int(math.ceil(br.right()))
        if (seg.left, seg.right) != (adv_left, adv_right):
            any_strictly_narrower = True
    # 至少一个字形墨水严格窄于 advance 框 → 证明确实按墨水而非 advance 走字
    assert any_strictly_narrower


def test_character_fill_ratio_honors_ink_ranges(qapp):
    """transition（utopia 等）路径的逐字走字 ratio 也按墨水边界推进。

    _character_fill_ratio 的 ruby 分支用传入的 x 范围把 ruby 进度映射成本字 ratio。
    现在 transition 路径传入墨水边界（而非 advance 框），故同一时刻、同一 ruby 进度下
    墨水与 advance 给出的 ratio 不同——本测试锁定这一差异，防止 transition 路径回退。
    """
    from krok_helper.subtitle_render.engine.timeline import (  # noqa: E402,PLC0415
        compute_char_intervals,
    )

    line = TimingLine(
        chars=[TimingChar(text="W", start_ms=1000), TimingChar(text="A", start_ms=1500)],
        end_ms=2000,
    )
    rubies = [
        RubyAnnotation(kanji="WA", reading="わ", pos_start_ms=1000, pos_end_ms=2000)
    ]
    intervals = compute_char_intervals(line)
    # 合成范围：advance 框相邻无空隙，墨水框两侧各留 bearing。
    advance_ranges = [(0, 100), (100, 200)]
    ink_ranges = [(15, 85), (115, 185)]

    t = 1200  # ruby 进度 ≈ 0.2 → 首字 W 处于部分填充
    r_adv = _character_fill_ratio(line, intervals, advance_ranges, rubies, 0, t)
    r_ink = _character_fill_ratio(line, intervals, ink_ranges, rubies, 0, t)
    assert 0.0 < r_adv < 1.0
    assert 0.0 < r_ink < 1.0
    assert r_adv != r_ink


def test_paint_frame_glow_decoration_changes_rendered_frame(qapp):
    img_plain = _blank()
    img_glow = _blank()
    orange = PaintFill(
        mode="solid",
        color="#FF8A00",
        start_color="#FF8A00",
        end_color="#FF8A00",
        split_top_color="#FF8A00",
        split_bottom_color="#FF8A00",
    )
    colors = KaraokeColors(
        before=KaraokeColorState(
            text=PaintFill(color="#FFFFFF"),
            stroke=PaintFill(color="#222222"),
            shadow=orange,
        ),
        after=KaraokeColorState(
            text=PaintFill(color="#FFFFFF"),
            stroke=PaintFill(color="#222222"),
            shadow=orange,
        ),
    )
    plain = Style(
        fill_color="#FFFFFF",
        base_color="#FFFFFF",
        stroke_color="#222222",
        shadow_color="",
        line_y_position="center",
    )
    glow = Style(
        fill_color="#FFFFFF",
        base_color="#FFFFFF",
        stroke_color="#222222",
        decoration_kind="glow",
        karaoke_colors=colors,
        line_y_position="center",
    )

    paint_frame(img_plain, _track(), 2400, plain)
    paint_frame(img_glow, _track(), 2400, glow)

    assert _pixel_hash(img_plain) != _pixel_hash(img_glow)


def test_paint_frame_glow_radius_changes_rendered_frame(qapp):
    img_small = _blank()
    img_large = _blank()
    orange = PaintFill(
        mode="solid",
        color="#FF8A00",
        start_color="#FF8A00",
        end_color="#FF8A00",
        split_top_color="#FF8A00",
        split_bottom_color="#FF8A00",
    )
    colors = KaraokeColors(
        before=KaraokeColorState(shadow=orange),
        after=KaraokeColorState(shadow=orange),
    )
    small = Style(
        decoration_kind="glow",
        glow_radius_px=4,
        karaoke_colors=colors,
        line_y_position="center",
    )
    large = Style(
        decoration_kind="glow",
        glow_radius_px=28,
        karaoke_colors=colors,
        line_y_position="center",
    )

    paint_frame(img_small, _track(), 2400, small)
    paint_frame(img_large, _track(), 2400, large)

    assert _pixel_hash(img_small) != _pixel_hash(img_large)


def test_paint_frame_default_dual_line_layout_renders_next_line(qapp):
    img_single = _blank()
    img_dual = _blank()
    style_single = Style(dual_line_layout=False)
    style_dual = Style()
    track = _two_line_track()

    paint_frame(img_single, track, 1500, style_single)
    paint_frame(img_dual, track, 1500, style_dual)

    assert _pixel_hash(img_single) != _pixel_hash(img_dual)


def test_dual_line_baselines_stay_fixed_when_lower_line_disappears(qapp):
    track = _two_line_track()
    style = Style()
    upper = DisplayLine(
        line=track.lines[0],
        lane=0,
        display_start_ms=0,
        display_end_ms=1000,
    )
    lower = DisplayLine(
        line=track.lines[1],
        lane=1,
        display_start_ms=0,
        display_end_ms=1000,
    )

    both = _resolve_display_baselines(720, track, [upper, lower], style)
    upper_only = _resolve_display_baselines(720, track, [upper], style)

    assert upper_only[0] == both[0]


def test_dual_line_gap_uses_main_text_bounds_not_ruby_block(qapp):
    track = _two_line_track()
    style = Style(font_size_px=100, ruby_font_size_px=35, ruby_gap_px=24, line_gap_px=90)
    upper = DisplayLine(
        line=track.lines[0],
        lane=0,
        display_start_ms=0,
        display_end_ms=1000,
    )
    lower = DisplayLine(
        line=track.lines[1],
        lane=1,
        display_start_ms=0,
        display_end_ms=1000,
    )

    baselines = _resolve_display_baselines(1080, track, [upper, lower], style)
    metrics = QFontMetrics(_build_font(style))
    visual_pad = _visual_text_padding(style)
    upper_main_bottom = baselines[0] + metrics.descent() + visual_pad
    lower_main_top = baselines[1] - metrics.ascent() - visual_pad

    assert lower_main_top - upper_main_bottom == style.line_gap_px


def test_glow_does_not_expand_dual_line_gap(qapp):
    track = _two_line_track()
    plain = Style(font_size_px=100, ruby_font_size_px=35, ruby_gap_px=24, line_gap_px=90)
    glow = replace(
        plain,
        decoration_kind="glow",
        glow_before_radius_px=28,
        glow_after_radius_px=36,
    )
    upper = DisplayLine(track.lines[0], lane=0, display_start_ms=0, display_end_ms=1000)
    lower = DisplayLine(track.lines[1], lane=1, display_start_ms=0, display_end_ms=1000)

    assert _resolve_display_baselines(1080, track, [upper, lower], glow) == _resolve_display_baselines(
        1080, track, [upper, lower], plain
    )


def test_double_stroke_width_expands_visual_bounds(qapp):
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="A", start_ms=0)],
                end_ms=1000,
            )
        ]
    )
    plain = _blank()
    stroked = _blank()

    paint_frame(
        plain,
        track,
        500,
        Style(
            font_size_px=110,
            line_y_position="center",
            stroke_width_px=0,
            stroke2_width_px=0,
        ),
    )
    paint_frame(
        stroked,
        track,
        500,
        Style(
            font_size_px=110,
            line_y_position="center",
            stroke_width_px=18,
            stroke2_width_px=30,
        ),
    )

    plain_w, plain_h = _bounds_size(_ink_bounds(plain))
    stroked_w, stroked_h = _bounds_size(_ink_bounds(stroked))

    assert stroked_w - plain_w >= 45
    assert stroked_h - plain_h >= 45


def test_after_stroke_clip_does_not_bleed_past_scanline(qapp):
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="A", start_ms=0)],
                end_ms=1000,
            )
        ]
    )
    after = KaraokeColorState(
        text=PaintFill(color="#FF0000"),
        stroke=PaintFill(color="#0055FF"),
        stroke2=PaintFill(color="#00FF00"),
        shadow=PaintFill(color="#000000"),
    )
    before = KaraokeColorState(
        text=PaintFill(color="#202020"),
        stroke=PaintFill(color="#202020"),
        stroke2=PaintFill(color="#202020"),
        shadow=PaintFill(color="#000000"),
    )
    style = Style(
        font_size_px=110,
        line_y_position="center",
        stroke_width_px=18,
        stroke2_width_px=30,
        karaoke_colors=KaraokeColors(before=before, after=after),
    )
    img = _blank()

    paint_frame(img, track, 500, style)

    metrics = QFontMetrics(_build_font(style))
    char_w = metrics.horizontalAdvance("A")
    visual_pad = _visual_text_padding(style)
    x0 = _resolve_line_x(img.width(), char_w + visual_pad * 2, style, None) + visual_pad
    scan_x = x0 + char_w // 2
    bounds = _ink_bounds(img)
    _left, top, _right, bottom = bounds
    for y in range(top, bottom + 1):
        for x in range(scan_x + 2, min(scan_x + 28, img.width())):
            color = QColor(img.pixel(x, y))
            has_after_blue = color.blue() > 180 and color.red() < 80
            has_after_green = color.green() > 180 and color.red() < 80
            assert not (has_after_blue or has_after_green)


def test_dual_line_x_positions_use_asymmetric_margins(qapp):
    style = Style()

    assert _resolve_line_x(1920, 600, style, 0) == 50
    assert _resolve_line_x(1920, 600, style, 1) == 1270


def test_dual_line_x_positions_can_be_centered(qapp):
    style = Style(line_horizontal_layout="center")

    assert _resolve_line_x(1920, 600, style, 0) == 660
    assert _resolve_line_x(1920, 600, style, 1) == 660


def test_paint_frame_ruby_changes_rendered_frame(qapp):
    img_plain = _blank()
    img_ruby = _blank()
    style = Style(
        font_size_px=64,
        ruby_font_size_px=30,
        ruby_color="#00FF88",
        line_y_position="center",
    )

    plain_track = TimingTrack(lines=[_track_with_ruby().lines[0]])
    paint_frame(img_plain, plain_track, 1500, style)
    paint_frame(img_ruby, _track_with_ruby(), 1500, style)

    assert _pixel_hash(img_ruby) != _pixel_hash(img_plain)


def test_layout_rubies_is_pure_t_independent_geometry(qapp):
    track = _track_with_ruby()
    line = track.lines[0]
    style = Style(font_size_px=64, ruby_font_size_px=30, line_y_position="center")
    ruby_font = _build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font)
    main_metrics = QFontMetrics(_build_font(style))
    intervals = [
        (
            ch.start_ms,
            line.chars[index + 1].start_ms
            if index + 1 < len(line.chars)
            else line.end_ms,
        )
        for index, ch in enumerate(line.chars)
    ]
    widths = [main_metrics.horizontalAdvance(ch.text) for ch in line.chars]
    lefts = _char_left_positions(widths, 100, False)
    ranges = [(left, left + width) for left, width in zip(lefts, widths)]

    layout = _layout_rubies(
        ruby_metrics,
        line,
        intervals,
        ranges,
        300,
        track.rubies,
        style,
    )
    again = _layout_rubies(
        ruby_metrics,
        line,
        intervals,
        ranges,
        300,
        track.rubies,
        style,
    )

    assert layout
    assert layout == again
    assert layout[0].target_width > 0
    assert layout[0].reading_width > 0


def test_ruby_text_layer_static_key_ignores_timing_progress(qapp):
    track = _track_with_ruby()
    line = track.lines[0]
    style = Style(font_size_px=64, ruby_font_size_px=30, line_y_position="center")
    ruby_font = _build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font)
    main_metrics = QFontMetrics(_build_font(style))
    intervals = [
        (
            ch.start_ms,
            line.chars[index + 1].start_ms
            if index + 1 < len(line.chars)
            else line.end_ms,
        )
        for index, ch in enumerate(line.chars)
    ]
    widths = [main_metrics.horizontalAdvance(ch.text) for ch in line.chars]
    lefts = _char_left_positions(widths, 100, False)
    ranges = [(left, left + width) for left, width in zip(lefts, widths)]
    ruby_layout = _layout_rubies(
        ruby_metrics,
        line,
        intervals,
        ranges,
        300,
        track.rubies,
        style,
    )[0]
    ctx = LayerContext(t_ms=1250, logical_w=0, logical_h=0)

    before_early = _RubyTextLayer(
        ruby_layout, ruby_font, ruby_metrics, 1250, style, False, after=False
    )
    before_late = _RubyTextLayer(
        ruby_layout, ruby_font, ruby_metrics, 1750, style, False, after=False
    )
    after_early = _RubyTextLayer(
        ruby_layout, ruby_font, ruby_metrics, 1250, style, False, after=True
    )
    after_late = _RubyTextLayer(
        ruby_layout, ruby_font, ruby_metrics, 1750, style, False, after=True
    )

    assert before_early.static_key(ctx, before_early) == before_late.static_key(
        ctx, before_late
    )
    assert after_early.static_key(ctx, after_early) == after_late.static_key(
        ctx, after_late
    )


def test_ruby_layer_stack_builds_from_line_layout(qapp):
    track = _track_with_ruby()
    line = track.lines[0]
    style = Style(font_size_px=64, ruby_font_size_px=30, line_y_position="center")
    layout = _layout_line(track, line, style, 640, 360)

    layers = _ruby_layer_stack(layout, line, 1500, style)

    assert len(layers) == 2


def test_paint_frame_ruby_k_timing_changes_between_timestamps(qapp):
    img1 = _blank()
    img2 = _blank()
    style = Style(base_color="#FFFFFF", fill_color="#FFFFFF", line_y_position="center")
    track = _track_with_timed_ruby()

    paint_frame(img1, track, 1250, style)
    paint_frame(img2, track, 2250, style)

    assert _pixel_hash(img1) != _pixel_hash(img2)


def test_paint_frame_ruby_without_k_timing_wipes_over_span(qapp):
    img1 = _blank()
    img2 = _blank()
    track = _track_with_ruby()
    style = Style(
        font_size_px=64,
        base_color="#FFFFFF",
        fill_color="#FFFFFF",
        stroke_color="",
        shadow_color="",
        ruby_font_size_px=30,
        ruby_color="#00FF88",
        line_y_position="center",
    )

    paint_frame(img1, track, 1000, style)
    paint_frame(img2, track, 1500, style)

    assert _pixel_hash(img1) != _pixel_hash(img2)


def test_ruby_timing_drives_main_text_fill_extent(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="A", start_ms=1000),
            TimingChar(text="B", start_ms=2000),
        ],
        end_ms=3000,
    )
    ruby = RubyAnnotation(
        kanji="B",
        reading="abc",
        reading_part_ms=[100, 900],
        pos_start_ms=2000,
        pos_end_ms=3000,
    )
    intervals = [(1000, 2000), (2000, 3000)]
    char_x_ranges = [(0, 100), (100, 200)]

    segments = _karaoke_fill_segments(
        [100, 100],
        intervals,
        char_x_ranges,
        [ruby],
        line,
    )

    assert _fill_extent_end(segments, 2400) == 146


def test_ruby_with_unmatched_kanji_does_not_group_timed_characters(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="A", start_ms=1000),
            TimingChar(text="B", start_ms=2000),
        ],
        end_ms=3000,
    )
    unrelated_ruby = RubyAnnotation(
        kanji="Z",
        reading="zed",
        reading_part_ms=[100, 900],
        pos_start_ms=0,
        pos_end_ms=4000,
    )
    intervals = [(1000, 2000), (2000, 3000)]
    char_x_ranges = [(0, 100), (100, 200)]

    assert _ruby_target_indices(unrelated_ruby, line, intervals) == []
    assert _ruby_target_x_range(unrelated_ruby, line, intervals, char_x_ranges) is None

    segments = _karaoke_fill_segments(
        [100, 100],
        intervals,
        char_x_ranges,
        [unrelated_ruby],
        line,
    )

    assert [segment.ruby for segment in segments] == [None, None]
    assert _fill_extent_end(segments, 1500) == 50


def test_global_ruby_uses_text_match_on_current_line(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="哀", start_ms=1000),
            TimingChar(text="し", start_ms=2000),
        ],
        end_ms=3000,
    )
    track = TimingTrack(
        lines=[line],
        rubies=[
            RubyAnnotation(kanji="哀", reading="かな", reading_part_ms=[290]),
            RubyAnnotation(kanji="夢", reading="ゆめ", reading_part_ms=[330]),
        ],
    )
    intervals = [(1000, 2000), (2000, 3000)]
    active = _active_rubies_for_line(track.rubies, line)

    assert active == track.rubies
    assert _ruby_target_indices(track.rubies[0], line, intervals) == [0]
    assert _ruby_target_indices(track.rubies[1], line, intervals) == []

    segments = _karaoke_fill_segments(
        [100, 100],
        intervals,
        [(0, 100), (100, 200)],
        active,
        line,
    )

    assert segments[0].ruby is not None
    assert segments[0].ruby.kanji == "哀"
    assert segments[0].ruby.reading_part_ms == [290]
    assert segments[1].ruby is None


def test_open_start_ruby_rebases_to_single_target_without_scaling_mora(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="夢", start_ms=18_790),
            TimingChar(text="を", start_ms=19_610),
        ],
        end_ms=20_090,
    )
    intervals = [(18_790, 19_610), (19_610, 20_090)]
    ruby = RubyAnnotation(
        kanji="夢",
        reading="ゆめ",
        reading_part_ms=[330],
        pos_start_ms=0,
        pos_end_ms=114_130,
    )

    effective = _effective_ruby_for_target(ruby, _ruby_target_indices(ruby, line, intervals), intervals)

    assert effective.pos_start_ms == 18_790
    assert effective.pos_end_ms == 19_610
    assert effective.reading_part_ms == [330]
    assert _ruby_reading_intervals(effective) == [(18_790, 19_120), (19_120, 19_610)]


def test_open_start_multi_kanji_ruby_rebases_to_text_group(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="彷", start_ms=62_880),
            TimingChar(text="徨", start_ms=63_320),
            TimingChar(text="い", start_ms=63_760),
        ],
        end_ms=64_200,
    )
    intervals = [(62_880, 63_320), (63_320, 63_760), (63_760, 64_200)]
    ruby = RubyAnnotation(
        kanji="彷徨",
        reading="さまよ",
        reading_part_ms=[130, 430],
        pos_start_ms=0,
        pos_end_ms=263_970,
    )

    effective = _effective_ruby_for_target(ruby, _ruby_target_indices(ruby, line, intervals), intervals)

    assert effective.pos_start_ms == 62_880
    assert effective.pos_end_ms == 63_760
    assert effective.reading_part_ms == [130, 430]
    assert _ruby_reading_intervals(effective) == [
        (62_880, 63_010),
        (63_010, 63_310),
        (63_310, 63_760),
    ]


def test_ruby_timing_maps_to_main_text_group_scanline(qapp):
    line = TimingLine(
        chars=[TimingChar(text="星", start_ms=166_160)],
        end_ms=169_580,
    )
    ruby = RubyAnnotation(
        kanji="星",
        reading="ほし",
        reading_part_ms=[360],
        pos_start_ms=166_160,
        pos_end_ms=169_580,
    )
    segments = _karaoke_fill_segments(
        [100],
        [(166_160, 169_580)],
        [(0, 100)],
        [ruby],
        line,
    )

    assert _fill_extent_end(segments, 166_530) == 50


def test_utopia_main_text_uses_ruby_k_timing_for_scanline(qapp):
    line = TimingLine(
        chars=[TimingChar(text="星", start_ms=166_160)],
        end_ms=169_580,
    )
    intervals = [(166_160, 169_580)]
    ruby = RubyAnnotation(
        kanji="星",
        reading="ほし",
        reading_part_ms=[360],
        pos_start_ms=166_160,
        pos_end_ms=169_580,
    )

    assert _character_fill_ratio(line, intervals, [(0, 100)], [ruby], 0, 166_530) == pytest.approx(
        0.5,
        abs=0.01,
    )


def test_utopia_ruby_group_scanline_spans_multiple_main_characters(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="明", start_ms=171_550),
            TimingChar(text="日", start_ms=171_995),
        ],
        end_ms=172_440,
    )
    intervals = [(171_550, 171_995), (171_995, 172_440)]
    ranges = [(0, 100), (100, 200)]
    ruby = RubyAnnotation(
        kanji="明日",
        reading="あした",
        reading_part_ms=[160, 500],
        pos_start_ms=171_550,
        pos_end_ms=172_440,
    )

    assert _character_fill_ratio(line, intervals, ranges, [ruby], 0, 171_810) == pytest.approx(
        0.86,
        abs=0.02,
    )
    assert _character_fill_ratio(line, intervals, ranges, [ruby], 1, 171_810) == pytest.approx(
        0.0,
        abs=0.01,
    )
    assert _character_fill_ratio(line, intervals, ranges, [ruby], 0, 172_100) == pytest.approx(
        1.0,
        abs=0.01,
    )
    assert _character_fill_ratio(line, intervals, ranges, [ruby], 1, 172_100) == pytest.approx(
        0.42,
        abs=0.02,
    )


def test_ruby_target_x_range_uses_kanji_subspan_inside_timed_unit(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="寄", start_ms=35_890),
            TimingChar(text="り", start_ms=36_100),
            TimingChar(text="添", start_ms=36_310),
            TimingChar(text="っ", start_ms=36_485),
            TimingChar(text="て", start_ms=36_660),
        ],
        end_ms=36_850,
    )
    intervals = [(35_890, 36_100), (36_100, 36_310), (36_310, 36_485), (36_485, 36_660), (36_660, 36_850)]
    ranges = [(0, 100), (100, 200), (200, 300), (300, 400), (400, 500)]
    ruby = RubyAnnotation(
        kanji="添",
        reading="そ",
        pos_start_ms=36_310,
        pos_end_ms=36_660,
    )

    assert _ruby_target_x_range(ruby, line, intervals, ranges) == (200, 300)
    assert _ruby_target_indices(ruby, line, intervals) == [2]
    assert _utopia_main_group_for_index([ruby], line, intervals, 2) is None


def test_single_kanji_ruby_does_not_slow_following_small_tsu(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="添", start_ms=36_310),
            TimingChar(text="っ", start_ms=36_485),
            TimingChar(text="て", start_ms=36_660),
        ],
        end_ms=36_850,
    )
    intervals = [(36_310, 36_485), (36_485, 36_660), (36_660, 36_850)]
    ranges = [(0, 100), (100, 200), (200, 300)]
    ruby = RubyAnnotation(
        kanji="添",
        reading="そ",
        pos_start_ms=36_310,
        pos_end_ms=36_660,
    )

    segments = _karaoke_fill_segments([100, 100, 100], intervals, ranges, [ruby], line)

    assert _character_fill_ratio(line, intervals, ranges, [ruby], 0, 36_570) == 1.0
    assert 100 < _fill_extent_end(segments, 36_570) < 200


def test_utopia_groups_main_characters_that_share_one_ruby(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="躊", start_ms=103_250),
            TimingChar(text="躇", start_ms=103_460),
            TimingChar(text="う", start_ms=103_600),
        ],
        end_ms=103_780,
    )
    intervals = [(103_250, 103_460), (103_460, 103_600), (103_600, 103_780)]
    ruby = RubyAnnotation(
        kanji="躊躇",
        reading="ためら",
        reading_part_ms=[100, 210],
        pos_start_ms=103_250,
        pos_end_ms=103_600,
    )

    group = _utopia_main_group_for_index([ruby], line, intervals, 0)
    assert group is not None
    assert group[0] == [0, 1]
    assert _utopia_main_group_for_index([ruby], line, intervals, 1) == group
    assert _utopia_main_group_for_index([ruby], line, intervals, 2) is None


def test_utopia_scope_layers_group_shared_ruby_main_text(qapp):
    track = _track_with_ruby()
    line = track.lines[0]
    style = Style(font_size_px=48, line_y_position="center", exit_anim="utopia")
    layout = _layout_line(track, line, style, 420, 220)
    assert layout is not None
    transition = _LineCharTransition(
        phase="utopia",
        effect="utopia",
        progress=1.0,
        start_ms=1000,
        end_ms=2500,
    )

    layers = _utopia_transition_scope_layers(layout, line, style, 1750, transition, 220)
    boxes = LayerCompositor().scope_boxes(
        LayerContext(t_ms=1750, logical_w=420, logical_h=220),
        layers,
    )
    main_boxes = [
        box
        for box in boxes
        if box.scope == SCOPE_GROUP
        and box.scope_id is not None
        and box.scope_id[1] == "main"
        and box.scope_id[4] == (0, 1)
    ]

    assert len(main_boxes) == 1
    assert main_boxes[0].layer_count == 2
    assert main_boxes[0].rect.height() > 0


def test_utopia_scope_ids_split_same_phrase_across_lines(qapp):
    chars = [
        TimingChar(text="A", start_ms=1000),
        TimingChar(text="B", start_ms=1500),
    ]
    line1 = TimingLine(chars=chars, end_ms=2000)
    line2 = TimingLine(
        chars=[
            TimingChar(text="A", start_ms=3000),
            TimingChar(text="B", start_ms=3500),
        ],
        end_ms=4000,
    )
    track = TimingTrack(
        lines=[line1, line2],
        rubies=[
            RubyAnnotation(
                kanji="AB",
                reading="ab",
                pos_start_ms=1000,
                pos_end_ms=2000,
            ),
            RubyAnnotation(
                kanji="AB",
                reading="ab",
                pos_start_ms=3000,
                pos_end_ms=4000,
            ),
        ],
    )
    style = Style(font_size_px=48, line_y_position="center", exit_anim="utopia")
    transition1 = _LineCharTransition(
        phase="utopia",
        effect="utopia",
        progress=1.0,
        start_ms=1000,
        end_ms=2500,
    )
    transition2 = _LineCharTransition(
        phase="utopia",
        effect="utopia",
        progress=1.0,
        start_ms=3000,
        end_ms=4500,
    )
    layout1 = _layout_line(track, line1, style, 420, 260, baseline_y=100, lane=0)
    layout2 = _layout_line(track, line2, style, 420, 260, baseline_y=180, lane=1)
    assert layout1 is not None
    assert layout2 is not None

    layers = [
        *_utopia_transition_scope_layers(layout1, line1, style, 1750, transition1, 260),
        *_utopia_transition_scope_layers(layout2, line2, style, 3750, transition2, 260),
    ]
    boxes = LayerCompositor().scope_boxes(
        LayerContext(t_ms=3750, logical_w=420, logical_h=260),
        layers,
    )
    main_scope_ids = {
        box.scope_id
        for box in boxes
        if box.scope == SCOPE_GROUP
        and box.scope_id is not None
        and box.scope_id[1] == "main"
        and box.scope_id[4] == (0, 1)
    }

    assert len(main_scope_ids) == 2
    assert {scope_id[2] for scope_id in main_scope_ids} == {1000, 3000}


def test_frame_vertical_bounds_covers_utopia_transition_pixels(qapp):
    track = _track_with_ruby()
    style = Style(font_size_px=48, line_y_position="center", exit_anim="utopia")
    t_ms = 1750

    bounds = frame_vertical_bounds(420, 220, track, t_ms, style)
    assert bounds is not None
    image = _blank(420, 220)
    paint_frame(image, track, t_ms, style)
    _left, top, _right, bottom = _ink_bounds(image)

    assert bounds[0] <= top
    assert bounds[1] >= bottom


def test_ruby_layout_spreads_reading_units_across_wide_target(qapp):
    metrics = QFontMetrics(_build_ruby_font(Style(ruby_font_size_px=36)))
    natural_positions = _ruby_layout_units(["か", "な", "た"], metrics, 100, None)
    spread_positions = _ruby_layout_units(["か", "な", "た"], metrics, 100, 180)

    natural_gap = natural_positions[1][1] - natural_positions[0][1]
    spread_gap = spread_positions[1][1] - spread_positions[0][1]

    assert spread_gap > natural_gap
    assert spread_positions[0][1] >= 100
    assert spread_positions[-1][1] + spread_positions[-1][2] <= 280


def test_ruby_layout_centers_single_reading_unit_in_target(qapp):
    metrics = QFontMetrics(_build_ruby_font(Style(ruby_font_size_px=36)))
    unit, x, width = _ruby_layout_units(["そ"], metrics, 200, 100)[0]

    assert unit == "そ"
    assert x + width / 2 == pytest.approx(250)


def test_ruby_small_kana_reading_uses_mora_units(qapp):
    ruby = RubyAnnotation(
        kanji="\u7d14",
        reading="\u3058\u3085\u3093",
        reading_part_ms=[350],
        pos_start_ms=89_280,
        pos_end_ms=89_860,
    )

    assert _ruby_reading_intervals(ruby) == [(89_280, 89_630), (89_630, 89_860)]
    assert _ruby_progress_ratio(ruby, 89_950) == 1.0


def test_ruby_consecutive_timestamps_create_reading_pause(qapp):
    ruby = RubyAnnotation(
        kanji="共",
        reading="とも",
        reading_part_ms=[480, 940],
        pos_start_ms=112_640,
        pos_end_ms=113_950,
    )

    assert _ruby_reading_intervals(ruby) == [
        (112_640, 113_120),
        (113_580, 113_950),
    ]
    assert _ruby_progress_ratio(ruby, 113_350) == pytest.approx(0.5)


def test_utopia_ruby_splits_small_kana_for_visual_bounce(qapp):
    ruby = RubyAnnotation(
        kanji="\u7d14",
        reading="\u3058\u3085\u3093",
        reading_part_ms=[350],
        pos_start_ms=89_280,
        pos_end_ms=89_860,
    )

    assert _ruby_utopia_reading_units_and_intervals(ruby) == [
        ("\u3058", (89_280, 89_455)),
        ("\u3085", (89_455, 89_630)),
        ("\u3093", (89_630, 89_860)),
    ]


def test_utopia_ruby_later_reading_unit_bounces(qapp):
    ruby = RubyAnnotation(
        kanji="A",
        reading="\u3058\u3085\u3093",
        reading_part_ms=[350],
        pos_start_ms=1000,
        pos_end_ms=1580,
    )
    style = Style(
        font_size_px=96,
        ruby_font_size_px=48,
        stroke_width_px=0,
        stroke2_width_px=0,
        shadow_color="",
        exit_anim="utopia",
    )
    ruby_font = _build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font)
    transition = _LineCharTransition(phase="utopia", effect="utopia", progress=1.0, start_ms=0, end_ms=2000)

    plain = _blank(320, 180)
    bounced = _blank(320, 180)
    for img, with_transition in ((plain, False), (bounced, True)):
        painter = QPainter(img)
        try:
            painter.setRenderHints(
                QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
            )
            if with_transition:
                _paint_ruby_text_units_with_transition(
                    painter,
                    ruby,
                    ruby_font,
                    ruby_metrics,
                    90,
                    100,
                    1190,
                    style,
                    transition,
                    0,
                    1,
                    2000,
                )
            else:
                _paint_ruby_text(painter, ruby, ruby_font, ruby_metrics, 90, 100, 1190, style)
        finally:
            painter.end()

    assert _pixel_hash(plain) != _pixel_hash(bounced)
    assert _bounds_size(_ink_bounds(bounced))[1] > _bounds_size(_ink_bounds(plain))[1]


def test_paint_frame_after_line_still_renders_no_active(qapp):
    img = _blank()
    baseline = _pixel_hash(img)
    paint_frame(img, _track(), 9999, Style())
    # 超出最后一行也算无活跃 → 不改像素
    assert _pixel_hash(img) == baseline


def test_paint_frame_zero_size_image_does_not_crash(qapp):
    img = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#000000"))
    # 字体大小 64 在 1×1 上画啥也画不出来，但不应抛
    paint_frame(img, _track(), 1500, Style())


def test_image_fill_brush_is_cached(qapp, tmp_path):
    clear_before_layer_cache()
    image_path = tmp_path / "fill.png"
    source = QImage(16, 16, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(QColor("#336699"))
    assert source.save(str(image_path))

    fill = PaintFill(mode="image", image_path=str(image_path), image_scale_pct=100)
    rect = QRectF(0, 0, 100, 40)

    first = _brush_for_fill(fill, rect)
    second = _brush_for_fill(fill, rect)
    scaled = _brush_for_fill(
        PaintFill(mode="image", image_path=str(image_path), image_scale_pct=150),
        rect,
    )

    assert first.style() == second.style()
    assert scaled.style() == first.style()
    assert len(_IMAGE_FILL_CACHE) == 1
    assert len(_IMAGE_BRUSH_CACHE) == 2


def test_image_fill_before_and_after_layers_share_text_anchor(qapp, tmp_path):
    clear_before_layer_cache()
    image_path = tmp_path / "pattern.png"
    source = QImage(12, 8, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(QColor("#FFFFFF"))
    for x in range(0, source.width(), 2):
        for y in range(source.height()):
            source.setPixelColor(x, y, QColor("#111111"))
    assert source.save(str(image_path))

    fill = PaintFill(mode="image", image_path=str(image_path), image_scale_pct=100)
    colors = KaraokeColors(
        before=KaraokeColorState(text=fill),
        after=KaraokeColorState(text=fill),
    )
    style = Style(
        font_size_px=96,
        stroke_width_px=0,
        shadow_color="",
        line_y_position="center",
        karaoke_colors=colors,
    )
    before_only = _blank()
    fully_sung = _blank()
    track = _track()

    paint_frame(before_only, track, 500, style)
    paint_frame(fully_sung, track, 2600, style)

    assert _pixel_hash(before_only) == _pixel_hash(fully_sung)


def test_paint_frame_entry_and_exit_animation_change_rendered_frame(qapp):
    track = _track()
    static = Style(line_y_position="center", line_tail_ms=0)
    animated = Style(
        line_y_position="center",
        line_tail_ms=0,
        entry_anim="fade",
        entry_lead_ms=600,
        exit_anim="rise",
        exit_fade_ms=600,
    )
    at_entry_static = _blank()
    at_entry_animated = _blank()
    at_exit_static = _blank()
    at_exit_animated = _blank()

    paint_frame(at_entry_static, track, 500, static)
    paint_frame(at_entry_animated, track, 500, animated)
    paint_frame(at_exit_static, track, 2900, static)
    paint_frame(at_exit_animated, track, 2900, animated)

    assert _pixel_hash(at_entry_static) != _pixel_hash(at_entry_animated)
    assert _pixel_hash(at_exit_static) != _pixel_hash(at_exit_animated)


def test_paint_frame_char_fade_entry_reveals_sentence_characters(qapp):
    track = _track()
    plain = _blank()
    char_fade = _blank()

    paint_frame(plain, track, 200, Style(line_y_position="center", entry_lead_ms=1000))
    paint_frame(
        char_fade,
        track,
        200,
        Style(line_y_position="center", entry_anim="char_fade", entry_lead_ms=1000),
    )

    assert _pixel_hash(plain) != _pixel_hash(char_fade)


def test_paint_frame_char_fade_exit_starts_after_sentence_end(qapp):
    track = _track()
    before_exit = _blank()
    during_exit = _blank()
    style = Style(
        line_y_position="center",
        line_tail_ms=1000,
        exit_anim="char_fade",
        exit_fade_ms=1000,
    )

    paint_frame(before_exit, track, 2800, style)
    paint_frame(during_exit, track, 3000, style)

    assert _pixel_hash(before_exit) != _pixel_hash(during_exit)


def test_char_fade_entry_matches_nkm3_linear_character_timing(qapp):
    style = Style()
    transition = _LineCharTransition(phase="entry", effect="char_fade", progress=1.0, start_ms=1000, end_ms=1600)

    first_start = _transition_char_state(style, transition, 0, 3, t_ms=1000)
    first_mid = _transition_char_state(style, transition, 0, 3, t_ms=1125)
    first_done = _transition_char_state(style, transition, 0, 3, t_ms=1250)
    second_before = _transition_char_state(style, transition, 1, 3, t_ms=1174)
    second_mid = _transition_char_state(style, transition, 1, 3, t_ms=1300)

    assert first_start[0] == pytest.approx(0.0)
    assert first_mid[0] == pytest.approx(0.5)
    assert first_done[0] == pytest.approx(1.0)
    assert second_before[0] == pytest.approx(0.0)
    assert second_mid[0] == pytest.approx(0.5)


def test_char_fade_exit_matches_nkm3_reverse_whole_fade(qapp):
    style = Style()
    transition = _LineCharTransition(phase="exit", effect="char_fade", progress=1.0, start_ms=2900, end_ms=3500)

    first_mid = _transition_char_state(style, transition, 0, 3, t_ms=3000)
    first_gone = _transition_char_state(style, transition, 0, 3, t_ms=3250)
    last_before = _transition_char_state(style, transition, 2, 3, t_ms=3249)
    last_mid = _transition_char_state(style, transition, 2, 3, t_ms=3375)
    last_gone = _transition_char_state(style, transition, 2, 3, t_ms=3501)

    assert first_mid[0] == pytest.approx(0.6)
    assert first_gone[0] == pytest.approx(0.0)
    assert last_before[0] == pytest.approx(1.0)
    assert last_mid[0] == pytest.approx(0.5)
    assert last_gone[0] == pytest.approx(0.0)


def test_char_fade_layer_stack_applies_staggered_per_char_opacity(qapp):
    # A1（§9.7）：char_fade 走 LayerCompositor —— 每个 glyph 一个烘焙复用的 before 层，
    # fade_opacity 取该字的 _char_fade_opacity；opacity<=0（尚未淡入）的字整字跳过。
    track = _track()
    line = track.lines[0]
    style = Style(line_y_position="center", entry_anim="char_fade", entry_lead_ms=1000)
    layout = _layout_line(track, line, style, 800, 450)
    count = len(line.chars)  # 3
    transition = _LineCharTransition(
        phase="entry", effect="char_fade", progress=1.0, start_ms=1000, end_ms=1600,
    )
    t_ms = 1250  # char0 全淡入、char1 半透明、char2 尚不可见（见 nkm3 timing 测试）

    layers = _char_transition_layer_stack(layout, t_ms, transition, count)
    before_by_index = {
        layer.glyphs[0].index: layer
        for layer in layers
        if isinstance(layer, _GlyphRunLayer) and not layer.after
    }

    assert before_by_index[0].fade_opacity == pytest.approx(1.0)
    assert before_by_index[1].fade_opacity == pytest.approx(
        _char_fade_opacity(transition, 1, count, t_ms=t_ms)
    )
    assert 0.0 < before_by_index[1].fade_opacity < 1.0
    # char_fade 仅 opacity，无变换残差。
    assert before_by_index[0].transform is None
    # 末字 opacity<=0 → 整字（含 before/after/glow 层）跳过。
    assert _char_fade_opacity(transition, count - 1, count, t_ms=t_ms) <= 0.0
    assert (count - 1) not in before_by_index


def test_char_transition_layer_stack_spin_flip_carries_scale_skew_transform(qapp):
    # A2（§9.7）：spin_flip 走同一 compositor stack，但每字带 scale(opacity)+skew
    # 残差变换，绕字心枢轴，与旧 _character_transform 几何一致。
    track = _track()
    line = track.lines[0]
    style = Style(line_y_position="center", exit_anim="spin_flip")
    layout = _layout_line(track, line, style, 800, 450)
    count = len(line.chars)
    transition = _LineCharTransition(
        phase="exit", effect="spin_flip", progress=1.0, start_ms=2900, end_ms=3500,
    )
    t_ms = 3375  # 末字半透明 → opacity≈0.5，skew/scale 非恒等

    layers = _char_transition_layer_stack(layout, t_ms, transition, count)
    before_by_index = {
        layer.glyphs[0].index: layer
        for layer in layers
        if isinstance(layer, _GlyphRunLayer) and not layer.after
    }

    glyph = layout.text_layout.glyphs[count - 1]
    opacity = _char_fade_opacity(transition, count - 1, count, t_ms=t_ms)
    assert 0.0 < opacity < 1.0
    layer = before_by_index[count - 1]
    assert layer.fade_opacity == pytest.approx(opacity)
    # 变换与旧 _character_transform 逐元素一致（同一 scale+skew+枢轴构造）。
    expected = _character_transform(
        center_x=glyph.left + glyph.width / 2,
        center_y=layout.baseline_y - glyph.metrics.ascent() + glyph.metrics.height() / 2,
        scale_x=opacity,
        scale_y=opacity,
        skew_y=_spin_flip_skew(opacity),  # exit → +skew
    )
    assert layer.transform is not None
    assert layer.transform == expected


def test_utopia_glow_uses_cached_run_glow(qapp):
    # A3（§9.7）：utopia transition + glow → glow 走上正烘焙缓存（before/after 各一条），
    # 逐帧不再重算高斯；同帧重画纯命中、缓存不增长。
    colors = KaraokeColors(
        before=KaraokeColorState(
            text=PaintFill(color="#FFFFFF"),
            stroke=PaintFill(color="#222222"),
            shadow=_solid_fill("#FF8A00"),
        ),
        after=KaraokeColorState(
            text=PaintFill(color="#FFFFFF"),
            stroke=PaintFill(color="#222222"),
            shadow=_solid_fill("#0080FF"),  # 与 before 不同 → 也走 after-glow 缓存
        ),
    )
    style = Style(
        fill_color="#FFFFFF",
        base_color="#FFFFFF",
        stroke_color="#222222",
        decoration_kind="glow",
        karaoke_colors=colors,
        line_y_position="center",
        entry_anim="utopia",
        exit_anim="utopia",
    )

    clear_before_layer_cache()
    assert len(_RUN_GLOW_CACHE) == 0

    paint_frame(_blank(), _track(), 2200, style)
    populated = len(_RUN_GLOW_CACHE)
    assert populated > 0  # 新缓存路径被走到

    # 同帧再画一次：同一上正 glyph 身份 → 纯命中，缓存不增长。
    paint_frame(_blank(), _track(), 2200, style)
    assert len(_RUN_GLOW_CACHE) == populated


def test_spin_flip_entry_uses_char_fade_timing_with_flip_transform(qapp):
    style = Style(entry_anim="spin_flip")
    transition = _LineCharTransition(phase="entry", effect="spin_flip", progress=1.0, start_ms=1000, end_ms=1600)

    start = _transition_char_state(style, transition, 0, 3, t_ms=1000)
    mid = _transition_char_state(style, transition, 0, 3, t_ms=1125)
    done = _transition_char_state(style, transition, 0, 3, t_ms=1250)

    assert start[0] == pytest.approx(0.0)
    assert start[4] == pytest.approx(0.0)
    assert start[6] == pytest.approx(0.0)
    assert mid[0] == pytest.approx(0.5)
    assert mid[4] == pytest.approx(0.5)
    assert mid[5] == pytest.approx(0.5)
    assert mid[6] == pytest.approx(-1.0)
    assert done == (1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0)


def test_spin_flip_exit_flips_in_opposite_direction(qapp):
    style = Style(exit_anim="spin_flip")
    transition = _LineCharTransition(phase="exit", effect="spin_flip", progress=1.0, start_ms=2900, end_ms=3500)

    mid = _transition_char_state(style, transition, 2, 3, t_ms=3375)

    assert mid[0] == pytest.approx(0.5)
    assert mid[4] == pytest.approx(0.5)
    assert mid[5] == pytest.approx(0.5)
    assert mid[6] == pytest.approx(1.0)


def test_paint_frame_utopia_exit_moves_characters_after_each_highlight(qapp):
    track = _track()
    exit_char_fade = _blank()
    exit_utopia = _blank()

    paint_frame(
        exit_char_fade,
        track,
        2200,
        Style(line_y_position="center", line_tail_ms=1000, exit_anim="char_fade", exit_fade_ms=1000),
    )
    paint_frame(
        exit_utopia,
        track,
        2200,
        Style(line_y_position="center", line_tail_ms=1000, exit_anim="utopia", exit_fade_ms=1000),
    )

    assert _pixel_hash(exit_char_fade) != _pixel_hash(exit_utopia)


def test_paint_frame_utopia_exit_does_not_reappear_after_flying_out(qapp):
    track = _track()
    blank = _blank()
    plain = _blank()
    utopia = _blank()
    base = Style(line_y_position="center", line_tail_ms=1100, exit_fade_ms=1000)

    paint_frame(plain, track, 3600, base)
    paint_frame(utopia, track, 3600, replace(base, exit_anim="utopia"))

    assert _pixel_hash(plain) != _pixel_hash(blank)
    assert _pixel_hash(utopia) == _pixel_hash(blank)


def test_utopia_exit_state_flies_character_down_left_after_highlight(qapp):
    style = Style(font_size_px=72, exit_fade_ms=1000)
    transition = _LineCharTransition(phase="exit", effect="utopia", progress=1.0)

    at_end = _transition_char_state(
        style,
        transition,
        0,
        3,
        char_start_ms=1000,
        char_end_ms=1500,
        t_ms=1500,
        frame_height=1080,
    )
    mid = _transition_char_state(
        style,
        transition,
        0,
        3,
        char_start_ms=1000,
        char_end_ms=1500,
        t_ms=2000,
        frame_height=1080,
    )
    final = _transition_char_state(
        style,
        transition,
        0,
        3,
        char_start_ms=1000,
        char_end_ms=1500,
        t_ms=2500,
        frame_height=1080,
    )

    assert at_end == (1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0)
    assert mid[0] == pytest.approx(1.0 / 3.0)
    assert mid[1] == pytest.approx(-108.0, abs=1.0)
    assert mid[2] == pytest.approx(62.4, abs=1.0)
    assert mid[3] == pytest.approx(-120.0, abs=1.0)
    assert mid[4] == pytest.approx(-1.0 / 6.0, abs=0.01)
    assert mid[5] == pytest.approx(1.0 / 3.0, abs=0.01)
    assert final[0] == pytest.approx(0.0)
    assert final[1] == pytest.approx(-144.0)
    assert final[2] == pytest.approx(72.0)
    assert final[3] == pytest.approx(-180.0)
    assert final[4] == pytest.approx(0.0)
    assert final[5] == pytest.approx(0.0)


def test_utopia_entry_state_bounces_each_character_from_line_start(qapp):
    style = Style(font_size_px=72)
    transition = _LineCharTransition(phase="entry", effect="utopia", progress=0.0, start_ms=1000)

    before_char = _transition_char_state(style, transition, 1, 3, t_ms=1050)
    over = _transition_char_state(style, transition, 1, 3, t_ms=1500)
    condensing = _transition_char_state(style, transition, 1, 3, t_ms=1550)
    settled = _transition_char_state(style, transition, 1, 3, t_ms=1600)

    assert before_char == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert over[0] == pytest.approx(1.0)
    assert over[4] == pytest.approx(1.3)
    assert over[5] == pytest.approx(1.3)
    assert condensing[4] == pytest.approx(1.15)
    assert condensing[5] == pytest.approx(1.15)
    assert settled == (1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0)


def test_utopia_wipe_state_bounces_currently_sung_character(qapp):
    style = Style(font_size_px=72)
    transition = _LineCharTransition(phase="wipe", effect="utopia", progress=1.0)

    rising = _transition_char_state(style, transition, 0, 1, char_start_ms=1000, char_end_ms=1500, t_ms=1050)
    peak = _transition_char_state(style, transition, 0, 1, char_start_ms=1000, char_end_ms=1500, t_ms=1100)
    released = _transition_char_state(style, transition, 0, 1, char_start_ms=1000, char_end_ms=1500, t_ms=1500)

    assert rising[4] == pytest.approx(1.075)
    assert rising[5] == pytest.approx(1.075)
    assert peak[4] == pytest.approx(1.15)
    assert peak[5] == pytest.approx(1.15)
    assert released == (1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0)


def test_utopia_mixes_outro_and_later_wipe_per_character(qapp):
    style = Style(font_size_px=72, line_tail_ms=1000, exit_anim="utopia")
    transition = _LineCharTransition(phase="utopia", effect="utopia", progress=1.0, start_ms=0, end_ms=3500)

    exiting_first = _transition_char_state(
        style,
        transition,
        0,
        3,
        char_start_ms=1000,
        char_end_ms=1500,
        t_ms=2100,
        frame_height=1080,
        following_done_ms=2000,
    )
    wiping_third = _transition_char_state(
        style,
        transition,
        2,
        3,
        char_start_ms=2000,
        char_end_ms=2500,
        t_ms=2100,
        frame_height=1080,
        following_done_ms=2750,
    )

    assert exiting_first[0] < 1.0
    assert exiting_first[1] < 0.0
    assert wiping_third == (1.0, 0.0, 0.0, 0.0, 1.15, 1.15, 0.0)


def test_utopia_transform_scales_from_character_origin_for_extra_drift(qapp):
    center_img = _blank(160, 160)
    origin_img = _blank(160, 160)

    def draw_box(img: QImage, *, use_origin: bool) -> tuple[int, int, int, int]:
        painter = QPainter(img)
        try:
            painter.fillRect(QRectF(80, 60, 20, 40), QColor("#FFFFFF"))
            painter.save()
            try:
                _apply_character_transform(
                    painter,
                    center_x=90,
                    center_y=80,
                    dx=-20,
                    dy=20,
                    rotation=0,
                    scale_x=0.5,
                    scale_y=0.5,
                    scale_origin_x=80 if use_origin else None,
                    scale_origin_y=100 if use_origin else None,
                )
                painter.fillRect(QRectF(80, 60, 20, 40), QColor("#FF0000"))
            finally:
                painter.restore()
        finally:
            painter.end()
        return _ink_bounds(img)

    center_bounds = draw_box(center_img, use_origin=False)
    origin_bounds = draw_box(origin_img, use_origin=True)

    assert origin_bounds[0] < center_bounds[0]
    assert origin_bounds[3] > center_bounds[3]


# ---------------------------------------------------------------------------
# 标题字幕 overlay（B7）
# ---------------------------------------------------------------------------


def _title_track() -> TimingTrack:
    line = TimingLine(
        chars=[TimingChar(text="あ", start_ms=2000), TimingChar(text="い", start_ms=2500)],
        end_ms=30000,
    )
    return TimingTrack(meta=TimingTrackMeta(title="曲名", artist="歌手"), lines=[line])


def test_title_overlay_renders_only_when_enabled(qapp):
    track = _title_track()
    base = Style(dual_line_layout=False)
    off = _blank()
    paint_frame(off, track, 500, base)

    on_img = _blank()
    title = TitleOverlay(enabled=True, anchor="top_left", font_size_px=48)
    paint_frame(on_img, track, 500, replace(base, title_overlay=title))
    # 标题在左上，会改变像素
    assert _pixel_hash(off) != _pixel_hash(on_img)
    # 关闭则与无标题一致
    disabled = _blank()
    paint_frame(disabled, track, 500, replace(base, title_overlay=replace(title, enabled=False)))
    assert _pixel_hash(off) == _pixel_hash(disabled)


def test_title_overlay_text_template_substitutes_metadata(qapp):
    track = _title_track()
    title = TitleOverlay(text_template="{title} / {artist}")
    assert _resolve_title_text(title, track) == "曲名 / 歌手"
    # 缺 artist 时清掉孤立分隔
    track2 = TimingTrack(meta=TimingTrackMeta(title="曲名", artist=None), lines=track.lines)
    assert _resolve_title_text(title, track2) == "曲名"


def test_title_overlay_show_modes_and_fade(qapp):
    track = _title_track()  # 时长 30000ms
    whole = TitleOverlay(enabled=True, show_mode="whole", fade_in_ms=300, fade_out_ms=300)
    assert _title_overlay_opacity(whole, track, 1500) == pytest.approx(1.0)
    assert _title_overlay_opacity(whole, track, 100) == pytest.approx(100 / 300)
    assert _title_overlay_opacity(None, track, 1500) == 0.0

    head = TitleOverlay(enabled=True, show_mode="head", duration_ms=8000, fade_in_ms=0, fade_out_ms=0)
    assert _title_overlay_opacity(head, track, 4000) == pytest.approx(1.0)
    assert _title_overlay_opacity(head, track, 12000) == 0.0

    tail = TitleOverlay(enabled=True, show_mode="tail", duration_ms=6000, fade_in_ms=0, fade_out_ms=0)
    assert _title_overlay_opacity(tail, track, 1000) == 0.0
    assert _title_overlay_opacity(tail, track, 27000) == pytest.approx(1.0)


def test_title_overlay_anchor_moves_block(qapp):
    track = _title_track()
    base = Style(dual_line_layout=False)
    title = TitleOverlay(enabled=True, font_size_px=40, align="left")

    top_left = _blank()
    paint_frame(top_left, track, 500, replace(base, title_overlay=replace(title, anchor="top_left")))
    bottom_right = _blank()
    paint_frame(
        bottom_right, track, 500, replace(base, title_overlay=replace(title, anchor="bottom_right"))
    )
    # 标题文字是非背景像素；不同锚点 ink 重心明显不同
    tl = _ink_bounds(top_left)
    br = _ink_bounds(bottom_right)
    assert tl[0] < br[0]  # 左 < 右
    assert tl[1] < br[1]  # 上 < 下


def test_title_overlay_defaults_match_nicokara(qapp):
    # ニコカラ「標準配色」走字前外观（标题永不走字）
    t = TitleOverlay()
    assert t.font_family == "游明朝"
    assert t.fill.color == "#FFEBEB"
    assert t.stroke.color == "#000000" and t.stroke_width_px == 15
    assert t.stroke2.color == "#FFFFFF" and t.stroke2_width_px == 5
    assert t.decoration_kind == "glow" and t.glow_radius_px == 10
    assert t.shadow.color == "#E19696"


def test_title_overlay_latin_font_splits_ascii(qapp):
    from krok_helper.subtitle_render.engine.painter import (
        _make_title_font_for,
        _build_title_font,
        _build_title_latin_font,
    )
    # 单字体时不分离
    single = TitleOverlay(font_family="Yu Mincho")
    assert _make_title_font_for(single, _build_title_font(single), _build_title_latin_font(single)) is None
    # JP + Latin 分开：ASCII 用英数字体，其余用日文字体
    split = TitleOverlay(font_family="Yu Mincho", font_family_latin="Arial")
    font_for = _make_title_font_for(split, _build_title_font(split), _build_title_latin_font(split))
    assert font_for is not None
    assert font_for("A").family() == "Arial"
    assert font_for("あ").family() == "Yu Mincho"


# ---------------------------------------------------------------------------
# 竖排（縦書き）整条路径迁入 LayerCompositor（主文本 + ruby），与直绘像素一致
# ---------------------------------------------------------------------------


def _img_rows_rgba(image: QImage) -> np.ndarray:
    img = image.convertToFormat(QImage.Format.Format_RGBA8888)
    h, w = img.height(), img.width()
    bpl = img.bytesPerLine()
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    arr = np.frombuffer(ptr, dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    return arr[:, : w * 4].copy()


@pytest.mark.parametrize("t_ms", [800, 1200, 1700, 2100, 2600])
def test_vertical_layer_path_matches_direct_within_rounding(qapp, monkeypatch, t_ms):
    # 竖排迁入 LayerCompositor（bake 缓存）与逐帧直绘**几何完全一致**，仅 premultiplied-alpha
    # 取整带来的 ≤1/255 单通道差异（与横排迁移同性质，肉眼不可见）。
    track = _track_with_ruby()
    style = Style(vertical=True, line_y_position="center", stroke_width_px=3, decoration_kind="glow")

    monkeypatch.setenv("KROK_SUBTITLE_VERTICAL_LAYER", "0")
    clear_before_layer_cache()
    direct = _blank()
    paint_frame(direct, track, t_ms, style)

    monkeypatch.setenv("KROK_SUBTITLE_VERTICAL_LAYER", "1")
    clear_before_layer_cache()
    layers = _blank()
    paint_frame(layers, track, t_ms, style)
    clear_before_layer_cache()

    diff = np.abs(_img_rows_rgba(direct).astype(int) - _img_rows_rgba(layers).astype(int))
    assert diff.max() <= 1  # 几何精确，仅 LSB 取整差异


def test_vertical_layer_populates_and_clears_cache(qapp, monkeypatch):
    from krok_helper.subtitle_render.engine.painter import _TEXT_RUN_LAYER_CACHE

    track = _track_with_ruby()
    style = Style(vertical=True, line_y_position="center", stroke_width_px=3)
    monkeypatch.setenv("KROK_SUBTITLE_VERTICAL_LAYER", "1")
    clear_before_layer_cache()
    paint_frame(_blank(), track, 1700, style)
    assert len(_TEXT_RUN_LAYER_CACHE) > 0
    clear_before_layer_cache()
    assert len(_TEXT_RUN_LAYER_CACHE) == 0
