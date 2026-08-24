"""Tests for ``krok_helper.subtitle_render.engine.painter``.

像素级断言不可移植（字形 / 字体可用性平台差异大），所以本测试聚焦：

- 函数能在不同时刻正常完成不抛
- 各阶段（未唱 / 半唱 / 全唱）画面像素与"完全空白"对比都有差异
- 空 track 不画任何东西
"""

from __future__ import annotations

import os
import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, QRectF  # noqa: E402
from PyQt6.QtGui import (  # noqa: E402
    QColor,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QTransform,
)
from PyQt6.QtWidgets import QApplication  # noqa: E402

import krok_helper.subtitle_render.engine.painter as subtitle_painter  # noqa: E402
import krok_helper.subtitle_render.engine.raster_blur as raster_blur  # noqa: E402
import krok_helper.subtitle_render.engine.ruby.style as ruby_style  # noqa: E402
import krok_helper.subtitle_render.engine.ruby.timing as ruby_timing  # noqa: E402
import krok_helper.subtitle_render.engine.text_metrics as text_metrics  # noqa: E402
from krok_helper.subtitle_render.engine.page_placement import (  # noqa: E402
    LineVisualBand,
)
from krok_helper.subtitle_render.engine.painter import (  # noqa: E402
    _HARD_BAND_BRUSH_CACHE,
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
    _linear_gradient_brush,
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
    _paint_text_layer_stack,
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
    _utopia_wipe_window_for_index,
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
    _title_show_window,
    frame_has_content,
    paint_frame,
    frame_vertical_bounds,
    clear_before_layer_cache,
    _TEXT_RUN_LAYER_CACHE,
    _RUN_GLOW_CACHE,
)
from krok_helper.subtitle_render.engine.layers import (  # noqa: E402
    BakedLayer,
    LayerCompositor,
    LayerContext,
    SCOPE_GROUP,
)
from krok_helper.subtitle_render.engine.timeline import DisplayLine  # noqa: E402
from krok_helper.subtitle_render.models import (  # noqa: E402
    GuideSymbol,
    KaraokeColors,
    KaraokeColorState,
    LyricsLayout,
    LineAnimationOverride,
    PaintFill,
    RubyAnnotation,
    SubtitleStyleScheme,
    Style,
    TITLE_SCHEME_NAME,
    TimingChar,
    TimingLine,
    TimingTrack,
    TimingTrackMeta,
    TrackPage,
    TrackPagePlan,
    TrackSection,
    TitleOverlay,
)
from krok_helper.subtitle_render.subtitle_sources import parse_nicokara_lrc  # noqa: E402


def test_painter_keeps_ruby_timing_compatibility_exports() -> None:
    names = (
        "_main_text_ruby_progress_ratio",
        "_main_text_ruby_progress_time_at_ratio",
        "_reading_unit_progress_ratio",
        "_ruby_main_text_slot_times",
        "_ruby_progress_parts_and_intervals",
        "_ruby_progress_ratio",
        "_ruby_progress_time_at_ratio",
        "_ruby_reading_boundaries",
        "_ruby_reading_intervals",
        "_ruby_reading_intervals_with_pauses",
        "_ruby_reading_unit_progress_points",
        "_ruby_reading_units",
        "_ruby_utopia_reading_units_and_intervals",
        "_ruby_utopia_visual_units",
        "_ruby_visual_units_and_intervals",
    )
    for name in names:
        assert getattr(subtitle_painter, name) is getattr(ruby_timing, name)


def test_painter_keeps_raster_blur_compatibility_exports() -> None:
    names = ("_blur_image", "_gaussian_blur_image", "_n3_gaussian_kernel_1d")
    for name in names:
        assert getattr(subtitle_painter, name) is getattr(raster_blur, name)


def test_track_layout_signature_includes_ruby_source_binding() -> None:
    """Equal text/timing on overlapping lines must not share a ruby layout."""

    first = RubyAnnotation(
        kanji="天",
        reading="てん",
        pos_start_ms=1000,
        pos_end_ms=1200,
        reading_parts=["てん"],
        target_line_index=0,
        target_char_start=0,
        target_char_end=1,
    )
    second = replace(first, target_line_index=1)
    first_track = TimingTrack(rubies=[first])
    second_track = TimingTrack(rubies=[second])

    assert subtitle_painter._track_layout_signature(first_track) != (
        subtitle_painter._track_layout_signature(second_track)
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


def _assert_blue_pixels_in(
    img: QImage,
    *,
    left: int,
    right: int,
    top: int = 0,
    bottom: int | None = None,
) -> None:
    bottom = img.height() - 1 if bottom is None else bottom
    for y in range(max(top, 0), min(bottom, img.height() - 1) + 1):
        for x in range(max(left, 0), min(right, img.width() - 1) + 1):
            color = QColor(img.pixel(x, y))
            if (
                color.blue() >= 160
                and color.blue() > color.red() + 40
                and color.blue() > color.green() + 40
            ):
                return
    pytest.fail("expected blue signal pixels in region")


def _assert_light_pixels_in(
    img: QImage,
    *,
    left: int,
    right: int,
    top: int = 0,
    bottom: int | None = None,
) -> None:
    bottom = img.height() - 1 if bottom is None else bottom
    for y in range(max(top, 0), min(bottom, img.height() - 1) + 1):
        for x in range(max(left, 0), min(right, img.width() - 1) + 1):
            color = QColor(img.pixel(x, y))
            if color.red() >= 180 and color.green() >= 180 and color.blue() >= 180:
                return
    pytest.fail("expected light text pixels in region")


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


def test_main_glyphs_use_script_specific_stroke_parameters(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="A", start_ms=0),
            TimingChar(text="あ", start_ms=100),
            TimingChar(text="É", start_ms=200),
        ],
        end_ms=300,
    )
    style = Style(
        stroke_width_px=12,
        stroke2_enabled=True,
        stroke2_width_px=5,
        latin_stroke_width_px=7,
        latin_stroke2_enabled=False,
        latin_stroke2_width_px=9,
    )

    layout = subtitle_painter._build_text_layout(
        line,
        style,
        x0=0,
        baseline_y=100,
        inline_styles=False,
    )
    assert [glyph.style.stroke_width_px for glyph in layout.glyphs] == [7, 12, 7]
    assert [glyph.style.stroke2_width_px for glyph in layout.glyphs] == [0, 5, 0]
    assert style.latin_stroke2_width_px == 9


def test_emoji_graphemes_use_symbol_outline_font_and_have_distinct_paths(qapp):
    line = TimingLine(
        chars=[TimingChar("❄️", 0), TimingChar("🔯", 500)],
        end_ms=1_000,
    )
    layout = subtitle_painter._build_text_layout(
        line,
        Style(font_family="Microsoft YaHei UI", font_size_px=96),
        x0=0,
        baseline_y=120,
        inline_styles=False,
    )

    assert [glyph.font.family() for glyph in layout.glyphs] == [
        "Segoe UI Symbol",
        "Segoe UI Symbol",
    ]
    paths = [subtitle_painter._glyph_path(glyph, 120) for glyph in layout.glyphs]
    assert all(not path.isEmpty() for path in paths)
    assert paths[0].boundingRect() != paths[1].boundingRect()


def test_ruby_latin_strokes_resolve_independently():
    style = Style(
        ruby_stroke_width_px=6,
        ruby_stroke2_enabled=True,
        ruby_stroke2_width_px=3,
        ruby_latin_stroke_width_px=4,
        ruby_latin_stroke2_enabled=False,
        ruby_latin_stroke2_width_px=2,
    )

    japanese = subtitle_painter._ruby_script_stroke_style(style, "かな")
    latin = subtitle_painter._ruby_script_stroke_style(style, "ABC")

    assert subtitle_painter._ruby_stroke_width(japanese) == 6
    assert subtitle_painter._ruby_stroke2_width(japanese) == 3
    assert subtitle_painter._ruby_stroke_width(latin) == 4
    assert subtitle_painter._ruby_stroke2_width(latin) == 0


def test_ruby_stroke2_flag_and_width_inherit_along_separate_chains():
    """描边 2 的开关与宽度各自继承，开关只在最后裁剪一次。

    N3 的 ``FontFaceInfoModel`` 就是分别回退 ``UseEdge2`` 与 ``EdgeSize2`` 的
    （见 ``n3_font_fallback``），属性面板显示的继承宽度也没有被开关裁剪过。
    宽度回退一旦顺带套用上一级的开关，显式打开描边 2 的注音就只能拿到 0。
    """
    def style(**changes) -> Style:
        return Style(
            font_size_px=64, ruby_font_size_px=45, stroke2_width_px=5, **changes
        )

    # 未设定 = 跟随主文字：主文字关掉时，保存的注音宽度不能自己把描边 2 打开。
    assert subtitle_painter._ruby_stroke2_width(
        style(stroke2_enabled=False, ruby_stroke2_enabled=None, ruby_stroke2_width_px=3)
    ) == 0
    assert subtitle_painter._ruby_stroke2_width(
        style(stroke2_enabled=True, ruby_stroke2_enabled=None, ruby_stroke2_width_px=3)
    ) == 3
    # 注音显式打开、自身没有宽度 -> 继承主文字宽度按注音比例缩放，
    # 与主文字开关无关（round(5 * 45/64) == 4）。
    assert subtitle_painter._ruby_stroke2_width(
        style(stroke2_enabled=True, ruby_stroke2_enabled=True, ruby_stroke2_width_px=None)
    ) == 4
    assert subtitle_painter._ruby_stroke2_width(
        style(stroke2_enabled=False, ruby_stroke2_enabled=True, ruby_stroke2_width_px=None)
    ) == 4
    # 显式关闭恒为 0，保存的宽度不参与。
    assert subtitle_painter._ruby_stroke2_width(
        style(stroke2_enabled=True, ruby_stroke2_enabled=False, ruby_stroke2_width_px=3)
    ) == 0

    # 英数槽同理：显式打开而自身无宽度时，继承的宽度不能被注音/主文字那一级的
    # 开关归零，否则英数注音永远画不出描边 2。
    latin = subtitle_painter._ruby_script_stroke_style(
        style(
            stroke2_enabled=False,
            ruby_stroke2_enabled=None,
            ruby_stroke2_width_px=3,
            ruby_latin_stroke2_enabled=True,
            ruby_latin_stroke2_width_px=None,
        ),
        "ABC",
    )
    assert latin.ruby_stroke2_width_px == 3
    # 与主文字英数槽的既有行为保持一致（对照组，本来就是对的）。
    main_latin = subtitle_painter._main_script_stroke_style(
        Style(
            font_size_px=64,
            stroke2_enabled=False,
            stroke2_width_px=5,
            latin_stroke2_enabled=True,
            latin_stroke2_width_px=None,
        ),
        "ABC",
    )
    assert main_latin.stroke2_width_px == 5


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
    _assert_blue_pixels_in(
        img,
        left=int(layout.signal_x),
        right=int(layout.text_x) - 1,
    )
    _assert_light_pixels_in(
        img,
        left=int(layout.text_x),
        right=bounds[2],
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
    _assert_light_pixels_in(
        img,
        left=int(layout.text_x),
        right=bounds[2],
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
    _assert_blue_pixels_in(
        img,
        left=int(layout.signal_x),
        right=int(layout.text_x) - 1,
    )
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
    _assert_blue_pixels_in(
        img,
        left=int(layout.signal_x),
        right=int(layout.text_x) - 1,
    )
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


def _two_section_track() -> TimingTrack:
    """S1 = 两页（每页 2 行），S2 = 一页；行 0 / 4 是各段第一页第一行。"""

    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("あ", 10_000)], end_ms=11_000),
            TimingLine(chars=[TimingChar("い", 12_000)], end_ms=13_000),
            TimingLine(chars=[TimingChar("う", 14_000)], end_ms=15_000),
            TimingLine(chars=[TimingChar("え", 16_000)], end_ms=17_000),
            TimingLine(chars=[TimingChar("お", 30_000)], end_ms=31_000),
            TimingLine(chars=[TimingChar("か", 32_000)], end_ms=33_000),
        ]
    )
    track.page_plan = TrackPagePlan(
        sections=[
            TrackSection(pages=[TrackPage(2), TrackPage(2)]),
            TrackSection(pages=[TrackPage(2)]),
        ]
    )
    return track


def _volume_style(**overrides) -> Style:
    values = {
        "font_size_px": 20,
        "line_y_margin_px": 10,
        "dual_line_layout": True,
        "line_lead_in_ms": 200,
        "line_tail_ms": 100,
        "lit_enabled": True,
        "lit_shadow": False,
        "signals_duration_ms": 4_000,
    }
    values.update(overrides)
    return Style(**values)


def test_volume_signal_candidate_lines_limited_to_section_heads(qapp):
    track = _two_section_track()
    style = _volume_style()

    # 行 0（S1 第一页第一行）的信号窗口内：只有段首行成为信号候选。
    visible = subtitle_painter._signal_display_lines_for_style(track, 6_500, style)
    assert [item.line.chars[0].text for item in visible] == ["あ"]

    # 行 2（S1 第二页第一行，非段首）的信号窗口起点是 10_000：旧行为会让它
    # 提前出现，现在非段首行不进入信号窗口（段首行自己也已退场）。
    assert subtitle_painter._signal_display_lines_for_style(track, 13_600, style) == []

    # 行 4（S2 第一页第一行）恢复信号窗口。
    visible = subtitle_painter._signal_display_lines_for_style(track, 26_500, style)
    assert [item.line.chars[0].text for item in visible] == ["お"]


def _plan_display_starts(track: TimingTrack, style: Style) -> list[int]:
    plan = subtitle_painter.build_track_layout_plan(track, style)
    return [int(item.display_start_ms or 0) for item in plan.lines]


def test_volume_signal_lead_extends_only_section_head_pages(qapp):
    track = _two_section_track()
    style = _volume_style()
    off = _plan_display_starts(track, replace(style, lit_enabled=False))
    on = _plan_display_starts(track, style)

    # 段首行所在的页仍被信号窗口提前。
    assert on[0] < off[0]
    assert on[4] < off[4]
    # 非段首页（S1 第二页）不受音量柱影响：上屏时刻与关闭指示灯完全一致。
    assert on[2] == off[2]
    assert on[3] == off[3]


def test_shape_signal_lead_also_limited_to_section_head_pages(qapp):
    track = _two_section_track()
    style = _volume_style(lit_style="circle", lit_number=4, lit_size=10)
    off = _plan_display_starts(track, replace(style, lit_enabled=False))
    on = _plan_display_starts(track, style)

    # 形状灯（circle/square/rounded）与音量柱同一语义：只有段首行所在的页
    # 被信号窗口提前，非段首页与关闭指示灯完全一致。
    assert on[0] < off[0]
    assert on[4] < off[4]
    assert on[2] == off[2]
    assert on[3] == off[3]


def test_volume_signal_paints_section_head_before_entry_only(qapp):
    track = _two_section_track()
    style = _volume_style(dual_line_layout=False)

    # 行 1 退场后到行 2（非段首）正常入场前：旧行为会因信号窗口提前显示并画
    # 音量柱，现在整段（含闪烁相位）都为空帧。非段首行保留自己的 PreTime。
    for probe in range(13_200, 13_800, 100):
        img = _blank(160, 90)
        baseline = _pixel_hash(img)
        paint_frame(img, track, probe, style)
        assert _pixel_hash(img) == baseline, probe

    # 段首行（行 4）开唱前的信号窗口：闪烁周期里总有一帧亮出蓝色音量柱。
    lit_frame_found = False
    for probe in range(26_000, 27_600, 100):
        img2 = _blank(160, 90)
        paint_frame(img2, track, probe, style)
        try:
            _assert_blue_pixels_in(img2, left=0, right=159)
        except AssertionError:
            continue
        lit_frame_found = True
        break
    assert lit_frame_found


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
    # 行盒不再为描边留位（对齐 N3 DrawLineLeft/Right），右缘就是 text_x + total_w。
    union_mid = (layout.signal_x + (layout.text_x + layout.total_w)) / 2
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
    _assert_blue_pixels_in(
        img,
        left=int(layout.signal_x),
        right=int(layout.text_x) - 1,
    )


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
    _assert_blue_pixels_in(
        img,
        left=int(layout.signal_x),
        right=int(layout.signal_x) + style.lit_size,
    )
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
        glow_before_radius_px=12,
        glow_after_radius_px=12,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(shadow=PaintFill(color="#000000")),
            after=KaraokeColorState(shadow=PaintFill(color="#FF8800")),
        ),
    )


def test_after_glow_layer_clip_releases_line_edges_when_fully_sung(qapp):
    track = _track()
    line = track.lines[0]
    style = replace(
        _glow_after_style(), glow_before_radius_px=10, glow_after_radius_px=12
    )
    layout = _layout_line(track, line, style, 800, 450)
    assert layout is not None
    ctx = LayerContext(t_ms=0, logical_w=800, logical_h=450)

    def glow_clips(t_ms: int) -> list[QRectF | None]:
        layers = [
            layer
            for layer in _line_layer_stack(layout, t_ms)
            if isinstance(layer, _GlyphRunAfterGlowLayer)
        ]
        assert layers
        return [layer.animate(ctx, layer.layout(ctx)).clip_rect for layer in layers]

    # 走字途中：已唱字符的 glow 已完整释放，当前字符仍停在自己的扫光线。
    mid = next(clip for clip in glow_clips(1700) if clip is not None)
    active_segment = next(
        segment
        for segment in layout.fill_segments
        if 0.0 < subtitle_painter._segment_fill_ratio(segment, 1700) < 1.0
    )
    assert mid.left() < active_segment.left
    assert mid.right() < active_segment.right
    # 唱完：所有字符都不再裁剪，halo 不再被硬截。
    assert all(clip is None for clip in glow_clips(9000))


def test_after_glow_layer_is_dynamic_until_run_is_complete(qapp):
    track = _track()
    line = track.lines[0]
    style = replace(
        _glow_after_style(), glow_before_radius_px=10, glow_after_radius_px=12
    )
    layout = _layout_line(track, line, style, 800, 450)
    assert layout is not None
    ctx = LayerContext(t_ms=0, logical_w=800, logical_h=450)

    def glow_layers(t_ms: int) -> list[_GlyphRunAfterGlowLayer]:
        layers = [
            layer
            for layer in _line_layer_stack(layout, t_ms)
            if isinstance(layer, _GlyphRunAfterGlowLayer)
        ]
        assert layers
        return layers

    mid = next(
        layer
        for layer in glow_layers(1700)
        if layer.static_key(ctx, layer.layout(ctx)) is None
    )
    assert mid.static_key(ctx, mid.layout(ctx)) is None

    done = glow_layers(9000)
    assert all(layer.static_key(ctx, layer.layout(ctx)) is not None for layer in done)
    assert all(layer.animate(ctx, layer.layout(ctx)).clip_rect is None for layer in done)


def test_after_glow_dynamic_paints_clipped_source_before_blur(qapp, monkeypatch):
    track = _track()
    line = track.lines[0]
    style = replace(
        _glow_after_style(), glow_before_radius_px=10, glow_after_radius_px=12
    )
    layout = _layout_line(track, line, style, 800, 450)
    assert layout is not None
    layers = [
        item
        for item in _line_layer_stack(layout, 1700)
        if isinstance(item, _GlyphRunAfterGlowLayer)
    ]
    ctx = LayerContext(t_ms=1700, logical_w=800, logical_h=450)
    layer = next(
        item
        for item in layers
        if item.animate(ctx, item.layout(ctx)).clip_rect is not None
    )
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


def test_equal_radius_glow_combines_dynamic_front_and_reuses_full_cache(
    qapp, monkeypatch
):
    track = _track()
    line = track.lines[0]
    layout = _layout_line(track, line, _glow_after_style(), 800, 450)
    assert layout is not None
    stack = _line_layer_stack(layout, 1700)
    layers = [
        item
        for item in stack
        if isinstance(item, subtitle_painter._GlyphRunSplitGlowLayer)
    ]
    assert len(layers) == 1
    assert not any(isinstance(item, _GlyphRunAfterGlowLayer) for item in stack)

    split_calls = 0
    blit_states: list[bool] = []
    original_split = subtitle_painter._paint_split_glow_path
    original_blit = subtitle_painter._blit_cached_run_glow

    def _count_split(*args, **kwargs):
        nonlocal split_calls
        split_calls += 1
        assert kwargs.get("target_clip") is not None
        return original_split(*args, **kwargs)

    def _count_blit(*args, **kwargs):
        blit_states.append(bool(kwargs["after"]))
        return original_blit(*args, **kwargs)

    monkeypatch.setattr(subtitle_painter, "_paint_split_glow_path", _count_split)
    monkeypatch.setattr(subtitle_painter, "_blit_cached_run_glow", _count_blit)
    clear_before_layer_cache()
    image = _blank()
    painter = QPainter(image)
    try:
        layers[0].paint_dynamic(
            painter,
            LayerContext(t_ms=1700, logical_w=800, logical_h=450),
            layers[0],
        )
    finally:
        painter.end()

    assert split_calls == 1
    assert blit_states == [False, True]
    assert len(_RUN_GLOW_CACHE) == 2


def test_equal_radius_ruby_glow_reuses_full_cache_around_dynamic_front(
    qapp, monkeypatch
):
    track = _track_with_ruby()
    line = track.lines[0]
    style = replace(
        _glow_after_style(),
        font_size_px=64,
        ruby_font_size_px=30,
        ruby_decoration_kind="glow",
        ruby_glow_before_radius_px=12,
        ruby_glow_after_radius_px=12,
    )
    layout = _layout_line(track, line, style, 800, 450)
    assert layout is not None
    layers = subtitle_painter._ruby_glow_layers(
        list(layout.ruby_layouts),
        layout.ruby_font,
        layout.ruby_metrics,
        1500,
        style,
        layout.rtl,
    )
    assert len(layers) == 1
    assert isinstance(layers[0], subtitle_painter._RubySplitGlowLayer)

    split_calls = 0
    blit_states: list[bool] = []
    original_split = subtitle_painter._paint_split_glow_path
    original_blit = subtitle_painter._blit_cached_ruby_glow

    def _count_split(*args, **kwargs):
        nonlocal split_calls
        split_calls += 1
        assert kwargs.get("target_clip") is not None
        return original_split(*args, **kwargs)

    def _count_blit(*args, **kwargs):
        blit_states.append(bool(kwargs["after"]))
        return original_blit(*args, **kwargs)

    monkeypatch.setattr(subtitle_painter, "_paint_split_glow_path", _count_split)
    monkeypatch.setattr(subtitle_painter, "_blit_cached_ruby_glow", _count_blit)
    clear_before_layer_cache()
    image = _blank()
    painter = QPainter(image)
    try:
        layers[0].paint_dynamic(
            painter,
            LayerContext(t_ms=1500, logical_w=800, logical_h=450),
            layers[0],
        )
    finally:
        painter.end()

    assert split_calls == 1
    assert blit_states == [False, True]
    assert len(_TEXT_RUN_LAYER_CACHE) == 2


def test_split_glow_dynamic_result_is_limited_to_target_strip(qapp):
    image = QImage(200, 100, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    path = QPainterPath()
    path.addRect(QRectF(20, 25, 160, 50))
    target = QRectF(90, 0, 20, 100)
    painter = QPainter(image)
    try:
        subtitle_painter._paint_split_glow_path(
            painter,
            path,
            _solid_fill("#FF0000"),
            _solid_fill("#0000FF"),
            QRectF(20, 25, 160, 50),
            10,
            4,
            0,
            before_source_clip=QRectF(100, 0, 100, 100),
            after_source_clip=QRectF(0, 0, 100, 100),
            target_clip=target,
        )
    finally:
        painter.end()

    pixels = _image_rgba_array(image)
    assert np.any(pixels[:, 90:110, 3] > 0)
    assert not np.any(pixels[:, :90, 3] > 0)
    assert not np.any(pixels[:, 110:, 3] > 0)


def test_after_body_layer_unclipped_when_fully_sung(qapp):
    track = _track()
    line = track.lines[0]
    layout = _layout_line(track, line, _glow_after_style(), 800, 450)
    assert layout is not None
    ctx = LayerContext(t_ms=0, logical_w=800, logical_h=450)

    def body_clips(t_ms: int) -> list[QRectF | None]:
        layers = [
            layer
            for layer in _line_layer_stack(layout, t_ms)
            if isinstance(layer, _GlyphRunLayer) and layer.after
        ]
        assert layers
        return [layer.animate(ctx, layer.layout(ctx)).clip_rect for layer in layers]

    # N3 连续边界：前一字唱完后仍跟随下一字的扫光线，避免交接处
    # 额外出现一个强制完整释放的视觉阶段。
    mid = body_clips(1700)
    assert mid[0] is not None
    assert any(clip is not None for clip in mid[1:])
    # 全部唱完后每个字符都不再裁切，描边/阴影完整。
    assert all(clip is None for clip in body_clips(9000))


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
    assert abs(center_ltr - center_rtl) <= 5


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


def test_vertical_reading_unit_mode_uses_same_base_character_mapping(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="一", start_ms=1_000),
            TimingChar(text="滴", start_ms=2_000),
        ],
        end_ms=4_000,
    )
    ruby = RubyAnnotation(
        kanji="一滴",
        reading="いってき",
        reading_part_ms=[1_000, 2_000],
        reading_parts=["いっ", "て", "き"],
        pos_start_ms=1_000,
        pos_end_ms=4_000,
    )

    assert _vertical_fill_band(
        [(100, 200), (200, 300)],
        [(1_000, 2_000), (2_000, 4_000)],
        3_000,
        line=line,
        active_rubies=[ruby],
        ruby_main_progress_mode="reading_units",
    ) == (100, 250)


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
            (30.0, "#FF0000"),
            (65.0, "#888888"),
            (100, "#888888"),
        ],
    )
    image = QImage(12, 100, QImage.Format.Format_RGB32)
    image.fill(QColor("#000000"))
    painter = QPainter(image)
    painter.fillRect(QRectF(0, 0, 12, 100), _brush_for_fill(fill, QRectF(0, 0, 12, 100)))
    painter.end()

    assert image.pixelColor(6, 29) == QColor("#FFFFFF")
    assert image.pixelColor(6, 30) == QColor("#FF0000")
    assert image.pixelColor(6, 64) == QColor("#FF0000")
    assert image.pixelColor(6, 65) == QColor("#888888")


def test_hard_band_brush_cache_keys_height_and_fractional_stops(qapp):
    clear_before_layer_cache()
    fill = PaintFill(
        mode="split_vertical",
        split_stops=[
            (0, "#FFFFFF"),
            (33.3333, "#FF0000"),
            (100, "#FF0000"),
        ],
    )

    _brush_for_fill(fill, QRectF(0, 0, 10, 120))
    _brush_for_fill(fill, QRectF(50, 200, 30, 120))
    assert len(_HARD_BAND_BRUSH_CACHE) == 1

    changed = replace(
        fill,
        split_stops=[(0, "#FFFFFF"), (33.3334, "#FF0000"), (100, "#FF0000")],
    )
    _brush_for_fill(changed, QRectF(0, 0, 10, 120))
    _brush_for_fill(fill, QRectF(0, 0, 10, 121))
    assert len(_HARD_BAND_BRUSH_CACHE) == 3

    clear_before_layer_cache()
    assert len(_HARD_BAND_BRUSH_CACHE) == 0


def test_n3_vertical_gradient_uses_render_target_local_height(qapp):
    fill = PaintFill(
        mode="gradient_vertical",
        gradient_stops=[
            (0, "#FFFFFF"),
            (33.3333, "#FF0000"),
            (100, "#000000"),
        ],
    )
    rect = QRectF(10, 20, 30, 80)

    brush = _linear_gradient_brush(fill, rect, 90)
    gradient = brush.gradient()

    assert gradient.start() == QPointF(25, 20)
    assert gradient.finalStop() == QPointF(25, 100)
    assert [position for position, _color in gradient.stops()] == pytest.approx(
        [0.0, 0.333333, 1.0]
    )


