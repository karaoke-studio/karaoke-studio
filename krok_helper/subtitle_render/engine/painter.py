"""单帧 QPainter 绘制（A4 阶段）。

入口 :func:`paint_frame` 把一行已唱 / 未唱字符渲染到给定 ``QImage`` 上；
预览路径可用 :func:`paint_frame_to_painter` 直接画到已有 ``QPainter``，避免每帧
额外分配整张离屏图。

绘制顺序（自底向上）：

1. **阴影**：整行文本按 ``shadow_offset_*`` 偏移绘一份阴影色
2. **描边**：用 ``QPainterPath.addText`` 取字形轮廓，``strokePath`` 描宽线
3. **底色**：整行字符（``base_color``）
4. **Ruby 注音**：按 ``@Ruby`` 时间区间映射到主歌词字符范围，画在主行上方
5. **填充层**：同样字符以 ``fill_color`` 重绘，但用 ``setClipRect`` 把每个字符
   裁切到"已唱比例"（左→右扫光）

预览路径与渲染路径**共用本函数**——预览给到的 image 是缩放后的 QImage、
渲染管线给的是 1080p QImage，绘制逻辑一致。

**性能优化**：1~3 步（阴影 + 描边 + 底色）每帧的内容 *完全不依赖* ``t_ms``，
只随 line text + font + style 变化。:func:`_get_or_build_before_layer` 把这
三层烘焙成透明 QImage 缓存到 :data:`_BEFORE_LAYER_CACHE`，绘制时一次
``drawImage`` blit；每帧只重画 5 步的逐字 clip。1080p 双行场景下，单帧
``paintEvent`` 工作量从 ~2× ``QPainterPath.addText + strokePath`` 降到一次
位图 blit，CPU 时间降幅 3~5×。缓存按 line/font/style 哈希索引，LRU 退役，
样式实时改动会自动 invalidate。

P1 阶段会在本函数基础上加：渐变填充（B3）、入场退场动画（B4）、
多歌手分色（B2）。
"""

from __future__ import annotations

import math
import os
from collections import OrderedDict
from dataclasses import dataclass, replace
from threading import Lock
from typing import Optional

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QTransform,
)


# ---------------------------------------------------------------------------
# Before-layer cache：阴影 + 描边 + 底色烘焙成透明 QImage
# ---------------------------------------------------------------------------
#
# Key 包含所有影响"未唱"层外观的字段：line text、字形 / 字号 / 字重 / 斜体、
# char_widths（同字体 + 文本下严格固定，作冗余校验）、KaraokeColorState.before
# 全部颜色 / 渐变签名、阴影偏移、描边宽度、装饰种类与 glow 半径。lane / x0 /
# y / 显示窗口等位置 / 时间字段 *不进 key*（缓存图带 offset 复位）。

_BEFORE_LAYER_CACHE_MAX = 64
_BEFORE_LAYER_CACHE: "OrderedDict[tuple, tuple[QImage, int, int]]" = OrderedDict()
_BEFORE_LAYER_LOCK = Lock()
_IMAGE_FILL_CACHE_MAX = 16
_IMAGE_FILL_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()
_IMAGE_BRUSH_CACHE: "OrderedDict[tuple, QBrush]" = OrderedDict()
_IMAGE_FILL_LOCK = Lock()
_RUBY_COMBINING_CHARS = set("ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ\u3099\u309A")


@dataclass(frozen=True)
class _FillSegment:
    left: int
    right: int
    start_ms: int = 0
    end_ms: int = 0
    ruby: RubyAnnotation | None = None


@dataclass(frozen=True)
class _LineCharTransition:
    phase: str
    effect: str
    progress: float
    start_ms: int | None = None
    end_ms: int | None = None


_UTOPIA_INTRO_TIME_MS = 700
_UTOPIA_INTRO_DELAY_MS = 200
_UTOPIA_INTRO_ENLARGE_MS = 400
_UTOPIA_INTRO_CONDENSE_MS = 100
_UTOPIA_INTRO_OVER_RATIO = 1.3
_UTOPIA_WIPE_OVER_RATIO = 1.15
_UTOPIA_WIPE_OVER_TIME_RATIO = 0.25
_UTOPIA_WIPE_OVER_TIME_LIMIT_MS = 100
_UTOPIA_FADE_OUT_TIME_MS = 750
_CHAR_FADE_INTRO_DELAY_MS = 350
_CHAR_FADE_IN_TIME_MS = 250
_CHAR_FADE_OUT_TIME_MS = 250


def clear_before_layer_cache() -> None:
    """测试 / 调试用：把所有"未唱"层位图缓存全部丢掉。"""
    with _BEFORE_LAYER_LOCK:
        _BEFORE_LAYER_CACHE.clear()
    with _IMAGE_FILL_LOCK:
        _IMAGE_FILL_CACHE.clear()
        _IMAGE_BRUSH_CACHE.clear()

from krok_helper.subtitle_render.engine.timeline import (
    DisplayLine,
    char_fill_ratio,
    compute_char_intervals,
    visible_display_lines,
)
from krok_helper.subtitle_render.engine.animator import line_animation_state
from krok_helper.subtitle_render.models import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
    RubyAnnotation,
    Style,
    TimingLine,
    TimingTrack,
)


def paint_frame(
    image: QImage,
    track: Optional[TimingTrack],
    t_ms: int,
    style: Style,
) -> QImage:
    """把 ``track`` 在 ``t_ms`` 时刻的活跃行渲染到 ``image``（原地修改）。

    若无活跃行则不画任何字（image 不变）。返回同一个 image 以便链式调用。
    """
    painter = QPainter(image)
    try:
        # QImage 上 setDevicePixelRatio 后，QPainter 在该 image 上的坐标系
        # 自动按 dpr 缩放——绘制坐标用"逻辑像素"，而 image.width()/height()
        # 返回的是物理像素。这里取逻辑尺寸，让上层布局算居中等都按屏幕
        # 实际可见尺寸来。
        dpr = image.devicePixelRatioF() or 1.0
        logical_w = max(int(round(image.width() / dpr)), 1)
        logical_h = max(int(round(image.height() / dpr)), 1)
        paint_frame_to_painter(painter, logical_w, logical_h, track, t_ms, style)
    finally:
        painter.end()
    return image


def paint_frame_to_painter(
    painter: QPainter,
    logical_w: int,
    logical_h: int,
    track: Optional[TimingTrack],
    t_ms: int,
    style: Style,
) -> None:
    """把当前字幕帧直接绘制到已打开的 ``QPainter``。

    ``logical_w`` / ``logical_h`` 使用 Qt 逻辑像素；调用方负责先绘制背景。
    """
    if track is None:
        return
    display_lines = _visible_lines_for_style(track, t_ms, style)
    if not display_lines:
        return

    painter.save()
    try:
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        baselines = _resolve_display_baselines(logical_h, track, display_lines, style)
        for display_line in display_lines:
            _paint_line(
                painter,
                logical_w,
                logical_h,
                track,
                display_line.line,
                t_ms,
                style,
                baseline_y=baselines[display_line.lane],
                lane=display_line.lane if style.dual_line_layout else None,
                display_start_ms=display_line.display_start_ms,
                display_end_ms=display_line.display_end_ms,
            )
    finally:
        painter.restore()


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _build_font(style: Style) -> QFont:
    font = QFont(style.font_family, max(style.font_size_px, 1))
    # QFont 用 PointSize 时 size 是 pt；这里我们当 px 用，强制 setPixelSize
    font.setPixelSize(max(style.font_size_px, 1))
    font.setWeight(_clamp_weight(style.font_weight))
    font.setItalic(style.italic)
    return font


def _visible_lines_for_style(
    track: TimingTrack,
    t_ms: int,
    style: Style,
) -> list[DisplayLine]:
    if style.dual_line_layout:
        return visible_display_lines(
            track,
            t_ms,
            lead_in_ms=style.line_lead_in_ms,
            tail_ms=style.line_tail_ms,
            lane_gap_ms=style.line_lane_gap_ms,
            max_hold_ms=style.line_max_hold_ms,
            continuity_snap_ms=style.line_continuity_snap_ms,
            pair_second_delay_ms=style.line_pair_second_delay_ms,
        )
    display_line = _single_visible_display_line(track, t_ms, style)
    if display_line is None:
        return []
    return [display_line]


def _single_visible_display_line(
    track: TimingTrack,
    t_ms: int,
    style: Style,
) -> DisplayLine | None:
    best_live: DisplayLine | None = None
    best_lead_or_tail: DisplayLine | None = None
    lead = max(style.line_lead_in_ms, 0)
    tail = max(style.line_tail_ms, 0)
    for line in track.lines:
        if line.is_blank or not line.chars:
            continue
        sing_start = _line_start_ms(line)
        sing_end = _line_end_ms(line)
        display_start = max(sing_start - lead, 0)
        display_end = sing_end + tail
        display_line = DisplayLine(
            line=line,
            lane=0,
            display_start_ms=display_start,
            display_end_ms=display_end,
        )
        if sing_start <= t_ms <= sing_end:
            if best_live is None or sing_start >= _line_start_ms(best_live.line):
                best_live = display_line
        elif display_start <= t_ms <= display_end:
            if best_lead_or_tail is None or sing_start >= _line_start_ms(best_lead_or_tail.line):
                best_lead_or_tail = display_line
    return best_live or best_lead_or_tail


