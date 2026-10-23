"""行背景带的「真实预览」窗口。

属性卡里的预览只显示切割前的原图（用户口径 2026-10-24）；最终效果——端点
缩放、中段平铺/拉伸、上色、整图投影/发光——在这个独立窗口里按成片比例尺
渲染。窗口是模型态的：样式变化时由属性面板调用 ``refresh`` 即时重画。
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import QWidget

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.engine.render.elements.band import (
    band_spec,
    paint_band,
)


def real_band_pixmap(
    style: Style,
    output_height: int = 1080,
    max_width: int = 900,
    max_height: int | None = None,
) -> QPixmap:
    """按成片比例尺画出带子的最终效果（含端点/中段/上色/描边）。"""
    output_scale = max(float(output_height), 1.0) / 1080.0
    band_height = (
        float(style.font_size_px) * output_scale
        + float(style.line_band_pad_top_px) * output_scale
        + float(style.line_band_pad_bottom_px) * output_scale
    )
    band_height = max(24.0, min(band_height, float(max_height or 360)))
    width = int(max(min(band_height * 4.5, float(max_width)), 260.0))
    height = int(round(band_height))
    image = QPixmap(width, height)
    image.fill(QColor("#242A33"))
    painter = QPainter(image)
    try:
        cell = 12
        painter.fillRect(0, 0, width, height, QColor("#3A414C"))
        for row in range(0, height, cell):
            for col in range(0, width, cell):
                if (row // cell + col // cell) % 2 == 0:
                    painter.fillRect(col, row, cell, cell, QColor("#2B313A"))
        spec = band_spec(style)
        if spec is None:
            painter.setPen(QColor("#8A8F98"))
            painter.drawText(
                QRectF(0, 0, width, height),
                Qt.AlignmentFlag.AlignCenter,
                "未选择图片",
            )
        else:
            paint_band(painter, QRectF(0, 0, width, height), spec)
    finally:
        painter.end()
    return image
