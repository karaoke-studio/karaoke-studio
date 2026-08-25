"""Responsive layout primitives shared by subtitle property pages."""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import (
    QPointF,
    QRectF,
    QSize,
    QTimer,
    Qt,
    pyqtSignal as Signal,
)
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWIDGETSIZE_MAX,
)

from krok_helper.subtitle_render.frontend.properties.controls.widgets import (
    CollapsibleSection,
    subgroup_label,
)
from krok_helper.subtitle_render.frontend.widgets.theme import control_qss, palette, themed


_COMPACT_CONTROL_HEIGHT = 32


def property_field(label_text: str, control: QWidget) -> QWidget:
    """Wrap one property control with its standard vertical label."""
    box = QWidget()
    box.setObjectName("SubtitlePropertyField")
    themed(box, lambda: "#SubtitlePropertyField { background: transparent; }")
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    label = QLabel(label_text)
    themed(label, lambda: f"color: {palette().text_secondary}; font-size: 9pt;")
    control.setParent(box)
    layout.addWidget(label)
    layout.addWidget(control)
    return box


def compact_property_control(widget: QWidget) -> None:
    """Apply the shared compact sizing and focus contract to one input."""
    widget.setMinimumWidth(0)
    widget.setFixedHeight(_COMPACT_CONTROL_HEIGHT)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)


def property_section(
    title: str,
    *,
    switch: bool = False,
) -> tuple[CollapsibleSection, QVBoxLayout]:
    """Build one styled, titled and optionally switchable property card."""
    section = CollapsibleSection(title, switch=switch)
    themed(
        section,
        lambda: (
            f"""
            QFrame#SubtitlePropertySection {{
                background: {palette().card_bg};
                border: 1px solid {palette().card_border};
                border-radius: 8px;
            }}
            QToolButton#SubtitlePropertySectionHeader {{
                color: {palette().title_text};
                border: 0;
                padding: 10px 12px;
                font-size: 10.5pt;
                font-weight: 700;
                text-align: left;
            }}
            QToolButton#SubtitlePropertySectionHeader:hover {{
                color: {palette().accent_primary};
            }}
            QFrame#SubtitlePropertySection QWidget {{
                background: transparent;
            }}
            {control_qss("QFrame#SubtitlePropertySection")}
            """
        ),
    )
    return section, section.content_layout


def plain_property_card() -> tuple[QFrame, QVBoxLayout]:
    """Build an untitled, non-collapsible property card."""
    card = QFrame()
    card.setObjectName("SubtitlePropertyCard")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(10)
    themed(
        card,
        lambda: (
            f"""
            QFrame#SubtitlePropertyCard {{
                background: {palette().card_bg};
                border: 1px solid {palette().card_border};
                border-radius: 8px;
            }}
            QFrame#SubtitlePropertyCard QWidget {{
                background: transparent;
            }}
            {control_qss("QFrame#SubtitlePropertyCard")}
            """
        ),
    )
    return card, layout


def inline_property_section(
    title: str,
    parent: Optional[QWidget] = None,
) -> tuple[QWidget, QVBoxLayout]:
    """Build a nested untitled section identified by an accent subheading."""
    section = QWidget(parent)
    layout = QVBoxLayout(section)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    layout.addWidget(subgroup_label(title))
    return section, layout