def _build_ruby_font(style: Style) -> QFont:
    font = QFont(style.font_family, max(style.ruby_font_size_px, 1))
    font.setPixelSize(max(style.ruby_font_size_px, 1))
    font.setWeight(QFont.Weight.Medium)
    font.setItalic(style.italic)
    return font


def _clamp_weight(w: int) -> QFont.Weight:
    # QFont.Weight 在 PyQt6 是 IntEnum，可直接传 int；不过为了取最近档位更稳，
    # 映射到 Thin/Normal/Bold/Black 几档。
    if w <= 250:
        return QFont.Weight.Thin
    if w <= 350:
        return QFont.Weight.Light
    if w <= 450:
        return QFont.Weight.Normal
    if w <= 550:
        return QFont.Weight.Medium
    if w <= 650:
        return QFont.Weight.DemiBold
    if w <= 750:
        return QFont.Weight.Bold
    if w <= 850:
        return QFont.Weight.ExtraBold
    return QFont.Weight.Black


def _visual_text_padding(style: Style) -> int:
    return _visual_stroke_extent(style.stroke_width_px, style.stroke2_width_px)


def _visual_stroke_extent(stroke_width: int, stroke2_width: int) -> int:
    return max(stroke_width, 0) + max(stroke2_width, 0)


def _stroke_pen_width(stroke_width: int) -> int:
    return max(stroke_width, 0) * 2


def _stroke2_pen_width(stroke_width: int, stroke2_width: int) -> int:
    return _visual_stroke_extent(stroke_width, stroke2_width) * 2


def _resolve_baseline_y(
    metrics: QFontMetrics,
    img_h: int,
    style: Style,
    ruby_metrics: QFontMetrics | None = None,
) -> int:
    pos = style.line_y_position
    margin = max(style.line_y_margin_px, 0)
    pad = _visual_text_padding(style)
    ruby_extra = 0
    if ruby_metrics is not None:
        ruby_extra = max(style.ruby_gap_px, 0) + ruby_metrics.height()
    if pos == "top":
        return margin + ruby_extra + pad + metrics.ascent()
    if pos == "center":
        block_h = metrics.height() + ruby_extra + pad * 2
        return (img_h - block_h) // 2 + ruby_extra + pad + metrics.ascent()
    # bottom（默认）
    return img_h - margin - pad - metrics.descent()


def _fixed_line_geometry(style: Style) -> tuple[int, int, int, int]:
    font = _build_font(style)
    metrics = QFontMetrics(font)
    ruby_metrics = QFontMetrics(_build_ruby_font(style))
    ruby_extra = max(style.ruby_gap_px, 0) + ruby_metrics.height()
    pad = _visual_text_padding(style)
    main_h = metrics.ascent() + metrics.descent() + pad * 2
    return main_h, metrics.ascent() + pad, metrics.descent() + pad, ruby_extra


def _resolve_display_baselines(
    img_h: int,
    track: TimingTrack,
    display_lines: list[DisplayLine],
    style: Style,
) -> dict[int, int]:
    if not style.dual_line_layout:
        font = _build_font(style)
        metrics = QFontMetrics(font)
        line = display_lines[0].line if display_lines else None
        ruby_metrics = (
            QFontMetrics(_build_ruby_font(style))
            if line is not None and _active_rubies_for_line(track.rubies, line)
            else None
        )
        return {0: _resolve_baseline_y(metrics, img_h, style, ruby_metrics)}

    main_h, main_ascent, main_descent, ruby_extra = _fixed_line_geometry(style)
    gap = max(style.line_gap_px, 0)
    margin = max(style.line_y_margin_px, 0)

    if style.line_y_position == "top":
        upper_baseline = margin + ruby_extra + main_ascent
        lower_baseline = upper_baseline + main_h + gap
    elif style.line_y_position == "center":
        total_h = main_h * 2 + gap
        upper_main_top = (img_h - total_h) // 2
        upper_baseline = upper_main_top + main_ascent
        lower_baseline = upper_baseline + main_h + gap
    else:
        lower_baseline = img_h - margin - main_descent
        upper_baseline = lower_baseline - main_h - gap
    return {
        0: upper_baseline,
        1: lower_baseline,
    }


def _paint_line(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    line: TimingLine,
    t_ms: int,
    style: Style,
    *,
    baseline_y: int | None = None,
    lane: int | None = None,
    display_start_ms: int | None = None,
    display_end_ms: int | None = None,
) -> None:
    style = _style_for_line(style, line)
    animation = line_animation_state(
        style,
        t_ms=t_ms,
        display_start_ms=display_start_ms if display_start_ms is not None else _line_start_ms(line),
        display_end_ms=display_end_ms if display_end_ms is not None else _line_end_ms(line),
        lane=lane,
    )
    if animation.opacity <= 0.0:
        return
    painter.save()
    try:
        if animation.opacity < 1.0:
            painter.setOpacity(painter.opacity() * animation.opacity)
        if animation.dx or animation.dy:
            painter.translate(animation.dx, animation.dy)
        _paint_line_static(
            painter,
            img_w,
            img_h,
            track,
            line,
            t_ms,
            style,
            baseline_y=baseline_y,
            lane=lane,
            display_start_ms=display_start_ms,
            display_end_ms=display_end_ms,
        )
    finally:
        painter.restore()


def _paint_line_static(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    line: TimingLine,
    t_ms: int,
    style: Style,
    *,
    baseline_y: int | None = None,
    lane: int | None = None,
    display_start_ms: int | None = None,
    display_end_ms: int | None = None,
) -> None:
    font = _build_font(style)
    painter.setFont(font)
    metrics = QFontMetrics(font)
    active_rubies = _active_rubies_for_line(track.rubies, line)
    ruby_font = _build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font) if active_rubies else None

    # 整行宽度 → 水平居中起点
    char_widths = [metrics.horizontalAdvance(c.text) for c in line.chars]
    total_w = sum(char_widths)
    visual_pad = _visual_text_padding(style)
    x0 = _resolve_line_x(img_w, total_w + visual_pad * 2, style, lane) + visual_pad
    y = (
        baseline_y
        if baseline_y is not None
        else _resolve_baseline_y(metrics, img_h, style, ruby_metrics)
    )

    intervals = compute_char_intervals(line)
    char_x_ranges: list[tuple[int, int]] = []
    cursor_x = x0
    for w in char_widths:
        char_x_ranges.append((cursor_x, cursor_x + w))
        cursor_x += w
    fill_segments = _karaoke_fill_segments(
        char_widths,
        intervals,
        char_x_ranges,
        active_rubies,
        line,
    )

    line_rect = QRectF(
        float(x0),
        float(y - metrics.ascent()),
        float(total_w),
        float(metrics.height()),
    )
    colors = _effective_karaoke_colors(style)
    line_path = _line_text_path(line, char_widths, font, x0, y)
    transition = _line_char_transition_context(
        style,
        line,
        t_ms,
        display_start_ms,
        display_end_ms,
        len(line.chars),
    )
    if active_rubies and ruby_metrics is not None:
        _paint_rubies(
            painter,
            ruby_font,
            ruby_metrics,
            line,
            intervals,
            char_x_ranges,
            y,
            t_ms,
            active_rubies,
            style,
            transition,
        )

    if transition is not None:
        _paint_line_with_character_transition(
            painter,
            line,
            char_widths,
            char_x_ranges,
            intervals,
            font,
            y,
            metrics,
            style,
            colors,
            line_rect,
            t_ms,
            transition,
        )
        return

    # --- "未唱"层（不依赖 t_ms）：查 / 建缓存后一次 blit ---
    if total_w > 0 and metrics.height() > 0:
        cache_key = _before_layer_cache_key(line, style, font, char_widths, colors)
        before_image, offset_x, offset_y = _get_or_build_before_layer(
            cache_key, line, char_widths, font, style, colors, metrics,
        )
        painter.drawImage(
            QPointF(float(x0 + offset_x), float(y + offset_y)),
            before_image,
        )

    # --- "已唱"层（依赖 t_ms）：逐字 clip 照旧每帧画 ---
    if style.decoration_kind == "glow":
        glow_radius = max(style.glow_radius_px, 1)
        _paint_after_glow_path(
            painter,
            line_path,
            colors.after.shadow,
            line_rect,
            glow_radius,
            fill_segments,
            y,
            metrics,
            t_ms,
        )
    elif style.shadow_color and (style.shadow_offset_x or style.shadow_offset_y):
        shadow_rect = line_rect.translated(style.shadow_offset_x, style.shadow_offset_y)
        shadow_path = _line_text_path(
            line,
            char_widths,
            font,
            x0 + style.shadow_offset_x,
            y + style.shadow_offset_y,
        )
        _paint_after_fill_path(
            painter,
            shadow_path,
            colors.after.shadow,
            shadow_rect,
            _offset_fill_segments(fill_segments, style.shadow_offset_x),
            y + style.shadow_offset_y,
            metrics,
            t_ms,
        )

    if style.stroke2_width_px > 0:
        _paint_after_stroke_path(
            painter,
            line_path,
            colors.after.stroke2,
            line_rect,
            _stroke2_pen_width(style.stroke_width_px, style.stroke2_width_px),
            fill_segments,
            y,
            metrics,
            t_ms,
        )

    if style.stroke_color and style.stroke_width_px > 0:
        _paint_after_stroke_path(
            painter,
            line_path,
            colors.after.stroke,
            line_rect,
            _stroke_pen_width(style.stroke_width_px),
            fill_segments,
            y,
            metrics,
            t_ms,
        )

    _paint_after_fill_path(
        painter,
        line_path,
        colors.after.text,
        line_rect,
        fill_segments,
        y,
        metrics,
        t_ms,
    )