def test_n3_main_fill_rect_uses_shared_integer_line_box(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="A", start_ms=1000, role_label="small"),
            TimingChar(text="B", start_ms=1500),
        ],
        end_ms=2000,
    )
    style = Style(
        font_size_px=72,
        stroke_width_px=7,
        stroke2_enabled=True,
        stroke2_width_px=5,
        custom_style_schemes={
            "small": SubtitleStyleScheme(
                font_size_px=48,
                stroke_width_px=3,
                stroke2_enabled=True,
                stroke2_width_px=3,
                latin_stroke_width_px=11,
                latin_stroke2_enabled=False,
                latin_stroke2_width_px=9,
            )
        },
    )
    layout = _layout_line(
        TimingTrack(lines=[line]), line, style, 500, 280, baseline_y=180
    )
    assert layout is not None

    fill_rect = subtitle_painter._n3_main_fill_rect(
        layout.text_layout, layout.baseline_y
    )
    first = layout.text_layout.glyphs[0]
    assert first.style.stroke_width_px == 11
    assert first.brush_style is not None
    assert first.brush_style.stroke_width_px == 3
    font_size = first.font.pixelSize()
    metric_total = first.metrics.ascent() + first.metrics.descent()
    descent = font_size * first.metrics.descent() // metric_total
    draw_bottom = layout.baseline_y + descent + first.style.stroke_width_px // 2
    draw_height = max(
        glyph.font.pixelSize() + glyph.style.stroke_width_px
        for glyph in layout.text_layout.glyphs
    )
    inset = (
        first.brush_style.stroke_width_px + first.brush_style.stroke2_width_px
    ) // 2

    assert fill_rect.top() == pytest.approx(draw_bottom - draw_height + inset)
    assert fill_rect.bottom() == pytest.approx(draw_bottom - inset)
    assert fill_rect.height() == pytest.approx(draw_height - inset * 2)

    layers = _line_layer_stack(layout, 1750)
    glyph_layers = [
        layer
        for layer in layers
        if isinstance(layer, (_GlyphRunLayer, _GlyphRunAfterGlowLayer))
    ]
    assert glyph_layers
    assert all(layer.fill_rect == fill_rect for layer in glyph_layers)


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
        assert box_left - 1 <= seg.left <= seg.right <= box_right + 1
        # 且与该字形的矢量墨水包围盒（与 fillPath 同源）一致
        path = QPainterPath()
        path.addText(float(box_left), 0.0, font, ch.text)
        br = path.boundingRect()
        assert abs(seg.left - int(math.floor(br.left()))) <= 1
        assert abs(seg.right - int(math.ceil(br.right()))) <= 1


def test_n3_main_wipe_bounds_exclude_advance_side_bearings(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="i", start_ms=1000),
            TimingChar(text="W", start_ms=2000),
        ],
        end_ms=3000,
    )
    track = TimingTrack(lines=[line])
    style = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=100,
        line_y_position="center",
        letter_spacing_px=30,
        stroke_width_px=13,
        stroke2_width_px=0,
        shadow_offset_x=0,
        shadow_offset_y=0,
    )

    layout = _layout_line(track, line, style, 800, 240)

    assert layout is not None
    edge_half = style.stroke_width_px // 2
    for index, segment in enumerate(layout.fill_segments):
        ink_left, ink_right = layout.ink_x_ranges[index]
        assert (segment.release_left, segment.release_right) == (
            ink_left - edge_half,
            ink_right + edge_half,
        )
    synthetic_layout = SimpleNamespace(
        glyphs=[SimpleNamespace(index=0, text="A", style=style)]
    )
    assert subtitle_painter._n3_char_wipe_ranges_by_index(
        TimingLine(chars=[TimingChar(text="A", start_ms=0)], end_ms=1000),
        synthetic_layout,
        [(100, 200)],
        [(120, 160)],
    ) == [(114, 166)]


def test_n3_transformed_wipe_span_uses_ink_plus_half_primary_edge(qapp):
    path = QPainterPath()
    path.addRect(QRectF(10.2, 20.0, 20.2, 30.0))

    left, width = subtitle_painter._n3_transformed_wipe_span(path, 5)

    assert left == 8  # floor(10.2) - 5 // 2
    assert left + width == 33  # ceil(30.4) + 5 // 2


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
    from krok_helper.subtitle_render.engine import text_layout  # noqa: PLC0415

    def controlled_path_offset(*args, **kwargs):
        return 12.25

    monkeypatch.setattr(text_layout, "char_path_left_offset", controlled_path_offset)
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


def test_space_width_uses_font_percentage_without_outline(qapp):
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
    assert layout.char_widths == [20]


def test_lrc_text_span_keeps_n3_count_timing_in_painter(qapp, monkeypatch):
    """LRC 由解析器按 N3 字符数补时，Painter 不再按字体宽度二次分配。"""
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
    assert layout.intervals == [(1000, 1500), (1500, 2000)]


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
        glow_before_radius_px=4,
        glow_after_radius_px=4,
        karaoke_colors=colors,
        line_y_position="center",
    )
    large = Style(
        decoration_kind="glow",
        glow_radius_px=28,
        glow_before_radius_px=28,
        glow_after_radius_px=28,
        karaoke_colors=colors,
        line_y_position="center",
    )

    paint_frame(img_small, _track(), 2400, small)
    paint_frame(img_large, _track(), 2400, large)

    assert _pixel_hash(img_small) != _pixel_hash(img_large)


@pytest.mark.parametrize(
    ("t_ms", "before_radius", "after_radius"),
    [
        (500, 0, 18),
        (3000, 18, 0),
    ],
)
def test_zero_glow_radius_removes_glow_from_corresponding_state(
    qapp, t_ms, before_radius, after_radius
):
    orange = _solid_fill("#FF8A00")
    colors = KaraokeColors(
        before=KaraokeColorState(shadow=orange),
        after=KaraokeColorState(shadow=orange),
    )
    zero_state = Style(
        decoration_kind="glow",
        glow_before_radius_px=before_radius,
        glow_after_radius_px=after_radius,
        karaoke_colors=colors,
        line_y_position="center",
    )
    transparent = _solid_fill("#00000000")
    no_glow = replace(
        zero_state,
        karaoke_colors=KaraokeColors(
            before=replace(colors.before, shadow=transparent),
            after=replace(colors.after, shadow=transparent),
        ),
    )
    actual = _blank()
    expected = _blank()

    paint_frame(actual, _track(), t_ms, zero_state)
    paint_frame(expected, _track(), t_ms, no_glow)

    assert _pixel_hash(actual) == _pixel_hash(expected)


def test_glow_before_and_after_radii_are_independent_of_legacy_field():
    style = Style(
        glow_radius_px=99,
        glow_before_radius_px=7,
        glow_after_radius_px=23,
    )

    assert subtitle_painter._glow_radius(style, after=False) == 7
    assert subtitle_painter._glow_radius(style, after=True) == 23


def test_zero_glow_radius_disables_each_state():
    style = Style(
        glow_radius_px=99,
        glow_before_radius_px=0,
        glow_after_radius_px=0,
        ruby_glow_before_radius_px=0,
        ruby_glow_after_radius_px=0,
    )

    assert subtitle_painter._glow_radius(style, after=False) == 0
    assert subtitle_painter._glow_radius(style, after=True) == 0
    assert subtitle_painter._ruby_glow_radius(style, after=False) == 0
    assert subtitle_painter._ruby_glow_radius(style, after=True) == 0
    assert subtitle_painter._glow_pen_width(15, 5, 0) == 0
    assert subtitle_painter._glow_extent(15, 5, 0) == 0
    assert subtitle_painter._glow_blur_radii(0, 2) == ()

    restored = style_from_dict(style_to_dict(style))
    assert restored.glow_before_radius_px == 0
    assert restored.glow_after_radius_px == 0


@pytest.mark.parametrize("t_ms", [500, 3000])
def test_no_glow_concentration_disables_both_states(qapp, t_ms):
    orange = _solid_fill("#FF8A00")
    transparent = _solid_fill("#00000000")
    colors = KaraokeColors(
        before=KaraokeColorState(shadow=orange),
        after=KaraokeColorState(shadow=orange),
    )
    disabled = Style(
        decoration_kind="glow",
        glow_before_radius_px=18,
        glow_after_radius_px=24,
        glow_concentration_level=-1,
        karaoke_colors=colors,
        line_y_position="center",
    )
    no_glow = replace(
        disabled,
        karaoke_colors=KaraokeColors(
            before=replace(colors.before, shadow=transparent),
            after=replace(colors.after, shadow=transparent),
        ),
    )
    actual = _blank()
    expected = _blank()

    paint_frame(actual, _track(), t_ms, disabled)
    paint_frame(expected, _track(), t_ms, no_glow)

    assert subtitle_painter._glow_radius(disabled, after=False) == 0
    assert subtitle_painter._glow_radius(disabled, after=True) == 0
    assert subtitle_painter._glow_blur_radii(18, -1) == ()
    assert _pixel_hash(actual) == _pixel_hash(expected)


def test_zero_after_glow_removes_previous_glow_from_ruby(qapp):
    orange = _solid_fill("#FF8A00")
    transparent = _solid_fill("#00000000")
    colors = KaraokeColors(
        before=KaraokeColorState(shadow=orange),
        after=KaraokeColorState(shadow=orange),
    )
    style = Style(
        decoration_kind="glow",
        glow_before_radius_px=18,
        glow_after_radius_px=0,
        karaoke_colors=colors,
        line_y_position="center",
    )
    no_glow = replace(
        style,
        karaoke_colors=KaraokeColors(
            before=replace(colors.before, shadow=transparent),
            after=replace(colors.after, shadow=transparent),
        ),
    )
    partial = _blank()
    partial_no_glow = _blank()
    actual = _blank()
    expected = _blank()

    paint_frame(partial, _track_with_timed_ruby(), 2400, style)
    paint_frame(partial_no_glow, _track_with_timed_ruby(), 2400, no_glow)
    paint_frame(actual, _track_with_timed_ruby(), 2500, style)
    paint_frame(expected, _track_with_timed_ruby(), 2500, no_glow)

    assert _pixel_hash(partial) != _pixel_hash(partial_no_glow)
    assert _pixel_hash(actual) == _pixel_hash(expected)


def test_n3_role_scheme_empty_slots_fallback_inside_same_scheme(qapp):
    scheme = SubtitleStyleScheme(
        font_family="Yu Mincho",
        font_family_latin=None,
        font_size_px=80,
        latin_font_size_px=None,
        font_weight=700,
        latin_font_weight=None,
        stroke_width_px=12,
        latin_stroke_width_px=None,
        ruby_font_family=None,
        ruby_font_weight=None,
        n3_font_inheritance=True,
    )
    global_style = Style(
        font_family="Arial",
        font_family_latin="Courier New",
        font_weight=400,
        latin_font_weight=500,
        custom_style_schemes={"N3": scheme},
    )

    merged = subtitle_painter._style_for_role(global_style, "N3")
    assert merged.font_family == "Yu Mincho"
    assert merged.font_family_latin is None
    qt_yu_mincho = subtitle_painter.resolve_qt_font_family("Yu Mincho")
    assert subtitle_painter._build_latin_font(merged).family() == qt_yu_mincho
    assert subtitle_painter._latin_font_weight(merged) == 700
    assert subtitle_painter._main_script_stroke_style(
        merged, "ABC"
    ).stroke_width_px == 12
    assert subtitle_painter._build_ruby_font(merged).family() == qt_yu_mincho


def test_font_builders_use_runtime_qt_family_alias(monkeypatch):
    resolve_family = (
        lambda family: "Arial" if family == "N3 Japanese Display Name" else family
    )
    monkeypatch.setattr(subtitle_painter, "resolve_qt_font_family", resolve_family)
    monkeypatch.setattr(ruby_style, "resolve_qt_font_family", resolve_family)
    monkeypatch.setattr(text_metrics, "resolve_qt_font_family", resolve_family)
    style = Style(
        font_family="N3 Japanese Display Name",
        font_family_latin="N3 Japanese Display Name",
        ruby_font_follow_main=False,
        ruby_font_family="N3 Japanese Display Name",
    )
    title = TitleOverlay(
        font_family="N3 Japanese Display Name",
        font_family_latin="N3 Japanese Display Name",
    )

    assert subtitle_painter._build_font(style).family() == "Arial"
    assert subtitle_painter._build_latin_font(style).family() == "Arial"
    assert subtitle_painter._build_ruby_font(style).family() == "Arial"
    assert subtitle_painter._build_title_font(title).family() == "Arial"
    assert subtitle_painter._build_title_latin_font(title).family() == "Arial"


def test_lead_symbol_ascii_character_uses_resolved_custom_latin_font(monkeypatch):
    monkeypatch.setattr(
        text_metrics,
        "resolve_qt_font_family",
        lambda family: "MyEmoji5 Qt Name" if family == "MyEmoji5" else family,
    )
    style = Style(font_family="Main Japanese", font_family_latin="MyEmoji5")
    japanese = subtitle_painter._build_font(style)
    latin = subtitle_painter._build_latin_font(style)
    font_for = subtitle_painter._make_font_for(style, japanese, latin)

    assert font_for is not None
    assert font_for("h").family() == "MyEmoji5 Qt Name"
    assert font_for("願").family() == "Main Japanese"


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


@pytest.mark.parametrize("layout_semantics", ["legacy", "n3_1074"])
def test_dual_line_cpu_visibility_is_projected_from_shared_layout_plan(
    qapp,
    monkeypatch,
    layout_semantics,
):
    clear_before_layer_cache()
    track = _two_line_track()
    style = Style(layout_semantics=layout_semantics, dual_line_layout=True)
    expected = _visible_lines_for_style(
        track,
        1_600,
        style,
        logical_w=640,
        logical_h=360,
    )
    original = subtitle_painter.build_track_layout_plan
    calls = []

    def build_plan(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(subtitle_painter, "build_track_layout_plan", build_plan)
    _track_t_ms, _display_style, actual, _signals, _title = (
        subtitle_painter._resolve_visible_content(
            track,
            1_600,
            style,
            logical_w=640,
            logical_h=360,
        )
    )

    assert len(calls) == 1
    assert [
        (
            item.line,
            item.lane,
            item.display_start_ms,
            item.display_end_ms,
            item.section_index,
            item.page_index,
            item.page_line_count,
        )
        for item in actual
    ] == [
        (
            item.line,
            item.lane,
            item.display_start_ms,
            item.display_end_ms,
            item.section_index,
            item.page_index,
            item.page_line_count,
        )
        for item in expected
    ]


def test_shared_track_layout_plan_cache_reuses_and_invalidates_mutable_inputs(qapp):
    clear_before_layer_cache()
    track = _two_line_track()
    style = Style(dual_line_layout=True)

    first = subtitle_painter.build_track_layout_plan(
        track, style, logical_w=640, logical_h=360
    )
    again = subtitle_painter.build_track_layout_plan(
        track, style, logical_w=640, logical_h=360
    )

    assert again is first
    assert first.lines[0].display_section_index == 0
    assert first.lines[0].display_page_index == 0

    style.line_tail_ms += 100
    changed = subtitle_painter.build_track_layout_plan(
        track, style, logical_w=640, logical_h=360
    )

    assert changed is not first
    assert changed.lines[0].display_end_ms != first.lines[0].display_end_ms


def test_dual_line_cpu_consumes_planned_animation_style(qapp, monkeypatch):
    clear_before_layer_cache()
    track = _two_line_track()
    style = Style(dual_line_layout=True, lit_enabled=False)
    subtitle_painter.build_track_layout_plan(
        track, style, logical_w=640, logical_h=360
    )
    expected = paint_frame(_blank(), track, 1_600, style)

    def fail_recompute(*_args, **_kwargs):
        raise AssertionError("CPU recomputed a line animation style")

    monkeypatch.setattr(
        subtitle_painter, "_style_for_line_display_window", fail_recompute
    )
    actual = paint_frame(_blank(), track, 1_600, style)

    assert _pixel_hash(actual) == _pixel_hash(expected)


@pytest.mark.parametrize("layout_semantics", ["legacy", "n3_1074"])
def test_planned_page_offset_windows_match_cpu_offset_selection(
    qapp,
    layout_semantics,
):
    lines = [
        TimingLine(
            chars=[TimingChar(text, start)],
            end_ms=start + 500,
            display_start_override_ms=0,
            display_end_override_ms=5_000,
        )
        for text, start in (("A", 1_000), ("B", 2_000), ("C", 3_000))
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(2, "default"), TrackPage(1, "builtin-1")])]
        ),
    )
    style = Style(layout_semantics=layout_semantics, dual_line_layout=True)
    plan = subtitle_painter.build_track_layout_plan(
        track, style, logical_w=640, logical_h=360
    )

    for t_ms in (0, 1_500, 3_000, 4_999, 5_000):
        assert subtitle_painter._active_page_offsets_from_layout_plan(
            plan, t_ms
        ) == subtitle_painter.resolved_page_offsets_for_style(
            640, 360, track, style, t_ms=t_ms
        )


@pytest.mark.parametrize("with_role", [False, True])
def test_planned_horizontal_line_inputs_preserve_legacy_geometry(
    qapp,
    with_role,
):
    role_label = "accent" if with_role else None
    line = TimingLine(
        chars=[
            TimingChar("A", 1_000, role_label=role_label),
            TimingChar("B", 1_500),
        ],
        end_ms=2_000,
        guide_symbol=GuideSymbol(
            path_commands=(
                ("M", 0.0, 0.0),
                ("L", 500.0, -800.0),
                ("L", 1_000.0, 0.0),
                ("Z",),
            ),
            duration_ms=400,
        ),
    )
    track = TimingTrack(lines=[line])
    style = Style(
        dual_line_layout=True,
        lit_enabled=False,
        custom_style_schemes=(
            {"accent": SubtitleStyleScheme(font_size_px=84)} if with_role else {}
        ),
    )
    plan = subtitle_painter.build_track_layout_plan(
        track, style, logical_w=640, logical_h=360
    ).lines[0]
    legacy = subtitle_painter._layout_line_uncached(
        track,
        line,
        plan.animation_style,
        640,
        360,
        lane=plan.lane,
    )
    planned = subtitle_painter._layout_line_uncached(
        track,
        line,
        plan.animation_style,
        640,
        360,
        lane=plan.lane,
        line_plan=plan,
    )

    assert legacy is not None and planned is not None
    assert planned.render_line == legacy.render_line == plan.render_line
    assert planned.intervals == legacy.intervals == list(plan.resolved_intervals)
    assert planned.char_x_ranges == legacy.char_x_ranges
    assert planned.ink_x_ranges == legacy.ink_x_ranges
    assert planned.baseline_y == legacy.baseline_y
    assert planned.line_rect == legacy.line_rect


def test_planned_vertical_line_inputs_preserve_legacy_geometry(qapp):
    line = TimingLine(
        chars=[TimingChar("縦", 1_000), TimingChar("A", 1_500)],
        end_ms=2_000,
        guide_symbol=GuideSymbol(duration_ms=400),
    )
    track = TimingTrack(lines=[line])
    style = Style(vertical=True, dual_line_layout=True, lit_enabled=False)
    plan = subtitle_painter.build_track_layout_plan(
        track, style, logical_w=640, logical_h=360
    ).lines[0]
    legacy_line = subtitle_painter._line_with_guide_symbol(line)
    legacy = _layout_vertical_line(
        track,
        legacy_line,
        plan.animation_style,
        640,
        360,
        column_x=320,
        source_line=line,
    )
    planned = _layout_vertical_line(
        track,
        plan.render_line,
        plan.animation_style,
        640,
        360,
        column_x=320,
        source_line=line,
        resolved_intervals=plan.resolved_intervals,
    )

    assert legacy is not None and planned is not None
    assert planned.intervals == legacy.intervals == list(plan.resolved_intervals)
    assert planned.cells == legacy.cells
    assert planned.column_x == legacy.column_x
    assert planned.line_rect == legacy.line_rect
    assert planned.text_path == legacy.text_path


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


def test_n3_top_margin_anchors_main_box_without_ruby_height(qapp):
    line = TimingLine(chars=[TimingChar(text="A", start_ms=0)], end_ms=1000)
    ruby = RubyAnnotation(
        kanji="A",
        reading="WWWW",
        pos_start_ms=0,
        pos_end_ms=1000,
    )
    display = DisplayLine(
        line=line,
        lane=0,
        display_start_ms=0,
        display_end_ms=1000,
    )
    style = Style(
        layout_semantics="n3_1074",
        dual_line_layout=False,
        line_y_position="top",
        line_y_margin_px=47,
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=64,
        stroke_width_px=6,
        ruby_font_family="Arial",
        ruby_font_family_latin="Arial",
        ruby_font_size_px=42,
        ruby_gap_px=9,
    )

    without_ruby = _resolve_display_baselines(
        360, TimingTrack(lines=[line]), [display], style
    )
    with_ruby = _resolve_display_baselines(
        360, TimingTrack(lines=[line], rubies=[ruby]), [display], style
    )

    assert with_ruby == without_ruby


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


def test_negative_layout_margins_move_lines_beyond_frame_edges(qapp):
    style = Style(
        line_y_position="bottom",
        line_y_margin_px=-40,
        horizontal_margin_px=-50,
        smart_horizontal="none",
    )
    metrics = QFontMetrics(_build_font(style))

    assert _resolve_baseline_y(metrics, 1080, style) > 1080 - metrics.descent()
    assert _resolve_line_x(1920, 600, style, 0) == -50
    assert _resolve_line_x(1920, 600, style, 1) == 1370


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


def test_ruby_wipe_uses_visible_door_glyphs_and_n3_character_times(qapp):
    """2:19: centered ``door`` must not finish in its trailing layout blank."""
    style = Style(ruby_font_size_px=30, ruby_alignment="center")
    font = _build_ruby_font(style)
    metrics = QFontMetrics(font)
    ruby = RubyAnnotation(
        kanji="ドア",
        reading="door",
        reading_part_ms=[290],
        reading_parts=["doo", "r"],
        pos_start_ms=139080,
        pos_end_ms=139540,
    )
    segments, ink_left, ink_right, _signature = subtitle_painter._ruby_wipe_geometry(
        ruby, font, metrics, 599, 200, 204, style, rtl=False
    )

    assert [(item.start_ms, item.end_ms) for item in segments] == [
        (139080, 139176),
        (139176, 139273),
        (139273, 139370),
        (139370, 139540),
    ]
    assert ink_left > 599  # centered leading blank is positioning only
    assert ink_right < 803  # centered trailing blank is not wipe distance

    layout = subtitle_painter._RubyLayout(
        ruby=ruby,
        indices=[0, 1],
        style=style,
        x=599,
        baseline_y=200,
        target_width=204,
        reading_width=204,
        gradient_rect=QRectF(599, 100, 204, 100),
        wipe_segments=segments,
        wipe_left=ink_left,
        wipe_right=ink_right,
    )
    visible, complete, front = subtitle_painter._ruby_wipe_state(layout, 139416)
    assert visible and not complete
    assert segments[-1].axis_start < front < ink_right


def test_single_centered_ruby_starts_at_its_visible_glyph(qapp):
    """4:21: a wide target's leading blank must not delay the ruby wipe."""
    style = Style(ruby_font_size_px=30, ruby_alignment="center")
    font = _build_ruby_font(style)
    metrics = QFontMetrics(font)
    ruby = RubyAnnotation(
        kanji="超",
        reading="こ",
        reading_parts=["こ"],
        pos_start_ms=261470,
        pos_end_ms=261790,
    )
    segments, ink_left, ink_right, _signature = subtitle_painter._ruby_wipe_geometry(
        ruby, font, metrics, 917, 200, 113, style, rtl=False
    )
    assert len(segments) == 1
    assert ink_left > 917
    assert ink_right < 1030

    layout = subtitle_painter._RubyLayout(
        ruby=ruby,
        indices=[0],
        style=style,
        x=917,
        baseline_y=200,
        target_width=113,
        reading_width=113,
        gradient_rect=QRectF(917, 100, 113, 100),
        wipe_segments=segments,
        wipe_left=ink_left,
        wipe_right=ink_right,
    )
    visible, complete, front = subtitle_painter._ruby_wipe_state(layout, 261500)
    assert visible and not complete
    assert front > ink_left  # visible ink has begun immediately after pos_start


def test_ruby_mora_boundary_uses_primary_stroke_draw_edges(qapp):
    """199467ms 的「は→な」边界不能在 ratio=0 时提前露出「な」的左描边。"""
    style = Style(
        ruby_font_size_px=45,
        ruby_stroke_width_px=10,
        ruby_stroke2_width_px=3,
        ruby_alignment="equal_space",
    )
    font = _build_ruby_font(style)
    metrics = QFontMetrics(font)
    ruby = RubyAnnotation(
        kanji="離",
        reading="はな",
        reading_part_ms=[415],
        reading_parts=["は", "な"],
        pos_start_ms=199_052,
        pos_end_ms=199_867,
    )
    x = 1184
    target_width = 112
    segments, wipe_left, wipe_right, _signature = (
        subtitle_painter._ruby_wipe_geometry(
            ruby, font, metrics, x, 200, target_width, style, rtl=False
        )
    )
    unit_layouts = subtitle_painter._ruby_layout_units(
        ["は", "な"],
        metrics,
        x,
        target_width,
        style=style,
        base_text="離",
    )
    ink_bounds = []
    for unit, unit_x, _unit_width in unit_layouts:
        path = QPainterPath()
        path.addText(float(unit_x), 200.0, font, unit)
        ink_bounds.append(path.boundingRect())

    edge_half = subtitle_painter._ruby_stroke_width(style) / 2.0
    assert segments[0].axis_start == pytest.approx(ink_bounds[0].left() - edge_half)
    assert segments[0].axis_end == pytest.approx(ink_bounds[0].right() + edge_half)
    assert segments[1].axis_start == pytest.approx(ink_bounds[1].left() - edge_half)
    assert segments[1].axis_end == pytest.approx(ink_bounds[1].right() + edge_half)
    assert wipe_left == pytest.approx(segments[0].axis_start)
    assert wipe_right == pytest.approx(segments[1].axis_end)

    layout = subtitle_painter._RubyLayout(
        ruby=ruby,
        indices=[0],
        style=style,
        x=x,
        baseline_y=200,
        target_width=target_width,
        reading_width=target_width,
        gradient_rect=QRectF(x, 100, target_width, 100),
        wipe_segments=segments,
        wipe_left=wipe_left,
        wipe_right=wipe_right,
    )
    # 60fps 恰好取到 199467ms：锋面位于「な」的完整绘制左缘，而不是
    # 填充墨水左缘，因此下一字仍是严格 0%，其左半描边不会提前变色。
    visible, complete, front = subtitle_painter._ruby_wipe_state(layout, 199_467)
    assert visible and not complete
    assert front == pytest.approx(segments[1].axis_start)
    assert front < ink_bounds[1].left()


def test_ruby_wipe_preserves_empty_part_pause_and_rtl_direction(qapp):
    ruby = RubyAnnotation(
        kanji="AB",
        reading="ab",
        reading_part_ms=[100, 200],
        reading_parts=["a", "", "b"],
        pos_start_ms=1000,
        pos_end_ms=1300,
    )
    style = Style(ruby_font_size_px=30, ruby_alignment="center")
    font = _build_ruby_font(style)
    metrics = QFontMetrics(font)
    segments, left, right, _signature = subtitle_painter._ruby_wipe_geometry(
        ruby, font, metrics, 100, 200, 160, style, rtl=True
    )
    layout = subtitle_painter._RubyLayout(
        ruby=ruby,
        indices=[0, 1],
        style=style,
        x=100,
        baseline_y=200,
        target_width=160,
        reading_width=160,
        gradient_rect=QRectF(100, 100, 160, 100),
        wipe_segments=segments,
        wipe_left=left,
        wipe_right=right,
    )

    assert [(item.start_ms, item.end_ms) for item in segments] == [
        (1000, 1100),
        (1200, 1300),
    ]
    assert segments[0].axis_start > segments[0].axis_end  # first sound wipes right-to-left
    pause_start = subtitle_painter._ruby_wipe_state(layout, 1100)[2]
    pause_mid = subtitle_painter._ruby_wipe_state(layout, 1150)[2]
    assert pause_mid == pytest.approx(pause_start)


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


