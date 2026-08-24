"""Reusable Qt primitives shared by subtitle property pages."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from krok_helper.subtitle_render.frontend.theme import palette, themed


class ToggleSwitch(QAbstractButton):
    """A compact iOS-style on/off switch used in place of a checkbox."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._track_w = 38
        self._track_h = 22
        self.setFixedSize(self._track_w, self._track_h)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._track_w, self._track_h)

    def paintEvent(self, event) -> None:  # noqa: N802, ARG002
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p = palette()
            checked = self.isChecked()
            track = QColor(p.accent_primary if checked else p.input_border)
            if not self.isEnabled():
                track.setAlpha(90)
            radius = self.height() / 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(track)
            painter.drawRoundedRect(
                QRectF(0, 0, self.width(), self.height()), radius, radius
            )
            knob = self.height() - 6
            x = self.width() - knob - 3 if checked else 3
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawEllipse(QRectF(x, 3, knob, knob))
        finally:
            painter.end()


class CollapsibleSection(QFrame):
    """A property card with a clickable header and collapsible content."""

    def __init__(
        self,
        title: str,
        parent: Optional[QWidget] = None,
        *,
        switch: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SubtitlePropertySection")
        self._content = QWidget(self)
        self._content.setObjectName("SubtitlePropertySectionContent")
        self._header = QToolButton(self)
        self._header.setObjectName("SubtitlePropertySectionHeader")
        self._header.setText(title)
        self._header.setCheckable(True)
        self._header.setChecked(True)
        self._header.setArrowType(Qt.ArrowType.DownArrow)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._header.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._header.clicked.connect(self.set_expanded)

        header_row = QWidget(self)
        header_row.setObjectName("SubtitlePropertySectionHeaderRow")
        header_layout = QHBoxLayout(header_row)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)
        header_layout.addWidget(self._header, 0)
        self._summary_text = ""
        self._summary_label = QLabel(header_row)
        self._summary_label.setObjectName("SubtitlePropertySectionSummary")
        self._summary_label.setVisible(False)
        themed(
            self._summary_label,
            lambda: f"color: {palette().text_secondary}; font-size: 9.5pt;",
        )
        header_layout.addWidget(self._summary_label, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addStretch(1)

        self.header_switch: Optional[ToggleSwitch] = None
        if switch:
            self.header_switch = ToggleSwitch(header_row)
            header_layout.addWidget(
                self.header_switch, 0, Qt.AlignmentFlag.AlignVCenter
            )
            header_layout.addSpacing(12)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(header_row)
        root.addWidget(self._content)

        self.content_layout = QVBoxLayout(self._content)
        self.content_layout.setContentsMargins(12, 0, 12, 12)
        self.content_layout.setSpacing(10)

    @property
    def header(self) -> QToolButton:
        return self._header

    def set_expanded(self, expanded: bool) -> None:
        self._header.setChecked(expanded)
        self._header.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._content.setVisible(expanded)
        self._refresh_summary()

    def is_expanded(self) -> bool:
        return self._content.isVisible()

    def set_collapsed_summary(self, text: str) -> None:
        """Set the summary shown in the header while the section is collapsed."""
        self._summary_text = str(text)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        collapsed = not self._header.isChecked()
        self._summary_label.setText(self._summary_text)
        self._summary_label.setVisible(collapsed and bool(self._summary_text))
