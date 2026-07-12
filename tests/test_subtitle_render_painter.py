"""Tests for ``krok_helper.subtitle_render.engine.painter``.

像素级断言不可移植（字形 / 字体可用性平台差异大），所以本测试聚焦：

- 函数能在不同时刻正常完成不抛
- 各阶段（未唱 / 半唱 / 全唱）画面像素与"完全空白"对比都有差异
- 空 track 不画任何东西
"""

from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRectF  # noqa: E402
from PyQt6.QtGui import QColor, QFontMetrics, QImage, QPainter, QPainterPath  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import krok_helper.subtitle_render.engine.painter as subtitle_painter  # noqa: E402
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
    _after_glow_loose_clip_rect,
    _after_glow_source_clip_rect,
    _fill_clip_band,
    _fill_extent_end,
    _run_fill_complete,
    _layout_vertical_line,
    _layout_rubies,
    _layout_line,
    _line_layer_stack,
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
    _main_text_ruby_progress_ratio,
    _ruby_reading_intervals,
    _ruby_layout_units,
    _ruby_layout_width,
    _ruby_baseline_y,
    _n3_char_box_ascent,
    _n3_char_box_descent,
    _ruby_stroke_extent,
    _ruby_target_indices,
    _ruby_target_x_range,
    _ruby_utopia_reading_units_and_intervals,
    _transition_char_state,
    _utopia_main_group_for_index,
    _utopia_transition_scope_layers,
    _visual_text_padding,
    _display_style_for_signal_window,
    _effective_ruby_for_target,
    _effective_ruby_karaoke_colors,
    _style_for_role,
    _style_for_line,
    _visible_lines_for_style,
    _resolve_title_text,
    _title_overlay_opacity,
    frame_has_content,
    paint_frame,
    frame_vertical_bounds,
    clear_before_layer_cache,
    _TEXT_RUN_LAYER_CACHE,
    _RUN_GLOW_CACHE,
)
from krok_helper.subtitle_render.engine.layers import LayerCompositor, LayerContext, SCOPE_GROUP  # noqa: E402
from krok_helper.subtitle_render.engine.timeline import DisplayLine  # noqa: E402
from krok_helper.subtitle_render.models import (  # noqa: E402
    KaraokeColors,
    KaraokeColorState,
    LineAnimationOverride,
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
from krok_helper.subtitle_render.subtitle_sources import parse_nicokara_lrc  # noqa: E402


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
        stroke_width_px=9,
        stroke2_width_px=0,
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
    assert any(
        QColor(img.pixel(x, 56)).name(QColor.NameFormat.HexRgb).upper() == "#FFFFFF"
        for x in range(layout.text_x, bounds[2] + 1)
    )


def test_frame_vertical_bounds_cover_signal_only_window(qapp):
    track = _singer_track(singer_id=0)
    style = Style(
        font_size_px=20,
        stroke_width_px=0,
        stroke2_width_px=0,
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
    assert any(
        QColor(img.pixel(x, 56)).name(QColor.NameFormat.HexRgb).upper() == "#FFFFFF"
        for x in range(layout.text_x, bounds[2] + 1)
    )


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
    visual_pad = _visual_text_padding(style)
    union_mid = (layout.signal_x + (layout.text_x + layout.total_w + visual_pad)) / 2
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
        stroke_width_px=0,
        stroke2_width_px=0,
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


def test_run_fill_complete_scopes_to_run_indices(qapp):
    segments = [
        _FillSegment(0, 100, 0, 1000, indices=(0,)),
        _FillSegment(100, 200, 1000, 2000, indices=(1,)),
    ]
    # t=1500：第一段唱完、第二段唱到一半
    assert _run_fill_complete(segments, {0}, 1500)
    assert not _run_fill_complete(segments, {1}, 1500)
    assert not _run_fill_complete(segments, {0, 1}, 1500)
    # 空 indices 回退整行判断（与 _fill_clip_band_for_indices 语义一致）
    assert not _run_fill_complete(segments, set(), 1500)
    assert _run_fill_complete(segments, {0, 1}, 2500)
    assert _run_fill_complete(segments, set(), 2500)


def test_after_glow_loose_clip_pads_trailing_edge_and_opens_when_complete(qapp):
    rect = QRectF(0.0, 100.0, 200.0, 60.0)
    glow_pad = 20
    # LTR 走字中：尾缘（左）与上下外扩 glow_pad，预 blur 的 after-glow 前缘严格停在扫光线
    mid = _after_glow_loose_clip_rect((0, 150), rect, glow_pad, False, False)
    assert mid.left() == -20.0
    assert mid.right() == 150.0
    assert mid.top() == 80.0
    assert mid.bottom() == 180.0
    # 唱完：前缘释放完整 halo，行尾不再被硬截
    done = _after_glow_loose_clip_rect((0, 200), rect, glow_pad, False, True)
    assert done.left() == -20.0
    assert done.right() == 220.0
    # RTL 镜像：尾缘在右恒外扩，前缘（左）走字中严格停在扫光线、唱完后放开
    mid_rtl = _after_glow_loose_clip_rect((50, 200), rect, glow_pad, True, False)
    assert mid_rtl.left() == 50.0
    assert mid_rtl.right() == 220.0
    done_rtl = _after_glow_loose_clip_rect((0, 200), rect, glow_pad, True, True)
    assert done_rtl.left() == -20.0
    assert done_rtl.right() == 220.0


def test_after_glow_source_clip_keeps_visible_front_soft(qapp):
    rect = QRectF(0.0, 100.0, 200.0, 60.0)
    glow_pad = 20

    mid = _after_glow_source_clip_rect((0, 150), rect, glow_pad, False, False)
    assert mid is not None
    assert mid.left() < -999_000.0
    assert mid.right() == 150.0
    assert mid.top() == 80.0
    assert mid.bottom() == 180.0

    mid_rtl = _after_glow_source_clip_rect((50, 200), rect, glow_pad, True, False)
    assert mid_rtl is not None
    assert mid_rtl.left() == 50.0
    assert mid_rtl.right() > 999_000.0

    assert _after_glow_source_clip_rect((0, 200), rect, glow_pad, False, True) is None


def _glow_after_style() -> Style:
    # before/after 发光色不同 → 已唱发光需要单独的 after-glow 层
    return Style(
        decoration_kind="glow",
        glow_radius_px=12,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(shadow=PaintFill(color="#000000")),
            after=KaraokeColorState(shadow=PaintFill(color="#FF8800")),
        ),
    )


def test_after_glow_layer_clip_releases_line_edges_when_fully_sung(qapp):
    track = _track()
    line = track.lines[0]
    layout = _layout_line(track, line, _glow_after_style(), 800, 450)
    assert layout is not None
    ink_left = min(seg.left for seg in layout.fill_segments)
    ink_right = max(seg.right for seg in layout.fill_segments)
    ctx = LayerContext(t_ms=0, logical_w=800, logical_h=450)

    def glow_clip(t_ms: int) -> QRectF:
        layers = [
            layer
            for layer in _line_layer_stack(layout, t_ms)
            if isinstance(layer, _GlyphRunAfterGlowLayer)
        ]
        assert layers
        return layers[0].animate(ctx, layers[0].layout(ctx)).clip_rect

    # 走字途中：尾缘（行首）已外扩，前缘停在扫光线（未越过 run 右缘）
    mid = glow_clip(1700)
    assert mid.left() < ink_left
    assert mid.right() < ink_right
    # 唱完：不再裁剪，行首/行尾 halo 不再被硬截
    done = glow_clip(9000)
    assert done is None


def test_after_glow_layer_is_dynamic_until_run_is_complete(qapp):
    track = _track()
    line = track.lines[0]
    layout = _layout_line(track, line, _glow_after_style(), 800, 450)
    assert layout is not None
    ctx = LayerContext(t_ms=0, logical_w=800, logical_h=450)

    def glow_layer(t_ms: int) -> _GlyphRunAfterGlowLayer:
        layers = [
            layer
            for layer in _line_layer_stack(layout, t_ms)
            if isinstance(layer, _GlyphRunAfterGlowLayer)
        ]
        assert layers
        return layers[0]

    mid = glow_layer(1700)
    assert mid.static_key(ctx, mid.layout(ctx)) is None

    done = glow_layer(9000)
    assert done.static_key(ctx, done.layout(ctx)) is not None
    assert done.animate(ctx, done.layout(ctx)).clip_rect is None


def test_after_glow_dynamic_paints_clipped_source_before_blur(qapp, monkeypatch):
    track = _track()
    line = track.lines[0]
    layout = _layout_line(track, line, _glow_after_style(), 800, 450)
    assert layout is not None
    layer = [
        item
        for item in _line_layer_stack(layout, 1700)
        if isinstance(item, _GlyphRunAfterGlowLayer)
    ][0]
    ctx = LayerContext(t_ms=1700, logical_w=800, logical_h=450)
    seen: list[QRectF | None] = []

    def fake_paint_glow_path(*args, source_clip=None, **kwargs):
        seen.append(source_clip)

    monkeypatch.setattr(subtitle_painter, "_paint_glow_path", fake_paint_glow_path)
    image = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        layer.paint_dynamic(painter, ctx, layer.layout(ctx))
    finally:
        painter.end()

    assert seen
    assert seen[0] is not None


def test_after_body_layer_unclipped_when_fully_sung(qapp):
    track = _track()
    line = track.lines[0]
    layout = _layout_line(track, line, _glow_after_style(), 800, 450)
    assert layout is not None
    ctx = LayerContext(t_ms=0, logical_w=800, logical_h=450)

    def body_clip(t_ms: int) -> QRectF | None:
        layers = [
            layer
            for layer in _line_layer_stack(layout, t_ms)
            if isinstance(layer, _GlyphRunLayer) and layer.after
        ]
        assert layers
        return layers[0].animate(ctx, layers[0].layout(ctx)).clip_rect

    # 走字途中仍需在扫光线处裁切
    assert body_clip(1700) is not None
    # 唱完后不裁切，行缘描边/阴影完整
    assert body_clip(9000) is None


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


def test_split_vertical_brush_renders_multiple_hard_color_bands(qapp):
    fill = PaintFill(
        mode="split_vertical",
        split_stops=[
            (0, "#FFFFFF"),
            (30, "#FF0000"),
            (65, "#888888"),
            (100, "#888888"),
        ],
    )
    image = QImage(12, 100, QImage.Format.Format_RGB32)
    image.fill(QColor("#000000"))
    painter = QPainter(image)
    painter.fillRect(QRectF(0, 0, 12, 100), _brush_for_fill(fill, QRectF(0, 0, 12, 100)))
    painter.end()

    assert image.pixelColor(6, 15) == QColor("#FFFFFF")
    assert image.pixelColor(6, 45) == QColor("#FF0000")
    assert image.pixelColor(6, 80) == QColor("#888888")


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
    for idx, ch in enumerate(line.chars):
        seg = layout.fill_segments[idx]
        box_left, box_right = layout.char_x_ranges[idx]
        # Nicokara 风格布局盒可能与墨水等宽；扫光仍严格取实际 path 边界。
        assert box_left <= seg.left <= seg.right <= box_right
        # 且与该字形的矢量墨水包围盒（与 fillPath 同源）一致
        path = QPainterPath()
        path.addText(float(box_left), 0.0, font, ch.text)
        br = path.boundingRect()
        assert seg.left == int(math.floor(br.left()))
        assert seg.right == int(math.ceil(br.right()))


def test_nicokara_layout_width_includes_edge_and_optional_biting():
    from krok_helper.subtitle_render.engine.painter import _nicokara_layout_width

    locked = _nicokara_layout_width(
        80, 100, -10, -5, edge_size=9, allow_biting=False,
    )
    biting = _nicokara_layout_width(
        80, 100, -10, -5, edge_size=9, allow_biting=True,
    )
    without_edge = _nicokara_layout_width(
        80, 100, -10, -5, edge_size=0, allow_biting=False,
    )

    assert locked == without_edge + 9
    assert biting < locked


def test_nicokara_char_geometry_left_offset_clamps_biting():
    from krok_helper.subtitle_render.engine.painter import _nicokara_char_geometry_left_offset

    assert _nicokara_char_geometry_left_offset(
        80, 100, 25, allow_biting=False,
    ) == 20
    assert _nicokara_char_geometry_left_offset(
        80, 100, -10, allow_biting=False,
    ) == 0
    assert _nicokara_char_geometry_left_offset(
        80, 100, -10, allow_biting=True,
    ) == -8


def test_glyph_path_offset_drives_render_path_and_ink_ranges(qapp, monkeypatch):
    import math  # noqa: PLC0415

    from PyQt6.QtGui import QPainterPath  # noqa: E402,PLC0415
    from krok_helper.subtitle_render.engine import painter as painter_module  # noqa: PLC0415

    def controlled_path_offset(*args, **kwargs):
        return 12.25

    monkeypatch.setattr(painter_module, "_char_path_left_offset", controlled_path_offset)
    line = TimingLine(
        chars=[TimingChar(text="A", start_ms=1000)],
        end_ms=2000,
    )
    track = TimingTrack(lines=[line])
    style = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=96,
        line_y_position="center",
        stroke_width_px=0,
        stroke2_width_px=0,
        shadow_offset_x=0,
        shadow_offset_y=0,
    )

    layout = painter_module._layout_line(track, line, style, 400, 200)

    assert layout is not None
    glyph = layout.text_layout.glyphs[0]
    assert glyph.path_offset_x == pytest.approx(12.25)

    raw_path = QPainterPath()
    raw_path.addText(float(glyph.left), float(layout.baseline_y), glyph.font, glyph.text)
    shifted_path = painter_module._glyph_run_path([glyph], layout.baseline_y)
    assert shifted_path.boundingRect().left() == pytest.approx(
        raw_path.boundingRect().left() + 12.25
    )

    shifted_bounds = shifted_path.boundingRect()
    assert layout.ink_x_ranges[0] == (
        int(math.floor(shifted_bounds.left())),
        int(math.ceil(shifted_bounds.right())),
    )