def test_ruby_baked_keys_include_relative_alignment_geometry(qapp):
    ruby = RubyAnnotation(
        kanji="漢",
        reading="abc",
        reading_parts=["abc"],
        pos_start_ms=1000,
        pos_end_ms=2000,
    )
    layouts = []
    for alignment in ("center", "equal_space"):
        style = Style(ruby_font_size_px=36, ruby_alignment=alignment)
        font = _build_ruby_font(style)
        metrics = QFontMetrics(font)
        segments, left, right, signature = subtitle_painter._ruby_wipe_geometry(
            ruby, font, metrics, 100, 200, 180, style, rtl=False
        )
        layouts.append(
            (
                subtitle_painter._RubyLayout(
                    ruby=ruby,
                    indices=[0],
                    style=style,
                    x=100,
                    baseline_y=200,
                    target_width=180,
                    reading_width=180,
                    gradient_rect=QRectF(100, 100, 180, 100),
                    wipe_segments=segments,
                    wipe_left=left,
                    wipe_right=right,
                    geometry_signature=signature,
                ),
                font,
                style,
            )
        )

    center, center_font, center_style = layouts[0]
    equal, equal_font, equal_style = layouts[1]
    assert center.geometry_signature != equal.geometry_signature
    assert subtitle_painter._ruby_text_layer_key(
        center, center_font, center_style, False, after=False
    ) != subtitle_painter._ruby_text_layer_key(
        equal, equal_font, equal_style, False, after=False
    )
    assert subtitle_painter._ruby_glow_layer_key(
        center, center_font, center_style, False, after=False
    ) != subtitle_painter._ruby_glow_layer_key(
        equal, equal_font, equal_style, False, after=False
    )


def test_clear_before_layer_cache_clears_ruby_unit_geometry(qapp):
    style = Style(ruby_font_size_px=36)
    metrics = QFontMetrics(_build_ruby_font(style))
    _ruby_layout_units(["a", "b"], metrics, 0, 100, style=style, base_text="漢")
    assert subtitle_painter._RUBY_UNIT_LAYOUT_CACHE

    clear_before_layer_cache()

    assert not subtitle_painter._RUBY_UNIT_LAYOUT_CACHE


def test_line_layout_cache_reuses_precomputed_ruby_wipe_geometry(qapp, monkeypatch):
    track = _track_with_ruby()
    style = Style(font_size_px=64, ruby_font_size_px=30, line_y_position="center")
    calls = 0
    original = subtitle_painter._ruby_wipe_geometry

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(subtitle_painter, "_ruby_wipe_geometry", counted)
    clear_before_layer_cache()
    first = _layout_line(track, track.lines[0], style, 640, 360, cache_sig=("ruby",))
    second = _layout_line(track, track.lines[0], style, 640, 360, cache_sig=("ruby",))

    assert first is second
    assert first.ruby_layouts
    assert calls == len(first.ruby_layouts)


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


def test_fill_extent_releases_completed_character_to_n3_draw_edge(qapp):
    """句中停顿停在已唱字符 DrawRight，不用与下一字间隙中点猜边界。"""
    from krok_helper.subtitle_render.engine.painter import _fill_extent_left

    segments = [
        _FillSegment(100, 200, 1000, 2000, indices=(0,)),
        # 墨水间隙 200→240，时间停顿 2000→2500
        _FillSegment(240, 300, 2500, 3000, indices=(1,)),
    ]
    # 没有单独 release 边界的合成 segment 回退到墨水边界。
    assert _fill_extent_end(segments, 1500) == 150
    assert _fill_extent_end(segments, 2000) == 200
    assert _fill_extent_end(segments, 2300) == 200
    # 下一段开始后：正常从其墨水左缘继续，前沿单调不回退
    assert _fill_extent_end(segments, 2750) == 270
    # 未开始时不受 previous_complete 影响
    assert _fill_extent_end(segments, 500) == 100

    # RTL 镜像：segments 从右往左排列
    rtl_segments = [
        _FillSegment(240, 300, 1000, 2000, indices=(0,)),
        _FillSegment(100, 200, 2500, 3000, indices=(1,)),
    ]
    assert _fill_extent_left(rtl_segments, 2300) == 240
    assert _fill_extent_left(rtl_segments, 500) == 300


def test_n3_release_edge_clamps_to_following_overlapping_draw_left(qapp):
    segments = subtitle_painter._adjust_fill_release_edges(
        [
            _FillSegment(
                100, 200, 1000, 1300, indices=(0,),
                release_left=92, release_right=218,
                layout_left=100, layout_right=200,
            ),
            _FillSegment(
                220, 280, 1700, 2000, indices=(1,),
                release_left=210, release_right=290,
                layout_left=190, layout_right=250,
            ),
        ]
    )

    expected = 92 + (218 - 92) * ((190 - 100) / (200 - 100 + 1))
    assert segments[0].release_right == pytest.approx(expected)
    assert _fill_extent_end(segments, 1300) == pytest.approx(expected)
    assert _fill_extent_end(segments, 1500) == pytest.approx(expected)


def test_n3_completed_glyph_follows_successor_wipe_boundary(qapp):
    segments = subtitle_painter._adjust_fill_release_edges(
        [
            _FillSegment(
                100, 200, 1000, 1300, indices=(0,),
                release_left=92, release_right=218,
                layout_left=100, layout_right=200,
            ),
            _FillSegment(
                220, 280, 1300, 1600, indices=(1,),
                release_left=210, release_right=290,
                layout_left=190, layout_right=250,
            ),
        ]
    )

    # 精确交接时仍使用前一字的 AdjustWipeEnd 终点。
    assert subtitle_painter._n3_following_wipe_band(
        segments, {0}, 1300, rtl=False
    ) == (92, 204)
    # 下一采样时刻复用后一字的移动边界，而不是解除前一字裁剪。
    following = subtitle_painter._n3_following_wipe_band(
        segments, {0}, 1450, rtl=False
    )
    assert following == (210, 250)
    # 后一字结束后，前一字才真正完整释放。
    assert subtitle_painter._n3_following_wipe_band(
        segments, {0}, 1600, rtl=False
    ) is None


def test_n3_wipe_interpolates_to_adjusted_draw_edge_without_boundary_jump(qapp):
    """N3 从首帧起就朝 AdjustWipeEnd 终点插值，不在字结束帧切换范围。"""
    from krok_helper.subtitle_render.engine.painter import _fill_extent_left

    ltr = subtitle_painter._adjust_fill_release_edges(
        [
            _FillSegment(
                100, 160, 1000, 2000, indices=(0,),
                release_left=80, release_right=220,
                layout_left=100, layout_right=160,
            ),
            _FillSegment(
                210, 270, 2000, 3000, indices=(1,),
                release_left=200, release_right=290,
                layout_left=150, layout_right=210,
            ),
        ]
    )
    expected = 80 + (220 - 80) * ((150 - 100) / (160 - 100 + 1))
    assert ltr[0].release_right == pytest.approx(expected)
    # 旧实现的中点是 ink 中点 130，且 1999→2000ms 会从 160 跳到 200。
    assert _fill_extent_end(ltr, 1500) == 137
    # 60fps 下结束前一帧约为 1983ms：新实现只再走 2px，旧实现会突跳 41px。
    assert _fill_extent_end(ltr, 1983) == 193
    assert _fill_extent_end(ltr, 1999) == 195
    assert _fill_extent_end(ltr, 2000) == pytest.approx(expected)

    rtl = [
        _FillSegment(
            210, 270, 1000, 2000, indices=(0,),
            release_left=200, release_right=290,
        ),
        _FillSegment(
            100, 160, 2000, 3000, indices=(1,),
            release_left=80, release_right=170,
        ),
    ]
    assert _fill_extent_left(rtl, 1500) == 245
    assert _fill_extent_left(rtl, 1999) == 200
    assert _fill_extent_left(rtl, 2000) == 200


def test_explicit_timed_space_has_layout_time_but_no_wipe_geometry(qapp):
    track = parse_nicokara_lrc(
        "[01:25:07]週[01:25:37] [01:25:76]バー[01:26:20]\n"
    )
    line = track.lines[0]
    style = Style(
        font_size_px=100,
        stroke_width_px=15,
        stroke2_width_px=5,
        space_width_percent=20,
        line_y_position="center",
    )

    layout = _layout_line(track, line, style, 1200, 300)

    assert layout is not None
    assert layout.intervals[:2] == [(85_070, 85_370), (85_370, 85_760)]
    assert layout.fill_segments[1].left == layout.fill_segments[1].right
    week_release = layout.fill_segments[0].release_right
    assert week_release is not None
    assert _fill_extent_end(layout.fill_segments, 85_370) == week_release
    assert _fill_extent_end(layout.fill_segments, 85_600) == pytest.approx(week_release)

    at_week_end = _blank()
    at_space_end = _blank()
    paint_frame(at_week_end, track, 85_370, style)
    paint_frame(at_space_end, track, 85_760, style)
    # 週在自己的结束时刻就必须与空格结束时完全一致；旧整行 clip 会让
    # 走字前阴影尾巴留到空格结束才消失，因此这两个像素哈希会不同。
    assert _pixel_hash(at_week_end) == _pixel_hash(at_space_end)


def test_completed_glyph_glow_does_not_wait_for_timed_space(qapp):
    track = parse_nicokara_lrc("[00:01:00]週[00:01:30] [00:01:70]バ[00:02:00]\n")
    style = Style(
        font_size_px=100,
        stroke_width_px=15,
        stroke2_width_px=5,
        space_width_percent=20,
        line_y_position="center",
        decoration_kind="glow",
        glow_radius_px=12,
        glow_before_radius_px=12,
        glow_after_radius_px=12,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(
                text=PaintFill(color="#FFFFFF"),
                stroke=PaintFill(color="#F28A32"),
                stroke2=PaintFill(color="#FFFFFF"),
                shadow=PaintFill(color="#FF2244"),
            ),
            after=KaraokeColorState(
                text=PaintFill(color="#FFFFFF"),
                stroke=PaintFill(color="#F28A32"),
                stroke2=PaintFill(color="#FFFFFF"),
                shadow=PaintFill(color="#2288FF"),
            ),
        ),
    )
    at_week_end = _blank()
    at_space_end = _blank()

    paint_frame(at_week_end, track, 1_300, style)
    paint_frame(at_space_end, track, 1_700, style)

    assert _pixel_hash(at_week_end) == _pixel_hash(at_space_end)


def test_completed_glyph_shadow_does_not_wait_for_timed_space(qapp):
    track = parse_nicokara_lrc("[00:01:00]週[00:01:30] [00:01:70]バ[00:02:00]\n")
    style = Style(
        font_size_px=100,
        stroke_width_px=15,
        stroke2_width_px=5,
        space_width_percent=20,
        line_y_position="center",
        decoration_kind="shadow",
        shadow_offset_x=10,
        shadow_offset_y=10,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(
                text=PaintFill(color="#FFFFFF"),
                stroke=PaintFill(color="#F28A32"),
                stroke2=PaintFill(color="#FFFFFF"),
                shadow=PaintFill(color="#FF2244"),
            ),
            after=KaraokeColorState(
                text=PaintFill(color="#FFFFFF"),
                stroke=PaintFill(color="#F28A32"),
                stroke2=PaintFill(color="#FFFFFF"),
                shadow=PaintFill(color="#F28A32"),
            ),
        ),
    )
    at_week_end = _blank()
    at_space_end = _blank()

    paint_frame(at_week_end, track, 1_300, style)
    paint_frame(at_space_end, track, 1_700, style)

    assert _pixel_hash(at_week_end) == _pixel_hash(at_space_end)


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


def test_reading_unit_mode_tracks_visible_ruby_characters(qapp):
    ruby = RubyAnnotation(
        kanji="頁",
        reading="ぺーじ",
        reading_part_ms=[750],
        reading_parts=["ぺー", "じ"],
        pos_start_ms=69_080,
        pos_end_ms=70_130,
    )

    # 69_642.5 ms is halfway through the long-vowel unit.  The historical
    # two-checkpoint clock is only 3/8 through the base glyph, while the N3
    # reading-unit clock is halfway through the three visible ruby units.
    assert _main_text_ruby_progress_ratio(ruby, 69_643) == pytest.approx(
        0.375,
        abs=0.002,
    )
    assert _main_text_ruby_progress_ratio(
        ruby, 69_643, mode="reading_units"
    ) == pytest.approx(0.5, abs=0.002)


def test_reading_unit_mode_maps_ruby_units_across_multiple_base_chars(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="一", start_ms=1_000),
            TimingChar(text="滴", start_ms=2_000),
        ],
        end_ms=4_000,
    )
    intervals = [(1_000, 2_000), (2_000, 4_000)]
    ranges = [(0, 100), (100, 200)]
    ruby = RubyAnnotation(
        kanji="一滴",
        reading="いってき",
        reading_part_ms=[1_000, 2_000],
        reading_parts=["いっ", "て", "き"],
        pos_start_ms=1_000,
        pos_end_ms=4_000,
    )

    legacy = _karaoke_fill_segments(
        [100, 100], intervals, ranges, [ruby], line
    )
    reading_units = _karaoke_fill_segments(
        [100, 100],
        intervals,
        ranges,
        [ruby],
        line,
        ruby_main_progress_mode="reading_units",
    )

    assert len(legacy) == 1
    assert len(reading_units) == 2
    assert _character_fill_ratio(
        line,
        intervals,
        ranges,
        [ruby],
        0,
        2_000,
        ruby_main_progress_mode="reading_units",
    ) == pytest.approx(1.0)
    assert _character_fill_ratio(
        line,
        intervals,
        ranges,
        [ruby],
        1,
        2_000,
        ruby_main_progress_mode="reading_units",
    ) == pytest.approx(0.0)
    assert _character_fill_ratio(
        line,
        intervals,
        ranges,
        [ruby],
        1,
        3_000,
        ruby_main_progress_mode="reading_units",
    ) == pytest.approx(0.5)
    assert _fill_extent_end(reading_units, 3_000) == 150


@pytest.mark.parametrize("mode", ["checkpoint_segments", "reading_units"])
def test_ruby_main_progress_modes_keep_explicit_main_text_timing(qapp, mode):
    """Regression for メロディー/melody: explicit base checkpoints always win."""
    from krok_helper.subtitle_render.engine.timeline import compute_char_intervals

    track = parse_nicokara_lrc(
        "[00:06:22]メ[00:06:47]ロ[00:06:74]デ[00:06:92]ィー[00:07:61]\n"
        "\n"
        "@Ruby1=メロディー,me[00:00:25]lo[00:00:52]d[00:00:70]y,"
        "[00:06:22],[00:07:61]\n"
    )
    line = track.lines[0]
    intervals = compute_char_intervals(line)
    ranges = [(index * 100, (index + 1) * 100) for index in range(5)]

    segments = _karaoke_fill_segments(
        [100] * 5,
        intervals,
        ranges,
        track.rubies,
        line,
        ruby_main_progress_mode=mode,
    )

    assert len(segments) == 5
    assert [(segment.start_ms, segment.end_ms) for segment in segments] == [
        (6_220, 6_470),
        (6_470, 6_740),
        (6_740, 6_920),
        (6_920, 7_265),
        (7_265, 7_610),
    ]
    assert _character_fill_ratio(
        line,
        intervals,
        ranges,
        track.rubies,
        2,
        6_740,
        ruby_main_progress_mode=mode,
    ) == 0.0  # d starts with デ
    assert _character_fill_ratio(
        line,
        intervals,
        ranges,
        track.rubies,
        3,
        6_920,
        ruby_main_progress_mode=mode,
    ) == 0.0  # y starts with ィ
    assert _character_fill_ratio(
        line,
        intervals,
        ranges,
        track.rubies,
        2,
        6_830,
        ruby_main_progress_mode=mode,
    ) == pytest.approx(0.5)


def test_reading_unit_mode_preserves_empty_part_pause(qapp):
    ruby = RubyAnnotation(
        kanji="字",
        reading="あい",
        reading_part_ms=[200, 500],
        reading_parts=["あ", "", "い"],
        pos_start_ms=1_000,
        pos_end_ms=2_000,
    )

    assert _main_text_ruby_progress_ratio(
        ruby, 1_200, mode="reading_units"
    ) == pytest.approx(0.5)
    assert _main_text_ruby_progress_ratio(
        ruby, 1_350, mode="reading_units"
    ) == pytest.approx(0.5)
    assert _main_text_ruby_progress_ratio(
        ruby, 1_499, mode="reading_units"
    ) == pytest.approx(0.5)


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


def test_next_line_ruby_is_not_active_at_trailing_space_boundary(qapp):
    text = (
        "[01:59:36]距[01:59:53]離[01:59:71]とっ[02:00:19]て[02:00:54] "
        "[02:00:66]笑[02:01:00]え[02:01:22]る[02:01:56]け[02:01:84]ど[02:03:11] \n"
        "[02:03:59]笑[02:04:08]え[02:04:31]て[02:04:48]る[02:04:84]け[02:05:01]ど[02:06:29]\n"
        "\n"
        "@Ruby1=笑,わ[00:00:16]ら,[02:00:66],[02:01:00]\n"
        "@Ruby2=笑,わ[00:00:30]ら,[02:03:59],[02:04:08]\n"
    )
    track = parse_nicokara_lrc(text)
    previous_line = track.lines[0]

    # The final space borrows the next line leader as its end.  A ruby that
    # starts exactly on that boundary belongs only to the next line.
    assert previous_line.chars[-1].text == " "
    assert previous_line.end_ms == 123_590
    assert _active_rubies_for_line(track.rubies, previous_line) == [track.rubies[0]]


def test_overlapping_next_line_ruby_is_not_active_on_previous_line(qapp):
    previous_line = TimingLine(
        chars=[
            TimingChar(text="私", start_ms=16_000),
            TimingChar(text="を", start_ms=16_600),
            TimingChar(text=" ", start_ms=16_900),
            TimingChar(text="私", start_ms=17_200),
            TimingChar(text="た", start_ms=17_800),
            TimingChar(text="ら", start_ms=18_200),
            TimingChar(text="し", start_ms=18_500),
            TimingChar(text="め", start_ms=18_600),
            TimingChar(text="て", start_ms=19_000),
            TimingChar(text="よ", start_ms=19_200),
        ],
        end_ms=20_300,
    )
    next_line = TimingLine(
        chars=[
            TimingChar(text="ど", start_ms=18_550),
            TimingChar(text="の", start_ms=18_700),
            TimingChar(text="私", start_ms=18_870),
            TimingChar(text="も", start_ms=19_460),
            TimingChar(text="私", start_ms=20_020),
            TimingChar(text="よ", start_ms=21_030),
        ],
        end_ms=21_440,
    )
    rubies = [
        RubyAnnotation(
            kanji="私",
            reading="わたし",
            reading_part_ms=[210, 400],
            pos_start_ms=16_000,
            pos_end_ms=16_600,
        ),
        RubyAnnotation(
            kanji="私",
            reading="わたし",
            reading_part_ms=[180, 450],
            pos_start_ms=17_200,
            pos_end_ms=17_800,
        ),
        RubyAnnotation(
            kanji="私",
            reading="わたし",
            reading_part_ms=[210, 380],
            pos_start_ms=18_870,
            pos_end_ms=19_460,
        ),
        RubyAnnotation(
            kanji="私",
            reading="わたし",
            reading_part_ms=[220, 820],
            pos_start_ms=20_020,
            pos_end_ms=21_030,
        ),
    ]

    assert _active_rubies_for_line(rubies, previous_line) == rubies[:2]
    assert _active_rubies_for_line(rubies, next_line) == rubies[2:]


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


def test_utopia_karaoke_wipe_uses_ruby_visual_window_for_shared_ruby(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="二", start_ms=13_712, explicit_start=True),
            TimingChar(text="人", start_ms=14_804, explicit_start=False),
        ],
        end_ms=15_897,
    )
    intervals = [(13_712, 14_804), (14_804, 15_897)]
    ranges = [(0, 100), (100, 200)]
    ruby = RubyAnnotation(
        kanji="二人",
        reading="ふたり",
        reading_part_ms=[1_391, 1_824],
        pos_start_ms=13_712,
        pos_end_ms=15_897,
    )
    groups = {0: ([0, 1], ruby), 1: ([0, 1], ruby)}
    style = Style(karaoke_anim="utopia")
    transition = _LineCharTransition(
        phase="utopia",
        effect="utopia",
        progress=1.0,
        start_ms=13_712,
        end_ms=15_897,
    )

    start, end = _utopia_wipe_window_for_index(
        line,
        intervals,
        ranges,
        groups,
        1,
        style,
        fallback_start=intervals[1][0],
        fallback_end=intervals[1][1],
    )

    assert start > intervals[1][0]
    assert start == 15_320
    before_visual_wipe = _transition_char_state(
        style,
        transition,
        1,
        2,
        char_start_ms=start,
        char_end_ms=end,
        t_ms=15_100,
    )
    during_visual_wipe = _transition_char_state(
        style,
        transition,
        1,
        2,
        char_start_ms=start,
        char_end_ms=end,
        t_ms=15_400,
    )

    assert before_visual_wipe == (1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0)
    assert during_visual_wipe[4] > 1.0
    assert during_visual_wipe[5] > 1.0


@pytest.mark.parametrize("marker_text", ["^", " "])
def test_utopia_linked_english_marker_keeps_syllable_wipe_timing(
    qapp, marker_text: str
):
    line = TimingLine(
        chars=[
            TimingChar(text="D", start_ms=1000),
            TimingChar(text="o", start_ms=1100),
            TimingChar(text=" ", start_ms=1200),
            TimingChar(text="y", start_ms=1500),
            TimingChar(text="o", start_ms=1600),
            TimingChar(text="u", start_ms=1700),
        ],
        end_ms=1800,
    )
    intervals = [
        (1000, 1100),
        (1100, 1200),
        (1200, 1500),
        (1500, 1600),
        (1600, 1700),
        (1700, 1800),
    ]
    ranges = [(0, 20), (20, 40), (40, 50), (50, 70), (70, 90), (90, 110)]
    marker = RubyAnnotation(
        kanji="Do you",
        reading=marker_text,
        reading_parts=[marker_text],
        pos_start_ms=1000,
        pos_end_ms=1800,
    )

    # The marker still groups the full phrase for Utopia transitions.
    assert _utopia_main_group_for_index([marker], line, intervals, 0)[0] == list(range(6))

    # The wipe no longer interpolates uniformly over the whole phrase.  At the
    # second syllable boundary, the first three timing units are complete and
    # the following syllable has not started.
    segments = _karaoke_fill_segments(
        [20, 20, 10, 20, 20, 20],
        intervals,
        ranges,
        [marker],
        line,
    )
    assert [segment.ruby for segment in segments] == [None] * 6
    # N3 treats touching DrawRight/DrawLeft boxes as overlap (>=) and keeps
    # its +1 denominator, so the completed third unit stops fractionally short.
    assert _fill_extent_end(segments, 1500) == pytest.approx(
        40 + 10 * (10 / 11)
    )
    assert _character_fill_ratio(line, intervals, ranges, [marker], 2, 1350) == 0.5
    assert _character_fill_ratio(line, intervals, ranges, [marker], 3, 1350) == 0.0


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
    assert _build_ruby_font(style).pixelSize() == 45
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


def test_ruby_gradient_reference_uses_n3_ruby_line_box(qapp):
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

    expected = subtitle_painter._n3_ruby_fill_rect(
        ruby_layout.x,
        ruby_layout.target_width,
        ruby_layout.baseline_y,
        layout.ruby_metrics,
        ruby_layout.style,
    )
    assert ruby_layout.gradient_rect == expected
    assert ruby_layout.gradient_rect.left() == pytest.approx(ruby_layout.x)
    assert ruby_layout.gradient_rect.width() == pytest.approx(
        ruby_layout.target_width
    )
    assert ruby_layout.gradient_rect.height() < layout.line_rect.height()


def test_ruby_horizontal_gradient_shares_main_line_progress_box_by_default(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="A", start_ms=1000),
            TimingChar(text="B", start_ms=1500),
        ],
        end_ms=2000,
    )
    rubies = [
        RubyAnnotation(
            kanji="A",
            reading="a",
            pos_start_ms=1000,
            pos_end_ms=1500,
        ),
        RubyAnnotation(
            kanji="B",
            reading="b",
            pos_start_ms=1500,
            pos_end_ms=2000,
        ),
    ]
    track = TimingTrack(lines=[line], rubies=rubies)
    style = Style(font_size_px=64, ruby_font_size_px=28)
    layout = _layout_line(track, line, style, 420, 240, baseline_y=140)
    assert layout is not None and layout.ruby_metrics is not None

    ruby_layouts = _layout_rubies(
        layout.ruby_metrics,
        line,
        layout.intervals,
        layout.char_x_ranges,
        layout.baseline_y,
        rubies,
        style,
        text_layout=layout.text_layout,
    )

    assert len(ruby_layouts) == 2
    shared = ruby_layouts[0].horizontal_gradient_rect
    assert shared is not None
    assert all(item.horizontal_gradient_rect == shared for item in ruby_layouts)
    main_rect = subtitle_painter._n3_main_fill_rect(
        layout.text_layout, layout.baseline_y
    )
    assert shared.left() == pytest.approx(main_rect.left())
    assert shared.width() == pytest.approx(main_rect.width())
    assert shared.top() <= min(item.gradient_rect.top() for item in ruby_layouts)
    assert shared.bottom() >= main_rect.bottom()

    disabled = replace(style, ruby_horizontal_gradient_with_main=False)
    disabled_layouts = _layout_rubies(
        layout.ruby_metrics,
        line,
        layout.intervals,
        layout.char_x_ranges,
        layout.baseline_y,
        rubies,
        disabled,
        text_layout=layout.text_layout,
    )
    assert all(item.horizontal_gradient_rect is None for item in disabled_layouts)


def test_shared_ruby_gradient_box_is_only_used_for_horizontal_fills():
    local = QRectF(100.0, 20.0, 40.0, 30.0)
    shared = QRectF(10.0, 5.0, 240.0, 80.0)

    horizontal = PaintFill(mode="gradient_horizontal")
    vertical = PaintFill(mode="gradient_vertical")

    assert subtitle_painter._fill_brush_rect(horizontal, local, shared) == shared
    assert subtitle_painter._fill_brush_rect(vertical, local, shared) == local


def test_ruby_shared_horizontal_gradient_changes_rendered_progress(qapp):
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
                kanji="A", reading="a", pos_start_ms=1000, pos_end_ms=1500
            ),
            RubyAnnotation(
                kanji="B", reading="b", pos_start_ms=1500, pos_end_ms=2000
            ),
        ],
    )
    gradient = PaintFill(
        mode="gradient_horizontal",
        gradient_stops=((0, "#FF0000"), (100, "#0000FF")),
    )
    colors = KaraokeColors(
        before=KaraokeColorState(text=gradient),
        after=KaraokeColorState(text=gradient),
    )
    style = Style(
        font_size_px=72,
        ruby_font_size_px=32,
        stroke_width_px=0,
        stroke2_enabled=False,
        ruby_stroke_width_px=0,
        ruby_stroke2_enabled=False,
        decoration_kind="none",
        karaoke_colors=colors,
        line_y_position="center",
        dual_line_layout=False,
    )
    shared = _blank()
    grouped = _blank()

    paint_frame(shared, track, 2000, style)
    paint_frame(
        grouped,
        track,
        2000,
        replace(style, ruby_horizontal_gradient_with_main=False),
    )

    assert _pixel_hash(shared) != _pixel_hash(grouped)


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


