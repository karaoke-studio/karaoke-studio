"""右侧属性面板。

A5 / A6 先落地 MVP 横书き子集：字体、字号、字重、斜体，以及纯色底色 /
填充色 / 描边 / 阴影。面板只维护并发出 :class:`Style`，主窗口负责把它同步给
预览画布与后续项目模型。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFontComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from krok_helper.subtitle_render.frontend.theme import palette, themed
from krok_helper.subtitle_render.models import LineYPosition, Style


def _placeholder_page(text: str) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(20, 24, 20, 24)
    label = QLabel(text)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    themed(label, lambda: f"color: {palette().text_hint}; font-size: 10pt;")
    layout.addStretch(1)
    layout.addWidget(label)
    layout.addStretch(2)
    return page


def _normalize_hex(value: str, fallback: str = "#000000") -> str:
    color = QColor(value)
    if not color.isValid():
        color = QColor(fallback)
    return color.name(QColor.NameFormat.HexRgb).upper()


class ColorButton(QPushButton):
    """Small color swatch button used by the style panel."""

    def __init__(self, color: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._color = _normalize_hex(color)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(30)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._apply()

    @property
    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        normalized = _normalize_hex(color, self._color)
        if normalized == self._color:
            return
        self._color = normalized
        self._apply()

    def _apply(self) -> None:
        text_color = "#111827" if QColor(self._color).lightness() > 150 else "#FFFFFF"
        self.setText(self._color)
        self.setStyleSheet(
            f"""
            QPushButton {{
                background: {self._color};
                color: {text_color};
                border: 1px solid {palette().card_border};
                border-radius: 6px;
                padding: 4px 10px;
                font-family: "Consolas", "Courier New", monospace;
            }}
            QPushButton:hover {{
                border-color: {palette().accent_primary};
            }}
            """
        )


class PropertyPanel(QTabWidget):
    """字幕样式 / 特效 / 装饰属性面板。"""

    styleChanged = Signal(Style)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._style = Style()
        self._syncing = False

        self.setObjectName("PropertyPanel")
        self.setMinimumWidth(280)
        self.setDocumentMode(True)
        self.setTabPosition(QTabWidget.TabPosition.North)
        themed(
            self,
            lambda: (
                f"""
                #PropertyPanel {{ background: {palette().panel_bg}; }}
                #PropertyPanel::pane {{
                    border: 1px solid {palette().card_border};
                    border-radius: 6px;
                    background: {palette().panel_bg};
                }}
                #PropertyPanel QTabBar::tab {{
                    padding: 6px 14px;
                    color: {palette().text_secondary};
                    background: transparent;
                    border: none;
                }}
                #PropertyPanel QTabBar::tab:selected {{
                    color: {palette().title_text};
                    border-bottom: 2px solid {palette().accent_primary};
                }}
                #PropertyPanel QTabBar::tab:hover {{
                    color: {palette().title_text};
                }}
                """
            ),
        )

        self.addTab(_placeholder_page("屏幕预设 / 宽高 / 时间偏移（A8 / A10 接入）"), "基本")
        self.addTab(self._make_subtitle_page(), "字幕")
        self.addTab(_placeholder_page("入场 / 退场动画、渐变填充、发光（P1 / P2）"), "特效")
        self.addTab(_placeholder_page("标题字幕、时段图片、注音样式（B7 / P2）"), "装饰")
        self.set_style(self._style, emit=False)

    # ------------------------------------------------------------------ public

    @property
    def style(self) -> Style:
        return self._style

    def set_style(self, style: Style, *, emit: bool = False) -> None:
        """Replace the displayed style and optionally emit ``styleChanged``."""
        self._style = replace(style)
        self._syncing = True
        try:
            self._font_combo.setCurrentFont(QFont(self._style.font_family))
            self._font_size_spin.setValue(self._style.font_size_px)
            self._font_weight_combo.setCurrentIndex(
                max(0, self._font_weight_combo.findData(self._style.font_weight))
            )
            self._italic_check.setChecked(self._style.italic)
            self._base_color_btn.set_color(self._style.base_color)
            self._fill_color_btn.set_color(self._style.fill_color)
            self._stroke_color_btn.set_color(self._style.stroke_color)
            self._stroke_width_spin.setValue(self._style.stroke_width_px)
            self._shadow_color_btn.set_color(self._style.shadow_color)
            self._shadow_x_spin.setValue(self._style.shadow_offset_x)
            self._shadow_y_spin.setValue(self._style.shadow_offset_y)
            self._line_position_combo.setCurrentIndex(
                max(0, self._line_position_combo.findData(self._style.line_y_position))
            )
            self._line_margin_spin.setValue(self._style.line_y_margin_px)
        finally:
            self._syncing = False
        if emit:
            self.styleChanged.emit(self._style)

    # ------------------------------------------------------------------ layout

    def _make_subtitle_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        layout.addWidget(self._make_font_group())
        layout.addWidget(self._make_color_group())
        layout.addWidget(self._make_layout_group())
        layout.addStretch(1)

        scroll.setWidget(page)
        return scroll

    def _make_font_group(self) -> QGroupBox:
        group = _styled_group("字体")
        form = _form_layout(group)

        self._font_combo = QFontComboBox(group)
        self._font_combo.currentFontChanged.connect(
            lambda font: self._update_style(font_family=font.family())
        )
        form.addRow("字体", self._font_combo)

        self._font_size_spin = _spin(12, 180, suffix=" px")
        self._font_size_spin.valueChanged.connect(
            lambda value: self._update_style(font_size_px=value)
        )
        form.addRow("字号", self._font_size_spin)

        self._font_weight_combo = QComboBox(group)
        for label, value in [
            ("常规 400", 400),
            ("中等 500", 500),
            ("半粗 600", 600),
            ("粗体 700", 700),
            ("特粗 800", 800),
            ("黑体 900", 900),
        ]:
            self._font_weight_combo.addItem(label, value)
        self._font_weight_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                font_weight=int(self._font_weight_combo.currentData())
            )
        )
        form.addRow("字重", self._font_weight_combo)

        self._italic_check = QCheckBox("斜体", group)
        self._italic_check.toggled.connect(lambda checked: self._update_style(italic=checked))
        form.addRow("", self._italic_check)
        return group

    def _make_color_group(self) -> QGroupBox:
        group = _styled_group("颜色")
        form = _form_layout(group)

        self._base_color_btn = self._color_row(form, "底色", "base_color", self._style.base_color)
        self._fill_color_btn = self._color_row(form, "填充", "fill_color", self._style.fill_color)

        stroke_row = QWidget(group)
        stroke_layout = QHBoxLayout(stroke_row)
        stroke_layout.setContentsMargins(0, 0, 0, 0)
        stroke_layout.setSpacing(8)
        self._stroke_color_btn = ColorButton(self._style.stroke_color, stroke_row)
        self._stroke_width_spin = _spin(0, 24, suffix=" px")
        self._stroke_color_btn.clicked.connect(lambda: self._choose_color("stroke_color"))
        self._stroke_width_spin.valueChanged.connect(
            lambda value: self._update_style(stroke_width_px=value)
        )
        stroke_layout.addWidget(self._stroke_color_btn, 1)
        stroke_layout.addWidget(self._stroke_width_spin)
        form.addRow("描边", stroke_row)

        shadow_row = QWidget(group)
        shadow_layout = QHBoxLayout(shadow_row)
        shadow_layout.setContentsMargins(0, 0, 0, 0)
        shadow_layout.setSpacing(8)
        self._shadow_color_btn = ColorButton(self._style.shadow_color, shadow_row)
        self._shadow_x_spin = _spin(-40, 40, suffix=" x")
        self._shadow_y_spin = _spin(-40, 40, suffix=" y")
        self._shadow_color_btn.clicked.connect(lambda: self._choose_color("shadow_color"))
        self._shadow_x_spin.valueChanged.connect(
            lambda value: self._update_style(shadow_offset_x=value)
        )
        self._shadow_y_spin.valueChanged.connect(
            lambda value: self._update_style(shadow_offset_y=value)
        )
        shadow_layout.addWidget(self._shadow_color_btn, 1)
        shadow_layout.addWidget(self._shadow_x_spin)
        shadow_layout.addWidget(self._shadow_y_spin)
        form.addRow("阴影", shadow_row)
        return group

    def _make_layout_group(self) -> QGroupBox:
        group = _styled_group("位置")
        form = _form_layout(group)

        self._line_position_combo = QComboBox(group)
        for label, value in [("底部", "bottom"), ("居中", "center"), ("顶部", "top")]:
            self._line_position_combo.addItem(label, value)
        self._line_position_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                line_y_position=self._line_position_combo.currentData()
            )
        )
        form.addRow("行位置", self._line_position_combo)

        self._line_margin_spin = _spin(0, 400, suffix=" px")
        self._line_margin_spin.valueChanged.connect(
            lambda value: self._update_style(line_y_margin_px=value)
        )
        form.addRow("边距", self._line_margin_spin)
        return group

    def _color_row(
        self,
        form: QFormLayout,
        label: str,
        field_name: str,
        color: str,
    ) -> ColorButton:
        button = ColorButton(color)
        button.clicked.connect(lambda _checked=False, field=field_name: self._choose_color(field))
        form.addRow(label, button)
        return button

    # ------------------------------------------------------------------ update

    def _choose_color(self, field_name: str) -> None:
        current = QColor(getattr(self._style, field_name))
        color = QColorDialog.getColor(current, self, "选择颜色")
        if color.isValid():
            self._set_color(field_name, color.name(QColor.NameFormat.HexRgb))

    def _set_color(self, field_name: str, color: str) -> None:
        normalized = _normalize_hex(color, getattr(self._style, field_name))
        self._update_style(**{field_name: normalized})

    def _update_style(self, **changes) -> None:
        if self._syncing:
            return
        if "line_y_position" in changes:
            changes["line_y_position"] = _normalize_line_position(changes["line_y_position"])
        self._style = replace(self._style, **changes)
        self._syncing = True
        try:
            if "base_color" in changes:
                self._base_color_btn.set_color(self._style.base_color)
            if "fill_color" in changes:
                self._fill_color_btn.set_color(self._style.fill_color)
            if "stroke_color" in changes:
                self._stroke_color_btn.set_color(self._style.stroke_color)
            if "shadow_color" in changes:
                self._shadow_color_btn.set_color(self._style.shadow_color)
        finally:
            self._syncing = False
        self.styleChanged.emit(self._style)


def _normalize_line_position(value: object) -> LineYPosition:
    if value in {"top", "center", "bottom"}:
        return value  # type: ignore[return-value]
    return "bottom"


def _spin(minimum: int, maximum: int, *, suffix: str = "") -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setSuffix(suffix)
    spin.setButtonSymbols(QSpinBox.ButtonSymbols.UpDownArrows)
    return spin


def _form_layout(parent: QWidget) -> QFormLayout:
    form = QFormLayout(parent)
    form.setContentsMargins(12, 12, 12, 12)
    form.setSpacing(10)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
    return form


def _styled_group(title: str) -> QGroupBox:
    group = QGroupBox(title)
    themed(
        group,
        lambda: (
            f"""
            QGroupBox {{
                color: {palette().title_text};
                border: 1px solid {palette().card_border};
                border-radius: 6px;
                margin-top: 10px;
                font-weight: 700;
                background: {palette().panel_bg};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }}
            QLabel, QCheckBox {{
                color: {palette().text_primary};
            }}
            QComboBox, QSpinBox {{
                min-height: 28px;
            }}
            """
        ),
    )
    return group