def test_nicokara_space_width_uses_font_percentage_and_edge(qapp):
    line = TimingLine(
        chars=[TimingChar(text=" ", start_ms=1000)],
        end_ms=2000,
    )
    track = TimingTrack(lines=[line])
    style = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=100,
        space_width_percent=20,
        stroke_width_px=9,
        dual_line_layout=False,
    )

    layout = _layout_line(track, line, style, 400, 200)

    assert layout is not None
    assert layout.char_widths == [29]


def test_shared_lrc_text_span_uses_rendered_char_widths_for_timing(qapp, monkeypatch):
    """``[start]多字[next]`` 在横排 Painter 中按当前字体 advance 分时。

    解析器保留等分 ``start_ms`` 供无字体消费者兼容；真正渲染 layout 必须覆盖为
    SUG 同款的像素宽度加权区间。选 ``W`` / ``i`` 是为了确保比例明显不等于 1:1。
    """
    track = parse_nicokara_lrc("[00:01:00]Wi[00:02:00]\n")
    line = track.lines[0]
    from krok_helper.subtitle_render.engine import painter as painter_module  # noqa: PLC0415

    real_layout_width = painter_module._char_layout_width

    def controlled_layout_width(text, font, metrics, latin_metrics, font_for, style):
        if text == "W":
            return 30
        if text == "i":
            return 10
        return real_layout_width(text, font, metrics, latin_metrics, font_for, style)

    # 系统测试字体可能回退为等宽字体；固定两个 advance，锁定本测试只验证
    # Painter 是否把自己的实际布局宽度传给时间分配，而不依赖宿主字体库。
    monkeypatch.setattr(painter_module, "_char_layout_width", controlled_layout_width)
    style = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=96,
        line_y_position="center",
        stroke_width_px=0,
        stroke2_width_px=0,
        shadow_offset_x=0,
        shadow_offset_y=0,
    )

    layout = _layout_line(track, line, style, 600, 240)
    assert layout is not None
    assert layout.char_widths[0] > layout.char_widths[1]
    expected_boundary = int(
        1000
        + 1000
        * layout.char_widths[0]
        / sum(layout.char_widths)
    )
    assert expected_boundary != 1500
    assert layout.intervals == [
        (1000, expected_boundary),
        (expected_boundary, 2000),
    ]


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


def test_n3_glow_blur_radii_match_three_concentration_levels():
    assert subtitle_painter._glow_blur_radii(13, 0) == (13,)
    assert subtitle_painter._glow_blur_radii(13, 1) == (13, 7)
    assert subtitle_painter._glow_blur_radii(13, 2) == (13, 9, 5)


def test_n3_gaussian_kernel_uses_decor_radius_as_standard_deviation():
    sigma = 4
    kernel = subtitle_painter._n3_gaussian_kernel_1d(sigma)

    assert len(kernel) == sigma * 6 + 1
    assert float(kernel.sum()) == pytest.approx(1.0)
    assert kernel == pytest.approx(kernel[::-1])
    assert float(kernel[sigma * 3 + 1] / kernel[sigma * 3]) == pytest.approx(
        np.exp(-1.0 / (2.0 * sigma * sigma))
    )