def _line_text_path(
    line: TimingLine,
    char_widths: list[int],
    font: QFont,
    x: int,
    y: int,
) -> QPainterPath:
    path = QPainterPath()
    cursor_x = x
    for ch, w in zip(line.chars, char_widths):
        path.addText(float(cursor_x), float(y), font, ch.text)
        cursor_x += w
    return path


def _line_char_transition_context(
    style: Style,
    line: TimingLine,
    t_ms: int,
    display_start_ms: int | None,
    display_end_ms: int | None,
    char_count: int,
) -> _LineCharTransition | None:
    if char_count <= 0:
        return None
    start = display_start_ms if display_start_ms is not None else _line_start_ms(line)
    end = display_end_ms if display_end_ms is not None else _line_end_ms(line)

    if style.entry_anim == "utopia" or style.exit_anim == "utopia":
        intervals = compute_char_intervals(line)
        in_intro = style.entry_anim == "utopia" and t_ms <= start + _UTOPIA_INTRO_TIME_MS
        in_exit = (
            style.exit_anim == "utopia"
            and bool(intervals)
            and _utopia_following_done_time(line, intervals, 0, style) <= t_ms <= end
        )
        in_wipe = any(_is_utopia_wiping(t_ms, char_start, char_end) for char_start, char_end in intervals)
        if in_intro or in_exit or in_wipe:
            return _LineCharTransition(
                phase="utopia",
                effect="utopia",
                progress=1.0,
                start_ms=start,
                end_ms=end,
            )

    if style.exit_anim == "char_fade" and style.exit_fade_ms > 0:
        exit_start = max(_line_end_ms(line), end - _CHAR_FADE_INTRO_DELAY_MS - _CHAR_FADE_OUT_TIME_MS)
        if t_ms >= exit_start:
            return _LineCharTransition(
                phase="exit",
                effect=style.exit_anim,
                progress=1.0,
                start_ms=exit_start,
                end_ms=end,
            )

    if style.entry_anim == "char_fade" and style.entry_lead_ms > 0:
        entry_end = start + _CHAR_FADE_INTRO_DELAY_MS + _CHAR_FADE_IN_TIME_MS
        if t_ms <= entry_end:
            return _LineCharTransition(
                phase="entry",
                effect=style.entry_anim,
                progress=1.0,
                start_ms=start,
                end_ms=entry_end,
            )
    return None


def _paint_line_with_character_transition(
    painter: QPainter,
    line: TimingLine,
    char_widths: list[int],
    char_x_ranges: list[tuple[int, int]],
    intervals: list[tuple[int, int]],
    font: QFont,
    baseline_y: int,
    metrics: QFontMetrics,
    style: Style,
    colors: KaraokeColors,
    line_rect: QRectF,
    t_ms: int,
    transition: _LineCharTransition,
) -> None:
    count = max(len(line.chars), 1)
    for index, (ch, width) in enumerate(zip(line.chars, char_widths)):
        if index >= len(intervals) or index >= len(char_x_ranges):
            continue
        left, _right = char_x_ranges[index]
        char_start, char_end = intervals[index]
        following_done_ms = (
            _utopia_following_done_time(line, intervals, index, style)
            if transition.effect == "utopia"
            else None
        )
        opacity, dx, dy, rotation, scale_x, scale_y = _transition_char_state(
            style,
            transition,
            index,
            count,
            char_start_ms=char_start,
            char_end_ms=char_end,
            t_ms=t_ms,
            frame_height=painter.device().height(),
            following_done_ms=following_done_ms,
        )
        if opacity <= 0.0:
            continue

        path = QPainterPath()
        path.addText(float(left), float(baseline_y), font, ch.text)
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * opacity)
            _apply_character_transform(
                painter,
                center_x=left + width / 2,
                center_y=baseline_y - metrics.ascent() + metrics.height() / 2,
                dx=dx,
                dy=dy,
                rotation=rotation,
                scale_x=scale_x,
                scale_y=scale_y,
                scale_origin_x=left if transition.effect == "utopia" else None,
                scale_origin_y=baseline_y if transition.effect == "utopia" else None,
            )
            _paint_char_karaoke_stack(
                painter,
                path,
                line_rect,
                char_x=left,
                char_width=width,
                baseline_y=baseline_y,
                metrics=metrics,
                colors=colors,
                style=style,
                ratio=char_fill_ratio(char_start, char_end, t_ms),
            )
        finally:
            painter.restore()


