"""Reusable Qt primitives shared by subtitle property pages."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.engine.render.elements.band import (
    band_artwork,
    band_spec,
)
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed


_COMPACT_CONTROL_HEIGHT = 32


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


class PillSelector(QWidget):
    """Compact horizontal or vertical single-choice pill group."""

    changed = Signal(str)

    def __init__(
        self,
        options: tuple[tuple[str, str], ...],
        parent: Optional[QWidget] = None,
        *,
        vertical: bool = False,
        icons: Optional[dict[str, QIcon]] = None,
    ) -> None:
        super().__init__(parent)
        self._current = options[0][0] if options else ""
        self._buttons: dict[str, QPushButton] = {}

        row = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for key, label in options:
            btn = QPushButton(label, self)
            btn.setObjectName("ColorPillCell")
            btn.setCheckable(True)
            btn.setMinimumHeight(28)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            icon = (icons or {}).get(key)
            if icon is not None:
                btn.setText("")
                btn.setIcon(icon)
                icon_extent = _COMPACT_CONTROL_HEIGHT * 3 // 4
                btn.setIconSize(QSize(icon_extent, icon_extent))
                btn.setFixedSize(_COMPACT_CONTROL_HEIGHT, _COMPACT_CONTROL_HEIGHT)
                btn.setToolTip(label)
                btn.setAccessibleName(label)
            if icon is None:
                btn.setSizePolicy(
                    QSizePolicy.Policy.Expanding
                    if vertical
                    else QSizePolicy.Policy.Maximum,
                    QSizePolicy.Policy.Fixed,
                )
            btn.clicked.connect(lambda _checked=False, k=key: self._select(k))
            self._buttons[key] = btn
            row.addWidget(btn, 0)
        row.addStretch(1)
        themed(
            self,
            lambda: (
                f"""
                QPushButton#ColorPillCell {{
                    background: {palette().secondary_button_bg};
                    color: {palette().secondary_button_text};
                    border: 1px solid {palette().secondary_button_border};
                    border-radius: 6px;
                    padding: 0 10px;
                    font-size: 9.5pt;
                }}
                QPushButton#ColorPillCell:hover {{
                    border-color: {palette().accent_primary};
                }}
                QPushButton#ColorPillCell:checked {{
                    background: {palette().accent_primary};
                    color: #FFFFFF;
                    border-color: {palette().accent_primary};
                    font-weight: 600;
                }}
                """
            ),
        )
        self._refresh_checked()

    def current(self) -> str:
        return self._current

    def set_current(self, key: str) -> None:
        if key == self._current or key not in self._buttons:
            self._refresh_checked()
            return
        self._current = key
        self._refresh_checked()

    def _select(self, key: str) -> None:
        if key != self._current:
            self._current = key
            self._refresh_checked()
            self.changed.emit(key)
        else:
            self._refresh_checked()

    def _refresh_checked(self) -> None:
        for key, btn in self._buttons.items():
            btn.setChecked(key == self._current)


class FolderTabPanel(QWidget):
    """Folder-style two-sided tab strip joined to one content panel."""

    leftChanged = Signal(str)
    rightChanged = Signal(str)

    _TAB_HEIGHT = 32
    _RADIUS = 8.0
    _TAB_RADIUS = 6.0

    def __init__(
        self,
        left_options: tuple[tuple[str, str], ...],
        right_options: tuple[tuple[str, str], ...],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._current = {
            "left": left_options[0][0] if left_options else "",
            "right": right_options[0][0] if right_options else "",
        }
        self._buttons: dict[tuple[str, str], QPushButton] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(12, 0, 12, 0)
        tab_row.setSpacing(0)
        for key, label in left_options:
            tab_row.addWidget(self._make_tab("left", key, label), 0)
        tab_row.addStretch(1)
        for key, label in right_options:
            tab_row.addWidget(self._make_tab("right", key, label), 0)
        root.addLayout(tab_row)

        self._content = QWidget(self)
        self.content_layout = QVBoxLayout(self._content)
        self.content_layout.setContentsMargins(10, 10, 10, 10)
        self.content_layout.setSpacing(10)
        root.addWidget(self._content)

        themed(self, self._tab_qss)
        self._refresh_checked()

    def _make_tab(self, side: str, key: str, label: str) -> QPushButton:
        btn = QPushButton(label, self)
        btn.setObjectName("FolderTabButton")
        btn.setCheckable(True)
        btn.setFixedHeight(self._TAB_HEIGHT)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        btn.clicked.connect(lambda _checked=False, s=side, k=key: self._select(s, k))
        self._buttons[(side, key)] = btn
        return btn

    @staticmethod
    def _tab_qss() -> str:
        return (
            "QPushButton#FolderTabButton {"
            " background: transparent; border: none; padding: 0 14px;"
            f" font-size: 9.5pt; color: {palette().text_secondary}; }}"
            "QPushButton#FolderTabButton:checked {"
            f" color: {palette().title_text}; font-weight: 600; }}"
        )

    def current_left(self) -> str:
        return self._current["left"]

    def current_right(self) -> str:
        return self._current["right"]

    def set_left(self, key: str) -> None:
        self._set_current("left", key)

    def set_right(self, key: str) -> None:
        self._set_current("right", key)

    def _set_current(self, side: str, key: str) -> None:
        if key != self._current[side] and (side, key) in self._buttons:
            self._current[side] = key
        self._refresh_checked()

    def _select(self, side: str, key: str) -> None:
        if key != self._current[side]:
            self._current[side] = key
            self._refresh_checked()
            (self.leftChanged if side == "left" else self.rightChanged).emit(key)
        else:
            self._refresh_checked()

    def _refresh_checked(self) -> None:
        for (side, key), btn in self._buttons.items():
            btn.setChecked(key == self._current[side])
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802, ARG002
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pal = palette()
            panel_bg = QColor(pal.shell_bg)
            tab_bg = QColor(pal.card_bg)
            border = QPen(QColor(pal.input_border), 1)

            content = QRectF(self._content.geometry()).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.setPen(border)
            painter.setBrush(panel_bg)
            painter.drawRoundedRect(content, self._RADIUS, self._RADIUS)

            for (side, key), btn in self._buttons.items():
                if btn.isHidden():
                    continue
                selected = key == self._current[side]
                rect = QRectF(btn.geometry()).adjusted(
                    0.5, 0.5, -0.5, 0.0 if selected else -1.0
                )
                radius = self._TAB_RADIUS
                path = QPainterPath()
                path.moveTo(rect.left(), rect.bottom())
                path.lineTo(rect.left(), rect.top() + radius)
                path.quadTo(rect.left(), rect.top(), rect.left() + radius, rect.top())
                path.lineTo(rect.right() - radius, rect.top())
                path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + radius)
                path.lineTo(rect.right(), rect.bottom())
                painter.setPen(border)
                painter.setBrush(panel_bg if selected else tab_bg)
                painter.drawPath(path)
                if selected:
                    painter.fillRect(
                        QRectF(
                            rect.left() + 0.5,
                            rect.bottom() - 0.5,
                            rect.width() - 1.0,
                            2.0,
                        ),
                        panel_bg,
                    )
        finally:
            painter.end()


class ClickableRow(QWidget):
    """Bare row widget that emits ``clicked`` on a left mouse press."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


