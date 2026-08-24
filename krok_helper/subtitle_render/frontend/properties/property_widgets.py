"""Reusable Qt primitives shared by subtitle property pages."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen
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

from krok_helper.subtitle_render.frontend.theme import palette, themed


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