def _transition_char_state(
    style: Style,
    transition: _LineCharTransition,
    index: int,
    count: int,
    *,
    char_start_ms: int | None = None,
    char_end_ms: int | None = None,
    t_ms: int | None = None,
    frame_height: int | None = None,
    following_done_ms: int | None = None,
) -> tuple[float, float, float, float, float, float]:
    if transition.effect == "utopia" and transition.phase == "utopia":
        if (
            style.entry_anim == "utopia"
            and t_ms is not None
            and transition.start_ms is not None
            and t_ms <= transition.start_ms + _UTOPIA_INTRO_TIME_MS
        ):
            intro_transition = _LineCharTransition(
                phase="entry",
                effect="utopia",
                progress=_clamped_ratio(t_ms - transition.start_ms, _UTOPIA_INTRO_TIME_MS),
                start_ms=transition.start_ms,
                end_ms=transition.start_ms + _UTOPIA_INTRO_TIME_MS,
            )
            return _transition_char_state(
                style,
                intro_transition,
                index,
                count,
                char_start_ms=char_start_ms,
                char_end_ms=char_end_ms,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        if (
            style.exit_anim == "utopia"
            and t_ms is not None
            and following_done_ms is not None
            and t_ms > following_done_ms
        ):
            outro_transition = _LineCharTransition(phase="exit", effect="utopia", progress=1.0)
            return _transition_char_state(
                style,
                outro_transition,
                index,
                count,
                char_start_ms=char_start_ms,
                char_end_ms=char_end_ms,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        if (
            t_ms is not None
            and char_start_ms is not None
            and char_end_ms is not None
            and _is_utopia_wiping(t_ms, char_start_ms, char_end_ms)
        ):
            wipe_transition = _LineCharTransition(phase="wipe", effect="utopia", progress=1.0)
            return _transition_char_state(
                style,
                wipe_transition,
                index,
                count,
                char_start_ms=char_start_ms,
                char_end_ms=char_end_ms,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        return 1.0, 0.0, 0.0, 0.0, 1.0, 1.0

    if transition.effect == "utopia" and transition.phase == "entry":
        if t_ms is None or transition.start_ms is None:
            local = _staggered_char_progress(transition.progress, index, count)
            opacity = min(max(local, 0.0), 1.0)
            return opacity, 0.0, 0.0, 0.0, opacity, opacity
        delay = _utopia_intro_delay_step(count) * index
        elapsed = t_ms - transition.start_ms - delay
        if elapsed < 0:
            return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        opacity = min(elapsed / _UTOPIA_INTRO_ENLARGE_MS, 1.0)
        if elapsed < _UTOPIA_INTRO_ENLARGE_MS:
            scale = _UTOPIA_INTRO_OVER_RATIO * elapsed / _UTOPIA_INTRO_ENLARGE_MS
        elif elapsed < _UTOPIA_INTRO_ENLARGE_MS + _UTOPIA_INTRO_CONDENSE_MS:
            remaining = _UTOPIA_INTRO_ENLARGE_MS + _UTOPIA_INTRO_CONDENSE_MS - elapsed
            scale = 1.0 + (_UTOPIA_INTRO_OVER_RATIO - 1.0) * remaining / _UTOPIA_INTRO_CONDENSE_MS
        else:
            scale = 1.0
        return opacity, 0.0, 0.0, 0.0, scale, scale

    if transition.phase == "exit" and transition.effect == "utopia":
        if t_ms is None:
            local = transition.progress
        else:
            done_ms = following_done_ms if following_done_ms is not None else char_end_ms
            if done_ms is None:
                local = transition.progress
            else:
                local = (t_ms - done_ms) / _UTOPIA_FADE_OUT_TIME_MS
        local = min(max(local, 0.0), 1.0)
        opacity = max(0.0, 1.0 - local)
        shrink = 1.0 - local
        height = frame_height if frame_height and frame_height > 0 else 1080
        amp = height / 15.0
        if local <= 0.5:
            x_travel = math.sin(math.pi * local) * amp
        else:
            x_travel = amp + math.sin((local - 0.5) * math.pi) * amp
        y_travel = math.sin(math.pi * local / 2.0) * amp
        x_flip = math.cos(math.pi * local)
        rotation = -180.0 * local
        return opacity, -x_travel, y_travel, rotation, shrink * x_flip, shrink

    if transition.phase == "wipe" and transition.effect == "utopia":
        if char_start_ms is None or char_end_ms is None or t_ms is None:
            return 1.0, 0.0, 0.0, 0.0, 1.0, 1.0
        scale = _utopia_wipe_scale(t_ms, char_start_ms, char_end_ms)
        return 1.0, 0.0, 0.0, 0.0, scale, scale

    if transition.effect == "char_fade":
        opacity = _char_fade_opacity(
            transition,
            index,
            count,
            t_ms=t_ms,
        )
        return opacity, 0.0, 0.0, 0.0, 1.0, 1.0

    local = _staggered_char_progress(transition.progress, index, count)
    eased = 1.0 - (1.0 - local) * (1.0 - local)
    if transition.phase == "entry":
        opacity = 0.22 + 0.78 * eased
        return opacity, 0.0, 0.0, 0.0, 1.0, 1.0

    opacity = 1.0 - eased
    if transition.effect == "utopia":
        return 0.0, 0.0, 0.0, 0.0, 1.0, 1.0
    return opacity, 0.0, 0.0, 0.0, 1.0, 1.0


def _apply_character_transform(
    painter: QPainter,
    *,
    center_x: float,
    center_y: float,
    dx: float,
    dy: float,
    rotation: float,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    scale_origin_x: float | None = None,
    scale_origin_y: float | None = None,
) -> None:
    if not dx and not dy and not rotation and scale_x == 1.0 and scale_y == 1.0:
        return
    if scale_origin_x is not None and scale_origin_y is not None:
        painter.translate(scale_origin_x + dx, scale_origin_y + dy)
        if scale_x != 1.0 or scale_y != 1.0:
            painter.scale(scale_x, scale_y)
        painter.translate(center_x - scale_origin_x, center_y - scale_origin_y)
        if rotation:
            painter.rotate(rotation)
        painter.translate(-center_x, -center_y)
        return
    painter.translate(center_x + dx, center_y + dy)
    if rotation:
        painter.rotate(rotation)
    if scale_x != 1.0 or scale_y != 1.0:
        painter.scale(scale_x, scale_y)
    painter.translate(-center_x, -center_y)


def _utopia_intro_delay_step(count: int) -> int:
    if count <= 1:
        return 0
    return _UTOPIA_INTRO_DELAY_MS // (count - 1)


def _is_utopia_wiping(t_ms: int, char_start_ms: int, char_end_ms: int) -> bool:
    return char_start_ms < t_ms < char_end_ms and char_start_ms != char_end_ms


def _utopia_wipe_scale(t_ms: int, char_start_ms: int, char_end_ms: int) -> float:
    if not _is_utopia_wiping(t_ms, char_start_ms, char_end_ms):
        return 1.0
    over_ms = min(int((char_end_ms - char_start_ms) * _UTOPIA_WIPE_OVER_TIME_RATIO), _UTOPIA_WIPE_OVER_TIME_LIMIT_MS)
    if over_ms <= 0:
        return 1.0
    peak_ms = char_start_ms + over_ms
    if t_ms <= peak_ms:
        progress = (t_ms - char_start_ms) / over_ms
    else:
        release_ms = max(char_end_ms - peak_ms, 1)
        progress = (char_end_ms - t_ms) / release_ms
    return 1.0 + (_UTOPIA_WIPE_OVER_RATIO - 1.0) * min(max(progress, 0.0), 1.0)


def _utopia_following_done_time(
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
    style: Style,
) -> int:
    if not intervals:
        return _line_end_ms(line)
    index = min(max(index, 0), len(intervals) - 1)
    current_end = intervals[index][1]
    next_index = _next_valid_char_index(line, index + 1)
    if next_index is not None and next_index < len(intervals):
        next_end = intervals[next_index][1]
        if current_end <= next_end:
            return next_end
    return current_end + _utopia_tail_delay_ms(style)


def _next_valid_char_index(line: TimingLine, start_index: int) -> int | None:
    for index in range(start_index, len(line.chars)):
        text = line.chars[index].text
        if text and not text.isspace():
            return index
    return None


def _utopia_tail_delay_ms(style: Style) -> int:
    return max(0, style.line_tail_ms - _UTOPIA_FADE_OUT_TIME_MS)


def _char_fade_delay_step(count: int) -> int:
    if count <= 1:
        return 0
    return _CHAR_FADE_INTRO_DELAY_MS // (count - 1)


def _char_fade_opacity(
    transition: _LineCharTransition,
    index: int,
    count: int,
    *,
    t_ms: int | None,
) -> float:
    if t_ms is None:
        return transition.progress
    if transition.phase == "entry":
        start_ms = (transition.start_ms or 0) + _char_fade_delay_step(count) * index
        return _clamped_ratio(t_ms - start_ms, _CHAR_FADE_IN_TIME_MS)
    if transition.phase == "exit":
        end_ms = (transition.end_ms or t_ms) - _char_fade_delay_step(count) * (count - index - 1)
        if t_ms > end_ms:
            return 0.0
        if t_ms < end_ms - _CHAR_FADE_OUT_TIME_MS:
            return 1.0
        return _clamped_ratio(end_ms - t_ms, _CHAR_FADE_OUT_TIME_MS)
    return 1.0


def _paint_char_karaoke_stack(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    *,
    char_x: int,
    char_width: int,
    baseline_y: int,
    metrics: QFontMetrics,
    colors: KaraokeColors,
    style: Style,
    ratio: float,
) -> None:
    if ratio <= 0.0:
        _paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.before,
            style,
            stroke_width=style.stroke_width_px,
            stroke2_width=style.stroke2_width_px,
            shadow_dx=style.shadow_offset_x,
            shadow_dy=style.shadow_offset_y,
            glow_radius=style.glow_radius_px,
        )
        return

    if ratio < 1.0:
        _paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.before,
            style,
            stroke_width=style.stroke_width_px,
            stroke2_width=style.stroke2_width_px,
            shadow_dx=style.shadow_offset_x,
            shadow_dy=style.shadow_offset_y,
            glow_radius=style.glow_radius_px,
        )
        stroke_pad = _visual_stroke_extent(style.stroke_width_px, style.stroke2_width_px)
        painter.save()
        try:
            painter.setClipRect(
                QRectF(
                    float(char_x - stroke_pad),
                    float(baseline_y - metrics.ascent() - stroke_pad),
                    float(char_width * ratio + stroke_pad),
                    float(metrics.height() + stroke_pad * 2),
                )
            )
            _paint_text_layer_stack(
                painter,
                path,
                rect,
                colors.after,
                style,
                stroke_width=style.stroke_width_px,
                stroke2_width=style.stroke2_width_px,
                shadow_dx=style.shadow_offset_x,
                shadow_dy=style.shadow_offset_y,
                glow_radius=style.glow_radius_px,
            )
        finally:
            painter.restore()
        return

    _paint_text_layer_stack(
        painter,
        path,
        rect,
        colors.after,
        style,
        stroke_width=style.stroke_width_px,
        stroke2_width=style.stroke2_width_px,
        shadow_dx=style.shadow_offset_x,
        shadow_dy=style.shadow_offset_y,
        glow_radius=style.glow_radius_px,
    )


def _staggered_char_progress(progress: float, index: int, count: int) -> float:
    if count <= 1:
        return progress
    span = 0.68
    window = 1.0 - span
    offset = (index / max(count - 1, 1)) * span
    return max(0.0, min(1.0, (progress - offset) / window))


def _clamped_ratio(elapsed_ms: int, duration_ms: int) -> float:
    if duration_ms <= 0:
        return 1.0
    return max(0.0, min(1.0, elapsed_ms / duration_ms))


def _paint_fill_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
) -> None:
    painter.fillPath(path, _brush_for_fill(fill, rect))