@pytest.mark.parametrize(
    ("reading", "expected_family", "expected_size", "expected_weight"),
    [
        ("こう", "Times New Roman", 46, 700),
        ("abc", "Courier New", 34, 500),
    ],
)
def test_role_ruby_layout_uses_target_role_font_resources(
    qapp, reading, expected_family, expected_size, expected_weight
):
    line = TimingLine(
        chars=[TimingChar(text="項", start_ms=0, role_label="lead")],
        end_ms=1_200,
    )
    ruby = RubyAnnotation(
        kanji="項",
        reading=reading,
        pos_start_ms=0,
        pos_end_ms=1_200,
    )
    style = Style(
        ruby_font_follow_main=False,
        ruby_font_family="Arial",
        ruby_font_family_latin="Arial",
        ruby_font_size_px=24,
        ruby_font_weight=400,
        custom_style_schemes={
            "lead": SubtitleStyleScheme(
                ruby_font_follow_main=False,
                ruby_font_family="Times New Roman",
                ruby_font_family_latin="Courier New",
                ruby_font_size_px=46,
                ruby_font_weight=700,
                ruby_latin_font_size_px=34,
                ruby_latin_font_weight=500,
                ruby_stroke_width_px=7,
                ruby_decoration_kind="shadow",
                ruby_shadow_offset_x=4,
                ruby_shadow_offset_y=5,
            )
        },
    )
    layout = _layout_line(
        TimingTrack(lines=[line], rubies=[ruby]), line, style, 640, 360
    )
    assert layout is not None
    ruby_layout = layout.ruby_layouts[0]
    assert ruby_layout.font is not None
    assert ruby_layout.metrics is not None
    assert ruby_layout.font.family() == expected_family
    assert ruby_layout.font.pixelSize() == expected_size
    assert ruby_layout.font.weight() == subtitle_painter._clamp_weight(expected_weight)
    assert ruby_layout.style.ruby_stroke_width_px == 7
    assert ruby_layout.style.ruby_shadow_offset_x == 4
    assert ruby_layout.style.ruby_shadow_offset_y == 5

    layers = _ruby_layer_stack(layout, line, 600, style)
    assert layers
    assert all(layer.ruby_font.family() == expected_family for layer in layers)
    assert all(layer.ruby_font.pixelSize() == expected_size for layer in layers)


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
    assert _bounds_size(_ink_bounds(bounced))[1] >= _bounds_size(_ink_bounds(plain))[1]


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
    assert first.transform().m11() == pytest.approx(1.0)
    assert scaled.transform().m11() == pytest.approx(1.5)
    assert len(_IMAGE_FILL_CACHE) == 1
    assert len(_IMAGE_BRUSH_CACHE) == 2


def test_image_fill_failure_warns_once_per_path(qapp, tmp_path, caplog):
    clear_before_layer_cache()
    painter_log = "krok_helper.subtitle_render.painter"

    missing_path = tmp_path / "missing.png"
    with caplog.at_level("WARNING", logger=painter_log):
        assert subtitle_painter._image_file_signature(str(missing_path)) is None
        assert subtitle_painter._image_file_signature(str(missing_path)) is None
    missing_warnings = [
        record
        for record in caplog.records
        if "字幕图片填充被跳过" in record.message
        and str(missing_path) in record.getMessage()
    ]
    assert len(missing_warnings) == 1

    bad_path = tmp_path / "bad.png"
    bad_path.write_bytes(b"not an image")
    with caplog.at_level("WARNING", logger=painter_log):
        assert subtitle_painter._cached_fill_image((str(bad_path), 0, 0)) is None
        assert subtitle_painter._cached_fill_image((str(bad_path), 0, 0)) is None
    bad_warnings = [
        record
        for record in caplog.records
        if "字幕图片填充被跳过" in record.message
        and str(bad_path) in record.getMessage()
    ]
    assert len(bad_warnings) == 1


def test_image_fill_wraps_from_the_canvas_origin(qapp, tmp_path):
    clear_before_layer_cache()
    image_path = tmp_path / "pattern.png"
    source = QImage(2, 1, QImage.Format.Format_ARGB32_Premultiplied)
    source.setPixelColor(0, 0, QColor("#FF0000"))
    source.setPixelColor(1, 0, QColor("#0000FF"))
    assert source.save(str(image_path))

    fill = PaintFill(mode="image", image_path=str(image_path), image_scale_pct=100)
    first = _brush_for_fill(fill, QRectF(7, 11, 20, 10))
    second = _brush_for_fill(fill, QRectF(101, 203, 20, 10))
    assert first.transform() == second.transform()

    canvas = QImage(8, 1, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(0)
    painter = QPainter(canvas)
    painter.fillRect(QRectF(0, 0, 8, 1), first)
    painter.end()
    assert [canvas.pixelColor(x, 0).name() for x in range(8)] == [
        "#ff0000",
        "#0000ff",
    ] * 4


def test_image_body_protects_primary_stroke_under_transparent_pixels(qapp, tmp_path):
    image_path = tmp_path / "alpha.png"
    source = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(QColor(255, 255, 255, 128))
    assert source.save(str(image_path))
    image_fill = PaintFill(
        mode="image", image_path=str(image_path), image_scale_pct=100
    )
    state = KaraokeColorState(
        text=image_fill,
        stroke=PaintFill(color="#FF0000"),
        stroke2=PaintFill(color="#000000"),
        shadow=PaintFill(color="#000000"),
    )
    path = QPainterPath()
    path.addRect(QRectF(10, 10, 20, 20))
    canvas = QImage(40, 40, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(0)
    painter = QPainter(canvas)
    _paint_text_layer_stack(
        painter,
        path,
        QRectF(10, 10, 20, 20),
        state,
        Style(decoration_kind="none"),
        stroke_width=8,
        stroke2_width=0,
        shadow_dx=0,
        shadow_dy=0,
        glow_radius=0,
    )
    painter.end()

    # The translucent bitmap stays translucent white instead of blending with
    # the red primary edge beneath it; the outside half remains visible.
    inside = canvas.pixelColor(12, 20)
    assert inside.red() == inside.green() == inside.blue() == 255
    assert 120 <= inside.alpha() <= 136
    assert canvas.pixelColor(8, 20).red() > 200


def test_none_decoration_draws_neither_shadow_nor_glow(qapp):
    state = KaraokeColorState(
        text=PaintFill(color="#FFFFFF"),
        stroke=PaintFill(color="#00000000"),
        stroke2=PaintFill(color="#00000000"),
        shadow=PaintFill(color="#FF0000"),
    )
    path = QPainterPath()
    path.addRect(QRectF(16, 16, 20, 20))

    def render(kind: str, offset: int) -> QImage:
        canvas = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(0)
        painter = QPainter(canvas)
        _paint_text_layer_stack(
            painter,
            path,
            QRectF(16, 16, 20, 20),
            state,
            Style(decoration_kind=kind),
            stroke_width=0,
            stroke2_width=0,
            shadow_dx=offset,
            shadow_dy=offset,
            glow_radius=16,
        )
        painter.end()
        return canvas

    no_decoration = render("none", 8)
    plain = render("none", 0)
    shadow = render("shadow", 8)

    assert _pixel_hash(no_decoration) == _pixel_hash(plain)
    assert _pixel_hash(no_decoration) != _pixel_hash(shadow)


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

    assert _ink_bounds(before_only) == _ink_bounds(fully_sung)


def test_image_fill_cached_layer_matches_canvas_anchored_direct_path(
    qapp, tmp_path, monkeypatch
):
    image_path = tmp_path / "canvas-pattern.png"
    source = QImage(7, 5, QImage.Format.Format_ARGB32_Premultiplied)
    for y in range(source.height()):
        for x in range(source.width()):
            source.setPixelColor(
                x,
                y,
                QColor("#F04A4A") if (x + y) % 3 == 0 else QColor("#4A70F0"),
            )
    assert source.save(str(image_path))
    fill = PaintFill(mode="image", image_path=str(image_path), image_scale_pct=175)
    colors = KaraokeColors(
        before=KaraokeColorState(text=fill),
        after=KaraokeColorState(text=fill),
    )
    style = Style(
        font_size_px=72,
        stroke_width_px=0,
        stroke2_width_px=0,
        decoration_kind="none",
        line_y_position="center",
        karaoke_colors=colors,
    )

    monkeypatch.setenv("KROK_SUBTITLE_HORIZONTAL_LAYER", "0")
    clear_before_layer_cache()
    direct = _blank()
    paint_frame(direct, _track(), 1300, style)

    monkeypatch.setenv("KROK_SUBTITLE_HORIZONTAL_LAYER", "1")
    clear_before_layer_cache()
    cached = _blank()
    paint_frame(cached, _track(), 1300, style)
    clear_before_layer_cache()

    diff = np.abs(
        _img_rows_rgba(direct).astype(int) - _img_rows_rgba(cached).astype(int)
    )
    assert diff.max() <= 1


def test_paint_frame_compression_keeps_entry_and_can_remove_exit_animation(qapp):
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
    at_exit_with_tail = _blank()
    after_line_blank = _blank()

    # 该行走字 1000–2500，line_tail_ms=0 → 退场余量为 0，有效退场动画被压到
    # 0 ms；入场仍使用走字开始前的显示区间。
    paint_frame(at_entry_static, track, 500, static)
    paint_frame(at_entry_animated, track, 500, animated)
    paint_frame(at_exit_static, track, 2300, static)
    paint_frame(at_exit_animated, track, 2300, animated)
    paint_frame(
        at_exit_with_tail,
        track,
        2900,
        replace(animated, line_tail_ms=600),
    )

    assert _pixel_hash(at_entry_static) != _pixel_hash(at_entry_animated)
    assert _pixel_hash(at_exit_static) == _pixel_hash(at_exit_animated)
    assert _pixel_hash(at_exit_with_tail) != _pixel_hash(after_line_blank)


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


def test_char_drip_uses_opaque_right_edge_shear_like_nkm3(qapp):
    track = _track()
    line = track.lines[0]
    style = Style(line_y_position="center", entry_anim="char_drip")
    layout = _layout_line(track, line, style, 800, 450)
    count = len(line.chars)
    transition = _LineCharTransition(
        phase="entry", effect="char_drip", progress=1.0, start_ms=1000, end_ms=1600,
    )
    t_ms = 1125  # first character transform progress = 0.5

    layers = _char_transition_layer_stack(layout, t_ms, transition, count)
    before_by_index = {
        layer.glyphs[0].index: layer
        for layer in layers
        if isinstance(layer, _GlyphRunLayer) and not layer.after
    }

    glyph = layout.text_layout.glyphs[0]
    progress = _char_fade_opacity(transition, 0, count, t_ms=t_ms)
    layer = before_by_index[0]
    assert progress == pytest.approx(0.5)
    # N3 CharDrip does not push an opacity layer: opacity is only transform progress.
    assert layer.fade_opacity == pytest.approx(1.0)
    expected = _character_transform(
        center_x=glyph.left + glyph.width,
        center_y=layout.baseline_y,
        skew_y=_spin_flip_skew(progress),
    )
    assert layer.transform == expected
    # The right-edge pivot remains fixed under the vertical shear.
    pivot = QPointF(glyph.left + glyph.width, layout.baseline_y)
    assert layer.transform.map(pivot) == pivot


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


def test_utopia_transformed_glow_keeps_n3_kernel_with_cache_enabled(
    qapp, monkeypatch
):
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="A", start_ms=1000)],
                end_ms=2000,
            )
        ]
    )
    style = Style(
        font_family="Arial",
        font_size_px=72,
        line_lead_in_ms=700,
        line_tail_ms=1200,
        line_y_position="center",
        stroke_width_px=0,
        stroke2_width_px=0,
        decoration_kind="glow",
        glow_before_radius_px=12,
        glow_after_radius_px=12,
        entry_anim="utopia",
        exit_anim="utopia",
    )

    def render(t_ms: int, cache_enabled: bool) -> str:
        monkeypatch.setenv(
            "KROK_SUBTITLE_GLOW_CACHE", "1" if cache_enabled else "0"
        )
        clear_before_layer_cache()
        image = _blank()
        paint_frame(image, track, t_ms, style)
        return _pixel_hash(image)

    # N3 transforms geometry before blur, so enabling the upright-glow cache
    # must not scale the blur kernel during either intro or outro.
    for t_ms in (400, 2700):
        assert render(t_ms, True) == render(t_ms, False)


def test_utopia_gradient_glow_caches_alpha_mask_not_coloured_bitmap(
    qapp, monkeypatch
):
    gradient = PaintFill(
        mode="gradient_vertical",
        gradient_stops=[(0, "#FF0000"), (100, "#0000FF")],
    )
    colors = KaraokeColors(
        before=KaraokeColorState(
            text=gradient,
            stroke=_solid_fill("#FFFFFF"),
            shadow=gradient,
        ),
        after=KaraokeColorState(
            text=_solid_fill("#FFFFFF"),
            stroke=_solid_fill("#FFFFFF"),
            shadow=gradient,
        ),
    )
    style = Style(
        decoration_kind="glow",
        karaoke_colors=colors,
        line_y_position="center",
        entry_anim="utopia",
        exit_anim="utopia",
    )
    blits = 0
    original = subtitle_painter._blit_cached_run_glow

    def _count_blits(*args, **kwargs):
        nonlocal blits
        blits += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(subtitle_painter, "_blit_cached_run_glow", _count_blits)
    clear_before_layer_cache()
    paint_frame(_blank(), _track(), 1100, style)

    # Utopia transforms glyph geometry only.  Baking a coloured gradient halo
    # and then transforming the bitmap would incorrectly transform the brush;
    # the cache therefore stores an uncoloured alpha mask and tints it after
    # the transform in the fixed line coordinate system.
    assert blits > 0
    populated = len(_RUN_GLOW_CACHE)
    assert populated > 0
    paint_frame(_blank(), _track(), 1100, style)
    assert len(_RUN_GLOW_CACHE) == populated


def test_utopia_glow_mask_is_tinted_after_transform_in_fixed_line_space(
    qapp, monkeypatch
):
    mask = QImage(10, 10, QImage.Format.Format_ARGB32_Premultiplied)
    mask.fill(QColor("#FFFFFF"))
    monkeypatch.setattr(
        subtitle_painter,
        "_get_or_build_run_glow_mask",
        lambda *_args, **_kwargs: BakedLayer(mask, QPointF(0.0, 0.0)),
    )
    canvas = QImage(80, 100, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(0)
    painter = QPainter(canvas)
    try:
        subtitle_painter._blit_tinted_run_glow_mask(
            painter,
            [SimpleNamespace(left=10)],
            10,
            Style(),
            PaintFill(
                mode="gradient_vertical",
                gradient_stops=[(0, "#FF0000"), (100, "#0000FF")],
            ),
            after=False,
            transform=QTransform.fromTranslate(0.0, 50.0),
            fill_rect=QRectF(0.0, 0.0, 80.0, 100.0),
        )
    finally:
        painter.end()

    # The mask started near y=10 but moved to y=60.  N3 keeps the gradient in
    # line coordinates, so it must sample the blue-dominant lower half after
    # the geometry transform instead of carrying the red source colour along.
    colour = canvas.pixelColor(15, 65)
    assert colour.blue() > colour.red()


def _image_rgba_array(img: QImage) -> np.ndarray:
    rgba = img.convertToFormat(QImage.Format.Format_RGBA8888)
    bits = rgba.constBits()
    bits.setsize(rgba.sizeInBytes())
    return (
        np.frombuffer(bytes(bits), dtype=np.uint8)
        .reshape(rgba.height(), rgba.width(), 4)
        .copy()
    )


def _glow_split_style(*, before_radius: int = 12, after_radius: int = 12) -> Style:
    colors = KaraokeColors(
        before=KaraokeColorState(
            text=_solid_fill("#FFFFFF"),
            stroke=_solid_fill("#FFFFFF"),
            shadow=_solid_fill("#FF0000"),
        ),
        after=KaraokeColorState(
            text=_solid_fill("#FFFFFF"),
            stroke=_solid_fill("#FFFFFF"),
            shadow=_solid_fill("#0000FF"),
        ),
    )
    return Style(
        decoration_kind="glow",
        glow_before_radius_px=before_radius,
        glow_after_radius_px=after_radius,
        karaoke_colors=colors,
        stroke_width_px=0,
        line_y_position="center",
        entry_anim="utopia",
        exit_anim="none",
    )


def test_utopia_wipe_source_splits_before_after_glow_at_scanline(qapp, monkeypatch):
    """N3 在模糊前分割发光源；离锋线足够远时各自颜色应占主导。

    单字行唱到一半：字形左侧（已唱侧）halo 应为纯已唱发光色（蓝），右侧
    （未唱侧）应为纯未唱发光色（红）。修复前未唱发光整字铺满 → 已唱侧红蓝
    混色。缓存 blit 与逐帧矢量两条 glow 子路径都要满足。
    """
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar(text="あ", start_ms=1000)], end_ms=2000)]
    )
    t_mid = 1500  # ratio=0.5，front 落在字形墨水中部

    for cache_flag in ("1", "0"):
        monkeypatch.setenv("KROK_SUBTITLE_GLOW_CACHE", cache_flag)
        clear_before_layer_cache()

        # 无发光渲染同帧同变换，取字形墨水包围盒（含 utopia 弹跳缩放）。
        body = _blank()
        paint_frame(body, track, t_mid, _glow_split_style(before_radius=0, after_radius=0))
        arr_body = _image_rgba_array(body).astype(int)
        lit = arr_body[:, :, :3].max(axis=2) > 60
        ys, xs = np.nonzero(lit)
        assert xs.size > 0, "字形必须有墨水"
        ink_left, ink_right = int(xs.min()), int(xs.max())
        cy = int(round((ys.min() + ys.max()) / 2))

        glow = _blank()
        paint_frame(glow, track, t_mid, _glow_split_style())
        arr = _image_rgba_array(glow).astype(int)

        # 扫光线 ≈ 墨水中线（ratio=0.5，front 按变换后墨水包围盒比例取）。
        front = (ink_left + ink_right) // 2
        reach = 30  # 半径 12 的 halo 外扩上界
        y0 = max(int(ys.min()) - reach, 0)
        y1 = min(int(ys.max()) + reach, arr.shape[0] - 1)

        def _halo_sums(x0: int, x1: int) -> tuple[int, int]:
            region = arr[y0 : y1 + 1, max(x0, 0) : x1 + 1]
            r, g, b = region[:, :, 0], region[:, :, 1], region[:, :, 2]
            # 纯红/纯蓝 halo 的 g≈0；白色字身/描边 g 高——用 g 通道剔除字身。
            halo = (g < 60) & ((r > 25) | (b > 25))
            return int(r[halo].sum()), int(b[halo].sum())

        left_r, left_b = _halo_sums(ink_left - reach, front - 8)  # 已唱侧
        right_r, right_b = _halo_sums(front + 8, ink_right + reach)  # 未唱侧
        assert left_b > 0 and right_r > 0, "两侧都必须有 halo"
        assert left_r * 4 < left_b, (
            f"cache={cache_flag} 已唱侧 halo 混入未唱发光: r_sum={left_r} b_sum={left_b}"
        )
        assert right_b * 4 < right_r, (
            f"cache={cache_flag} 未唱侧 halo 混入已唱发光: r_sum={right_r} b_sum={right_b}"
        )


def test_utopia_transformed_glow_does_not_join_cached_far_halves(
    qapp, monkeypatch
):
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar(text="た", start_ms=1000)], end_ms=2000)]
    )
    target_clips: list[QRectF | None] = []
    original = subtitle_painter._paint_split_glow_path

    def _record_split(*args, **kwargs):
        target_clips.append(kwargs.get("target_clip"))
        return original(*args, **kwargs)

    def _unexpected_cached_join(*args, **kwargs):
        raise AssertionError("transformed Utopia glow must not use strip/cache joins")

    monkeypatch.setenv("KROK_SUBTITLE_GLOW_CACHE", "1")
    monkeypatch.setattr(subtitle_painter, "_paint_split_glow_path", _record_split)
    monkeypatch.setattr(
        subtitle_painter,
        "_paint_cached_run_split_glow_source_wipe",
        _unexpected_cached_join,
    )
    clear_before_layer_cache()

    paint_frame(_blank(), track, 1500, _glow_split_style())

    assert target_clips
    assert all(clip is None for clip in target_clips)


def test_utopia_completed_glow_ignores_disabled_stroke2_saved_width(qapp):
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar(text="た", start_ms=1000)], end_ms=2000)]
    )
    zero_width = replace(
        _glow_split_style(),
        stroke2_enabled=False,
        stroke2_width_px=0,
    )
    stale_width = replace(zero_width, stroke2_width_px=40)

    clear_before_layer_cache()
    stale_frame = _blank()
    paint_frame(stale_frame, track, 2000, stale_width)
    populated = len(_RUN_GLOW_CACHE)
    assert populated > 0

    zero_frame = _blank()
    paint_frame(zero_frame, track, 2000, zero_width)

    assert _pixel_hash(stale_frame) == _pixel_hash(zero_frame)
    assert len(_RUN_GLOW_CACHE) == populated


def test_utopia_shadow_splits_source_before_bitmap_offset(qapp, monkeypatch):
    colors = KaraokeColors(
        before=KaraokeColorState(shadow=_solid_fill("#CC9966")),
        after=KaraokeColorState(shadow=_solid_fill("#6699CC")),
    )
    style = Style(
        decoration_kind="shadow",
        shadow_offset_x=12,
        shadow_offset_y=7,
        stroke_width_px=0,
    )
    clips: list[tuple[str, QRectF]] = []

    def _record_shadow(painter, _path, fill, *_args, **_kwargs):
        clips.append((fill.color, painter.clipBoundingRect()))

    monkeypatch.setattr(
        subtitle_painter, "_paint_shadow_silhouette", _record_shadow
    )
    image = QImage(300, 180, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    path = QPainterPath()
    path.addRect(QRectF(100, 40, 100, 80))
    try:
        subtitle_painter._paint_char_karaoke_stack(
            painter,
            path,
            path.boundingRect(),
            char_x=100,
            char_width=100,
            baseline_y=120,
            metrics=QFontMetrics(_build_font(style)),
            colors=colors,
            style=style,
            ratio=0.5,
            clip_rect=QRectF(100, 40, 100, 80),
            geometry_transform=QTransform.fromScale(1.1, 1.1),
        )
    finally:
        painter.end()

    assert [color for color, _clip in clips] == ["#CC9966", "#6699CC"]
    # N3 clips the source at x=150, then offsets the completed shadow bitmap
    # by +12.  The two shadow colours therefore meet at x=162, not x=150.
    assert clips[0][1].left() == pytest.approx(162.0)
    assert clips[1][1].right() == pytest.approx(162.0)


def test_static_wipe_source_splits_before_after_glow_at_scanline(qapp, monkeypatch):
    """静态路径同样在 blur 前互补裁剪两种发光源。"""
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar(text="あ", start_ms=1000)], end_ms=2000)]
    )
    t_mid = 1500
    static_style = replace(_glow_split_style(), entry_anim="none", exit_anim="none")
    static_body = replace(
        _glow_split_style(before_radius=0, after_radius=0),
        entry_anim="none",
        exit_anim="none",
    )

    for layer_flag in ("1", "0"):
        monkeypatch.setenv("KROK_SUBTITLE_HORIZONTAL_LAYER", layer_flag)
        clear_before_layer_cache()

        body = _blank()
        paint_frame(body, track, t_mid, static_body)
        arr_body = _image_rgba_array(body).astype(int)
        ys, xs = np.nonzero(arr_body[:, :, :3].max(axis=2) > 60)
        assert xs.size > 0
        ink_left, ink_right = int(xs.min()), int(xs.max())

        glow = _blank()
        paint_frame(glow, track, t_mid, static_style)
        arr = _image_rgba_array(glow).astype(int)

        front = (ink_left + ink_right) // 2
        reach = 30
        y0 = max(int(ys.min()) - reach, 0)
        y1 = min(int(ys.max()) + reach, arr.shape[0] - 1)

        def _halo_sums(x0: int, x1: int) -> tuple[int, int]:
            region = arr[y0 : y1 + 1, max(x0, 0) : x1 + 1]
            r, g, b = region[:, :, 0], region[:, :, 1], region[:, :, 2]
            halo = (g < 60) & ((r > 25) | (b > 25))
            return int(r[halo].sum()), int(b[halo].sum())

        left_r, left_b = _halo_sums(ink_left - reach, front - 8)
        right_r, right_b = _halo_sums(front + 8, ink_right + reach)
        assert left_b > 0 and right_r > 0
        assert left_r * 4 < left_b, (
            f"layer={layer_flag} 已唱侧 halo 混入未唱发光: r_sum={left_r} b_sum={left_b}"
        )
        assert right_b * 4 < right_r, (
            f"layer={layer_flag} 未唱侧 halo 混入已唱发光: r_sum={right_r} b_sum={right_b}"
        )


def test_static_glow_front_blends_outside_ink_instead_of_forming_hard_seam(
    qapp, monkeypatch
):
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar(text="あ", start_ms=1000)], end_ms=2000)]
    )
    style = replace(_glow_split_style(), entry_anim="none", exit_anim="none")
    body_style = replace(
        _glow_split_style(before_radius=0, after_radius=0),
        entry_anim="none",
        exit_anim="none",
    )
    body = _blank()
    paint_frame(body, track, 1500, body_style)
    body_pixels = _image_rgba_array(body).astype(int)
    ys, xs = np.nonzero(body_pixels[:, :, :3].max(axis=2) > 60)
    assert xs.size > 0
    front = (int(xs.min()) + int(xs.max())) // 2
    sample_y = int(ys.min()) - 10
    assert sample_y >= 0

    for layer_flag in ("1", "0"):
        monkeypatch.setenv("KROK_SUBTITLE_HORIZONTAL_LAYER", layer_flag)
        clear_before_layer_cache()
        glow = _blank()
        paint_frame(glow, track, 1500, style)
        pixels = _image_rgba_array(glow).astype(int)
        samples = pixels[sample_y, front - 2 : front + 3, :3]

        # N3 clips the red/blue outline sources at WipeLeft and then blurs the
        # result.  Above the glyph ink both halos must cross the boundary
        # smoothly; post-blur clipping would leave one channel at background
        # level on either side and expose a hard vertical seam.
        assert int(samples[:, 0].min()) > 20, f"layer={layer_flag} red halo was hard-clipped"
        assert int(samples[:, 2].min()) > 20, f"layer={layer_flag} blue halo was hard-clipped"


def test_ruby_keeps_unsung_reading_during_char_transition(qapp):
    """入退场过渡窗口与走字重叠时，未唱读音不得消失。

    修复前 _paint_ruby_karaoke_path 在 wiping 时强制 ratio=1.0，fragment 跳过
    before 层 → 只剩已唱裁剪带内的读音，未唱注音在过渡窗口内整体不可见。
    """
    line = TimingLine(
        chars=[TimingChar(text="漢", start_ms=1000), TimingChar(text="字", start_ms=2000)],
        end_ms=3000,
    )
    ruby = RubyAnnotation(kanji="漢字", reading="かんじ", pos_start_ms=1000, pos_end_ms=3000)
    track = TimingTrack(lines=[line], rubies=[ruby])
    track_no_ruby = TimingTrack(lines=[line])
    # lead_in=0 让 char_fade 入场窗口与第一个字的走字重叠。
    style = Style(
        line_y_position="center",
        line_lead_in_ms=0,
        entry_anim="char_fade",
        exit_anim="none",
    )

    def _ruby_ink_count(t_ms: int, base_style: Style) -> int:
        with_ruby = _blank()
        without_ruby = _blank()
        paint_frame(with_ruby, track, t_ms, base_style)
        paint_frame(without_ruby, track_no_ruby, t_ms, base_style)
        a = _image_rgba_array(with_ruby).astype(int)
        b = _image_rgba_array(without_ruby).astype(int)
        return int((np.abs(a - b).sum(axis=2) > 30).sum())

    # 参照：无逐字过渡（静态路径）下同帧的注音墨水量。
    reference = _ruby_ink_count(1200, replace(style, entry_anim="none"))
    transitioned = _ruby_ink_count(1200, style)
    assert reference > 0
    # char_fade 只影响透明度（该时刻首字透明度已接近 1），未唱读音必须仍然在。
    assert transitioned > reference * 0.6, (
        f"过渡窗口内注音墨水骤减: {transitioned} vs 参照 {reference}"
    )


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

    paint_frame(plain, track, 3599, base)
    paint_frame(utopia, track, 3599, replace(base, exit_anim="utopia"))

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