class ResponsivePropertyPair(QWidget):
    """Place two property cards side by side only while they genuinely fit."""

    def __init__(
        self, parent: Optional[QWidget] = None, *, min_side_width: int = 0
    ) -> None:
        super().__init__(parent)
        self._first: Optional[QWidget] = None
        self._divider: Optional[QFrame] = None
        self._second: Optional[QWidget] = None
        self._stacked: Optional[bool] = None
        self._min_side_width = min_side_width

        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(12)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_widgets(
        self, first: QWidget, divider: Optional[QFrame], second: QWidget
    ) -> None:
        self._first = first
        self._divider = divider
        self._second = second
        self._layout.addWidget(first)
        if divider is not None:
            self._layout.addWidget(divider)
        self._layout.addWidget(second)
        self._layout.setAlignment(first, Qt.AlignmentFlag.AlignTop)
        self._layout.setAlignment(second, Qt.AlignmentFlag.AlignTop)
        self._sync_direction(force=True)

    def is_stacked(self) -> bool:
        return bool(self._stacked)

    def _divider_width(self) -> int:
        if self._divider is None:
            return 0
        return max(1, self._divider.sizeHint().width())

    def _spacing_count(self) -> int:
        return 2 if self._divider is not None else 1

    def horizontal_width_hint(self) -> int:
        if self._first is None or self._second is None:
            return 0
        first_width = max(
            self._first.minimumSizeHint().width(), self._first.sizeHint().width()
        )
        second_width = max(
            self._second.minimumSizeHint().width(), self._second.sizeHint().width()
        )
        chrome = self._divider_width() + self._layout.spacing() * self._spacing_count()
        return max(
            first_width + second_width + chrome,
            self._min_side_width * 2 + chrome,
        )

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        if self._first is None or self._second is None:
            return super().minimumSizeHint()
        width = max(
            self._first.minimumSizeHint().width(),
            self._second.minimumSizeHint().width(),
        )
        if self.is_stacked():
            divider_height = 1 if self._divider is not None else 0
            height = (
                self._first.minimumSizeHint().height()
                + self._second.minimumSizeHint().height()
                + divider_height
                + self._layout.spacing() * self._spacing_count()
            )
        else:
            height = max(
                self._first.minimumSizeHint().height(),
                self._second.minimumSizeHint().height(),
            )
        return QSize(width, height)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        self._sync_direction()
        super().resizeEvent(event)

    def _sync_direction(self, *, force: bool = False) -> None:
        if self._first is None or self._second is None:
            return
        stacked = self.width() < self.horizontal_width_hint()
        if not force and stacked == self._stacked:
            return
        self._stacked = stacked

        if stacked:
            self._layout.setDirection(QBoxLayout.Direction.TopToBottom)
            self._layout.setStretchFactor(self._first, 0)
            self._layout.setStretchFactor(self._second, 0)
            if self._divider is not None:
                self._divider.setMinimumWidth(0)
                self._divider.setMaximumWidth(QWIDGETSIZE_MAX)
                self._divider.setFixedHeight(1)
                self._divider.setFrameShape(QFrame.Shape.HLine)
                self._divider.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
                )
        else:
            self._layout.setDirection(QBoxLayout.Direction.LeftToRight)
            self._layout.setStretchFactor(self._first, 1)
            self._layout.setStretchFactor(self._second, 1)
            if self._divider is not None:
                self._divider.setMinimumHeight(0)
                self._divider.setMaximumHeight(QWIDGETSIZE_MAX)
                self._divider.setFixedWidth(1)
                self._divider.setFrameShape(QFrame.Shape.VLine)
                self._divider.setSizePolicy(
                    QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding
                )
        self.updateGeometry()


def property_section_pair(first: QWidget, second: QWidget) -> ResponsivePropertyPair:
    """Build the standard two-card responsive property row."""
    pair = ResponsivePropertyPair(min_side_width=270)
    pair._layout.setSpacing(10)
    for card in (first, second):
        card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
    pair.set_widgets(first, None, second)
    return pair