def subgroup_label(text: str) -> QLabel:
    """Build the accent-bar heading shared by nested property groups."""
    label = QLabel(text)
    label.setObjectName("SubtitlePropertySubheading")
    themed(
        label,
        lambda: (
            f"color: {palette().title_text};"
            "font-size: 9.5pt;"
            "font-weight: 700;"
            f"border-left: 3px solid {palette().accent_primary};"
            "padding: 0 0 0 8px;"
        ),
    )
    return label


class SubGroup(QWidget):
    """Collapsible nested section inside a property card."""

    def __init__(
        self,
        title: str,
        *,
        collapsed: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(6)

        self._header = ClickableRow(self)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)
        label = subgroup_label(title)
        label.setParent(self._header)
        header_layout.addWidget(label, 0)
        header_layout.addStretch(1)
        self._chevron = QLabel(self._header)
        themed(
            self._chevron,
            lambda: f"color: {palette().text_secondary}; font-size: 9pt;",
        )
        header_layout.addWidget(self._chevron, 0, Qt.AlignmentFlag.AlignVCenter)

        self._host = QWidget(self)
        self.grid = QGridLayout(self._host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)

        root.addWidget(self._header)
        root.addWidget(self._host)

        self._header.clicked.connect(
            lambda: self.set_collapsed(self._host.isVisible())
        )
        self.set_collapsed(collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        self._host.setVisible(not collapsed)
        self._chevron.setText("▸" if collapsed else "▾")

    def is_collapsed(self) -> bool:
        return not self._host.isVisible()


class LineBandCapPreview(QWidget):
    """原图 + 可拖拽端点把手（参考音频合成步骤的裁剪把手样式）。

    上半区就是整张原图（棋盘格透明底），左右两条端点边界各有一个
    实心圆角把手（主题色 + 白描边 + 引线）。拖动把手直接改
    ``line_band_cap_left_px`` / ``line_band_cap_right_px``（源图像素）。
    """

    capsChanged = Signal(int, int)
    # 与音频合成步骤的裁剪把手同款配色（左右各用其中一色，便于区分）。
    _LEFT_COLOR = "#2F6BFF"
    _RIGHT_COLOR = "#F04452"
    _HANDLE_W = 12
    _HANDLE_H = 22
    _GRAB_PAD = 6

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._artwork: Optional[QImage] = None
        self._cap_left = 0
        self._cap_right = 0
        self._art_w = 1.0
        self._drag = ""
        self.setMinimumHeight(84)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip(
            "拖动两侧把手设置左/右端点宽度（源图像素）；"
            "中间是会被平铺/裁切/拉伸的中段。"
        )

    # ---- data
    def set_style(self, style: Style) -> None:
        spec = band_spec(style)
        artwork = band_artwork(spec) if spec is not None else None
        self._artwork = artwork
        self._cap_left = int(getattr(style, "line_band_cap_left_px", 0) or 0)
        self._cap_right = int(getattr(style, "line_band_cap_right_px", 0) or 0)
        self._art_w = float(artwork.width()) if artwork is not None else 1.0
        self.update()

    # ---- geometry helpers
    def _image_rect(self) -> QRectF:
        return QRectF(0.0, 0.0, float(self.width()), float(self.height()))

    def _cap_x(self, cap_px: float) -> float:
        return float(cap_px) / max(self._art_w, 1.0) * float(self.width())

    def _handle_rect(self, cap_px: float, *, right: bool = False) -> QRectF:
        x = self._cap_x(cap_px)
        if right:
            x = float(self.width()) - x
        top = (self.height() - self._HANDLE_H) / 2.0
        return QRectF(x - self._HANDLE_W / 2.0, top, self._HANDLE_W, self._HANDLE_H)

    def _hit(self, pos) -> str:
        for kind, cap in (("left", self._cap_left), ("right", self._cap_right)):
            rect = self._handle_rect(cap, right=(kind == "right"))
            if rect.adjusted(
                -self._GRAB_PAD, -self._GRAB_PAD,
                self._GRAB_PAD, self._GRAB_PAD,
            ).contains(QPointF(pos)):
                return kind
        return ""

    # ---- painting
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = self._image_rect()
            # 棋盘格透明底
            cell = 10
            painter.fillRect(rect, QColor("#3A414C"))
            for row in range(0, int(rect.height()), cell):
                for col in range(0, int(rect.width()), cell):
                    if (row // cell + col // cell) % 2 == 0:
                        painter.fillRect(col, row, cell, cell, QColor("#2B313A"))
            if self._artwork is None:
                painter.setPen(QColor("#8A8F98"))
                painter.drawText(
                    self.rect(), Qt.AlignmentFlag.AlignCenter, "未选择图片"
                )
                return
            painter.drawImage(rect, self._artwork)
            left_color = QColor(self._LEFT_COLOR)
            right_color = QColor(self._RIGHT_COLOR)
            left_x = self._cap_x(self._cap_left)
            right_x = self.width() - self._cap_x(self._cap_right)
            # 端点区域淡色遮罩，明确"这两块是端点"
            left_shade = QColor(left_color)
            left_shade.setAlpha(56)
            right_shade = QColor(right_color)
            right_shade.setAlpha(56)
            painter.fillRect(QRectF(0, 0, left_x, rect.height()), left_shade)
            painter.fillRect(
                QRectF(right_x, 0, rect.width() - right_x, rect.height()),
                right_shade,
            )
            for x, color in ((left_x, left_color), (right_x, right_color)):
                pen = QPen(QColor(color.red(), color.green(), color.blue(), 230), 2)
                painter.setPen(pen)
                painter.drawLine(int(x), 0, int(x), self.height())
            # 把手：与音频合成步骤同款（实心圆角块 + 白描边 + 引线）
            for cap, color, is_right in (
                (self._cap_left, left_color, False),
                (self._cap_right, right_color, True),
            ):
                handle = self._handle_rect(cap, right=is_right)
                painter.setPen(QPen(QColor("#FFFFFF"), 1.5))
                painter.setBrush(color)
                painter.drawRoundedRect(handle, 3.0, 3.0)
                painter.setBrush(Qt.BrushStyle.NoBrush)
            font = painter.font()
            font.setPixelSize(10)
            painter.setFont(font)
            painter.setPen(left_color)
            painter.drawText(
                QRectF(4, 2, 110, 14),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                f"左 {self._cap_left}",
            )
            painter.setPen(right_color)
            painter.drawText(
                QRectF(self.width() - 114, 2, 110, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                f"右 {self._cap_right}",
            )
        finally:
            painter.end()

    # ---- interaction
    def mousePressEvent(self, event) -> None:  # noqa: N802
        kind = self._hit(event.position())
        if kind:
            self._drag = kind
            self.setCursor(Qt.CursorShape.SizeHorCursor)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._drag:
            self.setCursor(
                Qt.CursorShape.SizeHorCursor
                if self._hit(event.position())
                else Qt.CursorShape.ArrowCursor
            )
            return
        value = int(round(
            event.position().x() / max(float(self.width()), 1.0) * self._art_w
        ))
        if self._drag == "left":
            limit = int(max(self._art_w - self._cap_right - 1, 0))
            self._cap_left = max(0, min(value, limit))
        else:
            limit = int(max(self._art_w - self._cap_left - 1, 0))
            self._cap_right = max(0, min(int(self._art_w) - value, limit))
        self.update()
        self.capsChanged.emit(self._cap_left, self._cap_right)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag = ""
        self.setCursor(Qt.CursorShape.ArrowCursor)