@pytest.mark.parametrize("exit_effect", ["char_fade", "spin_flip"])
def test_utopia_entry_does_not_override_active_character_exit(exit_effect):
    line = _track().lines[0]
    style = Style(
        entry_anim="utopia",
        exit_anim=exit_effect,
        exit_fade_ms=1000,
    )

    transition = subtitle_painter._line_char_transition_context(
        style,
        line,
        3000,
        0,
        3500,
        len(line.chars),
    )

    assert transition is not None
    assert transition.phase == "exit"
    assert transition.effect == exit_effect


@pytest.mark.parametrize("entry_effect", ["char_fade", "spin_flip"])
def test_utopia_exit_does_not_override_active_character_entry(entry_effect):
    line = _track().lines[0]
    style = Style(
        entry_anim=entry_effect,
        entry_lead_ms=1000,
        exit_anim="utopia",
    )

    transition = subtitle_painter._line_char_transition_context(
        style,
        line,
        300,
        0,
        3500,
        len(line.chars),
    )

    assert transition is not None
    assert transition.phase == "entry"
    assert transition.effect == entry_effect


def test_utopia_keeps_one_render_path_before_and_during_wipe(qapp):
    from krok_helper.subtitle_render.engine.painter import (
        _line_char_transition_context,
        display_windows_for_style,
    )
    from krok_helper.subtitle_render.engine.timeline import compute_char_intervals

    track = _track()
    line = track.lines[0]
    colors = KaraokeColors(
        before=KaraokeColorState(
            text=_solid_fill("#E8E8E8"),
            stroke=_solid_fill("#202020"),
            shadow=_solid_fill("#FF8800"),
        ),
        after=KaraokeColorState(
            text=_solid_fill("#E8E8E8"),
            stroke=_solid_fill("#202020"),
            shadow=_solid_fill("#FF8800"),
        ),
    )
    style = Style(
        line_y_position="center",
        font_size_px=90,
        letter_spacing_px=80,
        stroke_width_px=5,
        decoration_kind="glow",
        glow_before_radius_px=8,
        glow_after_radius_px=8,
        karaoke_colors=colors,
        entry_anim="utopia",
    )
    windows = display_windows_for_style(track, style)
    intervals = compute_char_intervals(line)

    before = _line_char_transition_context(
        style, line, 900, *windows[0], len(line.chars), intervals=intervals
    )
    at_boundary = _line_char_transition_context(
        style, line, 1000, *windows[0], len(line.chars), intervals=intervals
    )
    assert before is not None and before.effect == "utopia"
    assert at_boundary is not None and at_boundary.effect == "utopia"

    resting = _blank()
    bouncing = _blank()
    paint_frame(resting, track, 900, style)
    paint_frame(bouncing, track, 1200, style)
    assert _pixel_hash(resting) != _pixel_hash(bouncing)

    # 首字弹跳时，远离它的末字没有运动；持续使用同一绘制路径后，末字的
    # 填充、描边和发光应逐像素保持不变，不再随唱中特效启动而变色。
    layout = _layout_line(track, line, style, 800, 450)
    assert layout is not None
    left, right = layout.char_x_ranges[-1]
    top = layout.baseline_y - 130
    resting_tail = resting.copy(left - 25, top, right - left + 50, 175)
    bouncing_tail = bouncing.copy(left - 25, top, right - left + 50, 175)
    assert _pixel_hash(resting_tail) == _pixel_hash(bouncing_tail)


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


def _utopia_multi_character_group_case():
    line = TimingLine(
        chars=[
            TimingChar(text="A", start_ms=1000),
            TimingChar(text="B", start_ms=1500),
            TimingChar(text="C", start_ms=2000),
        ],
        end_ms=3000,
    )
    ruby = RubyAnnotation(
        kanji="AB",
        reading="ab",
        pos_start_ms=1000,
        pos_end_ms=2000,
    )
    style = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=72,
        line_tail_ms=1500,
        exit_anim="utopia",
        stroke_width_px=0,
        stroke2_width_px=0,
        shadow_offset_x=0,
        shadow_offset_y=0,
    )
    intervals = [(1000, 1500), (1500, 2000), (2000, 3000)]
    char_x_ranges = [(80, 130), (130, 180), (180, 230)]
    transition = _LineCharTransition(
        phase="utopia",
        effect="utopia",
        progress=1.0,
        start_ms=1000,
        end_ms=4500,
    )
    return line, ruby, style, intervals, char_x_ranges, transition


def test_utopia_multi_character_group_transforms_each_main_glyph(qapp, monkeypatch):
    line, ruby, style, intervals, char_x_ranges, transition = _utopia_multi_character_group_case()
    font = _build_font(style)
    metrics = QFontMetrics(font)
    transformed_origins: list[float] = []
    original_transform = subtitle_painter._character_transform

    def record_transform(**kwargs):
        if kwargs["rotation"] != 0.0:
            transformed_origins.append(kwargs["scale_origin_x"])
        return original_transform(**kwargs)

    monkeypatch.setattr(subtitle_painter, "_character_transform", record_transform)
    image = _blank(320, 220)
    painter = QPainter(image)
    try:
        subtitle_painter._paint_line_with_character_transition(
            painter,
            line,
            [50, 50, 50],
            char_x_ranges,
            intervals,
            [ruby],
            font,
            150,
            metrics,
            style,
            subtitle_painter._effective_karaoke_colors(style),
            QRectF(80, 150 - metrics.ascent(), 150, metrics.height()),
            3300,
            transition,
        )
    finally:
        painter.end()

    assert transformed_origins == [80, 130]


def test_utopia_multi_character_group_transforms_each_ruby_glyph(qapp, monkeypatch):
    line, ruby, style, intervals, char_x_ranges, transition = _utopia_multi_character_group_case()
    ruby_font = _build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font)
    transformed_origins: list[float] = []
    original_transform = subtitle_painter._character_transform

    def record_transform(**kwargs):
        if kwargs["rotation"] != 0.0:
            transformed_origins.append(kwargs["scale_origin_x"])
        return original_transform(**kwargs)

    monkeypatch.setattr(subtitle_painter, "_character_transform", record_transform)
    image = _blank(320, 220)
    painter = QPainter(image)
    try:
        subtitle_painter._paint_rubies(
            painter,
            ruby_font,
            ruby_metrics,
            line,
            intervals,
            char_x_ranges,
            150,
            3300,
            [ruby],
            style,
            transition,
        )
    finally:
        painter.end()

    assert len(transformed_origins) == 2
    assert transformed_origins[0] != transformed_origins[1]


def test_utopia_multi_character_ruby_group_applies_exit_opacity_once(qapp, monkeypatch):
    line, ruby, style, intervals, char_x_ranges, transition = _utopia_multi_character_group_case()
    ruby_font = _build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font)
    observed_opacities: list[float] = []

    def record_fragment(painter, *_args, **_kwargs):
        observed_opacities.append(painter.opacity())

    monkeypatch.setattr(subtitle_painter, "_paint_ruby_text_fragment", record_fragment)
    image = _blank(320, 220)
    painter = QPainter(image)
    try:
        subtitle_painter._paint_rubies(
            painter,
            ruby_font,
            ruby_metrics,
            line,
            intervals,
            char_x_ranges,
            150,
            3300,
            [ruby],
            style,
            transition,
        )
    finally:
        painter.end()

    assert observed_opacities == pytest.approx([0.6, 0.6])


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


def _core_pixels(img: QImage, rgb: int) -> set[tuple[int, int]]:
    """严格等于 ``rgb`` 的像素，即被该图层完全覆盖、没混进抗锯齿的核心。"""
    return {
        (x, y)
        for y in range(img.height())
        for x in range(img.width())
        if img.pixel(x, y) & 0xFFFFFF == rgb
    }


def test_title_overlay_draws_below_lyrics(qapp):
    """标题钉在最下层：与歌词重叠处歌词压住标题（GPU compositeOrder 同口径）。"""
    duration_ms = 30_000
    text = "あいうえおかきくけこ"
    meta = TimingTrackMeta(title="曲名", artist="歌手")
    track = TimingTrack(
        meta=meta,
        lines=[
            TimingLine(
                chars=[TimingChar(text=ch, start_ms=1000) for ch in text],
                end_ms=duration_ms,
            )
        ],
    )
    # 同一时长的另一条轨，歌词远在时间轴后段——用来单独量标题自己的覆盖范围。
    offscreen_track = TimingTrack(
        meta=meta,
        lines=[
            TimingLine(
                chars=[TimingChar(text=ch, start_ms=60_000) for ch in text],
                end_ms=70_000,
            )
        ],
    )
    base = Style(
        dual_line_layout=False,
        stroke_width_px=0,
        stroke2_width_px=0,
        decoration_kind="none",
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(text=_solid_fill("#FF0000")),
            after=KaraokeColorState(text=_solid_fill("#FF0000")),
        ),
    )
    schemes = dict(base.custom_style_schemes)
    schemes["标题"] = replace(
        schemes["标题"],
        font_size_px=90,
        stroke_width_px=0,
        stroke2_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        glow_concentration_level=-1,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(text=_solid_fill("#0000FF"))
        ),
    )
    style = replace(
        base,
        custom_style_schemes=schemes,
        title_overlay=TitleOverlay(
            enabled=True,
            # 显式 anchor/offset 生效，避免内置「タイトル左上」布局把标题移开歌词。
            layout_index=None,
            text_template=text,
            fade_in_ms=0,
            fade_out_ms=0,
            anchor="bottom_left",
            offset_x=0,
            offset_y=0,
        ),
    )

    lyrics_only = _blank()
    paint_frame(lyrics_only, track, 500, base, duration_ms=duration_ms)
    title_only = _blank()
    paint_frame(title_only, offscreen_track, 500, style, duration_ms=duration_ms)
    both = _blank()
    paint_frame(both, track, 500, style, duration_ms=duration_ms)

    overlap = _core_pixels(lyrics_only, 0xFF0000) & _core_pixels(title_only, 0x0000FF)
    assert overlap, "测试几何失效：标题与歌词没有互相覆盖的核心像素"
    assert {both.pixel(x, y) & 0xFFFFFF for x, y in overlap} == {0xFF0000}


def test_title_overlay_applies_role_scheme_per_character(qapp):
    track = _title_track()
    base = Style()
    schemes = dict(base.custom_style_schemes)
    schemes["标题"] = replace(
        schemes["标题"],
        font_size_px=48,
        stroke_width_px=0,
        stroke2_width_px=0,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(text=_solid_fill("#FF0000"))
        ),
    )
    schemes["蓝色大字"] = SubtitleStyleScheme(
        font_size_px=80,
        stroke_width_px=0,
        stroke2_width_px=0,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(text=_solid_fill("#0000FF"))
        ),
    )
    uniform = replace(
        base,
        custom_style_schemes=schemes,
        title_overlay=TitleOverlay(
            enabled=True,
            text_template="AB",
            char_role_labels=[[None, None]],
            fade_in_ms=0,
            fade_out_ms=0,
        ),
    )
    mixed = replace(
        uniform,
        title_overlay=replace(
            uniform.title_overlay,
            char_role_labels=[[None, "蓝色大字"]],
        ),
    )

    uniform_image = _blank()
    mixed_image = _blank()
    paint_frame(uniform_image, track, 500, uniform)
    paint_frame(mixed_image, track, 500, mixed)

    assert _pixel_hash(mixed_image) != _pixel_hash(uniform_image)
    mixed_rgba = _img_rows_rgba(mixed_image).reshape(
        mixed_image.height(), mixed_image.width(), 4
    ).astype(np.int16)
    uniform_rgba = _img_rows_rgba(uniform_image).reshape(
        uniform_image.height(), uniform_image.width(), 4
    ).astype(np.int16)
    assert np.any(mixed_rgba[:, :, 2] > mixed_rgba[:, :, 0] + 30)
    assert not np.any(uniform_rgba[:, :, 2] > uniform_rgba[:, :, 0] + 30)


def test_title_row_role_covers_expanded_template(qapp):
    """整行角色跟着展开后的元数据文字走，长出来的字符不退回标题默认配色。"""
    track = TimingTrack(
        meta=TimingTrackMeta(title="とても長いタイトル", artist="歌手"),
        lines=_title_track().lines,
    )
    base = Style()
    schemes = dict(base.custom_style_schemes)
    schemes["标题"] = replace(
        schemes["标题"],
        font_size_px=48,
        stroke_width_px=0,
        stroke2_width_px=0,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(text=_solid_fill("#FF0000"))
        ),
    )
    schemes["蓝色标题"] = SubtitleStyleScheme(
        font_size_px=48,
        stroke_width_px=0,
        stroke2_width_px=0,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(text=_solid_fill("#0000FF"))
        ),
    )
    template = "{title}"
    style = replace(
        base,
        custom_style_schemes=schemes,
        title_overlay=TitleOverlay(
            enabled=True,
            text_template=template,
            char_role_labels=[["蓝色标题"] * len(template)],
            fade_in_ms=0,
            fade_out_ms=0,
        ),
    )

    image = _blank()
    paint_frame(image, track, 500, style)

    rgba = _img_rows_rgba(image).reshape(
        image.height(), image.width(), 4
    ).astype(np.int16)
    assert np.any(rgba[:, :, 2] > rgba[:, :, 0] + 30)  # 角色蓝
    assert not np.any(rgba[:, :, 0] > rgba[:, :, 2] + 30)  # 没有标题默认红


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


def test_title_tail_windows_use_project_duration_and_two_segment_head_tail(qapp):
    track = _title_track()  # 歌词仅到 30000ms，媒体持续到 60000ms

    tail = TitleOverlay(
        enabled=True,
        show_mode="tail",
        duration_ms=6000,
        tail_offset_ms=2000,
        fade_in_ms=0,
        fade_out_ms=0,
    )
    assert _title_show_window(tail, track, duration_ms=60000) == [(52000, 58000)]
    assert _title_overlay_opacity(tail, track, 54000, duration_ms=60000) == 1.0
    assert _title_overlay_opacity(tail, track, 29000, duration_ms=60000) == 0.0

    head_tail = replace(
        tail,
        show_mode="head_tail",
        head_offset_ms=3000,
        fade_in_ms=500,
        fade_out_ms=700,
        tail_duration_ms=9000,
        tail_fade_in_ms=2000,
        tail_fade_out_ms=1500,
    )
    assert _title_show_window(head_tail, track, duration_ms=60000) == [
        (3000, 9000),
        (49000, 58000),
    ]
    assert _title_overlay_opacity(
        head_tail, track, 3250, duration_ms=60000
    ) == pytest.approx(0.5)
    assert _title_overlay_opacity(
        head_tail, track, 49500, duration_ms=60000
    ) == pytest.approx(0.25)

    zero_tail_offset = replace(tail, tail_offset_ms=0)
    assert _title_show_window(zero_tail_offset, track, duration_ms=60000) == [
        (54000, 60010)
    ]


def test_title_timing_does_not_follow_lyrics_track_offset(qapp):
    track = replace(
        _title_track(),
        meta=replace(_title_track().meta, offset_ms=5000),
    )
    style = Style(
        title_overlay=TitleOverlay(
            enabled=True,
            show_mode="head",
            duration_ms=1000,
            fade_in_ms=0,
            fade_out_ms=0,
        )
    )

    _track_t, _style, _lines, _signals, title_opacity = (
        subtitle_painter._resolve_visible_content(
            track,
            500,
            style,
            duration_ms=60000,
        )
    )
    assert title_opacity == 1.0


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
    # 指定 N3 项目「情報小」走字前外观（标题永不走字）
    t = TitleOverlay()
    assert t.font_family == "UD デジタル 教科書体 N-B"
    assert t.font_family_latin == "Comic Sans MS"
    assert t.font_size_px == 40 and t.font_weight == 700
    assert t.fill.color == "#EBEBEB"
    assert t.stroke.color == "#000000" and t.stroke_width_px == 5
    assert t.stroke2.color == "#FFFFFF" and t.stroke2_width_px == 0
    assert t.decoration_kind == "glow" and t.glow_radius_px == 2
    assert t.shadow.color == "#FFFFFF"


def test_resolve_title_overlay_uses_scheme_and_layout(qapp):
    from krok_helper.subtitle_render.engine.painter import resolve_title_overlay

    style = Style(title_overlay=TitleOverlay(enabled=True))
    resolved = resolve_title_overlay(style)
    # 默认「标题」方案 = 指定 N3 项目的「情報小」
    assert resolved.font_family == "UD デジタル 教科書体 N-B"
    assert resolved.font_family_latin == "Comic Sans MS"
    assert resolved.font_size_px == 40 and resolved.font_weight == 700
    assert resolved.fill.color == "#EBEBEB"
    assert resolved.stroke_width_px == 5
    assert resolved.stroke2_width_px == 0
    assert resolved.glow_radius_px == 2
    # 默认布局引用（内置タイトル左上）→ 顶部左上、余白 50/50、行間 15
    assert resolved.anchor == "top_left" and resolved.align == "left"
    assert resolved.offset_x == 50 and resolved.offset_y == 50
    assert resolved.line_gap_px == 15
    assert resolved.letter_spacing_px == 0

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


def test_title_layout_letter_spacing_overrides_title_and_character_schemes(qapp):
    from krok_helper.subtitle_render.engine.painter import (
        _layout_title_overlay,
        resolve_title_overlay,
    )

    schemes = dict(Style().custom_style_schemes)
    schemes["标题"] = replace(schemes["标题"], letter_spacing_px=37)
    schemes["角色A"] = SubtitleStyleScheme(letter_spacing_px=81)
    style = Style(
        letter_spacing_px=12,
        custom_style_schemes=schemes,
        layouts=[
            LyricsLayout(
                name="标题布局",
                letter_spacing_px=-8,
            )
        ],
        title_overlay=TitleOverlay(
            enabled=True,
            text_template="AB",
            char_role_labels=[["角色A", "角色A"]],
            layout_index=1,
        ),
    )

    resolved = resolve_title_overlay(style)
    assert resolved is not None
    assert resolved.letter_spacing_px == -8
    layout = _layout_title_overlay(1920, 1080, _title_track(), resolved, style=style)
    assert layout is not None
    first, second = layout.glyph_rows[0]
    assert first.title.letter_spacing_px == -8
    assert second.x == first.advance - 8

    inherited = replace(
        style,
        layouts=[LyricsLayout(name="继承全局字间距", letter_spacing_px=None)],
    )
    inherited_title = resolve_title_overlay(inherited)
    assert inherited_title is not None
    assert inherited_title.letter_spacing_px == 12


def test_title_line_box_uses_n3_char_box_not_font_metrics(qapp):
    """标题行盒 = 字号 + 描边（N3 DrawCharInfo.Height），不含 em 内部行距。

    Qt/DWrite 的 ascent 里带着大写字母上方那段内部行距；用它当行盒顶，同样的
    「上余白 40」看起来就比「左右余白 40」高一截。N3 把字体度量归一化到字号
    （``CreateTransformedCharGeometryChar``），上下左右才量到同一个盒。
    """
    from krok_helper.subtitle_render.engine.painter import (
        _layout_title_overlay,
        _n3_char_box_ascent,
        _n3_char_box_descent,
        resolve_title_overlay,
    )

    # 清空配色方案：resolve_title_overlay 会用「标题」方案覆盖字号/描边，
    # 这里要测的是布局几何，让 TitleOverlay 自己的字段生效。
    style = Style(
        custom_style_schemes={},
        title_overlay=TitleOverlay(
            enabled=True,
            text_template="AB",
            font_size_px=48,
            stroke_width_px=6,
            stroke2_width_px=4,
            layout_index=None,
        ),
    )
    resolved = resolve_title_overlay(style)
    assert resolved is not None

    layout = _layout_title_overlay(1920, 1080, _title_track(), resolved, style=style)
    assert layout is not None

    # 盒高恒等于 字号 + 描边，与字体 ascent/descent 无关；二重描边不占位。
    assert layout.line_heights[0] == pytest.approx(48 + 6, abs=0.001)
    assert layout.block_h == pytest.approx(48 + 6, abs=0.001)

    metrics = layout.glyph_rows[0][0].metrics
    assert layout.line_ascents[0] == pytest.approx(
        _n3_char_box_ascent(metrics, 48, 6), abs=0.001
    )
    assert layout.line_ascents[0] + _n3_char_box_descent(metrics, 48, 6) == (
        pytest.approx(48 + 6, abs=0.001)
    )


def test_title_line_box_follows_the_scheme_each_glyph_actually_uses(qapp):
    """行盒只由这一行画出来的字形决定，不受基础「标题」方案字号影响。

    标题整块套上别的角色方案后，改「标题」方案的字号仍会推动上边距——盒是用
    基础方案的字号定的，和实际渲染用的方案没解耦。
    """
    from krok_helper.subtitle_render.engine.painter import (
        _layout_title_overlay,
        resolve_title_overlay,
    )

    def _layout(title_scheme_size: int):
        schemes = dict(Style().custom_style_schemes)
        schemes[TITLE_SCHEME_NAME] = replace(
            schemes[TITLE_SCHEME_NAME], font_size_px=title_scheme_size
        )
        schemes["角色A"] = SubtitleStyleScheme(font_size_px=60, stroke_width_px=4)
        style = Style(
            custom_style_schemes=schemes,
            title_overlay=TitleOverlay(
                enabled=True,
                text_template="AB",
                char_role_labels=[["角色A", "角色A"]],
                anchor="top_left",
                align="left",
                offset_x=40,
                offset_y=40,
                layout_index=None,
            ),
        )
        resolved = resolve_title_overlay(style)
        assert resolved is not None
        result = _layout_title_overlay(1920, 1080, _title_track(), resolved, style=style)
        assert result is not None
        return result

    small = _layout(48)
    large = _layout(140)

    # 字形全部来自「角色A」，盒就该恒等于 60 + 4。
    assert [g.title.font_size_px for g in small.glyph_rows[0]] == [60, 60]
    assert [g.title.font_size_px for g in large.glyph_rows[0]] == [60, 60]
    assert small.line_heights[0] == pytest.approx(60 + 4, abs=0.001)
    assert large.line_heights[0] == pytest.approx(60 + 4, abs=0.001)
    assert small.line_ascents[0] == pytest.approx(large.line_ascents[0], abs=0.001)
    assert small.y_top == pytest.approx(large.y_top, abs=0.001)


def test_title_edge_anchor_keeps_stroke_inside_the_margin(qapp):
    """贴边锚点补半个描边：N3 的字符盒四边各含 Edge/2，描边不溢出余白。"""
    from krok_helper.subtitle_render.engine.painter import (
        _layout_title_overlay,
        resolve_title_overlay,
    )

    def _layout(anchor: str, stroke: int):
        style = Style(
            custom_style_schemes={},
            title_overlay=TitleOverlay(
                enabled=True,
                text_template="AB",
                font_size_px=48,
                stroke_width_px=stroke,
                anchor=anchor,
                align="left" if anchor.endswith("left") else "right",
                offset_x=40,
                offset_y=40,
                layout_index=None,
            ),
        )
        resolved = resolve_title_overlay(style)
        assert resolved is not None
        result = _layout_title_overlay(1920, 1080, _title_track(), resolved, style=style)
        assert result is not None
        return result

    plain_left = _layout("top_left", 0)
    stroked_left = _layout("top_left", 10)
    assert plain_left.x0 == pytest.approx(40.0, abs=0.001)
    assert stroked_left.x0 == pytest.approx(45.0, abs=0.001)

    # 右锚点向内缩同样的半描边，左右保持对称。
    plain_right = _layout("top_right", 0)
    stroked_right = _layout("top_right", 10)
    assert plain_right.x0 - stroked_right.x0 == pytest.approx(
        stroked_left.x0 - plain_left.x0, abs=0.001
    )

    # 竖向不重复补：那一半已经含在 N3 盒高里。
    assert plain_left.y_top == pytest.approx(40.0, abs=0.001)
    assert stroked_left.y_top == pytest.approx(40.0, abs=0.001)


def test_default_title_latin_font_does_not_inherit_global_lyrics_font(qapp):
    from krok_helper.subtitle_render.engine.painter import resolve_title_overlay

    style = Style(
        font_family="Global Japanese",
        font_family_latin="Comic Sans MS",
        latin_font_size_px=66,
        latin_font_weight=900,
        latin_stroke_width_px=1,
        latin_stroke2_enabled=False,
        latin_stroke2_width_px=0,
        title_overlay=TitleOverlay(enabled=True, text_template="English Title"),
    )

    scheme = style.custom_style_schemes["标题"]
    resolved = resolve_title_overlay(style)

    assert scheme.font_family_latin == "Comic Sans MS"
    assert scheme.latin_font_size_px == 40
    assert scheme.latin_font_weight == 700
    assert scheme.latin_stroke_width_px == 5
    assert scheme.latin_stroke2_enabled is False
    assert scheme.latin_stroke2_width_px == 5
    assert resolved is not None and resolved.font_family_latin == "Comic Sans MS"


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
    assert scheme.karaoke_colors.before.text.color == "#EBEBEB"
    # 位置折算成新布局并被标题引用
    assert restored.title_overlay.layout_index == 1
    layout = restored.layouts[0]
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
    single = TitleOverlay(font_family="Yu Mincho", font_family_latin=None)
    assert _make_title_font_for(single, _build_title_font(single), _build_title_latin_font(single)) is None
    # JP + Latin 分开：ASCII 用英数字体，其余用日文字体
    split = TitleOverlay(font_family="Yu Mincho", font_family_latin="Arial")
    font_for = _make_title_font_for(split, _build_title_font(split), _build_title_latin_font(split))
    assert font_for is not None
    assert font_for("A").family() == "Arial"
    assert font_for("あ").family() == subtitle_painter.resolve_qt_font_family(
        "Yu Mincho"
    )


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
    _fixed_line_geometry,
    _line_text_width,
    _make_font_for,
    _resolve_baseline_y,
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

    disabled = style_from_dict(style_to_dict(Style(glow_concentration_level=-1)))
    assert disabled.glow_concentration_level == -1


