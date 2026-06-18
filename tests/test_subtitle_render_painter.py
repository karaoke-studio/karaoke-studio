"""Tests for ``krok_helper.subtitle_render.engine.painter``.

像素级断言不可移植（字形 / 字体可用性平台差异大），所以本测试聚焦：

- 函数能在不同时刻正常完成不抛
- 各阶段（未唱 / 半唱 / 全唱）画面像素与"完全空白"对比都有差异
- 空 track 不画任何东西
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QFontMetrics, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.engine.painter import (  # noqa: E402
    _build_font,
    _resolve_display_baselines,
    _resolve_line_x,
    paint_frame,
)
from krok_helper.subtitle_render.engine.timeline import DisplayLine  # noqa: E402
from krok_helper.subtitle_render.models import (  # noqa: E402
    RubyAnnotation,
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
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
    upper_main_bottom = baselines[0] + metrics.descent()
    lower_main_top = baselines[1] - metrics.ascent()

    assert lower_main_top - upper_main_bottom == style.line_gap_px


def test_dual_line_x_positions_use_asymmetric_margins(qapp):
    style = Style()

    assert _resolve_line_x(1920, 600, style, 0) == 50
    assert _resolve_line_x(1920, 600, style, 1) == 1270


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