def _paint_stroke_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    width: int,
) -> None:
    pen = QPen(_brush_for_fill(fill, rect), max(width, 1))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.strokePath(path, pen)


def _paint_glow_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    radius: int,
) -> None:
    brush = _brush_for_fill(fill, rect)
    radius = max(radius, 1)
    steps = max(4, min(8, radius))
    alpha_step = 1.0 - (1.0 - 0.5) ** (1.0 / steps)
    for index in range(steps, 0, -1):
        width = max(1.0, radius * 2.0 * index / steps)
        painter.save()
        painter.setOpacity(alpha_step)
        pen = QPen(brush, width)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.strokePath(path, pen)
        painter.restore()


def _paint_after_fill_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    fill_segments: list[_FillSegment],
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
) -> None:
    _paint_after_path(
        painter, path, fill, rect, None, fill_segments, y, metrics, t_ms
    )


def _paint_after_stroke_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    width: int,
    fill_segments: list[_FillSegment],
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
) -> None:
    _paint_after_path(
        painter, path, fill, rect, width, fill_segments, y, metrics, t_ms
    )


def _paint_after_glow_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    width: int,
    fill_segments: list[_FillSegment],
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
) -> None:
    fill_start = _fill_extent_start(fill_segments)
    fill_end = _fill_extent_end(fill_segments, t_ms)
    if fill_start is None:
        return
    if fill_end <= fill_start:
        return
    painter.save()
    try:
        clip = QRectF(
            float(fill_start - width),
            float(y - metrics.ascent() - width),
            float((fill_end - fill_start) + width),
            float(metrics.height() + width * 2),
        )
        painter.setClipRect(clip)
        _paint_glow_path(painter, path, fill, rect, width)
    finally:
        painter.restore()


def _paint_after_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    stroke_width: int | None,
    fill_segments: list[_FillSegment],
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
) -> None:
    # 卡拉ok填色是连续左→右扫光，已唱字符总是连续从 x0 开始；
    # 把 N 个相邻 char clip 合并成单 clip rect → 整 line path 只画一次，
    # 不再 N 次重复绘制相同路径。
    fill_start = _fill_extent_start(fill_segments)
    fill_end = _fill_extent_end(fill_segments, t_ms)
    if fill_start is None:
        return
    if fill_end <= fill_start:
        return
    stroke_pad = 0 if stroke_width is None else math.ceil(stroke_width / 2)
    painter.save()
    try:
        clip = QRectF(
            float(fill_start - stroke_pad),
            float(y - metrics.ascent() - stroke_pad),
            float((fill_end - fill_start) + stroke_pad),
            float(metrics.height() + stroke_pad * 2),
        )
        painter.setClipRect(clip)
        if stroke_width is None:
            _paint_fill_path(painter, path, fill, rect)
        else:
            _paint_stroke_path(painter, path, fill, rect, stroke_width)
    finally:
        painter.restore()


def _legacy_fill_extent_end(
    char_widths: list[int],
    intervals: list[tuple[int, int]],
    x0: int,
    t_ms: int,
) -> int:
    """Return rightmost x of the karaoke-filled extent at ``t_ms``.

    卡拉ok填色按字符顺序左→右推进，给定 ``t_ms`` 时一定形如
    "前 k 个字符全填 + 第 k+1 个字符部分填 + 之后全空"。本函数返回填色
    末端的 x 坐标；与 ``x0`` 相等表示当前没有字符被填到（直接早退）。
    """
    fill_end = x0
    cursor_x = x0
    for w, (cs, ce) in zip(char_widths, intervals):
        ratio = char_fill_ratio(cs, ce, t_ms)
        if ratio <= 0.0:
            break
        if ratio >= 1.0:
            cursor_x += w
            fill_end = cursor_x
            continue
        # 部分填色——也是最后一个被填到的字符
        fill_end = cursor_x + int(round(w * ratio))
        break
    return fill_end


def _karaoke_fill_segments(
    char_widths: list[int],
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
    active_rubies: list[RubyAnnotation],
    line: TimingLine,
) -> list[_FillSegment]:
    segments: list[_FillSegment] = []
    index = 0
    while index < len(char_widths):
        ruby = _ruby_for_char_index(active_rubies, line, intervals, index)
        if ruby is None:
            left, right = char_x_ranges[index]
            start, end = intervals[index]
            segments.append(_FillSegment(left=left, right=right, start_ms=start, end_ms=end))
            index += 1
            continue

        indices = _ruby_target_indices(ruby, line, intervals)
        indices = [i for i in indices if 0 <= i < len(char_x_ranges)]
        if not indices:
            left, right = char_x_ranges[index]
            start, end = intervals[index]
            segments.append(_FillSegment(left=left, right=right, start_ms=start, end_ms=end))
            index += 1
            continue

        left = min(char_x_ranges[i][0] for i in indices)
        right = max(char_x_ranges[i][1] for i in indices)
        segments.append(_FillSegment(left=left, right=right, ruby=ruby))
        index = max(indices) + 1
    return segments


def _ruby_for_char_index(
    rubies: list[RubyAnnotation],
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
) -> RubyAnnotation | None:
    for ruby in rubies:
        if index in _ruby_target_indices(ruby, line, intervals):
            return ruby
    return None


def _ruby_target_indices(
    ruby: RubyAnnotation,
    line: TimingLine,
    intervals: list[tuple[int, int]],
) -> list[int]:
    indices = [
        index
        for index, (start, end) in enumerate(intervals)
        if start < ruby.pos_end_ms and end > ruby.pos_start_ms
    ]
    if not indices:
        indices = _find_ruby_text_indices(ruby.kanji, line)
    return indices


def _offset_fill_segments(segments: list[_FillSegment], dx: int) -> list[_FillSegment]:
    if dx == 0:
        return segments
    return [
        _FillSegment(
            left=segment.left + dx,
            right=segment.right + dx,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            ruby=segment.ruby,
        )
        for segment in segments
    ]


def _fill_extent_start(segments: list[_FillSegment]) -> int | None:
    return segments[0].left if segments else None


def _fill_extent_end(
    segments: list[_FillSegment],
    t_ms: int,
) -> int:
    """Return the current right edge of the continuous karaoke scan."""
    if not segments:
        return 0
    fill_end = segments[0].left
    for segment in segments:
        ratio = (
            _ruby_progress_ratio(segment.ruby, t_ms)
            if segment.ruby is not None
            else char_fill_ratio(segment.start_ms, segment.end_ms, t_ms)
        )
        if ratio <= 0.0:
            break
        if ratio >= 1.0:
            fill_end = segment.right
            continue
        fill_end = segment.left + int(round((segment.right - segment.left) * ratio))
        break
    return fill_end


def _brush_for_fill(fill: PaintFill, rect: QRectF) -> QBrush:
    if fill.mode == "image" and fill.image_path:
        brush = _cached_image_brush(fill.image_path, fill.image_scale_pct, rect)
        if brush is not None:
            return brush

    if fill.mode == "gradient_horizontal":
        return _linear_gradient_brush(fill, rect, 0)
    if fill.mode == "gradient_vertical":
        return _linear_gradient_brush(fill, rect, 90)
    if fill.mode == "split_vertical":
        return _split_vertical_brush(fill, rect)
    return QBrush(_valid_color(fill.color, "#FFFFFF"))


def _cached_image_brush(path: str, scale_pct: int, rect: QRectF) -> QBrush | None:
    signature = _image_file_signature(path)
    if signature is None:
        return None
    scale = max(scale_pct, 1)
    brush_key = (*signature, scale)
    with _IMAGE_FILL_LOCK:
        brush = _IMAGE_BRUSH_CACHE.get(brush_key)
        if brush is not None:
            _IMAGE_BRUSH_CACHE.move_to_end(brush_key)
            return _anchor_texture_brush(brush, rect)

    image = _cached_fill_image(signature)
    if image is None or image.isNull():
        return None
    brush = QBrush(image)
    brush_scale = scale / 100.0
    brush.setTransform(QTransform().scale(1.0 / brush_scale, 1.0 / brush_scale))

    with _IMAGE_FILL_LOCK:
        _IMAGE_BRUSH_CACHE[brush_key] = brush
        while len(_IMAGE_BRUSH_CACHE) > _IMAGE_FILL_CACHE_MAX:
            _IMAGE_BRUSH_CACHE.popitem(last=False)
    return _anchor_texture_brush(brush, rect)


def _anchor_texture_brush(brush: QBrush, rect: QRectF) -> QBrush:
    anchored = QBrush(brush)
    transform = QTransform(anchored.transform())
    transform.translate(rect.left(), rect.top())
    anchored.setTransform(transform)
    return anchored