def test_glow_concentration_payloads_are_clamped():
    assert style_from_dict({"glow_concentration_level": -1}).glow_concentration_level == -1
    assert style_from_dict({"glow_concentration_level": -2}).glow_concentration_level == -1
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
    assert restored.custom_style_schemes["B"].glow_concentration_level == -1
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
    # 行盒不含描边余量（对齐 N3 DrawLineLeft/Right），与 _line_total_width 同口径。
    return max(
        int(round(_line_text_width(widths, style))), 1
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


def test_bottom_short_page_takes_alignments_from_the_tail(qapp):
    """N3 ``CalcHorizontalAlignment``：Bottom 锚定从对齐列表末尾往回数。

    3 行布局 ``[左, 中, 右]`` 只排下两行时，N3 给的是「中 + 右」——不是「左 + 中」。
    满页仍旧正序，Top 锚定也仍旧正序。
    """

    track = _continuous_track(["あい", "うえ"])
    style = Style(
        line_alignments=["left", "center", "right"],
        smart_horizontal="none",
        line_y_position="bottom",
    )
    img_w = 1920
    line0, line1 = track.lines
    w0 = _line_total_width(line0, style)
    w1 = _line_total_width(line1, style)

    # 页内两行 → 取列表末两项：中、右
    assert _resolve_line_x_smart(img_w, w0, track, line0, style, 0) == (img_w - w0) // 2
    assert _resolve_line_x_smart(img_w, w1, track, line1, style, 1) == (
        img_w - 50 - w1
    )

    # Top 锚定不反向：仍是 左、中
    top_style = replace(style, line_y_position="top")
    assert _resolve_line_x_smart(img_w, w0, track, line0, top_style, 0) == 50
    assert _resolve_line_x_smart(img_w, w1, track, line1, top_style, 1) == (
        img_w - w1
    ) // 2

    # 满页（3 行）回到正序：左、中、右
    full = _continuous_track(["あい", "うえ", "おか"])
    lines = full.lines
    widths = [_line_total_width(item, style) for item in lines]
    assert _resolve_line_x_smart(img_w, widths[0], full, lines[0], style, 0) == 50
    assert _resolve_line_x_smart(img_w, widths[1], full, lines[1], style, 1) == (
        img_w - widths[1]
    ) // 2
    assert _resolve_line_x_smart(img_w, widths[2], full, lines[2], style, 2) == (
        img_w - 50 - widths[2]
    )


def test_bottom_single_line_page_uses_the_last_alignment(qapp):
    """单行页在 Bottom 锚定下占最下行，对齐取列表末项（N3 的 while 一次都不走）。

    SmartHorizon 开着时单行页本来就整行居中，所以这条只在「不调整」时可见。
    """

    track = _continuous_track(["あい"])
    style = Style(smart_horizontal="none")  # 默认 ["left", "right"] + bottom
    img_w = 1920
    line = track.lines[0]
    width = _line_total_width(line, style)

    assert _resolve_line_x_smart(img_w, width, track, line, style, 0) == (
        img_w - 50 - width
    )


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


def test_n3_center_lane_centers_main_text_without_ruby_overhang(qapp):
    lines = [
        TimingLine(chars=[TimingChar("L", 0)], end_ms=500),
        TimingLine(chars=[TimingChar("I", 500)], end_ms=1_000),
        TimingLine(chars=[TimingChar("R", 1_000)], end_ms=1_500),
    ]
    ruby = RubyAnnotation(
        kanji="I", reading="WWWWWW", pos_start_ms=500, pos_end_ms=1_000
    )
    track = TimingTrack(lines=lines, rubies=[ruby])
    style = Style(
        layout_semantics="n3_1074",
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=60,
        ruby_font_family="Arial",
        ruby_font_family_latin="Arial",
        ruby_font_size_px=60,
        ruby_alignment="center",
        smart_horizontal="none",
        line_alignments=["left", "center", "right"],
    )

    layout = _layout_line(track, lines[1], style, 640, 360, lane=1)

    assert layout is not None
    main_left, main_right = layout.char_x_ranges[0]
    assert abs((main_left + main_right) / 2 - 320) <= 1
    assert layout.ruby_layouts[0].reading_width > main_right - main_left


def test_n3_inline_role_left_anchor_ignores_secondary_visual_padding(qapp):
    line = TimingLine(
        chars=[TimingChar("A", 0, role_label="lead")], end_ms=1_000
    )
    track = TimingTrack(lines=[line])
    style = Style(
        layout_semantics="n3_1074",
        smart_horizontal="none",
        horizontal_margin_px=73,
        # 单行布局：本例只关心左锚点是否被次级描边撑开，别让 Bottom 短页规则
        # （N3 从对齐列表末尾往回取）把这一行变成右对齐。
        line_alignments=["left"],
        custom_style_schemes={
            "lead": SubtitleStyleScheme(
                font_family="Arial",
                font_size_px=64,
                stroke_width_px=4,
                stroke2_enabled=True,
                stroke2_width_px=28,
            )
        },
    )

    layout = _layout_line(track, line, style, 640, 360, lane=0)

    assert layout is not None
    assert layout.char_x_ranges[0][0] == 73


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


def test_style_dict_roundtrip_keeps_sync_entry():
    restored = style_from_dict(
        style_to_dict(
            Style(
                sync_entry=True,
                sync_ending=True,
                allow_entry_exit_animation_overlap=False,
                sync_each_page=True,
                auto_fill_section_time=False,
            )
        )
    )

    assert restored.sync_entry is True
    assert restored.sync_ending is True
    assert restored.allow_entry_exit_animation_overlap is False
    assert restored.sync_each_page is True
    assert restored.auto_fill_section_time is False
    assert style_from_dict({}).sync_entry is False
    assert style_from_dict({}).allow_entry_exit_animation_overlap is False
    assert style_from_dict({}).sync_each_page is False
    assert style_from_dict({}).auto_fill_section_time is True


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
)
from krok_helper.subtitle_render.engine.layout_assignment import (  # noqa: E402
    apply_layout_to_page,
    assign_layout_to_all,
    auto_assign_layouts_by_page,
)
from krok_helper.subtitle_render.engine.timeline import assign_lanes  # noqa: E402
from krok_helper.subtitle_render.models import (  # noqa: E402
    LyricsLayout,
    rescale_font_sizes,
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
        letter_spacing_px=-6,
        allow_biting=True,
        ruby_interval_px=3,
        ruby_alignment="center",
        ruby_gap_px=-2,
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
    assert effective.letter_spacing_px == -6
    assert effective.allow_biting is True
    assert effective.ruby_interval_px == 3
    assert effective.ruby_alignment == "center"
    assert effective.ruby_gap_px == -2
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


def test_display_window_compression_clamps_effective_animation_durations(qapp):
    style = Style(
        entry_anim="fade",
        entry_lead_ms=900,
        exit_anim="slide_out",
        exit_fade_ms=800,
    )
    line = TimingLine(chars=[TimingChar("歌", 1_000)], end_ms=2_000)

    effective = subtitle_painter._style_for_line_display_window(
        style,
        line,
        display_start_ms=750,
        display_end_ms=2_000,
    )
    manual = subtitle_painter._style_for_line_display_window(
        style,
        line,
        display_start_ms=950,
        display_end_ms=2_100,
    )

    assert effective.entry_lead_ms == 250
    assert effective.exit_fade_ms == 0
    assert manual.entry_lead_ms == 50
    assert manual.exit_fade_ms == 100


def test_auto_entry_reserve_preserves_user_configured_short_duration(qapp):
    line = TimingLine(chars=[TimingChar("歌", 1_000)], end_ms=2_000)

    assert subtitle_painter._auto_entry_reserve_ms(
        Style(entry_anim="fade", entry_lead_ms=900),
        line,
    ) == 250
    assert subtitle_painter._auto_entry_reserve_ms(
        Style(entry_anim="fade", entry_lead_ms=100),
        line,
    ) == 100
    assert subtitle_painter._auto_entry_reserve_ms(
        Style(entry_anim="none", entry_lead_ms=900),
        line,
    ) == 0


def test_auto_exit_reserve_preserves_user_configured_short_duration(qapp):
    line = TimingLine(chars=[TimingChar("歌", 1_000)], end_ms=2_000)

    assert subtitle_painter._auto_exit_reserve_ms(
        Style(exit_anim="fade", exit_fade_ms=900),
        line,
    ) == 100
    assert subtitle_painter._auto_exit_reserve_ms(
        Style(exit_anim="fade", exit_fade_ms=60),
        line,
    ) == 60
    assert subtitle_painter._auto_exit_reserve_ms(
        Style(exit_anim="none", exit_fade_ms=900),
        line,
    ) == 0


def test_layout_character_spacing_overrides_singer_scheme(qapp):
    style = Style(
        layouts=[_three_row_layout()],
        singer_style_overrides={
            1: SubtitleStyleScheme(
                letter_spacing_px=99,
                allow_biting=False,
                ruby_gap_px=77,
            )
        },
    )
    line = TimingLine(
        chars=[TimingChar("歌", 0)],
        end_ms=1000,
        layout_index=1,
        singer_id=1,
    )

    effective = _style_for_line(style, line)

    assert effective.letter_spacing_px == -6
    assert effective.allow_biting is True
    assert effective.ruby_interval_px == 3
    assert effective.ruby_alignment == "center"
    assert effective.ruby_gap_px == -2


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

    assert len(restored.layouts) == 8
    layout = restored.layouts[0]
    assert layout.name == "下寄せ3行"
    assert layout.line_y_position == "top"
    assert layout.line_alignments == ["left", "center", "right"]
    assert {
        item.layout_id for item in restored.layouts[1:]
    } >= {f"builtin-{rows}" for rows in (1, 3, 4, 5, 6, 7, 8)}
    assert layout.horizontal_margin_px == 60
    assert layout.letter_spacing_px == -6
    assert layout.allow_biting is True
    assert layout.ruby_interval_px == 3
    assert layout.ruby_alignment == "center"
    assert layout.ruby_gap_px == -2
    legacy = style_from_dict(
        {"letter_spacing_px": 9, "layouts": [{"name": "旧布局"}]}
    )
    assert legacy.layouts[0].letter_spacing_px is None
    legacy_line = TimingLine(chars=[TimingChar("旧", 0)], end_ms=1000, layout_index=1)
    assert _layout_style_for_line(legacy, legacy_line).letter_spacing_px == 9
    # 非法 payload 防御
    invalid = style_from_dict({"layouts": "bogus"})
    assert {
        item.layout_id for item in invalid.layouts
    } >= {f"builtin-{rows}" for rows in (1, 3, 4, 5, 6, 7, 8)}


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
    assert layout.letter_spacing_px == int(720 * -6 / 1080)
    assert layout.ruby_interval_px == int(720 * 3 / 1080)
    assert layout.ruby_gap_px == int(720 * -2 / 1080)
    assert scaled.letter_spacing_px == int(720 * style.letter_spacing_px / 1080)
    # 高度不变 → 原对象返回；0 保持 0
    assert rescale_layout_sizes(scaled, 720) is scaled
    zero = rescale_layout_sizes(replace(style, line_gap_px=0), 720)
    assert zero.line_gap_px == 0


def test_rescale_font_sizes_scales_all_visual_font_slots():
    scheme = SubtitleStyleScheme(
        font_size_px=80,
        latin_font_size_px=None,
        stroke_width_px=12,
        ruby_font_size_px=36,
        ruby_shadow_offset_x=-4,
    )
    style = Style(
        font_size_px=100,
        latin_font_size_px=90,
        stroke_width_px=15,
        glow_radius_px=10,
        shadow_offset_x=-8,
        ruby_font_size_px=45,
        letter_spacing_px=7,
        ruby_gap_px=3,
        font_reference_height=1080,
        custom_style_schemes={"角色": scheme},
        singer_style_overrides={1: scheme},
        title_overlay=TitleOverlay(font_size_px=40, stroke_width_px=5),
    )

    scaled = rescale_font_sizes(style, 2160)

    assert scaled.font_reference_height == 2160
    assert scaled.font_size_px == 200
    assert scaled.latin_font_size_px == 180
    assert scaled.stroke_width_px == 30
    assert scaled.glow_radius_px == 20
    assert scaled.shadow_offset_x == -16
    assert scaled.ruby_font_size_px == 90
    assert scaled.letter_spacing_px == 7
    assert scaled.ruby_gap_px == 3
    assert scaled.custom_style_schemes["角色"].font_size_px == 160
    assert scaled.custom_style_schemes["角色"].latin_font_size_px is None
    assert scaled.custom_style_schemes["角色"].ruby_shadow_offset_x == -8
    assert scaled.singer_style_overrides[1].stroke_width_px == 24
    assert scaled.title_overlay is not None
    assert scaled.title_overlay.font_size_px == 80
    assert scaled.title_overlay.stroke_width_px == 10
    assert rescale_font_sizes(scaled, 2160) is scaled


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


def test_n3_adjacent_ruby_boxes_only_shift_the_colliding_third_group(
    qapp, monkeypatch
):
    line = TimingLine(
        chars=[
            TimingChar(text="展", start_ms=0),
            TimingChar(text="開", start_ms=100),
            TimingChar(text="中", start_ms=200),
        ],
        end_ms=300,
    )
    rubies = [
        RubyAnnotation(kanji="展", reading="てん", pos_start_ms=0, pos_end_ms=100),
        RubyAnnotation(kanji="開", reading="かい", pos_start_ms=100, pos_end_ms=200),
        RubyAnnotation(kanji="中", reading="ちゅう", pos_start_ms=200, pos_end_ms=300),
    ]
    style = Style(ruby_alignment="equal_space", ruby_interval_px=0)

    # The real N3 project resolves every ruby glyph to a 42 px geometry box
    # plus its 10 px primary edge.  Keep this regression independent of the
    # fonts installed on the test machine.
    monkeypatch.setattr(
        subtitle_painter,
        "_ruby_unit_layouts",
        lambda units, _metrics, _style: [
            (unit, 52.0, 0.0) for unit in units
        ],
    )

    gaps, _left, _right = _ruby_char_gaps(line, [112, 112, 112], rubies, style)

    # てん and かい occupy 107 px after EqualSpace integer placement, so they
    # do not collide.  ちゅう occupies 156 px and overlaps かい by exactly 19 px.
    assert gaps == [0, 0, 19]


def test_ruby_collision_box_does_not_shrink_the_paint_clip(qapp, monkeypatch):
    style = Style(ruby_alignment="equal_space", ruby_interval_px=0)
    metrics = QFontMetrics(_build_ruby_font(style))
    ruby = RubyAnnotation(
        kanji="展", reading="てん", pos_start_ms=0, pos_end_ms=100
    )
    monkeypatch.setattr(
        subtitle_painter,
        "_ruby_unit_layouts",
        lambda units, _metrics, _style: [
            (unit, 52.0, 0.0) for unit in units
        ],
    )
    layout = subtitle_painter._RubyLayout(
        ruby=ruby,
        indices=[0],
        style=style,
        x=200,
        baseline_y=100,
        target_width=112,
        reading_width=subtitle_painter._ruby_layout_width(
            ruby.reading, metrics, 112, style, ruby.kanji
        ),
        gradient_rect=QRectF(200, 0, 112, 100),
    )

    # Collision uses the actual 107 px DrawLine box at x+2..x+109, while the
    # paint/wipe clip retains the complete 112 px target box.
    draw_left, draw_right = subtitle_painter._ruby_layout_draw_bounds(
        ["て", "ん"], metrics, 200, 112, style=style, base_text="展"
    )
    paint_rect = subtitle_painter._ruby_text_rect(layout, metrics)

    assert (draw_left, draw_right) == (202, 309)
    assert paint_rect.left() == 200
    assert paint_rect.width() == 112


def test_inline_role_line_applies_ruby_collision_gaps(qapp):
    line, rubies = _wide_ruby_line()
    for char in line.chars:
        char.role_label = "lead"
    style = Style(
        dual_line_layout=False,
        ruby_stroke_width_px=10,
        custom_style_schemes={
            "lead": SubtitleStyleScheme(font_size_px=86),
        },
    )

    layout = _layout_line(TimingTrack(lines=[line], rubies=rubies), line, style, 640, 300)

    assert layout is not None
    assert layout.has_inline_styles is True
    assert layout.char_x_ranges[1][0] - layout.char_x_ranges[0][1] > 0
    assert len(layout.ruby_layouts) == 2
    first, second = layout.ruby_layouts
    _first_left, first_right = subtitle_painter._ruby_layout_draw_bounds(
        subtitle_painter._ruby_utopia_visual_units(first.ruby.reading),
        layout.ruby_metrics,
        first.x,
        first.target_width,
        style=first.style,
        base_text=first.ruby.kanji,
    )
    second_left, _second_right = subtitle_painter._ruby_layout_draw_bounds(
        subtitle_painter._ruby_utopia_visual_units(second.ruby.reading),
        layout.ruby_metrics,
        second.x,
        second.target_width,
        style=second.style,
        base_text=second.ruby.kanji,
    )
    assert second_left - first_right >= _ruby_interval_px(style) - 1e-6


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


def test_tall_opted_out_glyph_does_not_raise_shared_ruby_baseline(qapp):
    line = TimingLine(
        chars=[
            TimingChar(text="♧", start_ms=0, role_label="导唱符"),
            TimingChar(text="願", start_ms=500),
        ],
        end_ms=1000,
    )
    ruby = RubyAnnotation(
        kanji="願",
        reading="ねが",
        pos_start_ms=500,
        pos_end_ms=1000,
    )
    track = TimingTrack(lines=[line], rubies=[ruby])
    base_scheme = SubtitleStyleScheme(font_size_px=220)
    ignored_style = Style(
        font_size_px=100,
        ruby_font_size_px=30,
        custom_style_schemes={
            "导唱符": replace(base_scheme, affects_ruby_anchor=False),
        },
    )
    included_style = replace(
        ignored_style,
        custom_style_schemes={
            "导唱符": replace(base_scheme, affects_ruby_anchor=True),
        },
    )

    ignored = _layout_line(track, line, ignored_style, 1000, 600, baseline_y=400)
    included = _layout_line(track, line, included_style, 1000, 600, baseline_y=400)

    assert ignored is not None and included is not None
    assert ignored.text_layout.glyphs[0].style.font_size_px == 220
    assert ignored.ruby_layouts[0].baseline_y > included.ruby_layouts[0].baseline_y


def test_layout_semantics_defaults_to_legacy_and_round_trips():
    assert style_from_dict({}).layout_semantics == "legacy"
    assert style_from_dict({"layout_semantics": "unknown"}).layout_semantics == "legacy"
    restored = style_from_dict(style_to_dict(Style(layout_semantics="n3_1074")))
    assert restored.layout_semantics == "n3_1074"


def test_n3_character_advance_never_backtracks_but_legacy_is_unchanged():
    widths = [20, 20, 20]
    assert _char_left_positions(widths, 100, False, -30) == [100, 90, 80]
    assert _char_left_positions(
        widths, 100, False, -30, n3_no_backtracking=True
    ) == [100, 100, 100]
    assert _line_text_width(widths, Style(letter_spacing_px=-30)) == 0
    assert _line_text_width(
        widths, Style(letter_spacing_px=-30, layout_semantics="n3_1074")
    ) == 20


def test_n3_negative_spacing_ruby_boxes_follow_non_backtracking_advance(
    qapp, monkeypatch
):
    line = TimingLine(
        chars=[
            TimingChar("A", 0),
            TimingChar("B", 100),
            TimingChar("C", 200),
        ],
        end_ms=300,
    )
    rubies = [
        RubyAnnotation(
            kanji=char,
            reading=char.lower(),
            pos_start_ms=start,
            pos_end_ms=start + 100,
        )
        for char, start in (("A", 0), ("B", 100), ("C", 200))
    ]
    style = Style(
        layout_semantics="n3_1074",
        letter_spacing_px=-30,
        ruby_interval_px=0,
    )
    monkeypatch.setattr(
        subtitle_painter,
        "_ruby_layout_draw_bounds",
        lambda _units, _metrics, left, width, **_kwargs: (left, left + width),
    )

    gaps, _left_ext, _right_ext = _ruby_char_gaps(
        line, [20, 20, 20], rubies, style
    )

    # Every 20 px character initially shares x=0 because -30 px spacing may
    # not move N3's cursor backwards. Ruby collision resolution then shifts
    # the second and third character by exactly one character box each.
    assert gaps == [0, 20, 20]


def test_n3_logical_line_width_does_not_add_visual_stroke_padding(qapp):
    line = TimingLine(chars=[TimingChar("A", 0)], end_ms=1000)
    style = Style(
        layout_semantics="n3_1074",
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=64,
        stroke_width_px=8,
        stroke2_enabled=True,
        stroke2_width_px=40,
    )
    widths = _base_char_widths(line, style)

    assert _line_total_width(line, style) == _line_text_width(widths, style)


def test_n3_page_width_measurement_uses_inline_role_font_geometry(qapp):
    line = TimingLine(
        chars=[
            TimingChar("W", 0, role_label="wide"),
            TimingChar("W", 500, role_label="wide"),
        ],
        end_ms=1000,
    )
    style = Style(
        layout_semantics="n3_1074",
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=32,
        stroke_width_px=0,
        custom_style_schemes={
            "wide": SubtitleStyleScheme(
                font_family="Arial",
                font_family_latin="Arial",
                font_size_px=128,
                stroke_width_px=0,
            )
        },
    )

    rendered = _layout_line(TimingTrack(lines=[line]), line, style, 1920, 1080)
    plain = replace(
        line,
        chars=[replace(char, role_label=None) for char in line.chars],
    )

    assert rendered is not None
    assert _line_total_width(line, style) == rendered.total_w
    assert _line_total_width(line, style) > _line_total_width(plain, style)


def test_n3_smart_horizontal_uses_page_head_role_font_size(qapp):
    upper = TimingLine(
        chars=[TimingChar("I", 0, role_label="head")],
        end_ms=500,
    )
    lower = TimingLine(chars=[TimingChar("R", 500)], end_ms=1000)
    track = TimingTrack(lines=[upper, lower])
    style = Style(
        layout_semantics="n3_1074",
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=32,
        stroke_width_px=0,
        dual_line_layout=True,
        line_horizontal_layout="asymmetric",
        line_alignments=["left", "right"],
        smart_horizontal="equal_margins",
        horizontal_margin_px=24,
        custom_style_schemes={
            "head": SubtitleStyleScheme(
                font_family="Arial",
                font_family_latin="Arial",
                font_size_px=128,
                stroke_width_px=0,
            )
        },
    )
    upper_width = _line_total_width(upper, style)
    lower_width = _line_total_width(lower, style)
    expected_slack = (
        640
        - style.horizontal_margin_px * 2
        - upper_width
        - lower_width
        + 128
    )

    x = _resolve_line_x_smart(
        640, upper_width, track, upper, style, 0, center_override=False
    )

    assert expected_slack > 0
    assert x == style.horizontal_margin_px + expected_slack // 2


def test_smart_horizontal_uses_page_head_role_font_size_without_n3_semantics(qapp):
    """SmartHorizon 不属于 N3 专属语义：布局页对所有工程都给这个开关，
    字号项同样取页首行首字符所属字体槽（N3 只有这一种算法）。"""

    upper = TimingLine(
        chars=[TimingChar("I", 0, role_label="head")],
        end_ms=500,
    )
    lower = TimingLine(chars=[TimingChar("R", 500)], end_ms=1000)
    track = TimingTrack(lines=[upper, lower])
    style = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=32,
        stroke_width_px=0,
        dual_line_layout=True,
        line_horizontal_layout="asymmetric",
        line_alignments=["left", "right"],
        smart_horizontal="equal_margins",
        horizontal_margin_px=24,
        custom_style_schemes={
            "head": SubtitleStyleScheme(
                font_family="Arial",
                font_family_latin="Arial",
                font_size_px=128,
                stroke_width_px=0,
            )
        },
    )
    assert style.layout_semantics == "legacy"
    upper_width = _line_total_width(upper, style)
    lower_width = _line_total_width(lower, style)
    expected_slack = (
        640
        - style.horizontal_margin_px * 2
        - upper_width
        - lower_width
        + 128
    )

    x = _resolve_line_x_smart(
        640, upper_width, track, upper, style, 0, center_override=False
    )

    assert expected_slack > 0
    assert x == style.horizontal_margin_px + expected_slack // 2


def test_n3_lane_box_uses_font_size_and_primary_edge_only(qapp):
    legacy = Style(font_size_px=100, stroke_width_px=12, stroke2_width_px=40)
    n3 = replace(legacy, layout_semantics="n3_1074")

    legacy_h, *_ = _fixed_line_geometry(legacy)
    n3_h, n3_ascent, n3_descent, _ = _fixed_line_geometry(n3)

    assert n3_h == 112
    assert n3_ascent + n3_descent == n3_h
    assert legacy_h != n3_h


def test_n3_bottom_baseline_ignores_secondary_edge_in_lane_position(qapp):
    base = Style(
        layout_semantics="n3_1074",
        font_size_px=100,
        stroke_width_px=12,
        stroke2_enabled=True,
        stroke2_width_px=2,
        line_y_position="bottom",
        line_y_margin_px=60,
    )
    expanded = replace(base, stroke2_width_px=80)

    assert _resolve_baseline_y(
        QFontMetrics(_build_font(base)), 1080, base
    ) == _resolve_baseline_y(
        QFontMetrics(_build_font(expanded)), 1080, expanded
    )


def test_ruby_anchor_participation_round_trips_for_global_and_role():
    style = Style(
        affects_ruby_anchor=False,
        custom_style_schemes={
            "导唱符": SubtitleStyleScheme(affects_ruby_anchor=False),
        },
    )

    restored = style_from_dict(style_to_dict(style))

    assert restored.affects_ruby_anchor is False
    assert restored.custom_style_schemes["导唱符"].affects_ruby_anchor is False


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


def test_cross_page_placement_is_rigid_and_does_not_rewrite_time(qapp):
    lines = [
        TimingLine(
            chars=[TimingChar(text, start)],
            end_ms=start + 500,
            display_start_override_ms=0,
            display_end_override_ms=5_000,
        )
        for text, start in (("A", 1_000), ("B", 2_000), ("C", 3_000), ("D", 4_000))
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(2, "default"), TrackPage(2, "default")])]
        ),
    )
    normal = Style()
    legacy = replace(normal, allow_inter_page_line_overlap=True)
    animated = replace(
        normal,
        entry_anim="utopia",
        entry_lead_ms=800,
        exit_anim="spin_flip",
        exit_fade_ms=800,
    )

    normal_windows = subtitle_painter.display_windows_for_style(track, normal)
    legacy_windows = subtitle_painter.display_windows_for_style(track, legacy)
    constrained_offsets = subtitle_painter.resolved_page_offsets_for_style(
        1280, 720, track, normal
    )
    offsets = subtitle_painter.resolved_page_offsets_for_style(
        1280, 1080, track, normal
    )

    assert normal_windows == {index: (0, 5_000) for index in range(4)}
    assert legacy_windows == normal_windows
    assert offsets[0] == offsets[1] == (0.0, 0.0)
    assert offsets[2] == offsets[3]
    assert offsets[2] != (0.0, 0.0)
    assert constrained_offsets[0] == constrained_offsets[1] == (0.0, 0.0)
    assert constrained_offsets[2] == constrained_offsets[3]
    assert constrained_offsets[2] != (0.0, 0.0)
    # Entry/exit and per-character animation overlap is intentional: animation
    # trajectories must not enlarge the static collision bands or move a page.
    assert subtitle_painter.resolved_page_offset_windows_for_style(
        1280, 1080, track, animated
    ) == subtitle_painter.resolved_page_offset_windows_for_style(
        1280, 1080, track, normal
    )
    assert subtitle_painter.resolved_page_offsets_for_style(
        1280, 720, track, legacy
    ) == {}

    diagnostics = subtitle_painter.layout_timing_diagnostics_for_style(
        1280, 1080, track, normal
    )
    shifts = [item for item in diagnostics if item.kind == "page_shift"]
    assert shifts
    assert any(item.line_indices == (0, 2) for item in shifts)
    assert any("最终整页偏移" in item.detail for item in shifts)


def test_cross_page_line_ink_height_excludes_layout_line_gap(qapp):
    line = TimingLine(chars=[TimingChar("Ag", 1_000)], end_ms=2_000)
    track = TimingTrack(lines=[line])
    display = DisplayLine(
        line=line,
        lane=0,
        display_start_ms=0,
        display_end_ms=3_000,
    )
    base = Style(
        dual_line_layout=False,
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=80,
        stroke_width_px=4,
        decoration_kind="shadow",
        shadow_offset_y=12,
    )

    heights = []
    for gap in (0, 40, 90):
        style = replace(base, line_gap_px=gap)
        layout = subtitle_painter._layout_line(
            track,
            line,
            style,
            640,
            360,
            baseline_y=220,
            line_x=100,
            lane=None,
        )
        assert layout is not None
        bounds = subtitle_painter._line_static_vertical_ink_bounds(layout)
        assert bounds is not None
        heights.append(bounds[1] - bounds[0])

    assert heights[0] == heights[1] == heights[2]


