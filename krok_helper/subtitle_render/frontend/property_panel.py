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
    QInputDialog,
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
from krok_helper.subtitle_render.models import (
    LineHorizontalLayout,
    LineYPosition,
    SubtitleStyleScheme,
    Style,
)

_SCHEME_FIELDS = {
    "font_family",
    "font_size_px",
    "font_weight",
    "italic",
    "base_color",
    "fill_color",
    "stroke_color",
    "stroke_width_px",
    "shadow_color",
    "shadow_offset_x",
    "shadow_offset_y",
    "ruby_font_size_px",
    "ruby_color",
    "ruby_gap_px",
}

_SINGER_FILL_PALETTE = ["#FF5A6F", "#0055FF", "#FFAA00", "#00A878", "#9B5CFF"]
_SINGER_RUBY_PALETTE = ["#FF5A6F", "#00AAFF", "#FFCC33", "#40D99A", "#C08CFF"]
_GLOBAL_SCHEME_KEY = "global"
_SINGER_SCHEME_PREFIX = "singer:"
_CUSTOM_SCHEME_PREFIX = "custom:"


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


class _WheelFocusedSpinBox(QSpinBox):
    """Only adjust by wheel after the control has explicit focus."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lineEdit().setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


class _WheelFocusedComboBox(QComboBox):
    """Avoid accidental option changes while scrolling the property panel."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


