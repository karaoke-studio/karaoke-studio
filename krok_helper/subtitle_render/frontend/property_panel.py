"""右侧字幕属性面板。

窄侧栏里不要使用横向表单布局：标签和输入框会互相挤压，尤其是
``QFontComboBox``。这里采用工具软件常见的分组卡片 + 垂直字段，保证
280-320px 宽度下没有横向溢出。
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
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
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
    """Compact color swatch button."""

    def __init__(self, color: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._color = _normalize_hex(color)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
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
                padding: 0 8px;
                font-family: "Consolas", "Courier New", monospace;
                font-size: 9pt;
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
        self.setMinimumWidth(260)
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
                    top: -1px;
                }}
                #PropertyPanel QTabBar::tab {{
                    min-width: 44px;
                    padding: 7px 10px;
                    color: {palette().text_secondary};
                    background: transparent;
                    border: none;
                    font-size: 9.5pt;
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
        self.addTab(_placeholder_page("标题字幕、时段图片（B7 / P2）"), "装饰")
        self.set_style(self._style, emit=False)

    @property
    def style(self) -> Style:
        return self._style

    def set_style(self, style: Style, *, emit: bool = False) -> None:
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
            self._ruby_font_size_spin.setValue(self._style.ruby_font_size_px)
            self._ruby_color_btn.set_color(self._style.ruby_color)
            self._ruby_gap_spin.setValue(self._style.ruby_gap_px)
        finally:
            self._syncing = False
        if emit:
            self.styleChanged.emit(self._style)

    # ------------------------------------------------------------------ layout

    def _make_subtitle_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        page = QWidget()
        page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 12)
        layout.setSpacing(10)
        layout.addWidget(self._make_font_section())
        layout.addWidget(self._make_ruby_section())
        layout.addWidget(self._make_color_section())
        layout.addWidget(self._make_position_section())
        layout.addStretch(1)

        scroll.setWidget(page)
        return scroll

    def _make_font_section(self) -> QFrame:
        section, layout = _section("字体")

        self._font_combo = QFontComboBox(section)
        _compact_control(self._font_combo)
        self._font_combo.currentFontChanged.connect(
            lambda font: self._update_style(font_family=font.family())
        )
        layout.addWidget(_field("字体", self._font_combo))

        row = QWidget(section)
        row_layout = QGridLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setHorizontalSpacing(8)
        row_layout.setVerticalSpacing(0)

        self._font_size_spin = _spin(12, 180, suffix=" px")
        self._font_size_spin.valueChanged.connect(
            lambda value: self._update_style(font_size_px=value)
        )
        row_layout.addWidget(_field("字号", self._font_size_spin), 0, 0)

        self._font_weight_combo = QComboBox(section)
        _compact_control(self._font_weight_combo)
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
        row_layout.addWidget(_field("字重", self._font_weight_combo), 0, 1)
        row_layout.setColumnStretch(0, 1)
        row_layout.setColumnStretch(1, 1)
        layout.addWidget(row)

        self._italic_check = QCheckBox("斜体", section)
        self._italic_check.toggled.connect(lambda checked: self._update_style(italic=checked))
        layout.addWidget(self._italic_check)
        return section

    def _make_ruby_section(self) -> QFrame:
        section, layout = _section("注音")

        row = QWidget(section)
        row_layout = QGridLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setHorizontalSpacing(8)
        row_layout.setVerticalSpacing(8)

        self._ruby_font_size_spin = _spin(8, 96, suffix=" px")
        self._ruby_font_size_spin.valueChanged.connect(
            lambda value: self._update_style(ruby_font_size_px=value)
        )
        row_layout.addWidget(_field("字号", self._ruby_font_size_spin), 0, 0)

        self._ruby_gap_spin = _spin(0, 40, suffix=" px")
        self._ruby_gap_spin.valueChanged.connect(
            lambda value: self._update_style(ruby_gap_px=value)
        )
        row_layout.addWidget(_field("间距", self._ruby_gap_spin), 0, 1)

        self._ruby_color_btn = self._color_button("ruby_color", self._style.ruby_color)
        row_layout.addWidget(_field("颜色", self._ruby_color_btn), 1, 0, 1, 2)

        row_layout.setColumnStretch(0, 1)
        row_layout.setColumnStretch(1, 1)
        layout.addWidget(row)
        return section

    def _make_color_section(self) -> QFrame:
        section, layout = _section("颜色")

        color_grid = QWidget(section)
        grid = QGridLayout(color_grid)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        self._base_color_btn = self._color_button("base_color", self._style.base_color)
        self._fill_color_btn = self._color_button("fill_color", self._style.fill_color)
        self._stroke_color_btn = self._color_button("stroke_color", self._style.stroke_color)
        self._shadow_color_btn = self._color_button("shadow_color", self._style.shadow_color)

        grid.addWidget(_field("底色", self._base_color_btn), 0, 0)
        grid.addWidget(_field("填充", self._fill_color_btn), 0, 1)
        grid.addWidget(_field("描边", self._stroke_color_btn), 1, 0)
        grid.addWidget(_field("阴影", self._shadow_color_btn), 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addWidget(color_grid)

        detail_grid = QWidget(section)
        detail_layout = QGridLayout(detail_grid)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setHorizontalSpacing(8)
        detail_layout.setVerticalSpacing(8)

        self._stroke_width_spin = _spin(0, 24, suffix=" px")
        self._stroke_width_spin.valueChanged.connect(
            lambda value: self._update_style(stroke_width_px=value)
        )
        detail_layout.addWidget(_field("描边宽度", self._stroke_width_spin), 0, 0)

        self._shadow_x_spin = _spin(-40, 40, suffix=" px")
        self._shadow_x_spin.valueChanged.connect(
            lambda value: self._update_style(shadow_offset_x=value)
        )
        detail_layout.addWidget(_field("阴影 X", self._shadow_x_spin), 0, 1)

        self._shadow_y_spin = _spin(-40, 40, suffix=" px")
        self._shadow_y_spin.valueChanged.connect(
            lambda value: self._update_style(shadow_offset_y=value)
        )
        detail_layout.addWidget(_field("阴影 Y", self._shadow_y_spin), 1, 1)
        detail_layout.setColumnStretch(0, 1)
        detail_layout.setColumnStretch(1, 1)
        layout.addWidget(detail_grid)
        return section

    def _make_position_section(self) -> QFrame:
        section, layout = _section("位置")

        row = QWidget(section)
        row_layout = QGridLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setHorizontalSpacing(8)

        self._line_position_combo = QComboBox(section)
        _compact_control(self._line_position_combo)
        for label, value in [("底部", "bottom"), ("居中", "center"), ("顶部", "top")]:
            self._line_position_combo.addItem(label, value)
        self._line_position_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                line_y_position=self._line_position_combo.currentData()
            )
        )
        row_layout.addWidget(_field("行位置", self._line_position_combo), 0, 0)

        self._line_margin_spin = _spin(0, 400, suffix=" px")
        self._line_margin_spin.valueChanged.connect(
            lambda value: self._update_style(line_y_margin_px=value)
        )
        row_layout.addWidget(_field("边距", self._line_margin_spin), 0, 1)
        row_layout.setColumnStretch(0, 1)
        row_layout.setColumnStretch(1, 1)
        layout.addWidget(row)
        return section

    def _color_button(self, field_name: str, color: str) -> ColorButton:
        button = ColorButton(color)
        button.clicked.connect(lambda _checked=False, field=field_name: self._choose_color(field))
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
            if "ruby_color" in changes:
                self._ruby_color_btn.set_color(self._style.ruby_color)
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
    _compact_control(spin)
    return spin


def _compact_control(widget: QWidget) -> None:
    widget.setMinimumWidth(0)
    widget.setFixedHeight(32)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


def _field(label_text: str, control: QWidget) -> QWidget:
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    label = QLabel(label_text)
    themed(label, lambda: f"color: {palette().text_secondary}; font-size: 9pt;")
    control.setParent(box)
    layout.addWidget(label)
    layout.addWidget(control)
    return box


def _section(title: str) -> tuple[QFrame, QVBoxLayout]:
    section = QFrame()
    section.setObjectName("SubtitlePropertySection")
    themed(
        section,
        lambda: (
            f"""
            QFrame#SubtitlePropertySection {{
                background: {palette().card_bg};
                border: 1px solid {palette().card_border};
                border-radius: 8px;
            }}
            QFrame#SubtitlePropertySection QComboBox,
            QFrame#SubtitlePropertySection QFontComboBox,
            QFrame#SubtitlePropertySection QSpinBox {{
                background: {palette().panel_bg};
                color: {palette().text_primary};
                border: 1px solid {palette().card_border};
                border-radius: 6px;
                padding: 0 8px;
                font-size: 9.5pt;
            }}
            QFrame#SubtitlePropertySection QComboBox:hover,
            QFrame#SubtitlePropertySection QFontComboBox:hover,
            QFrame#SubtitlePropertySection QSpinBox:hover {{
                border-color: {palette().accent_primary};
            }}
            QFrame#SubtitlePropertySection QCheckBox {{
                color: {palette().text_primary};
                font-size: 9.5pt;
                background: transparent;
            }}
            """
        ),
    )
    layout = QVBoxLayout(section)
    layout.setContentsMargins(12, 10, 12, 12)
    layout.setSpacing(10)
    title_label = QLabel(title)
    themed(
        title_label,
        lambda: f"color: {palette().title_text}; font-size: 10.5pt; font-weight: 700;",
    )
    layout.addWidget(title_label)
    return section, layout