def test_cross_page_line_ink_height_includes_ruby_and_static_effects(qapp):
    line = TimingLine(chars=[TimingChar("歌", 1_000)], end_ms=2_000)
    ruby = RubyAnnotation(
        kanji="歌",
        reading="うた",
        pos_start_ms=1_000,
        pos_end_ms=2_000,
    )
    plain = Style(
        dual_line_layout=False,
        font_size_px=80,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        ruby_font_size_px=36,
        ruby_stroke_width_px=0,
        ruby_stroke2_enabled=False,
        ruby_decoration_kind="none",
    )

    def bounds(track, style):
        layout = subtitle_painter._layout_line(
            track,
            line,
            style,
            640,
            360,
            baseline_y=220,
            line_x=100,
            lane=None,
        )
        assert layout is not None
        result = subtitle_painter._line_static_vertical_ink_bounds(layout)
        assert result is not None
        return result

    plain_bounds = bounds(TimingTrack(lines=[line]), plain)
    ruby_bounds = bounds(TimingTrack(lines=[line], rubies=[ruby]), plain)
    shadow_bounds = bounds(
        TimingTrack(lines=[line]),
        replace(
            plain,
            stroke_width_px=8,
            decoration_kind="shadow",
            shadow_offset_y=18,
        ),
    )
    glow_bounds = bounds(
        TimingTrack(lines=[line]),
        replace(
            plain,
            stroke_width_px=8,
            decoration_kind="glow",
            glow_before_radius_px=12,
            glow_after_radius_px=20,
        ),
    )

    assert ruby_bounds[0] < plain_bounds[0]
    assert shadow_bounds[1] > plain_bounds[1]
    assert glow_bounds[0] < plain_bounds[0]
    assert glow_bounds[1] > plain_bounds[1]


def test_collision_bands_ignore_glow_extent(qapp):
    line = TimingLine(chars=[TimingChar("歌", 1_000)], end_ms=2_000)
    ruby = RubyAnnotation(
        kanji="歌",
        reading="うた",
        pos_start_ms=1_000,
        pos_end_ms=2_000,
    )
    track = TimingTrack(lines=[line], rubies=[ruby])
    display_lines = [
        DisplayLine(
            line=line,
            lane=0,
            display_start_ms=0,
            display_end_ms=3_000,
        )
    ]
    plain = Style(
        dual_line_layout=False,
        font_size_px=80,
        stroke_width_px=8,
        stroke2_enabled=True,
        stroke2_width_px=4,
        decoration_kind="none",
        ruby_font_size_px=36,
        ruby_stroke_width_px=4,
        ruby_stroke2_enabled=True,
        ruby_stroke2_width_px=2,
        ruby_decoration_kind="none",
    )
    glow = replace(
        plain,
        decoration_kind="glow",
        glow_before_radius_px=24,
        glow_after_radius_px=24,
        ruby_decoration_kind="glow",
        ruby_glow_before_radius_px=24,
        ruby_glow_after_radius_px=24,
    )

    plain_band = subtitle_painter._measure_collision_bands(
        640,
        360,
        track,
        plain,
        display_lines,
    )[0][2]
    glow_band = subtitle_painter._measure_collision_bands(
        640,
        360,
        track,
        glow,
        display_lines,
    )[0][2]

    assert glow_band.axis_min == plain_band.axis_min
    assert glow_band.axis_max == plain_band.axis_max
    assert glow_band.cross_min == plain_band.cross_min
    assert glow_band.cross_max == plain_band.cross_max


def test_legacy_collision_box_uses_only_main_glyph_ink(qapp):
    line = TimingLine(chars=[TimingChar("歌", 1_000)], end_ms=2_000)
    ruby = RubyAnnotation(
        kanji="歌",
        reading="うた",
        pos_start_ms=1_000,
        pos_end_ms=2_000,
    )
    display_lines = [
        DisplayLine(
            line=line,
            lane=0,
            display_start_ms=0,
            display_end_ms=3_000,
        )
    ]
    plain = Style(
        layout_semantics="legacy",
        dual_line_layout=False,
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=80,
        stroke_width_px=8,
        stroke2_enabled=True,
        stroke2_width_px=4,
        decoration_kind="none",
        ruby_font_size_px=48,
        ruby_stroke_width_px=6,
    )
    decorated = replace(
        plain,
        decoration_kind="shadow",
        shadow_offset_y=30,
    )

    plain_band = subtitle_painter._measure_collision_bands(
        640,
        360,
        TimingTrack(lines=[line]),
        plain,
        display_lines,
    )[0][2]
    decorated_band = subtitle_painter._measure_collision_bands(
        640,
        360,
        TimingTrack(lines=[line], rubies=[ruby]),
        decorated,
        display_lines,
    )[0][2]
    metrics = QFontMetrics(_build_font(plain))
    baseline = subtitle_painter._resolve_baseline_y(metrics, 360, plain)
    path = QPainterPath()
    path.addText(0.0, float(baseline), _build_font(plain), "歌")
    glyph_bounds = path.boundingRect()

    assert plain_band.axis_min == math.floor(glyph_bounds.top())
    assert plain_band.axis_max == math.ceil(glyph_bounds.bottom())
    assert decorated_band.axis_min == plain_band.axis_min
    assert decorated_band.axis_max == plain_band.axis_max
    assert decorated_band.cross_min == plain_band.cross_min
    assert decorated_band.cross_max == plain_band.cross_max


def test_n3_collision_box_uses_only_main_glyph_ink(qapp):
    line = TimingLine(chars=[TimingChar("歌", 1_000)], end_ms=2_000)
    ruby = RubyAnnotation(
        kanji="歌",
        reading="うた",
        pos_start_ms=1_000,
        pos_end_ms=2_000,
    )
    display_lines = [
        DisplayLine(
            line=line,
            lane=0,
            display_start_ms=0,
            display_end_ms=3_000,
        )
    ]
    style = Style(
        layout_semantics="n3_1074",
        dual_line_layout=False,
        font_size_px=80,
        stroke_width_px=8,
        stroke2_enabled=True,
        stroke2_width_px=40,
        decoration_kind="shadow",
        shadow_offset_y=18,
        ruby_font_size_px=36,
        ruby_stroke_width_px=0,
        ruby_stroke2_enabled=False,
        ruby_decoration_kind="none",
    )

    plain_band = subtitle_painter._measure_collision_bands(
        640,
        360,
        TimingTrack(lines=[line]),
        style,
        display_lines,
    )[0][2]
    ruby_band = subtitle_painter._measure_collision_bands(
        640,
        360,
        TimingTrack(lines=[line], rubies=[ruby]),
        style,
        display_lines,
    )[0][2]
    metrics = QFontMetrics(_build_font(style))
    baseline = subtitle_painter._resolve_baseline_y(metrics, 360, style)
    path = QPainterPath()
    path.addText(0.0, float(baseline), _build_font(style), "歌")
    glyph_bounds = path.boundingRect()

    assert ruby_band.axis_min == plain_band.axis_min
    assert ruby_band.axis_max == plain_band.axis_max
    assert ruby_band.cross_min == plain_band.cross_min
    assert ruby_band.cross_max == plain_band.cross_max
    assert plain_band.axis_min == math.floor(glyph_bounds.top())
    assert plain_band.axis_max == math.ceil(glyph_bounds.bottom())


def test_cross_page_spatial_mode_squeezes_only_pixel_conflicting_lines(qapp):
    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("A", 10_000, 12_000),
            ("B", 12_500, 13_500),
            ("C", 14_000, 15_000),
            ("D", 16_000, 17_000),
        )
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(2, "default"), TrackPage(2, "default")])]
        ),
    )
    style = replace(
        Style(),
        auto_fill_section_time=False,
        line_lead_in_ms=1_800,
        line_tail_ms=1_000,
        line_lane_gap_ms=300,
    )

    normal = subtitle_painter.display_windows_for_style(track, style)
    legacy = subtitle_painter.display_windows_for_style(
        track, replace(style, allow_inter_page_line_overlap=True)
    )

    assert normal != legacy
    assert normal == {
        0: (8_200, 12_000),
        1: (10_700, 13_900),
        2: (12_300, 16_000),
        3: (14_200, 18_000),
    }
    # Both same-lane handoffs retain the configured 300 ms interval.
    assert normal[0] == (8_200, 12_000)
    assert normal[2] == (12_300, 16_000)
    assert normal[1][0] == lines[1].chars[0].start_ms - 1_800
    assert normal[3][0] == lines[3].chars[0].start_ms - 1_800
    assert all(
        start <= lines[index].chars[0].start_ms
        and end >= int(lines[index].end_ms)
        for index, (start, end) in normal.items()
    )


def test_overlap_mode_only_drops_avoidance_and_keeps_the_timing_pipeline(qapp):
    """``allow_inter_page_line_overlap`` must not switch timing algorithms.

    Both modes run the same lead-in / tail / page-boundary derivation.  Turning
    the option on only stops cross-page avoidance from consuming windows, so no
    line may be scheduled later or shorter than it is with avoidance enabled.
    """

    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("A", 10_000, 12_000),
            ("B", 12_500, 13_500),
            ("C", 14_000, 15_000),
            ("D", 16_000, 17_000),
        )
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(2, "default"), TrackPage(2, "default")])]
        ),
    )
    style = replace(
        Style(),
        auto_fill_section_time=False,
        line_lead_in_ms=1_800,
        line_tail_ms=1_000,
        line_lane_gap_ms=300,
    )

    normal = subtitle_painter.display_windows_for_style(track, style)
    overlap = subtitle_painter.display_windows_for_style(
        track, replace(style, allow_inter_page_line_overlap=True)
    )

    # Avoidance can only delay an entry or clip an exit, never the reverse.
    assert all(overlap[index][0] <= normal[index][0] for index in normal)
    assert all(overlap[index][1] >= normal[index][1] for index in normal)
    # Both same-lane handoffs retain the configured schedule gap.
    assert {index for index in normal if normal[index] != overlap[index]} == {0, 1, 2}
    assert normal[1] == (10_700, 13_900)
    assert overlap[1] == (10_700, int(lines[1].end_ms) + 1_000)
    assert normal[2][0] == overlap[2][0] + 100


@pytest.mark.parametrize(
    ("sync_entry", "sync_ending"),
    [(False, False), (True, False), (True, True)],
)
def test_overlap_mode_computes_page_sync_identically(
    qapp, sync_entry: bool, sync_ending: bool
):
    """Only collision compression may contract a synchronized page boundary."""

    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("A", 10_000, 12_000),
            ("B", 12_500, 13_500),
            ("C", 14_000, 15_000),
            ("D", 16_000, 17_000),
        )
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(2, "default"), TrackPage(2, "default")])]
        ),
    )
    style = replace(
        Style(),
        auto_fill_section_time=False,
        line_lead_in_ms=1_800,
        line_tail_ms=1_000,
        line_lane_gap_ms=300,
        sync_entry=sync_entry,
        sync_ending=sync_ending,
        sync_each_page=True,
    )

    normal = subtitle_painter.display_windows_for_style(track, style)
    overlap = subtitle_painter.display_windows_for_style(
        track, replace(style, allow_inter_page_line_overlap=True)
    )

    # Avoidance shortens and delays only the directly colliding line pairs.
    # A sibling from the same page keeps its own independently resolved window.
    expected_changed = {0, 1, 2}
    if sync_entry:
        expected_changed.add(3)
    assert {
        index for index in normal if normal[index] != overlap[index]
    } == expected_changed
    assert overlap[1] == (normal[1][0], int(lines[1].end_ms) + 1_000)
    if sync_entry:
        assert normal[2][0] == 12_300
        assert normal[3][0] == 13_800
        assert overlap[2][0] == overlap[3][0] == 12_200
    else:
        assert normal[2][0] == overlap[2][0] + 100
    if sync_entry:
        # The collision-driven contraction is per line, not page-wide.
        assert normal[0][0] == normal[1][0]
        assert normal[2][0] != normal[3][0]
        assert overlap[2][0] == overlap[3][0]


def test_animation_guard_keeps_exit_floor_and_delays_full_coverage_entry(qapp):
    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("A", 114_201, 115_544),
            ("B", 115_815, 117_240),
            ("C", 117_560, 120_563),
            ("D", 120_563, 123_908),
        )
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(2, "default"), TrackPage(2, "default")])]
        ),
    )
    plain = replace(
        Style(),
        auto_fill_section_time=False,
        allow_entry_exit_animation_overlap=True,
        line_lead_in_ms=1_800,
        line_tail_ms=1_000,
        line_lane_gap_ms=300,
    )
    animated = replace(
        plain,
        entry_anim="fade",
        entry_lead_ms=250,
        exit_anim="fade",
        exit_fade_ms=250,
    )

    plain_windows = subtitle_painter.display_windows_for_style(
        track, plain, logical_w=1920, logical_h=1080
    )
    animated_windows = subtitle_painter.display_windows_for_style(
        track, animated, logical_w=1920, logical_h=1080
    )

    assert plain_windows[0][1] == lines[0].end_ms
    # The automatic window may retain additional stable tail, but it must not
    # consume the protected 100 ms exit animation floor.
    assert animated_windows[0][1] >= lines[0].end_ms + 100
    # The display windows may overlap while one or both lines are animating.
    assert animated_windows[2][0] < animated_windows[0][1]
    display = subtitle_painter._display_lines_for_style(
        track, animated, logical_w=1920, logical_h=1080
    )
    stable = [
        subtitle_painter._display_line_static_collision_window(item, animated)
        for item in display
    ]
    assert stable[0][1] <= stable[2][0]
    assert lines[2].chars[0].start_ms - animated_windows[2][0] >= 250


def test_secondary_displacement_pairs_only_report_new_cascade(monkeypatch):
    lines = [
        TimingLine(chars=[TimingChar(text, 1_000)], end_ms=2_000)
        for text in ("A", "B")
    ]
    display = [
        DisplayLine(lines[0], 0, 0, 3_000, page_index=0),
        DisplayLine(lines[1], 0, 0, 3_000, page_index=1),
    ]
    measured = [
        (
            0,
            (0, 0),
            LineVisualBand(0, (0, 0), 0, 3_000, 100.0, 140.0),
            0.0,
        ),
        (
            1,
            (0, 1),
            LineVisualBand(1, (0, 1), 0, 3_000, 40.0, 80.0),
            0.0,
        ),
    ]
    monkeypatch.setattr(
        subtitle_painter,
        "_measure_collision_bands",
        lambda *_args: measured,
    )
    monkeypatch.setattr(
        subtitle_painter,
        "solve_page_axis_offsets",
        lambda *_args, **_kwargs: {(0, 0): -40.0, (0, 1): -80.0},
    )

    assert subtitle_painter._secondary_displacement_squeeze_pairs(
        1920, 1080, TimingTrack(lines=lines), Style(), display
    ) == ((0, 1),)

    # If the bands already collided at their authored positions, the primary
    # squeeze pass owns the pair and this discovery pass must not duplicate it.
    measured[1] = (
        1,
        (0, 1),
        LineVisualBand(1, (0, 1), 0, 3_000, 120.0, 160.0),
        0.0,
    )
    assert subtitle_painter._secondary_displacement_squeeze_pairs(
        1920, 1080, TimingTrack(lines=lines), Style(), display
    ) == ()


def test_animation_only_cross_page_overlap_does_not_move_incoming_page(qapp):
    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("A", 10_000, 12_000),
            ("B", 12_500, 13_500),
            ("C", 14_000, 15_000),
            ("D", 16_000, 17_000),
        )
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [
                TrackSection([TrackPage(2, "default")]),
                TrackSection([TrackPage(2, "default")]),
            ]
        ),
    )
    style = replace(
        Style(),
        line_lead_in_ms=1_800,
        line_tail_ms=1_000,
        line_lane_gap_ms=300,
        entry_anim="fade",
        entry_lead_ms=300,
        exit_anim="fade",
        exit_fade_ms=300,
    )

    windows = subtitle_painter.display_windows_for_style(track, style)
    offsets = subtitle_painter.resolved_page_offsets_for_style(
        1280, 1080, track, style
    )

    # 页面级显示窗口虽然大幅交叠，但碰撞必须逐行判断：P1T1/P2T1 与
    # P1T2/P2T2 的稳定文字窗口分别不相交，因此不能把整页抬高。
    assert windows[1][1] > windows[2][0]
    display = subtitle_painter._display_lines_for_style(
        track, style, logical_w=1280, logical_h=1080
    )
    stable = [
        subtitle_painter._display_line_static_collision_window(item, style)
        for item in display
    ]
    assert stable[0][1] <= stable[2][0]
    assert stable[1][1] <= stable[3][0]
    assert offsets == {index: (0.0, 0.0) for index in range(4)}


def test_sync_collision_time_window_keeps_fade_duration(qapp):
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("歌", 2_000)], end_ms=3_000)]
    )
    display = [
        DisplayLine(
            track.lines[0],
            lane=0,
            display_start_ms=1_000,
            display_end_ms=4_000,
        )
    ]
    style = replace(
        Style(),
        allow_entry_exit_animation_overlap=True,
        entry_anim="fade",
        entry_lead_ms=250,
        exit_anim="fade",
        exit_fade_ms=400,
    )

    stable = subtitle_painter._measure_collision_bands(
        1280, 720, track, style, display
    )
    synced = subtitle_painter._measure_collision_bands(
        1280,
        720,
        track,
        style,
        display,
        time_window="display",
    )

    assert (stable[0][2].display_start_ms, stable[0][2].display_end_ms) == (
        1_250,
        3_600,
    )
    assert (synced[0][2].display_start_ms, synced[0][2].display_end_ms) == (
        1_000,
        4_000,
    )


def test_animation_guard_measures_only_stable_text_collisions(qapp, monkeypatch):
    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("A", 10_000, 11_000),
            ("B", 11_100, 12_000),
            ("C", 12_100, 13_000),
        )
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [
                TrackSection([TrackPage(1, "default")]),
                TrackSection([TrackPage(1, "default")]),
                TrackSection([TrackPage(1, "default")]),
            ]
        ),
    )
    style = replace(
        Style(),
        allow_entry_exit_animation_overlap=True,
        line_lead_in_ms=0,
        line_tail_ms=0,
        line_lane_gap_ms=300,
        entry_anim="fade",
        entry_lead_ms=300,
        exit_anim="fade",
        exit_fade_ms=300,
    )

    calls: list[str] = []
    original = subtitle_painter._measure_collision_bands

    def wrapped_measure(*args, **kwargs):
        calls.append(kwargs.get("time_window", "stable"))
        return original(*args, **kwargs)

    subtitle_painter._DISPLAY_LINES_CACHE.clear()
    monkeypatch.setattr(subtitle_painter, "_measure_collision_bands", wrapped_measure)

    windows = subtitle_painter.display_windows_for_style(
        track, style, logical_w=1280, logical_h=1080
    )

    assert windows[0] == (9_700, 11_300)
    assert windows[1] == (10_800, 12_300)
    assert windows[2] == (11_800, 13_300)
    assert "display" not in calls
    assert "stable" in calls


def test_force_bottom_requires_measured_spatial_conflict(qapp):
    first = TimingLine(
        chars=[TimingChar("左", 1_000)],
        end_ms=4_000,
        display_start_override_ms=0,
        display_end_override_ms=5_000,
        layout_index=1,
    )
    second = TimingLine(
        chars=[TimingChar("右", 1_500)],
        end_ms=4_500,
        display_start_override_ms=0,
        display_end_override_ms=5_000,
    )
    track = TimingTrack(
        lines=[first, second],
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(1, "left"), TrackPage(1, "default")])]
        ),
    )
    style = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=48,
        line_alignments=["right", "right"],
        layouts=[
            LyricsLayout(
                name="左",
                layout_id="left",
                line_y_position="bottom",
                line_alignments=["left", "left"],
            )
        ],
    )

    display = subtitle_painter._display_lines_for_style(
        track, style, logical_w=1280, logical_h=720
    )
    measured = subtitle_painter._measure_collision_bands(
        1280, 720, track, style, display
    )

    assert len(measured) == 2
    assert measured[0][2].cross_max <= measured[1][2].cross_min
    assert [item.lane for item in display] == [1, 1]


def test_force_bottom_lane_lift_is_reported_by_diagnostics(qapp):
    lines = [
        TimingLine(
            chars=[TimingChar("前页", 1_000)],
            end_ms=4_000,
            display_start_override_ms=0,
            display_end_override_ms=5_000,
        ),
        TimingLine(
            chars=[TimingChar("后页", 1_500)],
            end_ms=4_500,
            display_start_override_ms=0,
            display_end_override_ms=5_000,
        ),
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(1, "default"), TrackPage(1, "default")])]
        ),
    )
    style = Style(
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=48,
        line_y_position="bottom",
        line_alignments=["right", "right"],
    )

    display = subtitle_painter._display_lines_for_style(
        track, style, logical_w=1280, logical_h=720
    )
    diagnostics = subtitle_painter.layout_timing_diagnostics_for_style(
        1280, 720, track, style
    )

    assert [item.lane for item in display] == [1, 0]
    lift = next(item for item in diagnostics if item.kind == "force_bottom_shift")
    assert lift.line_indices == (0, 1)
    assert "ForceBottom" in lift.detail
    assert "第 2 行位 → 第 1 行位" in lift.detail
    assert "00:00.000 – 00:05.000" in lift.detail


def test_changed_page_layout_does_not_make_entry_animation_collidable(qapp):
    from krok_helper.subtitle_render.engine.page_plan import (
        project_page_plan_to_legacy_fields,
    )
    from krok_helper.subtitle_render.models import ensure_page_layout_defaults

    style = ensure_page_layout_defaults(
        replace(
            Style(),
            line_lead_in_ms=1_800,
            line_tail_ms=1_000,
            line_lane_gap_ms=300,
            entry_anim="fade",
            entry_lead_ms=300,
            exit_anim="fade",
            exit_fade_ms=300,
        )
    )

    def make_track(second_layout_id: str) -> TimingTrack:
        lines = [
            TimingLine(chars=[TimingChar(text, start)], end_ms=end)
            for text, start, end in (
                ("A", 10_000, 12_000),
                ("B", 12_500, 13_500),
                ("C", 14_000, 15_000),
                ("D", 16_000, 17_000),
            )
        ]
        track = TimingTrack(
            lines=lines,
            page_plan=TrackPagePlan(
                [
                    TrackSection([TrackPage(2, "default")]),
                    TrackSection([TrackPage(2, second_layout_id)]),
                ]
            ),
        )
        project_page_plan_to_legacy_fields(track, style)
        return track

    same = make_track("default")
    changed = make_track("builtin-3")
    same_offsets = subtitle_painter.resolved_page_offsets_for_style(
        1280, 1080, same, style
    )
    changed_offsets = subtitle_painter.resolved_page_offsets_for_style(
        1280, 1080, changed, style
    )

    assert same_offsets == {index: (0.0, 0.0) for index in range(4)}
    assert changed_offsets[2] == changed_offsets[3]
    assert changed_offsets[2] == (0.0, 0.0)


def test_non_overlapping_layouts_keep_full_entry_and_exit_windows(qapp):
    from krok_helper.subtitle_render.engine.page_plan import (
        project_page_plan_to_legacy_fields,
    )

    top = LyricsLayout(
        name="顶部单行",
        layout_id="top-one",
        line_y_position="top",
        line_y_margin_px=80,
        line_gap_px=90,
        line_alignments=["left"],
    )
    style = replace(
        Style(),
        layouts=[*Style().layouts, top],
        line_lead_in_ms=1_800,
        line_tail_ms=1_000,
        entry_anim="fade",
        entry_lead_ms=300,
        exit_anim="fade",
        exit_fade_ms=300,
    )
    lines = [
        TimingLine(chars=[TimingChar("下", 10_000)], end_ms=12_000),
        TimingLine(chars=[TimingChar("上", 11_000)], end_ms=13_000),
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [
                TrackSection([TrackPage(1, "default")]),
                TrackSection([TrackPage(1, "top-one")]),
            ]
        ),
    )
    project_page_plan_to_legacy_fields(track, style)

    windows = subtitle_painter.display_windows_for_style(
        track, style, logical_w=1920, logical_h=1080
    )

    assert windows == {
        0: (8_200, 13_000),
        1: (9_200, 14_000),
    }


def test_sync_entry_is_controlled_only_by_its_switch(qapp):
    lines = [
        TimingLine(chars=[TimingChar("A", 10_000)], end_ms=11_000),
        TimingLine(chars=[TimingChar("B", 12_000)], end_ms=13_000),
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(2, "default")])]
        ),
    )
    base = replace(
        Style(),
        line_lead_in_ms=1_800,
        line_tail_ms=1_000,
    )

    independent = subtitle_painter.display_windows_for_style(
        track,
        replace(base, sync_entry=False),
        logical_w=1920,
        logical_h=1080,
    )
    synchronized = subtitle_painter.display_windows_for_style(
        track,
        replace(base, sync_entry=True),
        logical_w=1920,
        logical_h=1080,
    )

    assert [independent[index][0] for index in range(2)] == [8_200, 10_200]
    assert [synchronized[index][0] for index in range(2)] == [8_200, 8_200]


def test_page_sync_boundary_extension_never_shortens_existing_windows():
    lines = [
        TimingLine(chars=[TimingChar(text, 1_000)], end_ms=2_000)
        for text in ("A", "B", "C")
    ]
    display_lines = [
        DisplayLine(line, index, start, end)
        for index, (line, start, end) in enumerate(
            zip(lines, (100, 200, 300), (700, 600, 500))
        )
    ]

    entry = subtitle_painter._extend_page_display_boundary(
        display_lines,
        (0, 1, 2),
        start_ms=250,
    )
    ending = subtitle_painter._extend_page_display_boundary(
        display_lines,
        (0, 1, 2),
        end_ms=650,
    )

    assert [item.display_start_ms for item in entry] == [100, 200, 250]
    assert [item.display_end_ms for item in ending] == [700, 650, 650]


def test_page_sync_defaults_to_section_edges_and_can_run_on_every_page():
    lines = [
        TimingLine(chars=[TimingChar(text, 1_000 + index * 100)], end_ms=2_000)
        for index, text in enumerate(("A", "B", "C", "D", "E", "F"))
    ]
    display_lines = [
        DisplayLine(
            line,
            index % 2,
            100 + index * 100,
            1_000 + index * 100,
            0,
            index // 2,
            2,
        )
        for index, line in enumerate(lines)
    ]
    track = TimingTrack(lines=lines)

    section_edges = subtitle_painter._apply_constrained_page_sync(
        1_280, 720, track, Style(sync_entry=True, sync_ending=True), display_lines
    )
    every_page = subtitle_painter._apply_constrained_page_sync(
        1_280,
        720,
        track,
        Style(sync_entry=True, sync_ending=True, sync_each_page=True),
        display_lines,
    )

    assert [item.display_start_ms for item in section_edges] == [
        100, 100, 300, 400, 500, 600
    ]
    assert [item.display_end_ms for item in section_edges] == [
        1_000, 1_100, 1_200, 1_300, 1_500, 1_500
    ]
    assert [item.display_start_ms for item in every_page] == [
        100, 100, 300, 300, 500, 500
    ]
    assert [item.display_end_ms for item in every_page] == [
        1_100, 1_100, 1_300, 1_300, 1_500, 1_500
    ]


