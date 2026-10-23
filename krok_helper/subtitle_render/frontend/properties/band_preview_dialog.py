"""行背景带的「真实预览」窗口。

属性卡里的预览只显示切割前的原图（用户口径 2026-10-24）；最终效果——端点
缩放、中段平铺/拉伸、上色、整图投影/发光——在这个独立窗口里按成片比例尺
渲染。窗口是模型态的：样式变化时由属性面板调用 ``refresh`` 即时重画。
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.engine.render.elements.band import (
    band_spec,
    paint_band,
)


def real_band_pixmap(
    style: Style,
    output_height: int = 1080,
    max_width: int = 900,
) -> QPixmap:
    """按成片比例尺画出带子的最终效果（含端点/中段/上色/描边）。"""
    output_scale = max(float(output_height), 1.0) / 1080.0
    band_height = (
        float(style.font_size_px) * output_scale
        + float(style.line_band_pad_top_px) * output_scale
        + float(style.line_band_pad_bottom_px) * output_scale
    )
    band_height = max(24.0, min(band_height, 360.0))
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


class LineBandPreviewDialog(QDialog):
    """模型态真实预览窗口（跟随样式刷新）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("行背景带 · 真实预览")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(960, 340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        self._canvas = QLabel(self)
        self._canvas.setMinimumHeight(180)
        self._canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._canvas.setScaledContents(False)
        layout.addWidget(self._canvas, 1)
        self._hint = QLabel(
            "按成片比例尺渲染：端点原样缩放到带子两端，中段按设定铺满，"
            "投影/发光作用于合成后的整张带子图。",
            self,
        )
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)
        self._pixmap: QPixmap | None = None

    def set_output_height(self, output_height: int) -> None:
        self._output_height = max(int(output_height), 1)

    def refresh(self, style: Style, output_height: int = 1080) -> None:
        self._last_style = style
        self._output_height = max(int(output_height), 1)
        width = max(self._canvas.width(), 320)
        image = real_band_pixmap(style, self._output_height, width)
        self._pixmap = image
        self._canvas.setPixmap(image)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        style = getattr(self, "_last_style", None)
        if style is not None:
            self.refresh(style, getattr(self, "_output_height", 1080))
