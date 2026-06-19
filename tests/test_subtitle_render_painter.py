"""Tests for ``krok_helper.subtitle_render.engine.painter``.

像素级断言不可移植（字形 / 字体可用性平台差异大），所以本测试聚焦：

- 函数能在不同时刻正常完成不抛
- 各阶段（未唱 / 半唱 / 全唱）画面像素与"完全空白"对比都有差异
- 空 track 不画任何东西
"""

from __future__ import annotations

import os
from dataclasses import replace

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
    _apply_character_transform,
    _brush_for_fill,
    _build_font,
    _build_ruby_font,
    _char_left_positions,
    _character_fill_ratio,
    _fill_clip_band,
    _fill_extent_end,
    _resolve_vertical_columns,
    _ruby_utopia_visual_units,
    _vertical_fill_band,
    _vertical_orientation,
    _karaoke_fill_segments,
    _paint_ruby_text,
    _paint_ruby_text_units_with_transition,
    _resolve_display_baselines,
    _resolve_line_x,
    _ruby_progress_ratio,
    _ruby_reading_intervals,
    _ruby_utopia_reading_units_and_intervals,
    _transition_char_state,
    paint_frame,
    clear_before_layer_cache,
)
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
    visual_pad = style.stroke_width_px + style.stroke2_width_px
    upper_main_bottom = baselines[0] + metrics.descent() + visual_pad
    lower_main_top = baselines[1] - metrics.ascent() - visual_pad

    assert lower_main_top - upper_main_bottom == style.line_gap_px


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

    assert stroked_w - plain_w >= 60
    assert stroked_h - plain_h >= 60


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
    visual_pad = style.stroke_width_px + style.stroke2_width_px
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
    paint_frame(at_exit_static, track, 2400, static)
    paint_frame(at_exit_animated, track, 2400, animated)

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