def test_section_time_fill_matches_next_page_by_nearest_main_text_box(
    monkeypatch
):
    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("A", 1_000, 2_000),
            ("B", 2_000, 3_000),
            ("C", 5_000, 6_000),
            ("D", 7_000, 8_000),
            ("E", 9_000, 10_000),
        )
    ]
    track = TimingTrack(lines=lines)
    display_lines = [
        DisplayLine(lines[0], 0, 500, 2_500, 0, 0, 2),
        DisplayLine(lines[1], 1, 1_500, 3_500, 0, 0, 2),
        DisplayLine(lines[2], 0, 5_000, 6_000, 0, 1, 3),
        DisplayLine(lines[3], 1, 7_000, 8_000, 0, 1, 3),
        DisplayLine(lines[4], 2, 9_000, 10_000, 0, 1, 3),
    ]
    axis_boxes = ((0, 40), (100, 140), (105, 145), (5, 45), (200, 240))
    measured = [
        (
            index,
            (0, item.page_index),
            subtitle_painter.LineVisualBand(
                index,
                (0, item.page_index),
                item.display_start_ms,
                item.display_end_ms,
                float(axis_min),
                float(axis_max),
            ),
            0.0,
        )
        for index, (item, (axis_min, axis_max)) in enumerate(
            zip(display_lines, axis_boxes)
        )
    ]
    monkeypatch.setattr(
        subtitle_painter,
        "_measure_collision_bands",
        lambda *_args, **_kwargs: measured,
    )

    filled = subtitle_painter._apply_measured_section_time_fill(
        1_280,
        720,
        track,
        Style(line_lane_gap_ms=300),
        display_lines,
    )
    disabled = subtitle_painter._apply_measured_section_time_fill(
        1_280,
        720,
        track,
        Style(auto_fill_section_time=False, line_lane_gap_ms=300),
        display_lines,
    )

    # A matches D by height, B matches C; page order is deliberately opposite.
    assert [item.display_end_ms for item in filled[:2]] == [6_700, 4_700]
    # The three-line tail page fills its middle line as well as its first line.
    assert [item.display_end_ms for item in filled[2:]] == [10_000, 10_000, 10_000]
    assert disabled == display_lines


def test_section_time_fill_uses_strict_unique_matches_after_page_shift(
    monkeypatch,
):
    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("A", 1_000, 2_000),
            ("B", 2_000, 3_000),
            ("X", 3_000, 4_000),
            ("C", 6_000, 7_000),
            ("D", 8_000, 9_000),
        )
    ]
    track = TimingTrack(lines=lines)
    display_lines = [
        DisplayLine(lines[0], 0, 500, 2_500, 0, 0, 3),
        DisplayLine(lines[1], 1, 1_500, 3_500, 0, 0, 3),
        DisplayLine(lines[2], 2, 2_500, 4_500, 0, 0, 3),
        DisplayLine(lines[3], 0, 6_000, 7_000, 0, 1, 2),
        DisplayLine(lines[4], 1, 8_000, 9_000, 0, 1, 2),
    ]
    # Before the page displacement, C is closest to both A and B.  The final
    # +40 px placement makes C a valid match for B only; D remains too far
    # away from every source.  Thus C may be consumed once and A stays put.
    axis_boxes = ((0, 40), (50, 90), (100, 140), (5, 45), (200, 240))
    measured = [
        (
            index,
            (0, item.page_index),
            subtitle_painter.LineVisualBand(
                index,
                (0, item.page_index),
                item.display_start_ms,
                item.display_end_ms,
                float(axis_min),
                float(axis_max),
            ),
            0.0,
        )
        for index, (item, (axis_min, axis_max)) in enumerate(
            zip(display_lines, axis_boxes)
        )
    ]
    monkeypatch.setattr(
        subtitle_painter,
        "_measure_collision_bands",
        lambda *_args, **_kwargs: measured,
    )
    monkeypatch.setattr(
        subtitle_painter,
        "solve_page_axis_offsets",
        lambda *_args, **_kwargs: {(0, 0): 0.0, (0, 1): 40.0},
    )

    filled = subtitle_painter._apply_measured_section_time_fill(
        1_280,
        720,
        track,
        Style(line_lane_gap_ms=300),
        display_lines,
    )

    assert filled[0].display_end_ms == 2_500
    assert filled[1].display_end_ms == 5_700
    assert filled[2].display_end_ms == 4_500
    assert [item.display_end_ms for item in filled[3:]] == [9_000, 9_000]


def test_all_automatic_timing_options_preserve_stable_lane_gap(qapp):
    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("A", 1_000, 2_200),
            ("B", 1_800, 3_000),
            ("X", 2_600, 3_900),
            ("C", 5_000, 6_200),
            ("D", 6_800, 7_900),
        )
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(3, "default"), TrackPage(2, "default")])]
        ),
    )
    style = replace(
        Style(font_family="Arial", font_family_latin="Arial"),
        sync_entry=True,
        sync_ending=True,
        sync_each_page=True,
        allow_entry_exit_animation_overlap=True,
        auto_fill_section_time=True,
        entry_anim="fade",
        entry_lead_ms=250,
        exit_anim="fade",
        exit_fade_ms=250,
        line_lead_in_ms=1_800,
        line_tail_ms=1_000,
        line_lane_gap_ms=300,
    )

    display = subtitle_painter._display_lines_for_style(
        track, style, logical_w=1_280, logical_h=720
    )
    stable = [
        subtitle_painter._display_line_static_collision_window(item, style)
        for item in display
    ]

    # Page sync supplies the longest candidate first.  The collision solver
    # may then contract individual rows, but each matched lane keeps 300 ms
    # between stable main-text windows while the animations may overlap.
    assert display[0].display_end_ms > display[3].display_start_ms
    assert display[1].display_end_ms > display[4].display_start_ms
    assert stable[0][1] + 300 == stable[3][0]
    assert stable[1][1] + 300 == stable[4][0]
    # Three-to-two matching leaves X unmatched; it is not reused as another
    # correspondence.  The two-line section tail still shares its final end.
    assert display[2].display_end_ms == 4_150
    assert display[3].display_end_ms == display[4].display_end_ms == 8_900


def test_page_sync_entry_compresses_outgoing_then_incoming_page(qapp):
    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("A", 10_000, 11_000),
            ("B", 12_000, 13_500),
            ("C", 14_000, 15_000),
            ("D", 16_000, 17_000),
        )
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(2, "default"), TrackPage(2, "default")])]
        ),
    )
    base_style = replace(
        Style(),
        auto_fill_section_time=False,
        line_lead_in_ms=1_800,
        line_tail_ms=1_000,
        line_lane_gap_ms=300,
    )
    baseline = subtitle_painter.display_windows_for_style(
        track,
        base_style,
        logical_w=1920,
        logical_h=1080,
    )
    synchronized = subtitle_painter.display_windows_for_style(
        track,
        replace(base_style, sync_entry=True, sync_each_page=True),
        logical_w=1920,
        logical_h=1080,
    )

    # 第一页完整同步。第二页先尝试最长同步入场；发生碰撞后，每个碰撞对
    # 各自按「先压前句退场、再压自己入场」处理，不传播给页内兄弟行。
    assert synchronized[0][0] == synchronized[1][0] == baseline[0][0]
    assert synchronized[0][1] == baseline[0][1]
    assert synchronized[1][1] == lines[1].end_ms
    assert synchronized[2][0] == 12_200
    assert synchronized[3][0] == 13_800
    assert synchronized[1][1] <= synchronized[3][0]
    offsets = subtitle_painter.resolved_page_offsets_for_style(
        1920,
        1080,
        track,
        replace(base_style, sync_entry=True, sync_each_page=True),
    )
    assert offsets == {index: (0.0, 0.0) for index in range(4)}


def test_page_sync_entry_applies_longest_candidate_before_collision_guard(qapp):
    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("previous lower line", 24_320, 27_310),
            ("incoming upper line", 27_680, 29_340),
            ("incoming lower line", 29_710, 30_900),
        )
    ]
    track = TimingTrack(lines=lines)
    style = replace(
        Style(),
        sync_entry=True,
        sync_each_page=True,
        entry_anim="spin_flip",
        entry_lead_ms=250,
        exit_anim="char_fade",
        exit_fade_ms=250,
        line_lane_gap_ms=300,
    )
    display_lines = [
        DisplayLine(lines[0], 1, 21_490, 27_560, 0, 1, 2),
        DisplayLine(lines[1], 0, 24_680, 29_590, 0, 2, 2),
        DisplayLine(lines[2], 1, 27_860, 31_900, 0, 2, 2),
    ]

    synchronized = subtitle_painter._apply_constrained_page_sync(
        3840,
        2160,
        track,
        style,
        display_lines,
    )

    # Both automatic lines first receive the page's longest entry candidate.
    # The shared collision guard owns all later compression.
    assert synchronized[1].display_start_ms == 24_680
    assert synchronized[2].display_start_ms == 24_680


def test_animation_guard_keeps_100ms_exit_before_delaying_incoming(qapp):
    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("previous lower line", 24_320, 27_310),
            ("incoming lower line", 29_710, 30_900),
        )
    ]
    track = TimingTrack(lines=lines)
    style = replace(
        Style(),
        allow_entry_exit_animation_overlap=True,
        entry_anim="spin_flip",
        entry_lead_ms=250,
        exit_anim="char_fade",
        exit_fade_ms=250,
        line_lane_gap_ms=300,
    )
    display_lines = [
        DisplayLine(lines[0], 1, 21_490, 27_560, 0, 1, 2),
        DisplayLine(lines[1], 1, 27_060, 31_900, 0, 2, 2),
    ]

    guarded = subtitle_painter._apply_animation_time_guard(
        3840,
        2160,
        track,
        style,
        display_lines,
        enforce_inter_page_gap=True,
    )

    # Stable windows retain the configured 300 ms same-lane interval while
    # their 250 ms exit/entry animations may still overlap.
    assert guarded[0].display_end_ms == 27_560
    assert guarded[1].display_start_ms == 27_360


def test_animation_guard_does_not_clip_a_non_overlapping_exit(qapp):
    lines = [
        TimingLine(chars=[TimingChar("運命だよコレ", 27_680)], end_ms=29_340),
        TimingLine(chars=[TimingChar("理由があるんだ", 31_300)], end_ms=35_420),
    ]
    track = TimingTrack(lines=lines)
    style = replace(
        Style(),
        allow_entry_exit_animation_overlap=True,
        line_lane_gap_ms=300,
        exit_anim="char_fade",
        exit_fade_ms=250,
    )
    display_lines = [
        DisplayLine(lines[0], 0, 24_680, 29_590, 0, 1, 1),
        DisplayLine(lines[1], 0, 29_740, 35_670, 0, 2, 1),
    ]

    guarded = subtitle_painter._apply_animation_time_guard(
        3_840,
        2_160,
        track,
        style,
        display_lines,
        enforce_inter_page_gap=True,
    )

    # There is already a real 150 ms gap.  Do not turn the configured 300 ms
    # schedule spacing into a false collision or clip 29.590 to 29.440.
    assert guarded[0].display_end_ms == 29_590
    assert guarded[1].display_start_ms == 29_740


def test_animation_overlap_switch_changes_collision_time_window(qapp):
    lines = [
        TimingLine(chars=[TimingChar("前句", 1_000)], end_ms=2_000),
        TimingLine(chars=[TimingChar("后句", 2_850)], end_ms=4_000),
    ]
    track = TimingTrack(lines=lines)
    style = replace(
        Style(font_family="Arial", font_family_latin="Arial"),
        allow_entry_exit_animation_overlap=True,
        entry_anim="fade",
        entry_lead_ms=250,
        exit_anim="fade",
        exit_fade_ms=250,
        line_lane_gap_ms=300,
    )
    display_lines = [
        # Stable windows are 00.750–02.300 and 02.600–03.750: exactly 300 ms.
        # Complete animation windows overlap from 02.350 to 02.550.
        DisplayLine(lines[0], 0, 500, 2_550, 0, 1, 1),
        DisplayLine(lines[1], 0, 2_350, 4_250, 0, 2, 1),
    ]

    allowed = subtitle_painter._apply_animation_time_guard(
        1_280, 720, track, style, display_lines, enforce_inter_page_gap=True
    )
    forbidden = subtitle_painter._apply_animation_time_guard(
        1_280,
        720,
        track,
        replace(style, allow_entry_exit_animation_overlap=False),
        display_lines,
        enforce_inter_page_gap=True,
    )

    assert [(item.display_start_ms, item.display_end_ms) for item in allowed] == [
        (500, 2_550),
        (2_350, 4_250),
    ]
    assert [(item.display_start_ms, item.display_end_ms) for item in forbidden] == [
        (500, 2_250),
        (2_550, 4_250),
    ]


def test_same_lane_gap_does_not_depend_on_horizontal_glyph_intersection(
    qapp, monkeypatch
):
    lines = [
        TimingLine(chars=[TimingChar("左", 1_000)], end_ms=2_000),
        TimingLine(chars=[TimingChar("右", 4_000)], end_ms=5_000),
    ]
    track = TimingTrack(lines=lines)
    style = Style(line_lane_gap_ms=300)
    display_lines = [
        DisplayLine(lines[0], 0, 1_000, 2_500, 0, 1, 1),
        DisplayLine(lines[1], 0, 2_600, 5_000, 0, 2, 1),
    ]
    measured = [
        (
            0,
            (0, 1),
            subtitle_painter.LineVisualBand(
                0, (0, 1), 1_000, 2_500, 0.0, 40.0,
                cross_min=0.0, cross_max=40.0,
            ),
            0.0,
        ),
        (
            1,
            (0, 2),
            subtitle_painter.LineVisualBand(
                1, (0, 2), 2_600, 5_000, 0.0, 40.0,
                cross_min=100.0, cross_max=140.0,
            ),
            0.0,
        ),
    ]
    monkeypatch.setattr(
        subtitle_painter,
        "_measure_collision_bands",
        lambda *_args, **_kwargs: measured,
    )

    guarded = subtitle_painter._apply_animation_time_guard(
        1_280, 720, track, style, display_lines, enforce_inter_page_gap=True
    )

    assert guarded[0].display_end_ms == 2_300
    assert guarded[1].display_start_ms == 2_600


def test_animation_guard_compresses_stable_overlap_incrementally(qapp):
    lines = [
        TimingLine(chars=[TimingChar("前句", 1_000)], end_ms=2_000),
        TimingLine(chars=[TimingChar("后句", 4_000)], end_ms=5_000),
    ]
    track = TimingTrack(lines=lines)
    style = replace(
        Style(font_family="Arial", font_family_latin="Arial"),
        allow_entry_exit_animation_overlap=True,
        entry_anim="fade",
        entry_lead_ms=250,
        exit_anim="fade",
        exit_fade_ms=250,
    )
    display_lines = [
        # Stable windows are 01.000–02.750 and 02.650–04.000: overlap 100 ms.
        DisplayLine(lines[0], 0, 1_000, 3_000, 0, 1, 1),
        DisplayLine(lines[1], 0, 2_400, 5_250, 0, 2, 1),
    ]

    guarded = subtitle_painter._apply_animation_time_guard(
        1_280,
        720,
        track,
        style,
        display_lines,
        enforce_inter_page_gap=True,
    )

    # Consume the 100 ms overlap plus the configured 300 ms interval from the
    # outgoing stable tail without shortening either 250 ms animation.
    assert guarded[0].display_end_ms == 2_600
    assert guarded[1].display_start_ms == 2_400
    assert guarded[0].display_end_ms - lines[0].end_ms >= 250
    assert lines[1].chars[0].start_ms - guarded[1].display_start_ms >= 250


def test_animation_guard_spills_only_remaining_overlap_to_incoming(qapp):
    lines = [
        TimingLine(chars=[TimingChar("前句", 1_000)], end_ms=2_000),
        TimingLine(chars=[TimingChar("后句", 4_000)], end_ms=5_000),
    ]
    track = TimingTrack(lines=lines)
    style = replace(
        Style(font_family="Arial", font_family_latin="Arial"),
        allow_entry_exit_animation_overlap=True,
        entry_anim="fade",
        entry_lead_ms=250,
        exit_anim="fade",
        exit_fade_ms=250,
    )
    display_lines = [
        # Stable windows are 01.250–02.100 and 01.850–04.000: overlap 250 ms.
        # The outgoing side owns only 100 ms of stable tail.
        DisplayLine(lines[0], 0, 1_000, 2_350, 0, 1, 1),
        DisplayLine(lines[1], 0, 1_600, 5_250, 0, 2, 1),
    ]

    guarded = subtitle_painter._apply_animation_time_guard(
        1_280,
        720,
        track,
        style,
        display_lines,
        enforce_inter_page_gap=True,
    )

    # Resolve 250 ms overlap plus the 300 ms interval: first consume the
    # previous 100 ms stable tail, then move the incoming by the remaining
    # 450 ms.  Both animations stay 250 ms.
    assert guarded[0].display_end_ms == 2_250
    assert guarded[1].display_start_ms == 2_050
    assert guarded[0].display_end_ms - lines[0].end_ms == 250
    assert lines[1].chars[0].start_ms - guarded[1].display_start_ms > 250


def test_force_bottom_waits_for_automatic_time_avoidance(qapp, monkeypatch):
    """A resolved same-lane handoff must not leave a stale page reflow."""

    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("previous upper line", 22_030, 23_380),
            ("あたしを待っていた", 24_320, 27_310),
            ("運命だよコレ", 27_680, 29_340),
            ("放っておけない", 29_710, 30_900),
        )
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(2, "default"), TrackPage(2, "default")])]
        ),
    )
    style = replace(
        Style(font_family="Arial", font_family_latin="Arial"),
        allow_entry_exit_animation_overlap=True,
        line_lead_in_ms=3_000,
        line_tail_ms=1_000,
        line_lane_gap_ms=300,
        sync_entry=True,
        entry_anim="spin_flip",
        entry_lead_ms=250,
        exit_anim="char_fade",
        exit_fade_ms=250,
    )

    calls: list[object] = []
    original = subtitle_painter.compute_display_lines

    def wrapped_compute(*args, **kwargs):
        calls.append(kwargs.get("force_bottom_pairs"))
        return original(*args, **kwargs)

    subtitle_painter._DISPLAY_LINES_CACHE.clear()
    monkeypatch.setattr(subtitle_painter, "compute_display_lines", wrapped_compute)

    windows = subtitle_painter.display_windows_for_style(
        track,
        style,
        logical_w=3_840,
        logical_h=2_160,
    )

    # Display windows overlap only during animation; stable text does not.
    assert windows[3][0] < windows[1][1]
    assert windows[2][0] == 24_680
    assert windows[3][0] == 27_360
    assert all(not pairs for pairs in calls)

    diagnostics = subtitle_painter.layout_timing_diagnostics_for_style(
        3_840, 2_160, track, style
    )
    assert not any(
        item.kind in {"page_shift", "force_bottom_shift"}
        and 3 in item.line_indices
        for item in diagnostics
    )


@pytest.mark.parametrize(
    ("incoming_upper_start", "previous_lower_end"),
    [(24_000, None), (None, 28_000)],
)
def test_manual_cross_lane_extension_does_not_raise_incoming_page(
    qapp,
    incoming_upper_start: int | None,
    previous_lower_end: int | None,
):
    """A time overlap between vertically clear lanes must not move the page."""

    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("previous upper line", 22_030, 23_380),
            ("あたしを待っていた", 24_320, 27_310),
            ("運命だよコレ", 27_680, 29_340),
            ("放っておけない", 29_710, 30_900),
        )
    ]
    lines[1].display_end_override_ms = previous_lower_end
    lines[2].display_start_override_ms = incoming_upper_start
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(2, "default"), TrackPage(2, "default")])]
        ),
    )
    style = replace(
        Style(font_family="Arial", font_family_latin="Arial"),
        font_size_px=200,
        font_reference_height=2_160,
        layout_reference_height=2_160,
        line_y_margin_px=50,
        line_gap_px=85,
        line_lead_in_ms=3_000,
        line_tail_ms=1_000,
        line_lane_gap_ms=300,
        sync_entry=True,
        entry_anim="spin_flip",
        entry_lead_ms=250,
        exit_anim="char_fade",
        exit_fade_ms=250,
    )

    display = subtitle_painter._display_lines_for_style(
        track, style, logical_w=3_840, logical_h=2_160
    )
    measured = subtitle_painter._measure_collision_bands(
        3_840, 2_160, track, style, display, time_window="display"
    )
    bands = {index: band for index, _page, band, _gap in measured}
    offsets = subtitle_painter.resolved_page_offsets_for_style(
        3_840, 2_160, track, style
    )

    assert bands[2].axis_max <= bands[1].axis_min
    assert offsets == {index: (0.0, 0.0) for index in range(4)}


def test_page_sync_ending_defaults_to_the_section_tail_page(qapp):
    lines = [
        TimingLine(chars=[TimingChar(text, start)], end_ms=end)
        for text, start, end in (
            ("A", 10_000, 11_000),
            ("B", 10_500, 11_500),
            ("C", 14_000, 15_000),
            ("D", 16_000, 17_000),
        )
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(2, "default"), TrackPage(2, "default")])]
        ),
    )
    base_style = replace(
        Style(),
        auto_fill_section_time=False,
        line_lead_in_ms=1_800,
        line_tail_ms=1_000,
        line_lane_gap_ms=300,
    )
    baseline = subtitle_painter.display_windows_for_style(
        track,
        base_style,
        logical_w=1920,
        logical_h=1080,
    )
    synchronized = subtitle_painter.display_windows_for_style(
        track,
        replace(base_style, sync_ending=True),
        logical_w=1920,
        logical_h=1080,
    )

    # “每句同步”关闭时，非段尾页保持自己的自动窗口；段尾页的两行
    # 同步到该页最晚退场边界。
    assert synchronized[0] == baseline[0]
    assert synchronized[1] == baseline[1]
    assert synchronized[2] == (baseline[2][0], baseline[3][1])
    assert synchronized[3] == baseline[3]


def _line_fade_style(**changes) -> Style:
    """White body over a wide red edge: any bleed-through is unmistakable."""

    white = _solid_fill("#FFFFFF")
    red = _solid_fill("#FF0000")
    colors = KaraokeColors(
        before=KaraokeColorState(text=white, stroke=red, stroke2=red, shadow=red),
        after=KaraokeColorState(text=white, stroke=red, stroke2=red, shadow=red),
    )
    style = replace(
        Style(),
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=160,
        font_reference_height=450,
        layout_reference_height=450,
        stroke_width_px=24,
        stroke2_enabled=True,
        stroke2_width_px=8,
        decoration_kind="glow",
        glow_radius_px=6,
        dual_line_layout=False,
        line_horizontal_layout="center",
        line_y_position="center",
        karaoke_colors=colors,
        line_lead_in_ms=0,
        line_tail_ms=0,
        entry_anim="none",
        exit_anim="fade",
        exit_fade_ms=400,
        # utopia keeps the wipe per-character, so the line cannot collapse into
        # one baked blit -- the same dynamic path every real project uses.
        karaoke_anim="utopia",
    )
    return replace(style, **changes)


def _body_core_pixels(img: QImage) -> list[tuple[int, int]]:
    """Fully opaque pure-white pixels: the glyph body away from its edge."""

    out = []
    for y in range(img.height()):
        for x in range(img.width()):
            color = QColor(img.pixelColor(x, y))
            if (
                color.alpha() == 255
                and color.red() > 245
                and color.green() > 245
                and color.blue() > 245
            ):
                out.append((x, y))
    return out


def _mean_rgb(img: QImage, pixels: list[tuple[int, int]]) -> tuple[int, int, int]:
    total = [0, 0, 0]
    for x, y in pixels:
        color = QColor(img.pixelColor(x, y))
        total[0] += color.red()
        total[1] += color.green()
        total[2] += color.blue()
    count = max(len(pixels), 1)
    return tuple(value // count for value in total)


def test_line_level_exit_opacity_composes_before_fading(qapp):
    """A faded line must not blend its own layers into each other.

    N3 wraps the whole line in a Direct2D opacity layer (LineFade →
    PushOpacityLayer), so the body is composited over its edge at full opacity
    and only the finished line is faded.  Applying the opacity per draw call
    instead lets the wide red edge show through the semi-transparent white body,
    which reads as the palette changing colour mid-animation.
    """

    # ``fade`` keeps the line still, so one pixel mask stays valid across the
    # animation.  ``rise`` / ``slide_out`` share this exact opacity path.
    style = _line_fade_style()
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("あ", 0)],
                end_ms=1_000,
                display_start_override_ms=0,
                display_end_override_ms=2_000,
            )
        ]
    )

    stable = _blank()
    subtitle_painter.paint_frame(stable, track, 1_200, style)
    core = _body_core_pixels(stable)
    assert len(core) > 400, len(core)
    assert _mean_rgb(stable, core) == (255, 255, 255)

    # 2_000 - 400 == 1_600 starts the exit; sample it half way through.
    faded = _blank()
    subtitle_painter.paint_frame(faded, track, 1_800, style)
    red, green, blue = _mean_rgb(faded, core)
    assert green == blue, (red, green, blue)
    assert red - green <= 6, (red, green, blue)


def test_per_char_exit_opacity_composes_before_fading(qapp):
    """Per-character fades must compose the character before fading it.

    N3 cannot reuse one line-level opacity layer here, so its
    ``CharFadeInFadeOut`` clips the edge draws out of the glyph body instead.  We
    reach the same result differently: the glyph run is baked into an image at
    full opacity and that image is blitted with the character's opacity, which
    also keeps glow and shadow from bleeding -- N3 leaves those unprotected.

    ``char_fade`` is the pure per-character opacity case.  Effects that also
    transform the glyph (``spin_flip`` scales it toward zero) cannot be pinned
    with a static pixel mask, and they share this code path.
    """

    style = _line_fade_style(exit_anim="char_fade", exit_fade_ms=400)
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("あ", 0)],
                end_ms=1_000,
                display_start_override_ms=0,
                display_end_override_ms=2_000,
            )
        ]
    )

    stable = _blank()
    subtitle_painter.paint_frame(stable, track, 1_200, style)
    core = _body_core_pixels(stable)
    assert len(core) > 400, len(core)

    faded = _blank()
    subtitle_painter.paint_frame(faded, track, 1_800, style)
    red, green, blue = _mean_rgb(faded, core)
    assert green == blue, (red, green, blue)
    assert red - green <= 6, (red, green, blue)


def test_title_overlay_space_uses_n3_space_width(qapp):
    """N3 sizes a blank glyph from SpaceWidth, and its title is a lyrics line.

    ``LyricsLineKind.Title`` goes through the same DrawCharInfo pipeline as
    ``Kind == Lyrics``, so ``DirectXCommon``'s rule -- an empty glyph outline
    becomes ``FontSize * SpaceWidth / 100`` -- applies to the title too.  Using
    Qt's raw advance instead made the title wider than the GPU drew it: at Meiryo
    48 the space measured 18 px against N3's 9 px.
    """

    title = TitleOverlay(
        enabled=True,
        text_template="{title} {artist}",
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=48,
        stroke_width_px=0,
    )
    style = replace(Style(), space_width_percent=20, title_overlay=title)
    track = TimingTrack(
        meta=TimingTrackMeta(title="星空", artist="歌手"),
        lines=[TimingLine(chars=[TimingChar("尾", 0)], end_ms=1_000)],
    )

    layout = subtitle_painter._layout_title_overlay(
        1280, 720, track, title, style=style
    )
    assert layout is not None
    glyphs = layout.glyph_rows[0]
    spaces = [glyph for glyph in glyphs if glyph.text == " "]
    assert len(spaces) == 1, [glyph.text for glyph in glyphs]
    assert spaces[0].advance == 48 * 20 // 100

    # Every other glyph keeps its own advance, so the row width follows.
    expected = sum(
        float(48 * 20 // 100)
        if glyph.text == " "
        else float(glyph.metrics.horizontalAdvance(glyph.text))
        for glyph in glyphs
    )
    assert layout.widths[0] == expected