def _cached_fill_image(signature: tuple[str, int, int]) -> QImage | None:
    with _IMAGE_FILL_LOCK:
        cached = _IMAGE_FILL_CACHE.get(signature)
        if cached is not None:
            _IMAGE_FILL_CACHE.move_to_end(signature)
            return cached
    image = QImage(signature[0])
    if image.isNull():
        return None
    with _IMAGE_FILL_LOCK:
        _IMAGE_FILL_CACHE[signature] = image
        while len(_IMAGE_FILL_CACHE) > _IMAGE_FILL_CACHE_MAX:
            _IMAGE_FILL_CACHE.popitem(last=False)
    return image


def _image_file_signature(path: str) -> tuple[str, int, int] | None:
    try:
        normalized = os.path.abspath(os.path.normpath(path))
        stat = os.stat(normalized)
    except OSError:
        return None
    return normalized, int(stat.st_mtime_ns), int(stat.st_size)


def _linear_gradient_brush(fill: PaintFill, rect: QRectF, angle_deg: int) -> QBrush:
    angle = math.radians(angle_deg % 360)
    dx = math.cos(angle)
    dy = math.sin(angle)
    projection = abs(rect.width() * dx) + abs(rect.height() * dy)
    if projection <= 0:
        projection = max(rect.width(), rect.height(), 1.0)
    half = projection / 2.0
    center = rect.center()
    start = QPointF(center.x() - dx * half, center.y() - dy * half)
    end = QPointF(center.x() + dx * half, center.y() + dy * half)

    gradient = QLinearGradient(start, end)
    for position, color in _gradient_stops(fill):
        gradient.setColorAt(position / 100.0, _valid_color(color, fill.color))
    return QBrush(gradient)


def _split_vertical_brush(fill: PaintFill, rect: QRectF) -> QBrush:
    gradient = QLinearGradient(
        QPointF(rect.left(), rect.top()),
        QPointF(rect.left(), rect.bottom()),
    )
    position = max(0.0, min(1.0, fill.split_position_pct / 100.0))
    top = _valid_color(fill.split_top_color, fill.color)
    bottom = _valid_color(fill.split_bottom_color, fill.color)
    edge_before = max(0.0, position - 0.001)
    edge_after = min(1.0, position + 0.001)
    gradient.setColorAt(0.0, top)
    gradient.setColorAt(edge_before, top)
    gradient.setColorAt(edge_after, bottom)
    gradient.setColorAt(1.0, bottom)
    return QBrush(gradient)


# ---------------------------------------------------------------------------
# Before-layer 缓存：构建 / 查询
# ---------------------------------------------------------------------------


def _fill_signature(fill: PaintFill) -> tuple:
    return (
        fill.mode,
        fill.color,
        fill.start_color,
        fill.end_color,
        tuple(_gradient_stops(fill)),
        fill.split_top_color,
        fill.split_bottom_color,
        fill.split_position_pct,
        fill.image_path,
        fill.image_scale_pct,
    )


def _karaoke_state_signature(state: KaraokeColorState) -> tuple:
    return (
        _fill_signature(state.text),
        _fill_signature(state.stroke),
        _fill_signature(state.stroke2),
        _fill_signature(state.shadow),
    )


def _before_layer_cache_key(
    line: TimingLine,
    style: Style,
    font: QFont,
    char_widths: list[int],
    colors: KaraokeColors,
) -> tuple:
    text = "".join(ch.text for ch in line.chars)
    font_sig = (
        font.family(),
        font.pixelSize(),
        int(font.weight()),
        font.italic(),
    )
    return (
        text,
        font_sig,
        tuple(char_widths),
        _karaoke_state_signature(colors.before),
        style.shadow_offset_x,
        style.shadow_offset_y,
        style.stroke_width_px,
        style.stroke2_width_px,
        style.decoration_kind,
        style.glow_radius_px,
    )


def _get_or_build_before_layer(
    key: tuple,
    line: TimingLine,
    char_widths: list[int],
    font: QFont,
    style: Style,
    colors: KaraokeColors,
    metrics: QFontMetrics,
) -> tuple[QImage, int, int]:
    with _BEFORE_LAYER_LOCK:
        cached = _BEFORE_LAYER_CACHE.get(key)
        if cached is not None:
            _BEFORE_LAYER_CACHE.move_to_end(key)
            return cached

    # 构建在锁外做（QPainter 比较重，不阻塞别的线程）
    entry = _build_before_layer(line, char_widths, font, style, colors, metrics)

    with _BEFORE_LAYER_LOCK:
        _BEFORE_LAYER_CACHE[key] = entry
        while len(_BEFORE_LAYER_CACHE) > _BEFORE_LAYER_CACHE_MAX:
            _BEFORE_LAYER_CACHE.popitem(last=False)
    return entry


def _build_before_layer(
    line: TimingLine,
    char_widths: list[int],
    font: QFont,
    style: Style,
    colors: KaraokeColors,
    metrics: QFontMetrics,
) -> tuple[QImage, int, int]:
    """Render shadow + stroke2 + stroke + base text into a transparent QImage.

    返回 ``(image, offset_x, offset_y)``：blit 时把 image 的左上画在
    ``(target_x0 + offset_x, target_baseline_y + offset_y)``，文字基线就会
    落在 (target_x0, target_baseline_y)。
    """
    total_w = sum(char_widths)
    text_ascent = metrics.ascent()
    text_h = metrics.height()

    # padding：要把阴影偏移 / 描边宽度 / glow 半径都留出余量，免得轮廓被裁
    stroke_extent = _visual_stroke_extent(style.stroke_width_px, style.stroke2_width_px)
    stroke_max = stroke_extent
    glow_extra = style.glow_radius_px * 4 if style.decoration_kind == "glow" else 0
    extent = max(stroke_max, glow_extra, 0) + 4  # +4 安全边

    pad_left = max(0, -style.shadow_offset_x) + extent
    pad_right = max(0, style.shadow_offset_x) + extent
    pad_top = max(0, -style.shadow_offset_y) + extent
    pad_bottom = max(0, style.shadow_offset_y) + extent

    img_w = max(pad_left + total_w + pad_right, 1)
    img_h = max(pad_top + text_h + pad_bottom, 1)

    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)  # 全透明（0 = #00000000 在 ARGB32_Premultiplied 里）

    local_x0 = pad_left
    local_y = pad_top + text_ascent  # baseline 在 image 内坐标

    p = QPainter(image)
    try:
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
        )
        p.setFont(font)

        local_line_path = _line_text_path(line, char_widths, font, local_x0, local_y)
        local_line_rect = QRectF(
            float(local_x0),
            float(local_y - text_ascent),
            float(total_w),
            float(text_h),
        )

        # 1) 阴影 / glow
        if style.decoration_kind == "glow":
            glow_radius = max(style.glow_radius_px, 1)
            _paint_glow_path(p, local_line_path, colors.before.shadow, local_line_rect, glow_radius)
        elif style.shadow_color and (style.shadow_offset_x or style.shadow_offset_y):
            shadow_rect = local_line_rect.translated(style.shadow_offset_x, style.shadow_offset_y)
            shadow_path = _line_text_path(
                line,
                char_widths,
                font,
                local_x0 + style.shadow_offset_x,
                local_y + style.shadow_offset_y,
            )
            _paint_fill_path(p, shadow_path, colors.before.shadow, shadow_rect)

        # 2) stroke2（双描边外层）
        if style.stroke2_width_px > 0:
            _paint_stroke_path(
                p,
                local_line_path,
                colors.before.stroke2,
                local_line_rect,
                _stroke2_pen_width(style.stroke_width_px, style.stroke2_width_px),
            )

        # 3) stroke（主描边）
        if style.stroke_color and style.stroke_width_px > 0:
            _paint_stroke_path(
                p,
                local_line_path,
                colors.before.stroke,
                local_line_rect,
                _stroke_pen_width(style.stroke_width_px),
            )

        # 4) 底色文字（未唱状态主体颜色）
        _paint_fill_path(p, local_line_path, colors.before.text, local_line_rect)
    finally:
        p.end()

    offset_x = -pad_left
    offset_y = -(pad_top + text_ascent)
    return (image, offset_x, offset_y)


def _effective_karaoke_colors(style: Style) -> KaraokeColors:
    if style.karaoke_colors is not None:
        return style.karaoke_colors

    before = KaraokeColorState(
        text=_solid_fill(style.base_color),
        stroke=_solid_fill(style.stroke_color),
        stroke2=_solid_fill("#000000"),
        shadow=_solid_fill(style.shadow_color),
    )
    after_text = _legacy_after_text_fill(style)
    after = KaraokeColorState(
        text=after_text,
        stroke=_solid_fill(style.stroke_color),
        stroke2=_solid_fill("#000000"),
        shadow=_solid_fill(style.shadow_color),
    )
    return KaraokeColors(before=before, after=after)


