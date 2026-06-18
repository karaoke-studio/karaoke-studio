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

from dataclasses import replace
from typing import Optional

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
)

from krok_helper.subtitle_render.engine.timeline import (
    DisplayLine,
    char_fill_ratio,
    compute_char_intervals,
    find_active_line,
    visible_display_lines,
)
from krok_helper.subtitle_render.models import RubyAnnotation, Style, TimingLine, TimingTrack


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

    # 1) 阴影（整行偏移一次画完）
    if style.shadow_color and (style.shadow_offset_x or style.shadow_offset_y):
        painter.setPen(QColor(style.shadow_color))
        cursor_x = x0 + style.shadow_offset_x
        sy = y + style.shadow_offset_y
        for ch, w in zip(line.chars, char_widths):
            painter.drawText(cursor_x, sy, ch.text)
            cursor_x += w

    # 2) 描边（QPainterPath + strokePath，比 drawText 偏移叠加更平滑）
    if style.stroke_color and style.stroke_width_px > 0:
        path = QPainterPath()
        cursor_x = x0
        for ch, w in zip(line.chars, char_widths):
            path.addText(float(cursor_x), float(y), font, ch.text)
            cursor_x += w
        pen = QPen(QColor(style.stroke_color))
        pen.setWidth(style.stroke_width_px)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.strokePath(path, pen)

    # 3) 底色（整行 base_color）
    painter.setPen(QColor(style.base_color))
    cursor_x = x0
    for ch, w in zip(line.chars, char_widths):
        painter.drawText(cursor_x, y, ch.text)
        cursor_x += w

    # 4) 填充层（按字符 fill_ratio 裁切到左侧）
    painter.setPen(QColor(style.fill_color))
    cursor_x = x0
    for ch, w, (cs, ce) in zip(line.chars, char_widths, intervals):
        ratio = char_fill_ratio(cs, ce, t_ms)
        if ratio <= 0.0:
            cursor_x += w
            continue
        if ratio >= 1.0:
            painter.drawText(cursor_x, y, ch.text)
            cursor_x += w
            continue
        painter.save()
        fill_w = int(round(w * ratio))
        clip = QRectF(
            float(cursor_x),
            float(y - metrics.ascent()),
            float(fill_w),
            float(metrics.height()),
        )
        painter.setClipRect(clip)
        painter.drawText(cursor_x, y, ch.text)
        painter.restore()
        cursor_x += w


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
            "ruby_color": scheme.ruby_color,
            "stroke_color": scheme.stroke_color,
            "stroke_width_px": scheme.stroke_width_px,
            "shadow_color": scheme.shadow_color,
            "shadow_offset_x": scheme.shadow_offset_x,
            "shadow_offset_y": scheme.shadow_offset_y,
            "ruby_font_size_px": scheme.ruby_font_size_px,
            "ruby_gap_px": scheme.ruby_gap_px,
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