def test_separable_gaussian_blur_preserves_expected_impulse_energy(qapp):
    source = QImage(129, 129, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(0)
    source.setPixelColor(64, 64, QColor(255, 255, 255, 255))

    blurred = subtitle_painter._gaussian_blur_image(source, 4)
    alpha = _img_rows_rgba(blurred).reshape((129, 129, 4))[:, :, 3]

    # The normalized 25x25 Gaussian produces 219 alpha units after per-pixel
    # 8-bit quantization. Qt's former exponential blur only retained 98 and
    # concentrated 11 at the centre.
    assert int(alpha.sum()) == 219
    assert int(alpha[64, 64]) == 3


def test_n3_balanced_blur_matches_dark_spiral_radius_ten_response(qapp):
    """N3 Direct2D Balanced response for 1.n3proj's 15 px glow source."""
    source = QImage(129, 129, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(0)
    for y in range(129):
        for x in range(57, 72):
            source.setPixelColor(x, y, QColor(255, 255, 255, 255))

    blurred = subtitle_painter._blur_image(source, 10)
    alpha = _img_rows_rgba(blurred).reshape((129, 129, 4))[:, :, 3]
    actual = alpha[64, 64:98].astype(np.int16)
    direct2d_balanced = np.array(
        [
            139, 138, 135, 133, 130, 125, 119, 113, 107, 100, 92, 84,
            76, 69, 62, 55, 48, 42, 37, 31, 26, 22, 19, 15, 12, 10, 8,
            6, 4, 3, 2, 2, 1, 0,
        ],
        dtype=np.int16,
    )

    assert int(np.abs(actual - direct2d_balanced).max()) <= 1
    assert int(alpha.sum()) == pytest.approx(458_773, rel=0.002)


def test_glow_concentration_levels_stack_more_alpha(qapp):
    path = QPainterPath()
    path.addRoundedRect(QRectF(36, 36, 24, 24), 3, 3)
    rect = path.boundingRect()
    fill = _solid_fill("#36BFFA")

    def render(level: int) -> QImage:
        image = QImage(96, 96, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            subtitle_painter._paint_glow_path(
                painter,
                path,
                fill,
                rect,
                13,
                2,
                0,
                concentration_level=level,
            )
        finally:
            painter.end()
        return image

    images = [render(level) for level in range(3)]
    alpha_sums = [
        int(
            _img_rows_rgba(image)
            .reshape((image.height(), image.width(), 4))[:, :, 3]
            .sum()
        )
        for image in images
    ]

    assert len({_pixel_hash(image) for image in images}) == 3
    assert alpha_sums[0] < alpha_sums[1] < alpha_sums[2]


def test_glow_concentration_is_part_of_cached_run_signature():
    low = SimpleNamespace(style=Style(glow_concentration_level=0))
    medium = SimpleNamespace(style=Style(glow_concentration_level=1))

    assert subtitle_painter._glyph_run_signature(low) != subtitle_painter._glyph_run_signature(
        medium
    )


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


def test_horizontal_ruby_glow_stays_below_solid_text_layers(qapp, monkeypatch):
    """N3-like layering: ruby glow under text bodies, ruby solid layer on top."""
    import krok_helper.subtitle_render.engine.painter as painter_mod

    calls: list[str] = []
    ruby_kwargs: list[dict] = []

    monkeypatch.setattr(
        painter_mod,
        "_paint_ruby_glow_layers",
        lambda *args, **kwargs: calls.append("ruby_glow"),
    )
    monkeypatch.setattr(
        painter_mod,
        "_paint_line_layers",
        lambda *args, **kwargs: calls.append("main"),
    )
    monkeypatch.setattr(
        painter_mod,
        "_paint_rubies",
        lambda *args, **kwargs: (calls.append("ruby"), ruby_kwargs.append(kwargs)),
    )

    track = _track_with_ruby()
    image = _blank()
    painter = QPainter(image)
    try:
        painter_mod._paint_line_static(
            painter,
            image.width(),
            image.height(),
            track,
            track.lines[0],
            1500,
            Style(decoration_kind="glow", line_y_position="center"),
        )
    finally:
        painter.end()

    assert calls == ["ruby_glow", "main", "ruby"]
    assert ruby_kwargs[-1]["draw_glow"] is False


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


def test_fill_extent_rests_at_gap_midpoint_during_pause(qapp):
    """句中停顿：前沿推进到墨水间隙中点，盖住已唱字符的描边/发光外扩。

    前沿若停在已唱段墨水右缘，描边尾巴会留在走字前状态（wipe 不完全的
    「小尾巴」）；行尾停顿由 run 级裁剪释放处理，不经此路径。
    """
    from krok_helper.subtitle_render.engine.painter import _fill_extent_left

    segments = [
        _FillSegment(100, 200, 1000, 2000, indices=(0,)),
        # 墨水间隙 200→240，时间停顿 2000→2500
        _FillSegment(240, 300, 2500, 3000, indices=(1,)),
    ]
    # 唱到一半 / 恰好唱完瞬间：不受影响
    assert _fill_extent_end(segments, 1500) == 150
    assert _fill_extent_end(segments, 2000) == 220  # 停顿开始即推进到中点
    # 停顿中：前沿在间隙中点 (200+240)//2
    assert _fill_extent_end(segments, 2300) == 220
    # 下一段开始后：正常从其墨水左缘继续，前沿单调不回退
    assert _fill_extent_end(segments, 2750) == 270
    # 未开始时不受 previous_complete 影响
    assert _fill_extent_end(segments, 500) == 100

    # RTL 镜像：segments 从右往左排列
    rtl_segments = [
        _FillSegment(240, 300, 1000, 2000, indices=(0,)),
        _FillSegment(100, 200, 2500, 3000, indices=(1,)),
    ]
    assert _fill_extent_left(rtl_segments, 2300) == 220
    assert _fill_extent_left(rtl_segments, 500) == 300


def test_main_text_uses_all_ruby_checkpoints_even_when_reading_units_are_missing(qapp):
    """占位 ruby part 被 LRC 剥掉后，主文字仍按全部 checkpoint 分段。

    SUG 的 ``char_part_anchors`` 不依赖可见 reading unit 数；旧字幕路径复用
    ``_ruby_progress_ratio``，reading 只剩一个字时会忽略两个额外 checkpoint。
    """
    line = TimingLine(chars=[TimingChar(text="寿", start_ms=5000)], end_ms=6000)
    ruby = RubyAnnotation(
        kanji="寿",
        reading="す",
        reading_part_ms=[150, 300],
        pos_start_ms=5000,
        pos_end_ms=6000,
    )
    segments = _karaoke_fill_segments(
        [100],
        [(5000, 6000)],
        [(0, 100)],
        [ruby],
        line,
    )

    # anchors=[5000, 5150, 5300, 6000]；5200 位于第 2/3 段的 1/3 处，
    # 总进度=(1+1/3)/3=4/9。Ruby 自身仍按一个可见 unit 线性走到 20%。
    assert _main_text_ruby_progress_ratio(ruby, 5200) == pytest.approx(4 / 9)
    assert _ruby_progress_ratio(ruby, 5200) == pytest.approx(0.2)
    assert _fill_extent_end(segments, 5200) == 44
    assert _character_fill_ratio(
        line,
        [(5000, 6000)],
        [(0, 100)],
        [ruby],
        0,
        5200,
    ) == pytest.approx(4 / 9)


def test_ruby_progress_uses_rendered_part_widths(qapp):
    style = Style(ruby_font_size_px=32)
    metrics = QFontMetrics(_build_ruby_font(style))
    ruby = RubyAnnotation(
        kanji="字",
        reading="WWi",
        reading_part_ms=[500],
        pos_start_ms=1000,
        pos_end_ms=2000,
        reading_parts=["WW", "i"],
    )
    wide = metrics.horizontalAdvance("WW")
    narrow = metrics.horizontalAdvance("i")

    # 第一段走到一半：SUG 按该 part 的实际 advance 累计，而不是固定占 1/2。
    assert _ruby_progress_ratio(ruby, 1250, metrics) == pytest.approx(
        wide * 0.5 / (wide + narrow)
    )
    assert _ruby_progress_ratio(ruby, 1500, metrics) == pytest.approx(
        wide / (wide + narrow)
    )


def test_empty_ruby_part_consumes_time_without_spatial_progress(qapp):
    style = Style(ruby_font_size_px=32)
    metrics = QFontMetrics(_build_ruby_font(style))
    ruby = RubyAnnotation(
        kanji="字",
        reading="WWi",
        reading_part_ms=[200, 500],
        pos_start_ms=1000,
        pos_end_ms=2000,
        reading_parts=["WW", "", "i"],
    )

    assert _ruby_progress_ratio(ruby, 1200, metrics) == pytest.approx(
        _ruby_progress_ratio(ruby, 1350, metrics)
    )
    assert _ruby_progress_ratio(ruby, 1499, metrics) == pytest.approx(
        _ruby_progress_ratio(ruby, 1350, metrics)
    )


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
    style = Style(ruby_font_size_px=36, stroke_width_px=0, stroke2_width_px=0)
    natural_positions = _ruby_layout_units(["か", "な", "た"], metrics, 100, None, style=style)
    spread_positions = _ruby_layout_units(["か", "な", "た"], metrics, 100, 180, style=style)

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


def test_ruby_layout_centers_overwide_reading_like_nicokara(qapp):
    metrics = QFontMetrics(_build_ruby_font(Style(ruby_font_size_px=36)))
    units = ["ひ", "か", "り"]
    positions = _ruby_layout_units(units, metrics, 100, 50)
    total_width = _ruby_layout_width("ひかり", metrics, 50)

    assert positions[0][1] < 100
    assert positions[-1][1] + positions[-1][2] > 150
    assert (positions[0][1] + total_width / 2) == pytest.approx(125)


def test_default_ruby_geometry_uses_nicokara_ruby_font_defaults(qapp):
    style = Style()
    metrics = QFontMetrics(_build_ruby_font(style))
    old_style = Style(
        ruby_font_size_px=35,
        stroke_width_px=9,
        stroke2_width_px=0,
        ruby_stroke_width_px=None,
        ruby_stroke2_width_px=None,
    )
    old_metrics = QFontMetrics(_build_ruby_font(old_style))

    assert style.ruby_font_size_px == 45
    assert style.ruby_stroke_width_px == 10
    assert style.ruby_stroke2_width_px == 3
    assert _ruby_stroke_extent(style) == 7
    assert _ruby_layout_width("\u3072\u304b\u308a", metrics, 80, style=style) > _ruby_layout_width(
        "\u3072\u304b\u308a",
        old_metrics,
        80,
        style=old_style,
    )


def test_default_ruby_gap_matches_nicokara_zero_interval(qapp):
    style = Style(
        font_size_px=100,
        ruby_font_size_px=45,
        stroke_width_px=15,
        stroke2_width_px=5,
        ruby_stroke_width_px=10,
        ruby_stroke2_width_px=3,
        ruby_gap_px=0,
    )
    main_metrics = QFontMetrics(_build_font(style))
    ruby_metrics = QFontMetrics(_build_ruby_font(style))
    baseline_y = 180
    # N3 盒模型：盒高 = 字号 + 描边宽（edge2 不占位），基线按字体 A:(A+D) 比例分割。
    main_box_ascent = _n3_char_box_ascent(main_metrics, style.font_size_px, style.stroke_width_px)
    ruby_baseline = _ruby_baseline_y(baseline_y, main_box_ascent, ruby_metrics, style)

    main_box_top = baseline_y - main_box_ascent
    ruby_box_bottom = ruby_baseline + _n3_char_box_descent(
        ruby_metrics, style.ruby_font_size_px, style.ruby_stroke_width_px
    )
    assert style.ruby_gap_px == 0
    # 间隔 0 → ruby 盒底与主行盒顶相接（基线取整误差 ≤ 1px）
    assert abs(ruby_box_bottom - main_box_top) <= 1.0
    # N3 盒顶显著低于 Qt metric 顶（无 em 外头部空隙）——注音因此更贴近正文
    assert main_box_ascent < main_metrics.ascent() + _visual_text_padding(style)


def test_ruby_target_width_uses_main_draw_width_not_ink_bounds(qapp):
    line = TimingLine(
        chars=[TimingChar(text="\u5149", start_ms=1000)],
        end_ms=1600,
    )
    ruby = RubyAnnotation(
        kanji="\u5149",
        reading="\u3072\u304b\u308a",
        pos_start_ms=1000,
        pos_end_ms=1600,
    )
    track = TimingTrack(lines=[line], rubies=[ruby])
    style = Style(line_y_position="center", dual_line_layout=False)
    layout = _layout_line(track, line, style, 640, 360, baseline_y=220)
    assert layout is not None and layout.ruby_metrics is not None

    ruby_layouts = _layout_rubies(
        layout.ruby_metrics,
        line,
        layout.intervals,
        layout.char_x_ranges,
        layout.baseline_y,
        [ruby],
        style,
        text_layout=layout.text_layout,
    )

    assert ruby_layouts[0].target_width == layout.char_widths[0]
    assert ruby_layouts[0].reading_width > ruby_layouts[0].target_width


def test_ruby_gradient_reference_uses_main_text_run(qapp):
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
                kanji="B",
                reading="び",
                pos_start_ms=1500,
                pos_end_ms=2000,
            )
        ],
    )
    style = Style(
        font_size_px=64,
        ruby_font_size_px=28,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(text=PaintFill(mode="solid", color="#FFFFFF")),
            after=KaraokeColorState(
                text=PaintFill(
                    mode="gradient_horizontal",
                    gradient_stops=((0, "#FF0000"), (100, "#0000FF")),
                )
            ),
        ),
    )
    layout = _layout_line(track, line, style, 420, 240, baseline_y=140)
    assert layout is not None

    ruby_layers = _ruby_layer_stack(layout, line, 1750, style)
    assert ruby_layers
    ruby_layout = ruby_layers[0].ruby_layout

    assert ruby_layout.gradient_rect.left() == pytest.approx(layout.line_rect.left())
    assert ruby_layout.gradient_rect.width() == pytest.approx(layout.line_rect.width())
    assert ruby_layout.gradient_rect.width() > ruby_layout.target_width