def _legacy_after_text_fill(style: Style) -> PaintFill:
    if not style.fill_gradient_enabled:
        return _solid_fill(style.fill_color)
    mode = "gradient_vertical" if style.fill_gradient_angle_deg in {90, 270} else "gradient_horizontal"
    return PaintFill(
        mode=mode,
        color=style.fill_color,
        start_color=style.fill_gradient_start_color,
        end_color=style.fill_gradient_end_color,
        gradient_stops=[
            (0, style.fill_gradient_start_color),
            (100, style.fill_gradient_end_color),
        ],
        split_top_color=style.fill_gradient_start_color,
        split_bottom_color=style.fill_gradient_end_color,
    )


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


def _gradient_stops(fill: PaintFill) -> list[tuple[int, str]]:
    raw = fill.gradient_stops or [(0, fill.start_color), (100, fill.end_color)]
    normalized: dict[int, str] = {}
    for position, color in raw:
        pos = max(0, min(100, int(position)))
        normalized[pos] = color
    if 0 not in normalized:
        normalized[0] = fill.start_color
    if 100 not in normalized:
        normalized[100] = fill.end_color
    return sorted(normalized.items())


def _valid_color(value: str, fallback: str) -> QColor:
    color = QColor(value)
    if color.isValid():
        return color
    fallback_color = QColor(fallback)
    return fallback_color if fallback_color.isValid() else QColor("#FF5A6F")


def _resolve_line_x(
    img_w: int,
    total_w: int,
    style: Style,
    lane: int | None,
) -> int:
    if style.line_horizontal_layout == "center":
        return (img_w - total_w) // 2
    if style.dual_line_layout and lane == 0:
        return max(style.upper_line_left_margin_px, 0)
    if style.dual_line_layout and lane == 1:
        return img_w - max(style.lower_line_right_margin_px, 0) - total_w
    return (img_w - total_w) // 2


def _line_start_ms(line: TimingLine) -> int:
    return line.chars[0].start_ms if line.chars else 0


def _line_end_ms(line: TimingLine) -> int:
    if line.end_ms is not None:
        return line.end_ms
    return line.chars[-1].start_ms + 1000 if line.chars else 0


def _style_for_line(style: Style, line: TimingLine) -> Style:
    if line.singer_id is None:
        return style
    scheme = style.singer_style_overrides.get(line.singer_id)
    if scheme is None:
        return style
    changes = {
        field: value
        for field, value in {
            "font_family": scheme.font_family,
            "font_size_px": scheme.font_size_px,
            "font_weight": scheme.font_weight,
            "italic": scheme.italic,
            "base_color": scheme.base_color,
            "fill_color": scheme.fill_color,
            "fill_gradient_enabled": scheme.fill_gradient_enabled,
            "fill_gradient_start_color": scheme.fill_gradient_start_color,
            "fill_gradient_end_color": scheme.fill_gradient_end_color,
            "fill_gradient_angle_deg": scheme.fill_gradient_angle_deg,
            "ruby_color": scheme.ruby_color,
            "stroke_color": scheme.stroke_color,
            "stroke_width_px": scheme.stroke_width_px,
            "stroke2_width_px": scheme.stroke2_width_px,
            "decoration_kind": scheme.decoration_kind,
            "glow_radius_px": scheme.glow_radius_px,
            "shadow_color": scheme.shadow_color,
            "shadow_offset_x": scheme.shadow_offset_x,
            "shadow_offset_y": scheme.shadow_offset_y,
            "ruby_font_size_px": scheme.ruby_font_size_px,
            "ruby_gap_px": scheme.ruby_gap_px,
            "karaoke_colors": scheme.karaoke_colors,
        }.items()
        if value is not None
    }
    if not changes:
        return style
    return replace(style, **changes)


def _active_rubies_for_line(
    rubies: list[RubyAnnotation],
    line: TimingLine,
) -> list[RubyAnnotation]:
    if not rubies or not line.chars:
        return []
    line_start = line.chars[0].start_ms
    line_end = line.end_ms if line.end_ms is not None else line.chars[-1].start_ms
    return [
        ruby
        for ruby in rubies
        if ruby.reading and ruby.pos_end_ms >= line_start and ruby.pos_start_ms <= line_end
    ]


def _paint_rubies(
    painter: QPainter,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
    main_baseline_y: int,
    t_ms: int,
    rubies: list[RubyAnnotation],
    style: Style,
    transition: _LineCharTransition | None = None,
) -> None:
    painter.save()
    try:
        painter.setFont(ruby_font)
        ruby_baseline_y = main_baseline_y - QFontMetrics(_build_font(style)).ascent() - max(style.ruby_gap_px, 0)
        for ruby in rubies:
            indices = _ruby_target_indices(ruby, line, intervals)
            if not indices:
                continue
            left = min(char_x_ranges[index][0] for index in indices)
            right = max(char_x_ranges[index][1] for index in indices)
            reading_w = ruby_metrics.horizontalAdvance(ruby.reading)
            x = int(round((left + right - reading_w) / 2))
            opacity, dx, dy, rotation, scale_x, scale_y = 1.0, 0.0, 0.0, 0.0, 1.0, 1.0
            if transition is not None:
                first_index = min(indices)
                last_index = max(indices)
                following_done_ms = (
                    _utopia_following_done_time(line, intervals, last_index, style)
                    if transition.effect == "utopia"
                    else None
                )
                opacity, dx, dy, rotation, scale_x, scale_y = _transition_char_state(
                    style,
                    transition,
                    first_index,
                    max(len(line.chars), 1),
                    char_start_ms=intervals[first_index][0],
                    char_end_ms=intervals[last_index][1],
                    t_ms=t_ms,
                    frame_height=painter.device().height(),
                    following_done_ms=following_done_ms,
                )
            if opacity <= 0.0:
                continue
            painter.save()
            try:
                painter.setOpacity(painter.opacity() * opacity)
                use_utopia_origin = transition is not None and transition.effect == "utopia"
                if use_utopia_origin:
                    _paint_ruby_text_units_with_transition(
                        painter,
                        ruby,
                        ruby_font,
                        ruby_metrics,
                        x,
                        ruby_baseline_y,
                        t_ms,
                        style,
                        transition,
                        first_index,
                        max(len(line.chars), 1),
                        following_done_ms,
                    )
                else:
                    _apply_character_transform(
                        painter,
                        center_x=x + reading_w / 2,
                        center_y=ruby_baseline_y - ruby_metrics.ascent() + ruby_metrics.height() / 2,
                        dx=dx,
                        dy=dy,
                        rotation=rotation,
                        scale_x=scale_x,
                        scale_y=scale_y,
                    )
                    _paint_ruby_text(
                        painter,
                        ruby,
                        ruby_font,
                        ruby_metrics,
                        x,
                        ruby_baseline_y,
                        t_ms,
                        style,
                    )
            finally:
                painter.restore()
    finally:
        painter.restore()


def _ruby_target_x_range(
    ruby: RubyAnnotation,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
) -> tuple[int, int] | None:
    indices = _ruby_target_indices(ruby, line, intervals)
    if not indices:
        return None
    left = min(char_x_ranges[index][0] for index in indices)
    right = max(char_x_ranges[index][1] for index in indices)
    return left, right


def _find_ruby_text_indices(kanji: str, line: TimingLine) -> list[int]:
    if not kanji:
        return []
    text = "".join(ch.text for ch in line.chars)
    pos = text.find(kanji)
    if pos < 0:
        return []
    return list(range(pos, min(pos + len(kanji), len(line.chars))))


def _paint_ruby_text_units_with_transition(
    painter: QPainter,
    ruby: RubyAnnotation,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int,
    baseline_y: int,
    t_ms: int,
    style: Style,
    transition: _LineCharTransition,
    char_index: int,
    char_count: int,
    following_done_ms: int | None,
) -> None:
    visual_units = _ruby_utopia_reading_units_and_intervals(ruby)
    units = [unit for unit, _interval in visual_units]
    intervals = [interval for _unit, interval in visual_units]
    if not units or len(units) != len(intervals):
        _paint_ruby_text(
            painter,
            ruby,
            ruby_font,
            ruby_metrics,
            x,
            baseline_y,
            t_ms,
            style,
        )
        return

    cursor_x = x
    for unit, (start_ms, end_ms) in zip(units, intervals):
        unit_width = ruby_metrics.horizontalAdvance(unit)
        opacity, dx, dy, rotation, scale_x, scale_y = _transition_char_state(
            style,
            transition,
            char_index,
            char_count,
            char_start_ms=start_ms,
            char_end_ms=end_ms,
            t_ms=t_ms,
            frame_height=painter.device().height(),
            following_done_ms=following_done_ms,
        )
        if opacity > 0.0:
            painter.save()
            try:
                painter.setOpacity(painter.opacity() * opacity)
                _apply_character_transform(
                    painter,
                    center_x=cursor_x + unit_width / 2,
                    center_y=baseline_y - ruby_metrics.ascent() + ruby_metrics.height() / 2,
                    dx=dx,
                    dy=dy,
                    rotation=rotation,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    scale_origin_x=cursor_x,
                    scale_origin_y=baseline_y,
                )
                _paint_ruby_text_fragment(
                    painter,
                    unit,
                    ruby_font,
                    ruby_metrics,
                    cursor_x,
                    baseline_y,
                    char_fill_ratio(start_ms, end_ms, t_ms),
                    style,
                )
            finally:
                painter.restore()
        cursor_x += unit_width


