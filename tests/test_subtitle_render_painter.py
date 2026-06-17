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

from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.engine.painter import paint_frame  # noqa: E402
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


def _blank(w=400, h=200) -> QImage:
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
    paint_frame(img, _track(), 500, Style())  # 早于行起点
    assert _pixel_hash(img) == baseline


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