def test_role_ruby_defaults_to_role_main_colors_not_global_ruby(qapp):
    global_ruby = KaraokeColors(after=KaraokeColorState(text=_solid_fill("#FF0000")))
    role_main = KaraokeColors(after=KaraokeColorState(text=_solid_fill("#0088FF")))
    style = Style(
        karaoke_colors=KaraokeColors(after=KaraokeColorState(text=_solid_fill("#FFFFFF"))),
        ruby_karaoke_colors=global_ruby,
        custom_style_schemes={
            "A": SubtitleStyleScheme(karaoke_colors=role_main, fill_color="#0088FF")
        },
    )

    role_style = _style_for_role(style, "A")

    assert role_style.karaoke_colors == role_main
    assert role_style.ruby_karaoke_colors is None
    assert _effective_ruby_karaoke_colors(role_style).after.text.color == "#0088FF"


def test_role_style_applies_ruby_outline_decoration_and_glow_overrides(qapp):
    style = Style(
        custom_style_schemes={
            "A": SubtitleStyleScheme(
                glow_concentration_level=1,
                ruby_stroke_width_px=7,
                ruby_stroke2_width_px=3,
                ruby_decoration_kind="glow",
                ruby_glow_radius_px=11,
                ruby_glow_before_radius_px=12,
                ruby_glow_after_radius_px=13,
                ruby_glow_concentration_level=2,
                ruby_shadow_offset_x=4,
                ruby_shadow_offset_y=5,
            )
        }
    )

    role_style = _style_for_role(style, "A")

    assert role_style.glow_concentration_level == 1
    assert role_style.ruby_stroke_width_px == 7
    assert role_style.ruby_stroke2_width_px == 3
    assert role_style.ruby_decoration_kind == "glow"
    assert role_style.ruby_glow_radius_px == 11
    assert role_style.ruby_glow_before_radius_px == 12
    assert role_style.ruby_glow_after_radius_px == 13
    assert role_style.ruby_glow_concentration_level == 2
    assert role_style.ruby_shadow_offset_x == 4
    assert role_style.ruby_shadow_offset_y == 5


def test_role_ruby_layer_uses_target_role_style(qapp):
    line = TimingLine(
        chars=[TimingChar(text="歌", start_ms=1000, role_label="A")],
        end_ms=2000,
    )
    ruby = RubyAnnotation(
        kanji="歌",
        reading="うた",
        pos_start_ms=1000,
        pos_end_ms=2000,
    )
    role_main = KaraokeColors(after=KaraokeColorState(text=_solid_fill("#0088FF")))
    style = Style(
        ruby_karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=_solid_fill("#FF0000"))
        ),
        custom_style_schemes={
            "A": SubtitleStyleScheme(karaoke_colors=role_main, fill_color="#0088FF")
        },
    )
    layout = _layout_line(TimingTrack(lines=[line], rubies=[ruby]), line, style, 420, 240)
    assert layout is not None

    ruby_layers = _ruby_layer_stack(layout, line, 1500, style)
    after_layer = next(layer for layer in ruby_layers if layer.after)

    assert _effective_ruby_karaoke_colors(after_layer.style).after.text.color == "#0088FF"


def test_role_line_simultaneous_wipe_uses_scoped_after_band(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="溶", start_ms=35900, role_label="fhana"),
            TimingChar(text="け", start_ms=36130, role_label="fhana"),
            TimingChar(text="合", start_ms=36360, role_label="fhana"),
            TimingChar(text="い", start_ms=36490, role_label="fhana"),
            TimingChar(text=" ", start_ms=37580, role_label="未命名"),
            TimingChar(text="一", start_ms=36890, role_label="佐藤 純一"),
            TimingChar(text="つ", start_ms=37350, role_label="佐藤 純一"),
            TimingChar(text="に", start_ms=37480, role_label="佐藤 純一"),
        ],
        end_ms=38310,
    )
    track = TimingTrack(lines=[line])
    style = Style(
        font_size_px=64,
        custom_style_schemes={
            "fhana": SubtitleStyleScheme(fill_color="#FF5577"),
            "未命名": SubtitleStyleScheme(fill_color="#FFFFFF"),
            "佐藤 純一": SubtitleStyleScheme(fill_color="#4488FF"),
        },
    )
    layout = _layout_line(track, line, style, 900, 300, baseline_y=180)
    assert layout is not None
    one_glyph = next(glyph for glyph in layout.text_layout.glyphs if glyph.text == "一")

    layers = _line_layer_stack(layout, 37000)
    sato_after_layers = [
        layer
        for layer in layers
        if isinstance(layer, _GlyphRunLayer)
        and layer.after
        and any(glyph.text == "一" for glyph in layer.glyphs)
    ]

    assert sato_after_layers
    assert sato_after_layers[0].clip_band is not None
    assert sato_after_layers[0].clip_band[1] > one_glyph.left


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
        stroke2_width_px=0,
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
    # layout_index=None + 清空方案表：绕过引用解析，直接驱动画笔的锚点机制
    base = Style(dual_line_layout=False, custom_style_schemes={})
    title = TitleOverlay(enabled=True, font_size_px=40, align="left", layout_index=None)

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


def test_resolve_title_overlay_uses_scheme_and_layout(qapp):
    from krok_helper.subtitle_render.engine.painter import resolve_title_overlay

    style = Style(title_overlay=TitleOverlay(enabled=True))
    resolved = resolve_title_overlay(style)
    # 默认「标题」方案 = 原 TitleOverlay 默认外观（ニコカラ標準配色）
    assert resolved.font_family == "游明朝"
    assert resolved.fill.color == "#FFEBEB"
    assert resolved.stroke_width_px == 15
    # 默认布局引用（内置タイトル左上）→ 顶部左上、余白 50/50、行間 15
    assert resolved.anchor == "top_left" and resolved.align == "left"
    assert resolved.offset_x == 50 and resolved.offset_y == 50
    assert resolved.line_gap_px == 15

    # 编辑「标题」方案 → 标题外观跟随
    schemes = dict(style.custom_style_schemes)
    schemes["标题"] = replace(schemes["标题"], font_size_px=64, font_family="Yu Mincho")
    resolved = resolve_title_overlay(replace(style, custom_style_schemes=schemes))
    assert resolved.font_size_px == 64
    assert resolved.font_family == "Yu Mincho"

    # 布局引用悬空 → 位置保留字段原值
    dangling = replace(
        style, title_overlay=replace(style.title_overlay, layout_index=9)
    )
    assert resolve_title_overlay(dangling).anchor == "top_left"


def test_old_project_title_fields_migrate_to_scheme_and_layout():
    """旧工程（标题带显式外观、无布局引用）加载后外观折算进方案与布局。"""
    payload = {
        "title_overlay": {
            "enabled": True,
            "text_template": "T",
            "font_family": "Custom Font",
            "font_size_px": 77,
            "anchor": "bottom_right",
            "align": "right",
            "offset_x": 30,
            "offset_y": 40,
            "line_gap_px": 22,
        },
        "custom_style_schemes": {},
        "layouts": [],
    }
    restored = style_from_dict(payload)
    scheme = restored.custom_style_schemes["标题"]
    assert scheme.font_family == "Custom Font"
    assert scheme.font_size_px == 77
    assert scheme.karaoke_colors.before.text.color == "#FFEBEB"
    # 位置折算成新布局并被标题引用
    assert restored.title_overlay.layout_index == len(restored.layouts)
    layout = restored.layouts[-1]
    assert layout.line_y_position == "bottom"
    assert layout.line_alignments == ["right"]
    assert layout.horizontal_margin_px == 30
    assert layout.line_y_margin_px == 40
    assert layout.line_gap_px == 22


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