def _paint_ruby_text(
    painter: QPainter,
    ruby: RubyAnnotation,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int,
    baseline_y: int,
    t_ms: int,
    style: Style,
) -> None:
    path = QPainterPath()
    path.addText(float(x), float(baseline_y), ruby_font, ruby.reading)
    rect = QRectF(
        float(x),
        float(baseline_y - ruby_metrics.ascent()),
        float(ruby_metrics.horizontalAdvance(ruby.reading)),
        float(ruby_metrics.height()),
    )
    _paint_ruby_karaoke_path(
        painter,
        path,
        rect,
        ruby,
        t_ms,
        style,
    )


def _paint_ruby_text_fragment(
    painter: QPainter,
    text: str,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int,
    baseline_y: int,
    ratio: float,
    style: Style,
) -> None:
    path = QPainterPath()
    path.addText(float(x), float(baseline_y), ruby_font, text)
    rect = QRectF(
        float(x),
        float(baseline_y - ruby_metrics.ascent()),
        float(ruby_metrics.horizontalAdvance(text)),
        float(ruby_metrics.height()),
    )
    _paint_ruby_karaoke_fragment(
        painter,
        path,
        rect,
        ratio,
        style,
    )


def _paint_ruby_karaoke_path(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    ruby: RubyAnnotation,
    t_ms: int,
    style: Style,
) -> None:
    ratio = _ruby_progress_ratio(ruby, t_ms)
    _paint_ruby_karaoke_fragment(painter, path, rect, ratio, style)


def _paint_ruby_karaoke_fragment(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    ratio: float,
    style: Style,
) -> None:
    colors = _effective_ruby_karaoke_colors(style)
    scale = _ruby_scale(style)
    stroke_width = _scaled_px(style.stroke_width_px, scale)
    stroke2_width = _scaled_px(style.stroke2_width_px, scale)
    shadow_dx = _scaled_signed_px(style.shadow_offset_x, scale)
    shadow_dy = _scaled_signed_px(style.shadow_offset_y, scale)
    glow_radius = _scaled_px(style.glow_radius_px, scale)

    _paint_text_layer_stack(
        painter,
        path,
        rect,
        colors.before,
        style,
        stroke_width=stroke_width,
        stroke2_width=stroke2_width,
        shadow_dx=shadow_dx,
        shadow_dy=shadow_dy,
        glow_radius=glow_radius,
    )

    if ratio <= 0.0:
        return

    stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
    pad = max(stroke_extent, glow_radius * 2, abs(shadow_dx), abs(shadow_dy), 2)
    painter.save()
    try:
        painter.setClipRect(
            QRectF(
                rect.left() - pad,
                rect.top() - pad,
                rect.width() * min(ratio, 1.0) + pad,
                rect.height() + pad * 2,
            )
        )
        _paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.after,
            style,
            stroke_width=stroke_width,
            stroke2_width=stroke2_width,
            shadow_dx=shadow_dx,
            shadow_dy=shadow_dy,
            glow_radius=glow_radius,
        )
    finally:
        painter.restore()


def _paint_text_layer_stack(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    colors: KaraokeColorState,
    style: Style,
    *,
    stroke_width: int,
    stroke2_width: int,
    shadow_dx: int,
    shadow_dy: int,
    glow_radius: int,
) -> None:
    if style.decoration_kind == "glow":
        _paint_glow_path(painter, path, colors.shadow, rect, max(glow_radius, 1))
    elif shadow_dx or shadow_dy:
        shadow_path = QTransform().translate(shadow_dx, shadow_dy).map(path)
        _paint_fill_path(
            painter,
            shadow_path,
            colors.shadow,
            rect.translated(shadow_dx, shadow_dy),
        )

    if stroke2_width > 0:
        _paint_stroke_path(
            painter,
            path,
            colors.stroke2,
            rect,
            _stroke2_pen_width(stroke_width, stroke2_width),
        )
    if stroke_width > 0:
        _paint_stroke_path(
            painter,
            path,
            colors.stroke,
            rect,
            _stroke_pen_width(stroke_width),
        )
    _paint_fill_path(painter, path, colors.text, rect)


def _effective_ruby_karaoke_colors(style: Style) -> KaraokeColors:
    if style.karaoke_colors is not None:
        return style.karaoke_colors
    before = KaraokeColorState(
        text=_solid_fill(style.base_color),
        stroke=_solid_fill(style.stroke_color),
        stroke2=_solid_fill("#000000"),
        shadow=_solid_fill(style.shadow_color),
    )
    after = KaraokeColorState(
        text=_solid_fill(style.ruby_color),
        stroke=_solid_fill(style.stroke_color),
        stroke2=_solid_fill("#000000"),
        shadow=_solid_fill(style.shadow_color),
    )
    return KaraokeColors(before=before, after=after)


def _ruby_scale(style: Style) -> float:
    return max(style.ruby_font_size_px, 1) / max(style.font_size_px, 1)


def _scaled_px(value: int, scale: float) -> int:
    if value <= 0:
        return 0
    return max(1, int(round(value * scale)))


def _scaled_signed_px(value: int, scale: float) -> int:
    if value == 0:
        return 0
    sign = 1 if value > 0 else -1
    return sign * max(1, int(round(abs(value) * scale)))


def _ruby_progress_ratio(ruby: RubyAnnotation, t_ms: int) -> float:
    if not ruby.reading:
        return char_fill_ratio(ruby.pos_start_ms, ruby.pos_end_ms, t_ms)
    if not ruby.reading_part_ms:
        return char_fill_ratio(ruby.pos_start_ms, ruby.pos_end_ms, t_ms)

    intervals = _ruby_reading_intervals(ruby)
    total = max(len(intervals), 1)
    for index, (start, end) in enumerate(intervals):
        if t_ms < start:
            return index / total
        if t_ms < end:
            return (index + char_fill_ratio(start, end, t_ms)) / total
    return 1.0


def _ruby_reading_intervals(ruby: RubyAnnotation) -> list[tuple[int, int]]:
    units = _ruby_reading_units(ruby.reading)
    result: list[tuple[int, int]] = []
    boundaries = _ruby_reading_boundaries(ruby, len(units))
    for index, _unit in enumerate(units):
        start = boundaries[index]
        end = boundaries[index + 1]
        if end < start:
            end = start
        result.append((start, end))
    return result


def _ruby_utopia_reading_units_and_intervals(ruby: RubyAnnotation) -> list[tuple[str, tuple[int, int]]]:
    mora_units = _ruby_reading_units(ruby.reading)
    mora_intervals = _ruby_reading_intervals(ruby)
    result: list[tuple[str, tuple[int, int]]] = []
    for mora, (start, end) in zip(mora_units, mora_intervals):
        visual_units = _ruby_utopia_visual_units(mora)
        if len(visual_units) <= 1:
            result.append((mora, (start, end)))
            continue
        duration = max(end - start, 0)
        for index, visual in enumerate(visual_units):
            unit_start = start + round(duration * index / len(visual_units))
            unit_end = start + round(duration * (index + 1) / len(visual_units))
            result.append((visual, (unit_start, max(unit_start, unit_end))))
    return result


def _ruby_utopia_visual_units(text: str) -> list[str]:
    units: list[str] = []
    for ch in text:
        if units and ch in {"\u3099", "\u309A"}:
            units[-1] += ch
        else:
            units.append(ch)
    return units


def _ruby_reading_units(reading: str) -> list[str]:
    units: list[str] = []
    for ch in reading:
        if units and ch in _RUBY_COMBINING_CHARS:
            units[-1] += ch
        else:
            units.append(ch)
    return units


def _ruby_reading_boundaries(ruby: RubyAnnotation, unit_count: int) -> list[int]:
    if unit_count <= 0:
        return [ruby.pos_start_ms, ruby.pos_end_ms]
    boundaries = [ruby.pos_start_ms]
    for rel_ms in ruby.reading_part_ms[: max(unit_count - 1, 0)]:
        ts = ruby.pos_start_ms + rel_ms
        ts = max(boundaries[-1], min(ruby.pos_end_ms, ts))
        boundaries.append(ts)
    if len(boundaries) < unit_count:
        start = boundaries[-1]
        remaining = unit_count - len(boundaries) + 1
        for step in range(1, remaining):
            boundaries.append(start + round((ruby.pos_end_ms - start) * step / remaining))
    boundaries.append(max(boundaries[-1], ruby.pos_end_ms))
    return boundaries