class _WheelFocusedFontComboBox(QFontComboBox):
    """Font list variant of the focus-gated wheel behavior."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


class PropertyPanel(QTabWidget):
    """字幕样式 / 特效 / 装饰属性面板。"""

    styleChanged = Signal(Style)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._style = Style()
        self._syncing = False
        self._singer_options: list[tuple[int, str]] = []

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

        self.addTab(self._make_basic_page(), "基本")
        self.addTab(self._make_subtitle_page(), "字幕")
        self.addTab(_placeholder_page("入场 / 退场动画、渐变填充、发光（P1 / P2）"), "特效")
        self.addTab(_placeholder_page("标题字幕、时段图片（B7 / P2）"), "装饰")
        self.set_singers([])
        self.set_style(self._style, emit=False)

    @property
    def style(self) -> Style:
        return self._style

    def set_style(self, style: Style, *, emit: bool = False) -> None:
        self._style = replace(style)
        current_key = self._current_scheme_key()
        self._syncing = True
        try:
            self._refresh_scheme_combo(current_key)
            self._line_position_combo.setCurrentIndex(
                max(0, self._line_position_combo.findData(self._style.line_y_position))
            )
            self._line_margin_spin.setValue(self._style.line_y_margin_px)
            self._dual_line_check.setChecked(self._style.dual_line_layout)
            self._horizontal_layout_combo.setCurrentIndex(
                max(
                    0,
                    self._horizontal_layout_combo.findData(
                        self._style.line_horizontal_layout
                    ),
                )
            )
            self._line_gap_spin.setValue(self._style.line_gap_px)
            self._upper_left_spin.setValue(self._style.upper_line_left_margin_px)
            self._lower_right_spin.setValue(self._style.lower_line_right_margin_px)
            self._line_lead_spin.setValue(self._style.line_lead_in_ms)
            self._line_tail_spin.setValue(self._style.line_tail_ms)
            self._line_lane_gap_spin.setValue(self._style.line_lane_gap_ms)
            self._line_max_hold_spin.setValue(self._style.line_max_hold_ms)
            self._sync_subtitle_scheme_controls()
        finally:
            self._syncing = False
        if emit:
            self.styleChanged.emit(self._style)

    def set_singers(self, singers: list[tuple[int, str]]) -> None:
        self._singer_options = list(singers)
        current_key = self._current_scheme_key()
        changed = self._ensure_singer_schemes()
        self._syncing = True
        try:
            self._refresh_scheme_combo(current_key)
        finally:
            self._syncing = False
        self._sync_subtitle_scheme_controls()
        if changed:
            self.styleChanged.emit(self._style)

    # ------------------------------------------------------------------ layout

    def _make_basic_page(self) -> QWidget:
        scroll, layout = _scroll_page()
        layout.addWidget(self._make_position_section())
        layout.addWidget(self._make_timing_section())
        layout.addStretch(1)
        return scroll

    def _make_subtitle_page(self) -> QWidget:
        scroll, layout = _scroll_page()
        layout.addWidget(self._make_scheme_section())
        layout.addWidget(self._make_font_section())
        layout.addWidget(self._make_ruby_section())
        layout.addWidget(self._make_color_section())
        layout.addStretch(1)
        return scroll

    def _make_font_section(self) -> QFrame:
        section, layout = _section("字体")

        self._font_combo = _WheelFocusedFontComboBox(section)
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

        self._font_weight_combo = _WheelFocusedComboBox(section)
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

    def _make_scheme_section(self) -> QFrame:
        section, layout = _section("配色方案")

        self._singer_combo = _WheelFocusedComboBox(section)
        _compact_control(self._singer_combo)
        self._singer_combo.currentIndexChanged.connect(
            lambda _index: self._sync_subtitle_scheme_controls()
        )
        self._add_scheme_button = QPushButton("添加方案", section)
        self._add_scheme_button.setMinimumHeight(32)
        self._add_scheme_button.clicked.connect(
            lambda _checked=False: self._add_custom_scheme()
        )

        row = QWidget(section)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        row_layout.addWidget(self._singer_combo, 1)
        row_layout.addWidget(self._add_scheme_button)
        layout.addWidget(_field("当前方案", row))
        return section

    def _make_position_section(self) -> QFrame:
        section, layout = _section("位置")

        self._dual_line_check = QCheckBox("双行显示", section)
        self._dual_line_check.toggled.connect(
            lambda checked: self._update_style(dual_line_layout=checked)
        )
        layout.addWidget(self._dual_line_check)

        row = QWidget(section)
        row_layout = QGridLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setHorizontalSpacing(8)
        row_layout.setVerticalSpacing(8)
        self._line_position_combo = _WheelFocusedComboBox(section)
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
        row_layout.addWidget(_field("下行底边距", self._line_margin_spin), 0, 1)

        self._horizontal_layout_combo = _WheelFocusedComboBox(section)
        _compact_control(self._horizontal_layout_combo)
        for label, value in [("上左下右", "asymmetric"), ("居中", "center")]:
            self._horizontal_layout_combo.addItem(label, value)
        self._horizontal_layout_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                line_horizontal_layout=self._horizontal_layout_combo.currentData()
            )
        )
        row_layout.addWidget(_field("水平布局", self._horizontal_layout_combo), 1, 0)

        self._line_gap_spin = _spin(0, 400, suffix=" px")
        self._line_gap_spin.valueChanged.connect(
            lambda value: self._update_style(line_gap_px=value)
        )
        row_layout.addWidget(_field("两行间距", self._line_gap_spin), 1, 1)

        self._upper_left_spin = _spin(0, 800, suffix=" px")
        self._upper_left_spin.valueChanged.connect(
            lambda value: self._update_style(upper_line_left_margin_px=value)
        )
        row_layout.addWidget(_field("上行左边距", self._upper_left_spin), 2, 0)

        self._lower_right_spin = _spin(0, 800, suffix=" px")
        self._lower_right_spin.valueChanged.connect(
            lambda value: self._update_style(lower_line_right_margin_px=value)
        )
        row_layout.addWidget(_field("下行右边距", self._lower_right_spin), 2, 1)

        row_layout.setColumnStretch(0, 1)
        row_layout.setColumnStretch(1, 1)
        layout.addWidget(row)
        return section

    def _make_timing_section(self) -> QFrame:
        section, layout = _section("显示时间")

        row = QWidget(section)
        row_layout = QGridLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setHorizontalSpacing(8)
        row_layout.setVerticalSpacing(8)

        self._line_lead_spin = _spin(0, 10_000, suffix=" ms")
        self._line_lead_spin.valueChanged.connect(
            lambda value: self._update_style(line_lead_in_ms=value)
        )
        row_layout.addWidget(_field("提前显示", self._line_lead_spin), 0, 0)

        self._line_tail_spin = _spin(0, 10_000, suffix=" ms")
        self._line_tail_spin.valueChanged.connect(
            lambda value: self._update_style(line_tail_ms=value)
        )
        row_layout.addWidget(_field("唱完保留", self._line_tail_spin), 0, 1)

        self._line_lane_gap_spin = _spin(0, 5_000, suffix=" ms")
        self._line_lane_gap_spin.valueChanged.connect(
            lambda value: self._update_style(line_lane_gap_ms=value)
        )
        row_layout.addWidget(_field("同轨间隔", self._line_lane_gap_spin), 1, 0)

        self._line_max_hold_spin = _spin(1_000, 60_000, suffix=" ms")
        self._line_max_hold_spin.valueChanged.connect(
            lambda value: self._update_style(line_max_hold_ms=value)
        )
        row_layout.addWidget(_field("最大挂屏", self._line_max_hold_spin), 1, 1)

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
        current = QColor(self._scheme_value(field_name))
        color = QColorDialog.getColor(current, self, "选择颜色")
        if color.isValid():
            self._set_color(field_name, color.name(QColor.NameFormat.HexRgb))

    def _set_color(self, field_name: str, color: str) -> None:
        normalized = _normalize_hex(color, str(self._scheme_value(field_name)))
        self._update_style(**{field_name: normalized})

    def _refresh_scheme_combo(self, selected_key: Optional[str] = None) -> None:
        self._singer_combo.clear()
        self._singer_combo.addItem("全局默认", _GLOBAL_SCHEME_KEY)
        for singer_id, label in self._singer_options:
            self._singer_combo.addItem(label, f"{_SINGER_SCHEME_PREFIX}{singer_id}")
        for name in self._style.custom_style_schemes:
            self._singer_combo.addItem(name, f"{_CUSTOM_SCHEME_PREFIX}{name}")
        if selected_key is not None:
            index = self._singer_combo.findData(selected_key)
            if index >= 0:
                self._singer_combo.setCurrentIndex(index)

    def _add_custom_scheme(self, name: Optional[str] = None) -> None:
        if name is None or isinstance(name, bool):
            name, ok = QInputDialog.getText(self, "添加配色方案", "方案名称")
            if not ok:
                return
        name = name.strip()
        if not name:
            return
        schemes = dict(self._style.custom_style_schemes)
        original = name
        suffix = 2
        while name in schemes:
            name = f"{original} {suffix}"
            suffix += 1
        schemes[name] = _scheme_from_current(self)
        self._update_style(custom_style_schemes=schemes)
        self._syncing = True
        try:
            self._refresh_scheme_combo(f"{_CUSTOM_SCHEME_PREFIX}{name}")
        finally:
            self._syncing = False
        self._sync_subtitle_scheme_controls()

    def _current_scheme_key(self) -> Optional[str]:
        if not hasattr(self, "_singer_combo"):
            return None
        data = self._singer_combo.currentData()
        return str(data) if data is not None else _GLOBAL_SCHEME_KEY

    def _current_singer_id(self) -> Optional[int]:
        key = self._current_scheme_key()
        if key is None or not key.startswith(_SINGER_SCHEME_PREFIX):
            return None
        try:
            return int(key.removeprefix(_SINGER_SCHEME_PREFIX))
        except ValueError:
            return None

    def _current_custom_scheme_name(self) -> Optional[str]:
        key = self._current_scheme_key()
        if key is None or not key.startswith(_CUSTOM_SCHEME_PREFIX):
            return None
        return key.removeprefix(_CUSTOM_SCHEME_PREFIX)

    def _scheme_value(self, field_name: str):
        custom_name = self._current_custom_scheme_name()
        if custom_name is not None:
            scheme = self._style.custom_style_schemes.get(custom_name)
            value = getattr(scheme, field_name, None) if scheme is not None else None
            if value is not None:
                return value
        singer_id = self._current_singer_id()
        if singer_id is not None:
            scheme = self._style.singer_style_overrides.get(singer_id)
            value = getattr(scheme, field_name, None) if scheme is not None else None
            if value is not None:
                return value
        return getattr(self._style, field_name)

    def _ensure_singer_schemes(self) -> bool:
        overrides = dict(self._style.singer_style_overrides)
        changed = False
        for singer_id, _label in self._singer_options:
            if singer_id in overrides:
                continue
            overrides[singer_id] = _scheme_from_style(self._style, singer_id)
            changed = True
        if changed:
            self._style = replace(self._style, singer_style_overrides=overrides)
        return changed

    def _sync_subtitle_scheme_controls(self) -> None:
        if not hasattr(self, "_singer_combo"):
            return
        was_syncing = self._syncing
        self._syncing = True
        try:
            self._font_combo.setCurrentFont(QFont(str(self._scheme_value("font_family"))))
            self._font_size_spin.setValue(int(self._scheme_value("font_size_px")))
            self._font_weight_combo.setCurrentIndex(
                max(0, self._font_weight_combo.findData(int(self._scheme_value("font_weight"))))
            )
            self._italic_check.setChecked(bool(self._scheme_value("italic")))
            self._base_color_btn.set_color(str(self._scheme_value("base_color")))
            self._fill_color_btn.set_color(str(self._scheme_value("fill_color")))
            self._stroke_color_btn.set_color(str(self._scheme_value("stroke_color")))
            self._stroke_width_spin.setValue(int(self._scheme_value("stroke_width_px")))
            self._shadow_color_btn.set_color(str(self._scheme_value("shadow_color")))
            self._shadow_x_spin.setValue(int(self._scheme_value("shadow_offset_x")))
            self._shadow_y_spin.setValue(int(self._scheme_value("shadow_offset_y")))
            self._ruby_font_size_spin.setValue(int(self._scheme_value("ruby_font_size_px")))
            self._ruby_color_btn.set_color(str(self._scheme_value("ruby_color")))
            self._ruby_gap_spin.setValue(int(self._scheme_value("ruby_gap_px")))
        finally:
            self._syncing = was_syncing

    def _update_style(self, **changes) -> None:
        if self._syncing:
            return
        if changes and set(changes).issubset(_SCHEME_FIELDS):
            custom_name = self._current_custom_scheme_name()
            if custom_name is not None:
                schemes = dict(self._style.custom_style_schemes)
                scheme = schemes.get(custom_name) or _scheme_from_current(self)
                schemes[custom_name] = replace(scheme, **changes)
                changes = {"custom_style_schemes": schemes}
            else:
                singer_id = self._current_singer_id()
                if singer_id is not None:
                    overrides = dict(self._style.singer_style_overrides)
                    scheme = overrides.get(singer_id) or _scheme_from_style(self._style, singer_id)
                    overrides[singer_id] = replace(scheme, **changes)
                    changes = {"singer_style_overrides": overrides}
        if "line_y_position" in changes:
            changes["line_y_position"] = _normalize_line_position(changes["line_y_position"])
        if "line_horizontal_layout" in changes:
            changes["line_horizontal_layout"] = _normalize_horizontal_layout(
                changes["line_horizontal_layout"]
            )
        self._style = replace(self._style, **changes)
        self._syncing = True
        try:
            if set(changes).intersection(
                _SCHEME_FIELDS | {"singer_style_overrides", "custom_style_schemes"}
            ):
                self._sync_subtitle_scheme_controls()
        finally:
            self._syncing = False
        self.styleChanged.emit(self._style)


def _normalize_line_position(value: object) -> LineYPosition:
    if value in {"top", "center", "bottom"}:
        return value  # type: ignore[return-value]
    return "bottom"


def _normalize_horizontal_layout(value: object) -> LineHorizontalLayout:
    if value in {"asymmetric", "center"}:
        return value  # type: ignore[return-value]
    return "asymmetric"


def _scheme_from_style(style: Style, singer_id: int) -> SubtitleStyleScheme:
    fill = _SINGER_FILL_PALETTE[singer_id % len(_SINGER_FILL_PALETTE)]
    ruby = _SINGER_RUBY_PALETTE[singer_id % len(_SINGER_RUBY_PALETTE)]
    return SubtitleStyleScheme(
        font_family=style.font_family,
        font_size_px=style.font_size_px,
        font_weight=style.font_weight,
        italic=style.italic,
        base_color=style.base_color,
        fill_color=fill,
        stroke_color=style.stroke_color,
        stroke_width_px=style.stroke_width_px,
        shadow_color=style.shadow_color,
        shadow_offset_x=style.shadow_offset_x,
        shadow_offset_y=style.shadow_offset_y,
        ruby_font_size_px=style.ruby_font_size_px,
        ruby_color=ruby,
        ruby_gap_px=style.ruby_gap_px,
    )


def _scheme_from_current(panel: PropertyPanel) -> SubtitleStyleScheme:
    return SubtitleStyleScheme(
        font_family=str(panel._scheme_value("font_family")),
        font_size_px=int(panel._scheme_value("font_size_px")),
        font_weight=int(panel._scheme_value("font_weight")),
        italic=bool(panel._scheme_value("italic")),
        base_color=str(panel._scheme_value("base_color")),
        fill_color=str(panel._scheme_value("fill_color")),
        stroke_color=str(panel._scheme_value("stroke_color")),
        stroke_width_px=int(panel._scheme_value("stroke_width_px")),
        shadow_color=str(panel._scheme_value("shadow_color")),
        shadow_offset_x=int(panel._scheme_value("shadow_offset_x")),
        shadow_offset_y=int(panel._scheme_value("shadow_offset_y")),
        ruby_font_size_px=int(panel._scheme_value("ruby_font_size_px")),
        ruby_color=str(panel._scheme_value("ruby_color")),
        ruby_gap_px=int(panel._scheme_value("ruby_gap_px")),
    )


def _spin(minimum: int, maximum: int, *, suffix: str = "") -> QSpinBox:
    spin = _WheelFocusedSpinBox()
    spin.setRange(minimum, maximum)
    spin.setSuffix(suffix)
    spin.setButtonSymbols(QSpinBox.ButtonSymbols.UpDownArrows)
    _compact_control(spin)
    return spin


def _compact_control(widget: QWidget) -> None:
    widget.setMinimumWidth(0)
    widget.setFixedHeight(32)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


def _scroll_page() -> tuple[QScrollArea, QVBoxLayout]:
    scroll = QScrollArea()
    scroll.setObjectName("SubtitlePropertyScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    themed(
        scroll,
        lambda: (
            """
            QScrollArea#SubtitlePropertyScroll {
                background: transparent;
                border: 0;
            }
            QScrollArea#SubtitlePropertyScroll > QWidget > QWidget {
                background: transparent;
            }
            """
        ),
    )

    page = QWidget()
    page.setObjectName("SubtitlePropertyPage")
    page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    themed(page, lambda: "#SubtitlePropertyPage { background: transparent; }")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(10, 10, 10, 12)
    layout.setSpacing(10)
    scroll.setWidget(page)
    return scroll, layout


def _field(label_text: str, control: QWidget) -> QWidget:
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
            QFrame#SubtitlePropertySection QWidget {{
                background: transparent;
            }}
            QFrame#SubtitlePropertySection QComboBox,
            QFrame#SubtitlePropertySection QFontComboBox,
            QFrame#SubtitlePropertySection QSpinBox {{
                background: {palette().card_bg};
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