@pytest.mark.parametrize("t_ms", [1000, 1300, 1800, 2500])
def test_horizontal_layer_path_matches_direct_within_rounding(qapp, monkeypatch, t_ms):
    track = _track()
    style = Style(
        font_size_px=48,
        line_lead_in_ms=0,
        stroke_width_px=4,
        stroke2_width_px=2,
        decoration_kind="none",
    )

    monkeypatch.setenv("KROK_SUBTITLE_HORIZONTAL_LAYER", "0")
    clear_before_layer_cache()
    direct = _blank()
    paint_frame(direct, track, t_ms, style)
    assert len(_TEXT_RUN_LAYER_CACHE) == 0

    monkeypatch.setenv("KROK_SUBTITLE_HORIZONTAL_LAYER", "1")
    clear_before_layer_cache()
    layers = _blank()
    paint_frame(layers, track, t_ms, style)
    assert len(_TEXT_RUN_LAYER_CACHE) > 0
    clear_before_layer_cache()

    diff = np.abs(_img_rows_rgba(direct).astype(int) - _img_rows_rgba(layers).astype(int))
    assert diff.max() <= 1


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


@pytest.mark.parametrize("t_ms", [1000, 1700, 2500])
def test_line_layout_cache_matches_uncached(qapp, monkeypatch, t_ms):
    # 行级布局缓存命中与逐帧重算必须逐字节一致（同一计算，只是缓存了产物）。
    track = _track_with_ruby()
    style = Style(line_y_position="center", stroke_width_px=4, ruby_font_size_px=20)

    monkeypatch.setenv("KROK_SUBTITLE_LAYOUT_CACHE", "0")
    clear_before_layer_cache()
    direct = _blank()
    paint_frame(direct, track, t_ms, style)

    monkeypatch.setenv("KROK_SUBTITLE_LAYOUT_CACHE", "1")
    clear_before_layer_cache()
    paint_frame(_blank(), track, t_ms, style)  # 先填缓存
    cached = _blank()
    paint_frame(cached, track, t_ms, style)  # 命中路径
    clear_before_layer_cache()

    assert _img_rows_rgba(direct).tobytes() == _img_rows_rgba(cached).tobytes()


def test_line_layout_cache_picks_up_inplace_track_edits(qapp, monkeypatch):
    # models 是可变 dataclass、前端不调失效接口：布局缓存 key 必须每帧由当前值
    # 重建，track 被就地修改（如打轴微调）后下一帧立刻生效，不许吐旧几何。
    monkeypatch.setenv("KROK_SUBTITLE_LAYOUT_CACHE", "1")
    track = _track()
    style = Style(line_y_position="center")
    original_start = track.lines[0].chars[1].start_ms

    clear_before_layer_cache()
    before = _blank()
    paint_frame(before, track, 1700, style)

    track.lines[0].chars[1].start_ms = original_start + 400  # 走字推进点右移
    edited = _blank()
    paint_frame(edited, track, 1700, style)
    assert _pixel_hash(edited) != _pixel_hash(before)

    track.lines[0].chars[1].start_ms = original_start
    restored = _blank()
    paint_frame(restored, track, 1700, style)
    clear_before_layer_cache()
    assert _pixel_hash(restored) == _pixel_hash(before)


@pytest.mark.parametrize("t_ms", [1300, 1700, 2100])
@pytest.mark.parametrize("rtl", [False, True])
def test_after_glow_strip_matches_full_blur_within_tolerance(qapp, monkeypatch, t_ms, rtl):
    # 走字中 after-glow 的窄带优化（前沿逐帧模糊 + 其余贴烘焙位图）与整行逐帧模糊
    # 的差异只剩烘焙位图与直绘之间既有的模糊尾部容差（软晕上 ≤12/255，肉眼不可见），
    # 且扫光前沿不得偏移（窄带画布保留原亚像素相位）。
    track = _track()
    style = replace(
        _glow_after_style(),
        glow_concentration_level=2,
        right_to_left=rtl,
    )

    monkeypatch.setenv("KROK_SUBTITLE_AFTERGLOW_STRIP", "0")
    clear_before_layer_cache()
    full = _blank()
    paint_frame(full, track, t_ms, style)

    monkeypatch.setenv("KROK_SUBTITLE_AFTERGLOW_STRIP", "1")
    clear_before_layer_cache()
    strip = _blank()
    paint_frame(strip, track, t_ms, style)
    clear_before_layer_cache()

    diff = np.abs(_img_rows_rgba(full).astype(int) - _img_rows_rgba(strip).astype(int))
    assert diff.max() <= 12
    assert diff.mean() < 0.5


# ---------------------------------------------------------------------------
# P1：N3 布局对齐（负值间距 / ルビ間隔 / ルビ配置 / 余白警告）
# ---------------------------------------------------------------------------

from krok_helper.subtitle_render.engine.painter import (  # noqa: E402
    LayoutMarginWarning,
    _build_latin_font,
    _char_layout_width,
    _line_text_width,
    _make_font_for,
    _resolve_ruby_alignment,
    check_layout_margins,
)
from krok_helper.subtitle_render.models import style_from_dict, style_to_dict  # noqa: E402


def test_negative_line_gap_overlaps_dual_line_boxes(qapp):
    track = TimingTrack()
    zero = _resolve_display_baselines(1080, track, [], Style(line_gap_px=0))
    negative = _resolve_display_baselines(1080, track, [], Style(line_gap_px=-40))

    # bottom 锚定：下行基线不动，负行距让上行向下靠近 40 px。
    assert negative[1] == zero[1]
    assert negative[0] == zero[0] + 40


def test_negative_ruby_gap_moves_ruby_down_into_main_text(qapp):
    base = Style(ruby_gap_px=0)
    biting = Style(ruby_gap_px=-10)
    metrics = QFontMetrics(_build_font(base))
    ruby_metrics = QFontMetrics(_build_ruby_font(base))
    main_box_ascent = _n3_char_box_ascent(metrics, base.font_size_px, base.stroke_width_px)

    zero_baseline = _ruby_baseline_y(300, main_box_ascent, ruby_metrics, base)
    biting_baseline = _ruby_baseline_y(300, main_box_ascent, ruby_metrics, biting)

    assert biting_baseline == zero_baseline + 10


def test_ruby_interval_enforces_min_gap_between_units(qapp):
    tight_style = Style(
        ruby_font_size_px=36, ruby_alignment="equal_space", ruby_interval_px=0
    )
    spaced_style = Style(
        ruby_font_size_px=36, ruby_alignment="equal_space", ruby_interval_px=12
    )
    metrics = QFontMetrics(_build_ruby_font(tight_style))
    units = ["か", "な", "た"]

    # 目标宽度远小于自然宽度 → equal_space 的摊分间距为负，被 interval 抬到下限。
    tight = _ruby_layout_units(units, metrics, 100, 50, style=tight_style)
    spaced = _ruby_layout_units(units, metrics, 100, 50, style=spaced_style)

    tight_step = tight[1][1] - tight[0][1]
    spaced_step = spaced[1][1] - spaced[0][1]
    assert spaced_step == pytest.approx(tight_step + 12)


def test_ruby_alignment_center_keeps_natural_spacing_in_wide_target(qapp):
    center_style = Style(ruby_font_size_px=36, ruby_alignment="center")
    equal_style = Style(ruby_font_size_px=36, ruby_alignment="equal_space")
    metrics = QFontMetrics(_build_ruby_font(center_style))
    units = ["か", "な", "た"]
    target = 400

    center = _ruby_layout_units(units, metrics, 100, target, style=center_style)
    equal = _ruby_layout_units(units, metrics, 100, target, style=equal_style)

    center_step = center[1][1] - center[0][1]
    equal_step = equal[1][1] - equal[0][1]
    assert center_step < equal_step

    # center：整组围绕正文范围中心（布局盒居中；墨水中点受字形左偏移影响有几 px 漂移）。
    group_mid = (center[0][1] + center[-1][1] + center[-1][2]) / 2
    assert group_mid == pytest.approx(100 + target / 2, abs=8)


def test_ruby_alignment_auto_matches_n3_rules(qapp):
    style = Style()  # 默认 auto
    assert _resolve_ruby_alignment(style, "星", "ほし") == "equal_space"
    assert _resolve_ruby_alignment(style, "STAR", "すたー") == "center"
    assert _resolve_ruby_alignment(style, "星", "hoshi") == "center"
    assert (
        _resolve_ruby_alignment(Style(ruby_alignment="center"), "星", "ほし")
        == "center"
    )
    assert (
        _resolve_ruby_alignment(Style(ruby_alignment="equal_space"), "STAR", "SUTA")
        == "equal_space"
    )


def test_style_dict_roundtrip_keeps_n3_layout_fields():
    style = Style(
        line_gap_px=-30,
        letter_spacing_px=-8,
        ruby_gap_px=-4,
        ruby_interval_px=-6,
        ruby_alignment="center",
    )
    restored = style_from_dict(style_to_dict(style))

    assert restored.line_gap_px == -30
    assert restored.letter_spacing_px == -8
    assert restored.ruby_gap_px == -4
    assert restored.ruby_interval_px == -6
    assert restored.ruby_alignment == "center"
    # 非法值回退默认
    assert style_from_dict({"ruby_alignment": "bogus"}).ruby_alignment == "auto"


def test_style_dict_roundtrip_keeps_glow_concentration_levels():
    style = Style(
        glow_concentration_level=2,
        ruby_glow_concentration_level=1,
        custom_style_schemes={
            "B": SubtitleStyleScheme(
                glow_concentration_level=1,
                ruby_glow_concentration_level=2,
            )
        },
        title_overlay=TitleOverlay(glow_concentration_level=2),
    )

    restored = style_from_dict(style_to_dict(style))

    assert restored.glow_concentration_level == 2
    assert restored.ruby_glow_concentration_level == 1
    assert restored.custom_style_schemes["B"].glow_concentration_level == 1
    assert restored.custom_style_schemes["B"].ruby_glow_concentration_level == 2
    assert restored.title_overlay is not None
    assert restored.title_overlay.glow_concentration_level == 2


