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

P1 阶段会在本函数基础上加：渐变填充（B3）、入场退场动画（B4）、
多歌手分色（B2）。
"""

from __future__ import annotations

import math
from dataclasses import replace
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

from krok_helper.subtitle_render.engine.timeline import (
    DisplayLine,
    char_fill_ratio,
    compute_char_intervals,
    find_active_line,
    visible_display_lines,
)
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
    line = find_active_line(track, t_ms, lead_in_ms=style.line_lead_in_ms)
    if line is None:
        return []
    return [DisplayLine(line=line, lane=0, display_start_ms=0, display_end_ms=0)]


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


def _resolve_baseline_y(
    metrics: QFontMetrics,
    img_h: int,
    style: Style,
    ruby_metrics: QFontMetrics | None = None,
) -> int:
    pos = style.line_y_position
    margin = max(style.line_y_margin_px, 0)
    ruby_extra = 0
    if ruby_metrics is not None:
        ruby_extra = max(style.ruby_gap_px, 0) + ruby_metrics.height()
    if pos == "top":
        return margin + ruby_extra + metrics.ascent()
    if pos == "center":
        block_h = metrics.height() + ruby_extra
        return (img_h - block_h) // 2 + ruby_extra + metrics.ascent()
    # bottom（默认）
    return img_h - margin - metrics.descent()


def _fixed_line_geometry(style: Style) -> tuple[int, int, int, int]:
    font = _build_font(style)
    metrics = QFontMetrics(font)
    ruby_metrics = QFontMetrics(_build_ruby_font(style))
    ruby_extra = max(style.ruby_gap_px, 0) + ruby_metrics.height()
    main_h = metrics.ascent() + metrics.descent()
    return main_h, metrics.ascent(), metrics.descent(), ruby_extra


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
) -> None:
    style = _style_for_line(style, line)
    font = _build_font(style)
    painter.setFont(font)
    metrics = QFontMetrics(font)
    active_rubies = _active_rubies_for_line(track.rubies, line)
    ruby_font = _build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font) if active_rubies else None

    # 整行宽度 → 水平居中起点
    char_widths = [metrics.horizontalAdvance(c.text) for c in line.chars]
    total_w = sum(char_widths)
    x0 = _resolve_line_x(img_w, total_w, style, lane)
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
        )

    line_rect = QRectF(
        float(x0),
        float(y - metrics.ascent()),
        float(total_w),
        float(metrics.height()),
    )
    colors = _effective_karaoke_colors(style)
    line_path = _line_text_path(line, char_widths, font, x0, y)

    if style.decoration_kind == "glow":
        glow_radius = max(style.glow_radius_px, 1)
        _paint_glow_path(painter, line_path, colors.before.shadow, line_rect, glow_radius)
        _paint_after_glow_path(
            painter,
            line_path,
            colors.after.shadow,
            line_rect,
            glow_radius,
            char_widths,
            intervals,
            x0,
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
        _paint_fill_path(painter, shadow_path, colors.before.shadow, shadow_rect)
        _paint_after_fill_path(
            painter,
            shadow_path,
            colors.after.shadow,
            shadow_rect,
            char_widths,
            intervals,
            x0 + style.shadow_offset_x,
            y + style.shadow_offset_y,
            metrics,
            t_ms,
        )

    if style.stroke2_width_px > 0:
        _paint_stroke_path(
            painter, line_path, colors.before.stroke2, line_rect, style.stroke2_width_px
        )
        _paint_after_stroke_path(
            painter,
            line_path,
            colors.after.stroke2,
            line_rect,
            style.stroke2_width_px,
            char_widths,
            intervals,
            x0,
            y,
            metrics,
            t_ms,
        )

    if style.stroke_color and style.stroke_width_px > 0:
        _paint_stroke_path(
            painter, line_path, colors.before.stroke, line_rect, style.stroke_width_px
        )
        _paint_after_stroke_path(
            painter,
            line_path,
            colors.after.stroke,
            line_rect,
            style.stroke_width_px,
            char_widths,
            intervals,
            x0,
            y,
            metrics,
            t_ms,
        )

    _paint_fill_path(painter, line_path, colors.before.text, line_rect)
    _paint_after_fill_path(
        painter,
        line_path,
        colors.after.text,
        line_rect,
        char_widths,
        intervals,
        x0,
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
    char_widths: list[int],
    intervals: list[tuple[int, int]],
    x0: int,
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
) -> None:
    _paint_after_path(
        painter, path, fill, rect, None, char_widths, intervals, x0, y, metrics, t_ms
    )


def _paint_after_stroke_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    width: int,
    char_widths: list[int],
    intervals: list[tuple[int, int]],
    x0: int,
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
) -> None:
    _paint_after_path(
        painter, path, fill, rect, width, char_widths, intervals, x0, y, metrics, t_ms
    )


def _paint_after_glow_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    width: int,
    char_widths: list[int],
    intervals: list[tuple[int, int]],
    x0: int,
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
) -> None:
    cursor_x = x0
    for w, (cs, ce) in zip(char_widths, intervals):
        ratio = char_fill_ratio(cs, ce, t_ms)
        if ratio <= 0.0:
            cursor_x += w
            continue
        painter.save()
        fill_w = w if ratio >= 1.0 else int(round(w * ratio))
        clip = QRectF(
            float(cursor_x - width),
            float(y - metrics.ascent() - width),
            float(fill_w + width * 2),
            float(metrics.height() + width * 2),
        )
        painter.setClipRect(clip)
        _paint_glow_path(painter, path, fill, rect, width)
        painter.restore()
        cursor_x += w


def _paint_after_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    stroke_width: int | None,
    char_widths: list[int],
    intervals: list[tuple[int, int]],
    x0: int,
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
) -> None:
    cursor_x = x0
    for w, (cs, ce) in zip(char_widths, intervals):
        ratio = char_fill_ratio(cs, ce, t_ms)
        if ratio <= 0.0:
            cursor_x += w
            continue
        painter.save()
        fill_w = w if ratio >= 1.0 else int(round(w * ratio))
        clip = QRectF(
            float(cursor_x),
            float(y - metrics.ascent()),
            float(fill_w),
            float(metrics.height()),
        )
        painter.setClipRect(clip)
        if stroke_width is None:
            _paint_fill_path(painter, path, fill, rect)
        else:
            _paint_stroke_path(painter, path, fill, rect, stroke_width)
        painter.restore()
        cursor_x += w


def _brush_for_fill(fill: PaintFill, rect: QRectF) -> QBrush:
    if fill.mode == "image" and fill.image_path:
        image = QImage(fill.image_path)
        if not image.isNull():
            brush = QBrush(image)
            scale = max(fill.image_scale_pct, 1) / 100.0
            brush.setTransform(QTransform().scale(1.0 / scale, 1.0 / scale))
            return brush

    if fill.mode == "gradient_horizontal":
        return _linear_gradient_brush(fill, rect, 0)
    if fill.mode == "gradient_vertical":
        return _linear_gradient_brush(fill, rect, 90)
    if fill.mode == "split_vertical":
        return _split_vertical_brush(fill, rect)
    return QBrush(_valid_color(fill.color, "#FFFFFF"))


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
    gradient.setColorAt(0.0, _valid_color(fill.start_color, fill.color))
    gradient.setColorAt(1.0, _valid_color(fill.end_color, fill.color))
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
        split_top_color=style.fill_gradient_start_color,
        split_bottom_color=style.fill_gradient_end_color,
    )


def _solid_fill(color: str) -> PaintFill:
    return PaintFill(
        mode="solid",
        color=color,
        start_color=color,
        end_color=color,
        split_top_color=color,
        split_bottom_color=color,
    )


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
) -> None:
    painter.save()
    try:
        painter.setFont(ruby_font)
        ruby_baseline_y = main_baseline_y - QFontMetrics(_build_font(style)).ascent() - max(style.ruby_gap_px, 0)
        for ruby in rubies:
            target = _ruby_target_x_range(ruby, line, intervals, char_x_ranges)
            if target is None:
                continue
            left, right = target
            reading_w = ruby_metrics.horizontalAdvance(ruby.reading)
            x = int(round((left + right - reading_w) / 2))
            if style.stroke_color and style.stroke_width_px > 0:
                path = QPainterPath()
                path.addText(float(x), float(ruby_baseline_y), ruby_font, ruby.reading)
                pen = QPen(QColor(style.stroke_color))
                pen.setWidth(max(1, style.stroke_width_px // 2))
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.strokePath(path, pen)
            _paint_ruby_text(painter, ruby, ruby_metrics, x, ruby_baseline_y, t_ms, style)
    finally:
        painter.restore()


def _ruby_target_x_range(
    ruby: RubyAnnotation,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
) -> tuple[int, int] | None:
    indices = [
        index
        for index, (start, end) in enumerate(intervals)
        if start < ruby.pos_end_ms and end > ruby.pos_start_ms
    ]
    if not indices:
        indices = _find_ruby_text_indices(ruby.kanji, line)
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


def _paint_ruby_text(
    painter: QPainter,
    ruby: RubyAnnotation,
    ruby_metrics: QFontMetrics,
    x: int,
    baseline_y: int,
    t_ms: int,
    style: Style,
) -> None:
    painter.setPen(QColor(style.base_color))
    painter.drawText(x, baseline_y, ruby.reading)

    if not ruby.reading_part_ms:
        ratio = char_fill_ratio(ruby.pos_start_ms, ruby.pos_end_ms, t_ms)
        if ratio <= 0.0:
            return
        painter.save()
        try:
            if ratio < 1.0:
                painter.setClipRect(
                    QRectF(
                        float(x),
                        float(baseline_y - ruby_metrics.ascent()),
                        float(int(round(ruby_metrics.horizontalAdvance(ruby.reading) * ratio))),
                        float(ruby_metrics.height()),
                    )
                )
            painter.setPen(QColor(style.ruby_color))
            painter.drawText(x, baseline_y, ruby.reading)
        finally:
            painter.restore()
        return

    cursor_x = x
    intervals = _ruby_reading_intervals(ruby)
    painter.setPen(QColor(style.ruby_color))
    for ch, (start, end) in zip(ruby.reading, intervals):
        width = ruby_metrics.horizontalAdvance(ch)
        ratio = char_fill_ratio(start, end, t_ms)
        if ratio <= 0.0:
            cursor_x += width
            continue
        painter.save()
        if ratio < 1.0:
            painter.setClipRect(
                QRectF(
                    float(cursor_x),
                    float(baseline_y - ruby_metrics.ascent()),
                    float(int(round(width * ratio))),
                    float(ruby_metrics.height()),
                )
            )
        painter.drawText(cursor_x, baseline_y, ch)
        painter.restore()
        cursor_x += width


def _ruby_reading_intervals(ruby: RubyAnnotation) -> list[tuple[int, int]]:
    chars = list(ruby.reading)
    result: list[tuple[int, int]] = []
    for index, _ch in enumerate(chars):
        start = (
            ruby.pos_start_ms
            if index == 0
            else ruby.pos_start_ms + _safe_ruby_part_ms(ruby, index - 1)
        )
        end = (
            ruby.pos_start_ms + _safe_ruby_part_ms(ruby, index)
            if index < len(ruby.reading_part_ms)
            else ruby.pos_end_ms
        )
        if end < start:
            end = start
        result.append((start, end))
    return result


def _safe_ruby_part_ms(ruby: RubyAnnotation, index: int) -> int:
    if 0 <= index < len(ruby.reading_part_ms):
        return ruby.reading_part_ms[index]
    return ruby.pos_end_ms