class ResponsiveFieldGrid(QWidget):
    """Reflow property fields into 1–N columns for the available width."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        min_column_width: int = 150,
        max_columns: int = 4,
    ) -> None:
        super().__init__(parent)
        self._min_column_width = max(1, min_column_width)
        self._max_columns = max(1, max_columns)
        self._items: list[QWidget] = []
        self._columns = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def add_field(self, label_text: str, control: QWidget) -> None:
        self.add_widget(property_field(label_text, control))

    def add_widget(self, widget: QWidget) -> None:
        widget.setParent(self)
        self._items.append(widget)
        self._relayout(force=True)

    def clear(self) -> None:
        while self._grid.count():
            self._grid.takeAt(0)
        for widget in self._items:
            widget.deleteLater()
        self._items.clear()
        self._columns = 0

    def _target_columns(self) -> int:
        spacing = self._grid.horizontalSpacing()
        fit = (max(self.width(), 1) + spacing) // (self._min_column_width + spacing)
        return int(max(1, min(fit, self._max_columns, max(1, len(self._items)))))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        base = super().minimumSizeHint()
        if not self._items:
            return base
        width = max(item.minimumSizeHint().width() for item in self._items)
        return QSize(width, base.height())

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        self._relayout()
        super().resizeEvent(event)

    def _relayout(self, *, force: bool = False) -> None:
        columns = self._target_columns()
        if not force and columns == self._columns:
            return
        previous = max(self._columns, columns)
        self._columns = columns
        while self._grid.count():
            self._grid.takeAt(0)
        for index, widget in enumerate(self._items):
            self._grid.addWidget(widget, index // columns, index % columns)
        for column in range(previous):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)
        self.updateGeometry()


class ResponsiveRoleHeader(QWidget):
    """Keep role navigation left-aligned and its preview right-aligned."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._navigation: Optional[QWidget] = None
        self._preview: Optional[QWidget] = None
        self._stacked: Optional[bool] = None
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(12)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_widgets(self, navigation: QWidget, preview: QWidget) -> None:
        self._navigation = navigation
        self._preview = preview
        self._layout.addWidget(
            navigation, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._layout.addStretch(1)
        self._spacer = self._layout.itemAt(1).spacerItem()
        self._layout.addWidget(
            preview, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        self._sync_direction(force=True)

    def is_stacked(self) -> bool:
        return bool(self._stacked)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        self._sync_direction()
        super().resizeEvent(event)

    def _sync_direction(self, *, force: bool = False) -> None:
        if self._navigation is None or self._preview is None:
            return
        required = (
            self._navigation.sizeHint().width()
            + self._preview.sizeHint().width()
            + self._layout.spacing()
        )
        stacked = self.width() < required
        if not force and stacked == self._stacked:
            return
        self._stacked = stacked
        self._layout.setDirection(
            QBoxLayout.Direction.TopToBottom
            if stacked
            else QBoxLayout.Direction.LeftToRight
        )
        if self._spacer is not None:
            self._spacer.changeSize(
                0,
                0,
                QSizePolicy.Policy.Minimum
                if stacked
                else QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
        self._layout.setAlignment(
            self._navigation,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
        )
        self._layout.setAlignment(
            self._preview,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
        )
        self.updateGeometry()


class _GlyphToggleButton(QToolButton):
    """Self-painted icon toggle: glyph colors always follow the live palette,
    so no pixmap regeneration is needed on theme switch."""

    def __init__(self, glyph: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._glyph = glyph
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setFixedSize(38, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event: Any) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event: Any) -> None:
        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.isChecked():
            bg = QColor(p.accent_primary)
            border = QColor(p.accent_primary)
            fg = QColor("#FFFFFF")
        elif self.underMouse():
            bg = QColor(p.secondary_button_hover_bg)
            border = QColor(p.secondary_button_hover_border)
            fg = QColor(p.text_secondary)
        else:
            bg = QColor(p.secondary_button_bg)
            border = QColor(p.secondary_button_border)
            fg = QColor(p.text_secondary)
        painter.setPen(QPen(border, 1))
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 6, 6)
        self._draw_glyph(painter, fg)
        painter.end()

    def _draw_glyph(self, painter: QPainter, color: QColor) -> None:
        inner = QRectF(self.rect()).adjusted(11, 9, -11, -9)
        pen = QPen(color, 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        if self._glyph.startswith("align_"):
            # 三条横线，按左/中/右对齐——文本对齐的通用隐喻
            painter.setPen(pen)
            widths = (1.0, 0.6, 0.85)
            for index, ratio in enumerate(widths):
                y = inner.top() + inner.height() * index / (len(widths) - 1)
                line_w = inner.width() * ratio
                if self._glyph == "align_left":
                    x = inner.left()
                elif self._glyph == "align_right":
                    x = inner.right() - line_w
                else:
                    x = inner.center().x() - line_w / 2
                painter.drawLine(QPointF(x, y), QPointF(x + line_w, y))
        else:
            # pos_top / pos_middle / pos_bottom：屏幕框 + 一条粗线标出位置
            frame_pen = QPen(color, 1.2)
            painter.setPen(frame_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            frame = inner.adjusted(-2, -2, 2, 2)
            painter.drawRoundedRect(frame, 2, 2)
            painter.setPen(pen)
            if self._glyph == "pos_top":
                y = inner.top() + 1
            elif self._glyph == "pos_middle":
                y = inner.center().y()
            else:
                y = inner.bottom() - 1
            painter.drawLine(
                QPointF(inner.left() + 1, y), QPointF(inner.right() - 1, y)
            )


_ALIGN_SEGMENT_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("left", "align_left", "左对齐"),
    ("center", "align_center", "居中"),
    ("right", "align_right", "右对齐"),
)

_POSITION_SEGMENT_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("top", "pos_top", "顶部"),
    ("center", "pos_middle", "居中"),
    ("bottom", "pos_bottom", "底部"),
)


class _GlyphSegment(QWidget):
    """互斥图标按钮组（借鉴 N3 的对齐按钮）：三值枚举用下拉要点开才能看到
    选项，图标组当前值一眼可见、切换只要一次点击。

    ``setValue`` 在值变化时发射 ``valueChanged``，与 ComboBox 的
    ``setCurrentIndex`` 语义一致（面板同步路径靠 ``_syncing`` 防环）。
    """

    valueChanged = Signal(str)

    def __init__(
        self,
        options: tuple[tuple[str, str, str], ...],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._buttons: dict[str, _GlyphToggleButton] = {}
        self._value = options[0][0]
        for value, glyph, tooltip in options:
            btn = _GlyphToggleButton(glyph, self)
            btn.setToolTip(tooltip)
            btn.setAccessibleName(tooltip)
            btn.clicked.connect(lambda _checked=False, v=value: self._on_clicked(v))
            layout.addWidget(btn, 0)
            self._buttons[value] = btn
        layout.addStretch(1)
        self._buttons[self._value].setChecked(True)

    def value(self) -> str:
        return self._value

    def setValue(self, value: str) -> None:  # noqa: N802 (Qt 风格)
        if value not in self._buttons or value == self._value:
            return
        self._value = value
        self._buttons[value].setChecked(True)
        self.valueChanged.emit(value)

    def _on_clicked(self, value: str) -> None:
        # autoExclusive 保证选中态互斥；重复点击已选中项不发信号
        if value == self._value:
            return
        self._value = value
        self.valueChanged.emit(value)


class _LayoutSchematic(QWidget):
    """布局示意图（借鉴 N3）：微缩屏幕 + 色条，不读数字也能看懂当前布局的
    行数、对齐、锚定和余白。跟随属性修改实时刷新。"""

    _DEFAULT_VIRTUAL_W = 1920.0
    _DEFAULT_VIRTUAL_H = 1080.0
    _DISPLAY_HEIGHT = 150
    # 色条宽度做些许长短变化，模拟真实歌词行（N3 同款处理）
    _BAR_RATIOS = (0.58, 0.46, 0.53, 0.42, 0.5, 0.44, 0.55, 0.47)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._state: dict = {}
        self._virtual_width = self._DEFAULT_VIRTUAL_W
        self._virtual_height = self._DEFAULT_VIRTUAL_H
        self.setFixedHeight(self._DISPLAY_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_output_size(self, width: int, height: int) -> None:
        """Use the real output canvas for pixel mapping and screen aspect ratio."""
        width = int(width)
        height = int(height)
        if width <= 0 or height <= 0:
            return
        if width == self._virtual_width and height == self._virtual_height:
            return
        self._virtual_width = float(width)
        self._virtual_height = float(height)
        self.setFixedWidth(round(self._DISPLAY_HEIGHT * width / height))
        self.updateGeometry()
        self.update()

    def set_state(self, **state: Any) -> None:
        if state != self._state:
            self._state = state
            self.update()

    def _bar_specs(self) -> list[tuple[str, float, float]]:
        """Return ``(align, offset_x, offset_y)`` per displayed row."""
        state = self._state
        mode = state.get("mode", "asymmetric")
        alignments = list(state.get("alignments") or ["left"])
        if not state.get("dual_line", True):
            alignments = alignments[:1]
        if mode == "per_row":
            return [
                (align, float(dx), float(dy))
                for align, dx, dy in state.get("rows", [("left", 0, 0)])
            ]
        if mode == "center":
            return [("center", 0.0, 0.0) for _ in alignments]
        return [(align, 0.0, 0.0) for align in alignments]

    def paintEvent(self, event: Any) -> None:
        state = self._state
        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        scale = min(
            self.width() / self._virtual_width,
            self.height() / self._virtual_height,
        )
        screen_w = self._virtual_width * scale
        screen_h = self._virtual_height * scale
        screen = QRectF(
            (self.width() - screen_w) / 2,
            (self.height() - screen_h) / 2,
            screen_w,
            screen_h,
        )
        painter.setPen(QPen(QColor(p.card_border), 1))
        painter.setBrush(QColor("#0B0D12"))
        painter.drawRoundedRect(screen, 4, 4)
        if not state:
            painter.end()
            return
        painter.setClipRect(screen)

        font_px = max(20.0, min(200.0, float(state.get("font_px", 70))))
        gap = float(state.get("gap", 0))
        y_margin = float(state.get("y_margin", 0))
        h_margin = float(state.get("h_margin", 0))
        y_position = state.get("y_position", "bottom")
        specs = self._bar_specs()

        guide_color = QColor(p.text_secondary)
        guide_color.setAlphaF(0.55)
        guide_pen = QPen(guide_color, 1, Qt.PenStyle.DashLine)

        bar_color = QColor(p.accent_primary)

        if state.get("vertical"):
            self._paint_vertical(
                painter, screen, scale, specs, font_px, gap, y_margin, h_margin,
                y_position, guide_pen, bar_color,
            )
        else:
            self._paint_horizontal(
                painter, screen, scale, specs, font_px, gap, y_margin, h_margin,
                y_position, guide_pen, bar_color,
            )
        painter.end()

    def _paint_horizontal(
        self, painter, screen, scale, specs, font_px, gap, y_margin, h_margin,
        y_position, guide_pen, bar_color,
    ) -> None:
        count = len(specs)
        bar_h = font_px
        block_h = count * bar_h + (count - 1) * gap
        if y_position == "top":
            y0 = y_margin
        elif y_position == "center":
            y0 = (self._virtual_height - block_h) / 2
        else:
            y0 = self._virtual_height - y_margin - block_h

        painter.setPen(guide_pen)
        if h_margin > 0:
            for x in (h_margin, self._virtual_width - h_margin):
                painter.drawLine(
                    QPointF(screen.left() + x * scale, screen.top()),
                    QPointF(screen.left() + x * scale, screen.bottom()),
                )
        if y_margin > 0 and y_position != "center":
            guide_y = (
                y_margin if y_position == "top" else self._virtual_height - y_margin
            )
            painter.drawLine(
                QPointF(screen.left(), screen.top() + guide_y * scale),
                QPointF(screen.right(), screen.top() + guide_y * scale),
            )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bar_color)
        usable_w = max(100.0, self._virtual_width - 2 * h_margin)
        for index, (align, dx, dy) in enumerate(specs):
            bar_w = usable_w * self._BAR_RATIOS[index % len(self._BAR_RATIOS)]
            if align == "left":
                x = h_margin
            elif align == "right":
                x = self._virtual_width - h_margin - bar_w
            else:
                x = (self._virtual_width - bar_w) / 2
            y = y0 + index * (bar_h + gap)
            painter.drawRect(
                QRectF(
                    screen.left() + (x + dx) * scale,
                    screen.top() + (y + dy) * scale,
                    bar_w * scale,
                    bar_h * scale,
                )
            )

    def _paint_vertical(
        self, painter, screen, scale, specs, font_px, gap, y_margin, h_margin,
        y_position, guide_pen, bar_color,
    ) -> None:
        """竖排近似示意：行变成从右往左排的竖条，对齐映射到上/中/下。"""
        count = len(specs)
        col_w = font_px
        block_w = count * col_w + (count - 1) * gap
        if y_position == "top":
            x0 = y_margin
        elif y_position == "center":
            x0 = (self._virtual_width - block_w) / 2
        else:
            x0 = self._virtual_width - y_margin - block_w

        painter.setPen(guide_pen)
        if h_margin > 0:
            for y in (h_margin, self._virtual_height - h_margin):
                painter.drawLine(
                    QPointF(screen.left(), screen.top() + y * scale),
                    QPointF(screen.right(), screen.top() + y * scale),
                )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bar_color)
        usable_h = max(100.0, self._virtual_height - 2 * h_margin)
        for index, (align, dx, dy) in enumerate(specs):
            col_h = usable_h * self._BAR_RATIOS[index % len(self._BAR_RATIOS)]
            if align == "left":
                y = h_margin
            elif align == "right":
                y = self._virtual_height - h_margin - col_h
            else:
                y = (self._virtual_height - col_h) / 2
            # 第1行在最右（竖排从右往左读）
            x = x0 + (count - 1 - index) * (col_w + gap)
            painter.drawRect(
                QRectF(
                    screen.left() + (x + dx) * scale,
                    screen.top() + (y + dy) * scale,
                    col_w * scale,
                    col_h * scale,
                )
            )


class _SchematicBoard(QWidget):
    """N3 式空间编排：示意图居中，锚定贴左上，左右余白在左侧垂直
    居中，上/下余白贴下边，行布局贴右边。窄面板退化为竖向堆叠。"""

    def __init__(
        self,
        left: QWidget,
        center: QWidget,
        bottom: QWidget,
        right: QWidget,
        parent: Optional[QWidget] = None,
        *,
        header_left: Optional[QWidget] = None,
        header_right: Optional[QWidget] = None,
        top_left: Optional[QWidget] = None,
        top_center: Optional[QWidget] = None,
        bottom_left: Optional[QWidget] = None,
        bottom_right: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._left = left
        self._center = center
        self._bottom = bottom
        self._right = right
        self._header_left = header_left
        self._header_right = header_right
        self._top_left = top_left
        self._top_center = top_center
        self._bottom_left = bottom_left
        self._bottom_right = bottom_right
        self._wide: Optional[bool] = None
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(8)
        for child in (
            left,
            center,
            bottom,
            right,
            header_left,
            header_right,
            top_left,
            top_center,
            bottom_left,
            bottom_right,
        ):
            if child is not None:
                child.setParent(self)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._sync(force=True)

    @staticmethod
    def _side_width(widget: QWidget) -> int:
        return max(widget.minimumSizeHint().width(), widget.sizeHint().width())

    def _wide_width_hint(self) -> int:
        # 中列现在通常是固定 16:9 幕布；断点必须使用它的真实宽度，
        # 否则 600px 左右的面板会误判为三列并导致行布局覆盖幕布。
        center_width = max(
            180,
            self._center.minimumWidth(),
            self._center.minimumSizeHint().width(),
            self._center.sizeHint().width(),
            self._side_width(self._top_center)
            if self._top_center is not None
            else 0,
        )
        return (
            max(
                self._side_width(self._left),
                self._side_width(self._top_left) if self._top_left is not None else 0,
            )
            + max(
                self._side_width(self._right),
                self._side_width(self._bottom_right)
                if self._bottom_right is not None
                else 0,
            )
            + center_width
            + self._grid.horizontalSpacing() * 2
        )

    def minimumSizeHint(self) -> QSize:
        # 只汇报竖向堆叠的最小宽：宽模式的三列行宽会卡住父级收窄，
        # 收窄不发生就永远不会切回堆叠（与 _ResponsiveFieldGrid 同款死锁）
        base = super().minimumSizeHint()
        width = max(
            self._left.minimumSizeHint().width(),
            self._right.minimumSizeHint().width(),
            self._header_left.minimumSizeHint().width()
            if self._header_left is not None
            else 0,
            self._header_right.minimumSizeHint().width()
            if self._header_right is not None
            else 0,
            self._bottom.minimumSizeHint().width(),
            self._center.minimumSizeHint().width(),
            self._top_left.minimumSizeHint().width()
            if self._top_left is not None
            else 0,
            self._top_center.minimumSizeHint().width()
            if self._top_center is not None
            else 0,
            self._bottom_left.minimumSizeHint().width()
            if self._bottom_left is not None
            else 0,
            self._bottom_right.minimumSizeHint().width()
            if self._bottom_right is not None
            else 0,
        )
        return QSize(width, base.height())

    def resizeEvent(self, event: Any) -> None:
        self._sync()
        super().resizeEvent(event)

    def _sync(self, *, force: bool = False) -> None:
        wide = self.width() >= self._wide_width_hint()
        if not force and wide == self._wide:
            return
        self._wide = wide
        while self._grid.count():
            self._grid.takeAt(0)
        if wide:
            if self._header_left is not None:
                self._grid.addWidget(
                    self._header_left,
                    0,
                    0,
                    1,
                    2,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                )
            if self._header_right is not None:
                self._grid.addWidget(
                    self._header_right,
                    0,
                    1,
                    1,
                    2,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                )
            self._grid.addWidget(
                self._left,
                1,
                0,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
            if self._top_left is not None:
                self._grid.addWidget(
                    self._top_left,
                    1,
                    0,
                    Qt.AlignmentFlag.AlignTop,
                )
            if self._top_center is not None:
                self._grid.addWidget(
                    self._top_center,
                    0,
                    1,
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                )
            if self._bottom_left is not None:
                self._grid.addWidget(
                    self._bottom_left,
                    1,
                    0,
                    2,
                    1,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                )
            self._grid.addWidget(self._center, 1, 1)
            self._grid.addWidget(
                self._right,
                1,
                2,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            )
            self._grid.addWidget(
                self._bottom,
                2,
                1,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            )
            if self._bottom_right is not None:
                self._grid.addWidget(
                    self._bottom_right,
                    2,
                    2,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                )
            # 幕布是固定 16:9 的紧凑中心列；两侧列均分剩余空间。
            # 左右余白在左列右对齐，因而紧贴幕布而不是贴中间列边界。
            self._grid.setColumnStretch(0, 1)
            self._grid.setColumnStretch(1, 0)
            self._grid.setColumnStretch(2, 1)
        else:
            next_row = 0
            if self._header_left is not None:
                self._grid.addWidget(
                    self._header_left,
                    next_row,
                    0,
                    Qt.AlignmentFlag.AlignLeft,
                )
                next_row += 1
            if self._header_right is not None:
                self._grid.addWidget(
                    self._header_right,
                    next_row,
                    0,
                    Qt.AlignmentFlag.AlignLeft,
                )
                next_row += 1
            if self._top_center is not None:
                self._grid.addWidget(
                    self._top_center, next_row, 0, Qt.AlignmentFlag.AlignHCenter
                )
                next_row += 1
            self._grid.addWidget(self._center, next_row, 0)
            next_row += 1
            self._grid.addWidget(
                self._bottom, next_row, 0, Qt.AlignmentFlag.AlignHCenter
            )
            next_row += 1
            if self._top_left is not None:
                self._grid.addWidget(self._top_left, next_row, 0)
                next_row += 1
            self._grid.addWidget(self._left, next_row, 0)
            next_row += 1
            if self._bottom_left is not None:
                self._grid.addWidget(self._bottom_left, next_row, 0)
                next_row += 1
            self._grid.addWidget(self._right, next_row, 0)
            next_row += 1
            if self._bottom_right is not None:
                self._grid.addWidget(self._bottom_right, next_row, 0)
            self._grid.setColumnStretch(0, 1)
            self._grid.setColumnStretch(1, 0)
            self._grid.setColumnStretch(2, 0)
        self.updateGeometry()
        # 换行后子控件的 sizeHint 可能改变；Qt 不一定会再派发一次
        # resizeEvent。下一轮事件再核对一次，消除断点附近的迟滞。
        QTimer.singleShot(0, self._sync)
