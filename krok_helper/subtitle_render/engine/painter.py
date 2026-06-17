"""单帧 QPainter 绘制（A4 阶段）。

入口 :func:`paint_frame` 把一行已唱 / 未唱字符渲染到给定 ``QImage`` 上。

绘制顺序（自底向上）：

1. **阴影**：整行文本按 ``shadow_offset_*`` 偏移绘一份阴影色
2. **描边**：用 ``QPainterPath.addText`` 取字形轮廓，``strokePath`` 描宽线
3. **底色**：整行字符（``base_color``）
4. **填充层**：同样字符以 ``fill_color`` 重绘，但用 ``setClipRect`` 把每个字符
   裁切到"已唱比例"（左→右扫光）

预览路径与渲染路径**共用本函数**——预览给到的 image 是缩放后的 QImage、
渲染管线给的是 1080p QImage，绘制逻辑一致。

P1 阶段会在本函数基础上加：渐变填充（B3）、ruby 注音（B1）、入场退场动画
（B4）、多歌手分色（B2）。
"""

from __future__ import annotations

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
    char_fill_ratio,
    compute_char_intervals,
    find_active_line,
)
from krok_helper.subtitle_render.models import Style, TimingLine, TimingTrack


def paint_frame(
    image: QImage,
    track: Optional[TimingTrack],
    t_ms: int,
    style: Style,
) -> QImage:
    """把 ``track`` 在 ``t_ms`` 时刻的活跃行渲染到 ``image``（原地修改）。

    若无活跃行则不画任何字（image 不变）。返回同一个 image 以便链式调用。
    """
    if track is None:
        return image
    line = find_active_line(track, t_ms)
    if line is None or not line.chars:
        return image

    painter = QPainter(image)
    try:
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        # QImage 上 setDevicePixelRatio 后，QPainter 在该 image 上的坐标系
        # 自动按 dpr 缩放——绘制坐标用"逻辑像素"，而 image.width()/height()
        # 返回的是物理像素。这里取逻辑尺寸，让上层布局算居中等都按屏幕
        # 实际可见尺寸来。
        dpr = image.devicePixelRatioF() or 1.0
        logical_w = max(int(round(image.width() / dpr)), 1)
        logical_h = max(int(round(image.height() / dpr)), 1)
        _paint_line(painter, logical_w, logical_h, line, t_ms, style)
    finally:
        painter.end()
    return image


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


def _resolve_baseline_y(metrics: QFontMetrics, img_h: int, style: Style) -> int:
    pos = style.line_y_position
    margin = max(style.line_y_margin_px, 0)
    if pos == "top":
        return margin + metrics.ascent()
    if pos == "center":
        return (img_h - metrics.height()) // 2 + metrics.ascent()
    # bottom（默认）
    return img_h - margin - metrics.descent()


def _paint_line(
    painter: QPainter,
    img_w: int,
    img_h: int,
    line: TimingLine,
    t_ms: int,
    style: Style,
) -> None:
    font = _build_font(style)
    painter.setFont(font)
    metrics = QFontMetrics(font)

    # 整行宽度 → 水平居中起点
    char_widths = [metrics.horizontalAdvance(c.text) for c in line.chars]
    total_w = sum(char_widths)
    x0 = (img_w - total_w) // 2
    y = _resolve_baseline_y(metrics, img_h, style)

    intervals = compute_char_intervals(line)

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