def test_glow_concentration_payloads_are_clamped():
    assert style_from_dict({"glow_concentration_level": -1}).glow_concentration_level == 0
    assert style_from_dict({"glow_concentration_level": 8}).glow_concentration_level == 2
    assert style_from_dict({}).ruby_glow_concentration_level is None
    restored = style_from_dict(
        {
            "ruby_glow_concentration_level": 9,
            "custom_style_schemes": {
                "B": {
                    "glow_concentration_level": -2,
                    "ruby_glow_concentration_level": 5,
                }
            },
            "title_overlay": {"glow_concentration_level": 99},
        }
    )
    assert restored.ruby_glow_concentration_level == 2
    assert restored.custom_style_schemes["B"].glow_concentration_level == 0
    assert restored.custom_style_schemes["B"].ruby_glow_concentration_level == 2
    assert restored.title_overlay is not None
    assert restored.title_overlay.glow_concentration_level == 2


def _margin_track(text: str) -> TimingTrack:
    chars = [TimingChar(text=ch, start_ms=index * 500) for index, ch in enumerate(text)]
    return TimingTrack(lines=[TimingLine(chars=chars, end_ms=len(text) * 500)])


def _measured_line_width(style: Style, track: TimingTrack) -> int:
    line = track.lines[0]
    font = _build_font(style)
    metrics = QFontMetrics(font)
    latin_font = _build_latin_font(style)
    font_for = _make_font_for(style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    widths = [
        _char_layout_width(c.text, font, metrics, latin_metrics, font_for, style)
        for c in line.chars
    ]
    return max(
        int(round(_line_text_width(widths, style) + _visual_text_padding(style) * 2)), 1
    )


def test_main_latin_font_uses_independent_size_and_weight(qapp):
    style = Style(
        font_family="Yu Gothic UI",
        font_family_latin="Arial",
        font_size_px=52,
        font_weight=400,
        latin_font_size_px=68,
        latin_font_weight=700,
    )
    japanese = _build_font(style)
    latin = _build_latin_font(style)
    font_for = _make_font_for(style, japanese, latin)

    assert latin.pixelSize() == 68
    assert int(latin.weight()) == 700
    assert font_for is not None
    assert font_for("A") == latin
    assert font_for("あ") == japanese


def test_check_layout_margins_flags_overflow(qapp):
    track = _margin_track("あいうえおかきくけこ")
    style = Style()
    total_w = _measured_line_width(style, track)

    warnings = check_layout_margins(track, style, total_w - 10)

    assert warnings
    assert warnings[0].level == "overflow"
    assert warnings[0].line_index == 0


def test_check_layout_margins_flags_margin_intrusion(qapp):
    track = _margin_track("あいうえおかきくけこ")
    style = Style()
    total_w = _measured_line_width(style, track)

    # 画面比行宽 60 px：行放得下但左右余白（默认 50）无法同时确保。
    warnings = check_layout_margins(track, style, total_w + 60)

    assert warnings
    assert all(w.level == "margin" for w in warnings)


def test_check_layout_margins_ok_when_screen_is_wide(qapp):
    track = _margin_track("あい")
    warnings = check_layout_margins(track, Style(), 1920)
    assert warnings == []


def test_check_layout_margins_skips_vertical_mode(qapp):
    track = _margin_track("あいうえおかきくけこ")
    assert check_layout_margins(track, Style(vertical=True), 200) == []


# ---------------------------------------------------------------------------
# P2：SmartHorizon（N3 スマート水平配置）
# ---------------------------------------------------------------------------

from krok_helper.subtitle_render.engine.painter import (  # noqa: E402
    _line_center_override,
    _line_total_width,
    _resolve_line_x_smart,
)


def _continuous_track(texts: list[str]) -> TimingTrack:
    """无间隙连续演唱的多行 track。"""
    lines = []
    t = 0
    for text in texts:
        chars = [
            TimingChar(text=ch, start_ms=t + index * 300)
            for index, ch in enumerate(text)
        ]
        lines.append(TimingLine(chars=chars, end_ms=t + len(text) * 300))
        t += len(text) * 300
    return TimingTrack(lines=lines)


def test_smart_equal_margins_shifts_short_pair_toward_center(qapp):
    track = _continuous_track(["あい", "うえ", "おか", "きく"])
    style = Style()  # 默认 equal_margins
    img_w = 1920
    line0, line1 = track.lines[0], track.lines[1]
    w0 = _line_total_width(line0, style)
    w1 = _line_total_width(line1, style)
    slack = img_w - 50 - 50 - w0 - w1 + style.font_size_px
    assert slack > 0

    x0_plain = _resolve_line_x(img_w, w0, style, 0, center_override=False)
    x1_plain = _resolve_line_x(img_w, w1, style, 1, center_override=False)
    x0_smart = _resolve_line_x_smart(img_w, w0, track, line0, style, 0)
    x1_smart = _resolve_line_x_smart(img_w, w1, track, line1, style, 1)

    assert x0_smart == x0_plain + slack // 2
    assert x1_smart == x1_plain - slack // 2


def test_smart_equal_margins_skips_pair_without_slack(qapp):
    track = _continuous_track(["あ" * 12, "い" * 12, "う" * 2, "え" * 2])
    style = Style()
    img_w = 1920
    line0 = track.lines[0]
    w0 = _line_total_width(line0, style)

    x0_smart = _resolve_line_x_smart(img_w, w0, track, line0, style, 0)

    assert x0_smart == _resolve_line_x(img_w, w0, style, 0, center_override=False)


def test_smart_none_keeps_margin_anchor_and_single_page_centering_off(qapp):
    track = _continuous_track(["あい", "うえ", "おか", "きく"])
    style = Style(smart_horizontal="none")
    img_w = 1920
    line0 = track.lines[0]
    w0 = _line_total_width(line0, style)

    assert _resolve_line_x_smart(img_w, w0, track, line0, style, 0) == 50
    # 4 行按两行一页分成两页，末行不是单行页，任何模式都不得强制居中。
    last = track.lines[-1]
    assert _line_center_override(track, last, style) is False
    assert _line_center_override(track, last, Style()) is False


def test_two_line_page_last_line_is_not_centered_after_large_gap(qapp):
    """Marginality 回归：ParagraphBreak 前的第二行仍属于两行页。"""
    first = TimingLine(
        chars=[TimingChar(text="人", start_ms=9_670)], end_ms=16_480
    )
    second = TimingLine(
        chars=[TimingChar(text="答", start_ms=16_670)], end_ms=23_210
    )
    later = TimingLine(
        chars=[TimingChar(text="存", start_ms=32_220)], end_ms=41_940
    )
    track = TimingTrack(lines=[first, second, later])

    assert _line_center_override(track, second, Style()) is False
    assert _line_center_override(track, later, Style()) is True


def test_explicit_n3_breaks_make_middle_line_a_centered_single_page(qapp):
    """Marginality 回归：「真っ白な頁…」前后有显式 break，独占一页。"""
    lines = [
        TimingLine(chars=[TimingChar(text="感", start_ms=55_020)], end_ms=63_040),
        TimingLine(chars=[TimingChar(text="哀", start_ms=63_720)], end_ms=66_940),
        TimingLine(chars=[TimingChar(text="真", start_ms=67_610)], end_ms=72_940),
        TimingLine(chars=[TimingChar(text="わ", start_ms=76_180)], end_ms=81_560),
    ]
    lines[2].break_before = "page"
    lines[3].break_before = "paragraph"
    track = TimingTrack(lines=lines)

    assert _line_center_override(track, lines[1], Style()) is False
    assert _line_center_override(track, lines[2], Style()) is True


def test_smart_center_position_moves_short_lines_near_center(qapp):
    track = _continuous_track(["あい", "うえ", "おか", "きく"])
    style = Style(smart_horizontal="center_position")
    img_w = 1920
    font = style.font_size_px
    line0, line1 = track.lines[0], track.lines[1]
    w0 = _line_total_width(line0, style)
    w1 = _line_total_width(line1, style)

    # 短左行：从中心附近开始；短右行：在中心附近结束。
    assert (
        _resolve_line_x_smart(img_w, w0, track, line0, style, 0)
        == img_w // 2 + font // 2 - w0
    )
    assert (
        _resolve_line_x_smart(img_w, w1, track, line1, style, 1)
        == img_w // 2 - font // 2
    )


def test_smart_center_position_keeps_long_line_at_margin(qapp):
    track = _continuous_track(["あ" * 18, "い" * 18, "う" * 2, "え" * 2])
    style = Style(smart_horizontal="center_position")
    img_w = 1920
    line0 = track.lines[0]
    w0 = _line_total_width(line0, style)
    assert img_w // 2 + style.font_size_px // 2 - w0 <= 50  # 阈值不满足

    assert _resolve_line_x_smart(img_w, w0, track, line0, style, 0) == 50


def test_smart_single_page_line_is_centered(qapp):
    track = _continuous_track(["あい", "うえ", "おか"])  # 第 3 行无配对行
    style = Style()
    img_w = 1920
    last = track.lines[-1]
    w = _line_total_width(last, style)

    x = _resolve_line_x_smart(img_w, w, track, last, style, 0)

    assert x == (img_w - w) // 2


def test_style_dict_roundtrip_keeps_smart_horizontal():
    restored = style_from_dict(style_to_dict(Style(smart_horizontal="center_position")))
    assert restored.smart_horizontal == "center_position"
    assert style_from_dict({"smart_horizontal": "bogus"}).smart_horizontal == "equal_margins"


# ---------------------------------------------------------------------------
# P3：任意行数布局列表（N3 行ごとの左右レイアウト）
# ---------------------------------------------------------------------------

from krok_helper.subtitle_render.engine.painter import (  # noqa: E402
    _lane_count,
    _lane_alignment,
)
from krok_helper.subtitle_render.engine.timeline import compute_display_lines  # noqa: E402


def test_lane_count_follows_line_alignments_length(qapp):
    assert _lane_count(Style()) == 2
    assert _lane_count(Style(line_alignments=["left", "center", "right"])) == 3
    assert _lane_count(Style(dual_line_layout=False)) == 1


def test_lane_alignment_maps_rows_and_clamps_overflow(qapp):
    style = Style(line_alignments=["left", "center", "right"])
    assert _lane_alignment(style, 0) == "left"
    assert _lane_alignment(style, 1) == "center"
    assert _lane_alignment(style, 2) == "right"
    assert _lane_alignment(style, 5) == "right"  # 越界沿用端项


def test_timeline_rotates_three_lanes(qapp):
    track = _continuous_track(["あい", "うえ", "おか", "きく", "けこ", "さし"])
    display = compute_display_lines(
        track,
        lead_in_ms=0,
        tail_ms=0,
        lane_gap_ms=0,
        max_hold_ms=0,
        continuity_snap_ms=0,
        lane_count=3,
    )
    assert [item.lane for item in display] == [0, 1, 2, 0, 1, 2]


def test_three_lane_baselines_stack_from_bottom(qapp):
    style = Style(line_alignments=["left", "center", "right"])
    track = TimingTrack()
    baselines = _resolve_display_baselines(1080, track, [], style)

    assert set(baselines) == {0, 1, 2}
    dual = _resolve_display_baselines(1080, track, [], Style())
    # bottom 锚定：最下行（末 lane）位置一致，其余向上等距堆叠
    assert baselines[2] == dual[1]
    step = baselines[2] - baselines[1]
    assert step == baselines[1] - baselines[0]
    assert step > 0


def test_resolve_line_x_uses_alignment_list_and_single_margin(qapp):
    style = Style(
        line_alignments=["left", "center", "right"],
        horizontal_margin_px=64,
        smart_horizontal="none",
    )
    img_w, total_w = 1920, 400
    assert _resolve_line_x(img_w, total_w, style, 0) == 64
    assert _resolve_line_x(img_w, total_w, style, 1) == (img_w - total_w) // 2
    assert _resolve_line_x(img_w, total_w, style, 2) == img_w - 64 - total_w


def test_smart_equal_margins_three_lane_page_uses_max_widths(qapp):
    texts = ["あい", "うえお", "かき", "くけ", "こさ", "しす"]
    track = _continuous_track(texts)
    style = Style(line_alignments=["left", "center", "right"])
    img_w = 1920
    line0, line1, line2 = track.lines[0], track.lines[1], track.lines[2]
    w0 = _line_total_width(line0, style)
    w1 = _line_total_width(line1, style)
    w2 = _line_total_width(line2, style)
    slack = img_w - 50 * 2 - w0 - w1 - w2 + style.font_size_px
    assert slack > 0

    x0 = _resolve_line_x_smart(img_w, w0, track, line0, style, 0)
    x1 = _resolve_line_x_smart(img_w, w1, track, line1, style, 1)
    x2 = _resolve_line_x_smart(img_w, w2, track, line2, style, 2)

    assert x0 == 50 + slack // 2  # Left 行右移
    assert x1 == (img_w - w1) // 2  # Center 行不动
    assert x2 == img_w - 50 - w2 - slack // 2  # Right 行左移


def test_style_dict_roundtrip_keeps_line_alignments_and_margin():
    style = Style(
        line_alignments=["center", "left", "right"],
        horizontal_margin_px=72,
    )
    restored = style_from_dict(style_to_dict(style))
    assert restored.line_alignments == ["center", "left", "right"]
    assert restored.horizontal_margin_px == 72
    # 非法项回退 left；空列表回退默认
    assert style_from_dict({"line_alignments": ["left", "bogus"]}).line_alignments == [
        "left",
        "left",
    ]
    assert style_from_dict({"line_alignments": []}).line_alignments == ["left", "right"]


def test_style_dict_migrates_legacy_margin_when_new_key_missing():
    payload = style_to_dict(Style())
    del payload["horizontal_margin_px"]
    payload["upper_line_left_margin_px"] = 80
    restored = style_from_dict(payload)
    assert restored.horizontal_margin_px == 80


# ---------------------------------------------------------------------------
# P4：多布局定义 + 页级应用 + 自动选择器 + SizeAndRatio
# ---------------------------------------------------------------------------

from krok_helper.subtitle_render.engine.painter import (  # noqa: E402
    _layout_style_for_line,
    apply_layout_to_page,
    assign_layout_to_all,
    auto_assign_layouts_by_page,
)
from krok_helper.subtitle_render.engine.timeline import assign_lanes  # noqa: E402
from krok_helper.subtitle_render.models import (  # noqa: E402
    LyricsLayout,
    rescale_layout_sizes,
)


def _three_row_layout(name: str = "三行") -> LyricsLayout:
    return LyricsLayout(
        name=name,
        line_y_position="top",
        line_y_margin_px=40,
        line_gap_px=30,
        horizontal_margin_px=60,
        line_alignments=["left", "center", "right"],
    )


def test_layout_style_for_line_applies_referenced_layout(qapp):
    style = Style(layouts=[_three_row_layout()])
    line = TimingLine(chars=[TimingChar(text="あ", start_ms=0)], end_ms=500)

    assert _layout_style_for_line(style, line) is style  # index 0 = 默认布局

    line.layout_index = 1
    effective = _layout_style_for_line(style, line)
    assert effective.line_y_position == "top"
    assert effective.line_alignments == ["left", "center", "right"]
    assert effective.horizontal_margin_px == 60
    assert effective.layouts == style.layouts  # 布局列表保留，可继续解析同页其他行

    line.layout_index = 9  # 越界 → 回默认
    assert _layout_style_for_line(style, line) is style


def test_style_for_line_applies_animation_override_after_other_line_styles(qapp):
    style = Style(entry_anim="fade", entry_lead_ms=900, exit_anim="fade", exit_fade_ms=800)
    line = TimingLine(
        chars=[TimingChar("歌", 1000)],
        end_ms=2000,
        animation_override=LineAnimationOverride(
            entry_anim="slide_in",
            entry_duration_ms=450,
            exit_anim="none",
            exit_duration_ms=0,
        ),
    )

    effective = _style_for_line(style, line)

    assert effective.entry_anim == "slide_in"
    assert effective.entry_lead_ms == 450
    assert effective.exit_anim == "none"
    assert effective.exit_fade_ms == 0


def test_apply_layout_to_page_links_whole_page(qapp):
    track = _continuous_track(["あい", "うえ", "おか", "きく"])
    style = Style(layouts=[_three_row_layout()])

    affected = apply_layout_to_page(track, style, 1, 1)  # 第 2 行 → 页 (0,1)

    assert affected == [0, 1]
    assert [line.layout_index for line in track.lines] == [1, 1, 0, 0]


def test_apply_layout_pages_follow_page_head_row_count(qapp):
    # 前两行已用三行布局 → 第一页覆盖 0..2，后续页从第 3 行重新开始
    track = _continuous_track(["あい", "うえ", "おか", "きく", "けこ"])
    style = Style(layouts=[_three_row_layout()])
    track.lines[0].layout_index = 1

    render_lines = [l for l in track.lines if not l.is_blank and l.chars]
    lanes, page_starts, _rows = assign_lanes(
        render_lines,
        2,
        lambda line: len(_layout_style_for_line(style, line).line_alignments),
    )
    assert lanes == [0, 1, 2, 0, 1]
    assert page_starts == [0, 0, 0, 3, 3]


def test_assign_layout_to_all_and_auto_assign(qapp):
    # 两页：显式 break 前 3 行 + 后 2 行。
    lines = []
    t = 0
    for count, texts in ((3, ["あい", "うえ", "おか"]), (2, ["きく", "けこ"])):
        for text in texts:
            chars = [
                TimingChar(text=ch, start_ms=t + i * 300) for i, ch in enumerate(text)
            ]
            lines.append(TimingLine(chars=chars, end_ms=t + len(text) * 300))
            t += len(text) * 300
        lines.append(TimingLine(is_blank=True))
        t += 10_000
    track = TimingTrack(lines=lines)
    renderable = [l for l in track.lines if not l.is_blank and l.chars]
    renderable[3].break_before = "paragraph"
    style = Style(layouts=[_three_row_layout()])

    assert auto_assign_layouts_by_page(track, style) is True
    # 3 行页命中三行布局（index 1），2 行页命中默认布局（行数 2）
    assert [l.layout_index for l in renderable] == [1, 1, 1, 0, 0]

    assert assign_layout_to_all(track, 1) is True
    assert all(l.layout_index == 1 for l in renderable)
    assert assign_layout_to_all(track, 1) is False  # 无变化


def test_style_dict_roundtrip_keeps_layout_definitions():
    style = Style(layouts=[_three_row_layout("下寄せ3行")])
    restored = style_from_dict(style_to_dict(style))

    assert len(restored.layouts) == 1
    layout = restored.layouts[0]
    assert layout.name == "下寄せ3行"
    assert layout.line_y_position == "top"
    assert layout.line_alignments == ["left", "center", "right"]
    assert layout.horizontal_margin_px == 60
    # 非法 payload 防御
    assert style_from_dict({"layouts": "bogus"}).layouts == []


def test_rescale_layout_sizes_matches_n3_size_and_ratio():
    style = Style(
        line_y_margin_px=80,
        line_gap_px=90,
        horizontal_margin_px=50,
        layout_reference_height=1080,
        layouts=[_three_row_layout()],
    )
    scaled = rescale_layout_sizes(style, 720)

    assert scaled.layout_reference_height == 720
    assert scaled.line_y_margin_px == int(720 * 80 / 1080)
    assert scaled.line_gap_px == int(720 * 90 / 1080)
    assert scaled.horizontal_margin_px == int(720 * 50 / 1080)
    assert scaled.upper_line_left_margin_px == scaled.horizontal_margin_px
    layout = scaled.layouts[0]
    assert layout.line_y_margin_px == int(720 * 40 / 1080)
    assert layout.line_gap_px == int(720 * 30 / 1080)
    assert layout.horizontal_margin_px == int(720 * 60 / 1080)
    # 高度不变 → 原对象返回；0 保持 0
    assert rescale_layout_sizes(scaled, 720) is scaled
    zero = rescale_layout_sizes(replace(style, line_gap_px=0), 720)
    assert zero.line_gap_px == 0


# ---------------------------------------------------------------------------
# 像素级规则：相邻 ruby 避让（N3 无条件）+ 行缘 ruby 溢出
# ---------------------------------------------------------------------------

from krok_helper.subtitle_render.engine.painter import (  # noqa: E402
    _char_layout_width,
    _ruby_char_gaps,
    _ruby_interval_px,
    _ruby_layout_left_offset,
)


def _wide_ruby_line() -> tuple[TimingLine, list[RubyAnnotation]]:
    line = TimingLine(
        chars=[TimingChar(text="星", start_ms=0), TimingChar(text="空", start_ms=500)],
        end_ms=1000,
    )
    rubies = [
        RubyAnnotation(kanji="星", reading="きらきらぼし", pos_start_ms=0, pos_end_ms=500),
        RubyAnnotation(kanji="空", reading="おおぞらさま", pos_start_ms=500, pos_end_ms=1000),
    ]
    return line, rubies


def _base_char_widths(line: TimingLine, style: Style) -> list[int]:
    font = _build_font(style)
    metrics = QFontMetrics(font)
    return [
        _char_layout_width(c.text, font, metrics, metrics, None, style)
        for c in line.chars
    ]


def test_adjacent_wide_rubies_insert_avoidance_gap(qapp):
    line, rubies = _wide_ruby_line()
    style = Style()
    widths = _base_char_widths(line, style)

    gaps, left_ext, right_ext = _ruby_char_gaps(line, widths, rubies, style)

    assert gaps[0] == 0
    assert gaps[1] > 0  # 第二条 ruby 首字符前插入推移
    assert left_ext > 0  # 行首 ruby 左溢出
    assert right_ext > 0  # 行末 ruby 右溢出

    # 推移后两条 ruby 的排布缘间距 >= RubyInterval
    ruby_metrics = QFontMetrics(_build_ruby_font(style))
    r0_left = 0 + _ruby_layout_left_offset(
        rubies[0].reading, ruby_metrics, widths[0], style, rubies[0].kanji
    )
    r0_right = r0_left + _ruby_layout_width(
        rubies[0].reading, ruby_metrics, widths[0], style, rubies[0].kanji
    )
    span1_left = widths[0] + gaps[1]
    r1_left = span1_left + _ruby_layout_left_offset(
        rubies[1].reading, ruby_metrics, widths[1], style, rubies[1].kanji
    )
    assert r1_left - r0_right >= _ruby_interval_px(style) - 1e-6


def test_ruby_gaps_zero_without_collision(qapp):
    line = TimingLine(
        chars=[TimingChar(text="星", start_ms=0), TimingChar(text="空", start_ms=500)],
        end_ms=1000,
    )
    rubies = [
        RubyAnnotation(kanji="星", reading="ほし", pos_start_ms=0, pos_end_ms=500),
    ]
    style = Style()
    gaps, _left, _right = _ruby_char_gaps(line, _base_char_widths(line, style), rubies, style)
    assert gaps == [0, 0]


def test_ruby_gaps_skipped_in_vertical_and_rtl(qapp):
    line, rubies = _wide_ruby_line()
    for mode_style in (Style(vertical=True), Style(right_to_left=True)):
        widths = _base_char_widths(line, mode_style)
        gaps, left_ext, right_ext = _ruby_char_gaps(line, widths, rubies, mode_style)
        assert gaps == [0, 0]
        assert (left_ext, right_ext) == (0, 0)


def test_line_total_width_includes_ruby_push_and_overhang(qapp):
    line, rubies = _wide_ruby_line()
    style = Style()
    assert _line_total_width(line, style, rubies) > _line_total_width(line, style)


def test_layout_line_applies_ruby_gaps_to_boxes_and_glyphs(qapp):
    line, rubies = _wide_ruby_line()
    track = TimingTrack(lines=[line], rubies=rubies)
    style = Style(dual_line_layout=False)

    layout = _layout_line(track, line, style, 1920, 1080)

    # 字符盒之间出现避让间隙，且 glyph 位置与 char_lefts 同源一致
    assert layout.char_x_ranges[1][0] - layout.char_x_ranges[0][1] > 0
    assert layout.text_layout.glyphs[1].left == layout.char_lefts[1]
    # 行盒（含 ruby 溢出）不越出画面（1920 足够宽时整体居中）
    assert layout.x0 > 0


def test_effective_ruby_clamps_but_never_stretches_wipe_clock(qapp):
    """ruby 走字时钟对齐基字区间只收窄不拉长（诉求：唱完即完整变色）。"""
    intervals = [(0, 1000), (1000, 2000)]  # 基字区间末端 = 下一字开始

    # ruby 自身 500 结束：不能被拉长到基字区间末（1000）
    ruby = RubyAnnotation(kanji="星", reading="ほし", pos_start_ms=0, pos_end_ms=500)
    effective = _effective_ruby_for_target(ruby, [0], intervals)
    assert effective.pos_end_ms == 500
    assert _ruby_progress_ratio(effective, 700) == 1.0

    # 收窄方向保留（呼吸停顿场景：导出的 pos 区间比基字区间长）
    wide = RubyAnnotation(kanji="星", reading="ほし", pos_start_ms=0, pos_end_ms=1500)
    shrunk = _effective_ruby_for_target(wide, [0], intervals)
    assert shrunk.pos_end_ms == 1000


def test_whitespace_glyphs_do_not_inflate_line_metrics(qapp, monkeypatch):
    """空白字符不参与行高：半角空格走英数字体时不得把注音基线顶高（N3 按墨水求行盒）。"""
    import krok_helper.subtitle_render.engine.painter as painter_mod

    real_metrics = QFontMetrics

    class TallLatinMetrics:
        """给英数字体伪造超高 metrics，模拟 Comic Sans 之类的字体。"""

        def __init__(self, font):
            self._metrics = real_metrics(font)
            self._tall = font.family() == "FakeTallLatin"

        def ascent(self):
            return self._metrics.ascent() + (40 if self._tall else 0)

        def descent(self):
            return self._metrics.descent() + (20 if self._tall else 0)

        def __getattr__(self, name):
            return getattr(self._metrics, name)

    monkeypatch.setattr(painter_mod, "QFontMetrics", TallLatinMetrics)
    style = replace(Style(), font_family_latin="FakeTallLatin")

    def build(chars: list[str]):
        line = TimingLine(
            chars=[TimingChar(text=ch, start_ms=1000 + i * 500) for i, ch in enumerate(chars)],
            end_ms=5000,
        )
        return painter_mod._build_text_layout(
            line, style, x0=0, baseline_y=0, inline_styles=True
        )

    with_space = build(["あ", " ", "い"])
    without_space = build(["あ", "い"])
    assert with_space.ascent == without_space.ascent
    assert with_space.descent == without_space.descent

    # 整行空白：行高退回首字符 metrics，不为 0
    all_blank = build([" ", " "])
    assert all_blank.ascent > 0


def test_paint_frame_renders_extra_tracks(qapp):
    """副字幕源（N3 多歌词文件）与主轨同帧叠绘：副轨行出现在主轨行之外的区域。"""
    main = _track()
    # 和声行放在主轨结束很久之后，该时刻只有副轨可见 → 有墨水即证明副轨被绘制。
    chorus_line = TimingLine(
        chars=[
            TimingChar(text="ラ", start_ms=60000),
            TimingChar(text="ラ", start_ms=60500),
        ],
        end_ms=61500,
    )
    chorus = TimingTrack(lines=[chorus_line])
    # 和声轨用独立布局（顶部居中），与主轨（底部）不重叠
    style = replace(
        Style(),
        layouts=[
            LyricsLayout(
                name="コーラス",
                line_y_position="top",
                line_y_margin_px=40,
                line_alignments=["center"],
            )
        ],
    )
    chorus_line.layout_index = 1

    bg = QColor("#101010")

    def render(track, extras, t_ms):
        img = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(bg)
        paint_frame(img, track, t_ms, style, extras)
        return _bounds_size(_ink_bounds(img, bg))

    # 主轨早已唱完的时刻：无副轨 → 空帧；带副轨 → 画出和声行
    assert render(main, [], 60800) == (0, 0)
    assert render(main, [chorus], 60800) != (0, 0)
    # 只有副轨时（主轨为 None）也能渲染
    assert render(None, [chorus], 60800) != (0, 0)
    # frame_has_content 与绘制口径一致
    assert not frame_has_content(main, 60800, style)
    assert frame_has_content(main, 60800, style, [chorus])


def test_display_windows_for_style_maps_line_indices_and_overrides():
    from krok_helper.subtitle_render.engine.painter import display_windows_for_style
    from krok_helper.subtitle_render.models import Style, TimingChar, TimingLine, TimingTrack

    line1 = TimingLine(chars=[TimingChar("あ", 5000)], end_ms=6000)
    blank = TimingLine(is_blank=True)
    line2 = TimingLine(chars=[TimingChar("い", 20000)], end_ms=21000)
    line2.display_end_override_ms = 30000
    track = TimingTrack(lines=[line1, blank, line2])
    style = replace(Style(), line_lead_in_ms=1000, line_tail_ms=500)

    windows = display_windows_for_style(track, style)

    # key = track.lines 索引；空行不产生窗口
    assert set(windows.keys()) == {0, 2}
    assert windows[0][0] == 4000  # 5000 - 提前入场 1000
    assert windows[2][1] == 30000  # 消失时刻手动覆盖

    # 单行模式同样生效
    single = replace(style, dual_line_layout=False)
    windows_single = display_windows_for_style(track, single)
    assert set(windows_single.keys()) == {0, 2}
    assert windows_single[0] == (4000, 6500)
    assert windows_single[2][1] == 30000
