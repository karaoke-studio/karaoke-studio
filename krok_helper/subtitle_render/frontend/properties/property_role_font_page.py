"""Construction of one role font-settings page."""

from __future__ import annotations

from typing import Any, Callable, Optional

from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CheckBox

from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    WheelFocusedComboBox,
    WheelFocusedFontComboBox,
)
from krok_helper.subtitle_render.frontend.properties.controls.layout import (
    compact_property_control,
    property_field,
)
from krok_helper.subtitle_render.frontend.properties.property_timing_page import timing_spin


FONT_SIZE_MAX_PX = 4096


class RoleFontSettingsPageBuilder:
    """Build font-slot controls while all state transitions stay on the host."""

    def __init__(
        self,
        host: Any,
        *,
        spin_factory: Callable[..., Any] = timing_spin,
        combo_factory: Callable[..., Any] = WheelFocusedComboBox,
        font_combo_factory: Callable[..., Any] = WheelFocusedFontComboBox,
    ) -> None:
        self._host = host
        self._spin_factory = spin_factory
        self._combo_factory = combo_factory
        self._font_combo_factory = font_combo_factory

    def make_page(
        self, subject: str, script: str, parent: QWidget
    ) -> QWidget:
        host = self._host
        page = QWidget(parent)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        font_combo = self._font_combo_factory(page)
        compact_property_control(font_combo)
        inherits_script = script == "latin"
        inheritance_label: Optional[str] = None
        if (subject, script) == ("main", "latin"):
            inheritance_label = "跟随主文字日文（0）"
        elif (subject, script) == ("ruby", "japanese"):
            inheritance_label = "跟随主文字（0）"
        elif (subject, script) == ("ruby", "latin"):
            inheritance_label = "跟随注音日文（0）"
        if inheritance_label is not None:
            font_combo.enable_inheritance(inheritance_label)
        size_spin = self._spin_factory(
            0 if inherits_script else (8 if subject == "ruby" else 12),
            FONT_SIZE_MAX_PX,
            suffix=" px",
        )
        weight_combo = self._combo_factory(page)
        compact_property_control(weight_combo)
        slot = (subject, script)
        host._font_controls[slot] = (
            font_combo,
            weight_combo,
            inheritance_label,
        )
        host._refresh_font_weight_combo(
            slot, preferred_weight=0 if inheritance_label is not None else 400
        )
        font_combo.currentFontChanged.connect(
            lambda font, current_slot=slot: host._on_font_family_changed(
                current_slot, font
            )
        )

        if (subject, script) == ("main", "japanese"):
            host._font_combo = font_combo
            host._font_size_spin = size_spin
            host._font_weight_combo = weight_combo
            size_spin.valueChanged.connect(
                lambda value: host._update_style(font_size_px=value)
            )
            weight_combo.currentIndexChanged.connect(
                lambda _index: host._update_style(
                    font_weight=int(weight_combo.currentData())
                )
            )
        elif (subject, script) == ("main", "latin"):
            host._font_latin_combo = font_combo
            host._font_latin_size_spin = size_spin
            host._font_latin_weight_combo = weight_combo
            size_spin.valueChanged.connect(
                lambda value: host._update_style(
                    latin_font_size_px=None if value == 0 else value
                )
            )
            weight_combo.currentIndexChanged.connect(
                lambda _index: host._update_style(
                    latin_font_weight=(
                        None
                        if int(weight_combo.currentData()) == 0
                        else int(weight_combo.currentData())
                    )
                )
            )
        elif (subject, script) == ("ruby", "japanese"):
            host._ruby_font_combo = font_combo
            host._ruby_font_size_spin = size_spin
            host._ruby_font_weight_combo = weight_combo
            size_spin.valueChanged.connect(
                lambda value: host._update_ruby_font_override(
                    ruby_font_size_px=value
                )
            )
            weight_combo.currentIndexChanged.connect(
                lambda _index: host._update_ruby_font_override(
                    ruby_font_weight=(
                        None
                        if int(weight_combo.currentData()) == 0
                        else int(weight_combo.currentData())
                    )
                )
            )
        else:
            host._ruby_font_latin_combo = font_combo
            host._ruby_font_latin_size_spin = size_spin
            host._ruby_font_latin_weight_combo = weight_combo
            size_spin.valueChanged.connect(
                lambda value: host._update_ruby_font_override(
                    ruby_latin_font_size_px=None if value == 0 else value
                )
            )
            weight_combo.currentIndexChanged.connect(
                lambda _index: host._update_ruby_font_override(
                    ruby_latin_font_weight=(
                        None
                        if int(weight_combo.currentData()) == 0
                        else int(weight_combo.currentData())
                    )
                )
            )

        stroke_fields = {
            ("main", "japanese"): (
                "stroke_width_px", "stroke2_enabled", "stroke2_width_px"
            ),
            ("main", "latin"): (
                "latin_stroke_width_px",
                "latin_stroke2_enabled",
                "latin_stroke2_width_px",
            ),
            ("ruby", "japanese"): (
                "ruby_stroke_width_px",
                "ruby_stroke2_enabled",
                "ruby_stroke2_width_px",
            ),
            ("ruby", "latin"): (
                "ruby_latin_stroke_width_px",
                "ruby_latin_stroke2_enabled",
                "ruby_latin_stroke2_width_px",
            ),
        }[(subject, script)]
        stroke_width_field, stroke2_enabled_field, stroke2_width_field = stroke_fields
        stroke_width_spin = self._spin_factory(0, 120, suffix=" px")
        stroke2_enabled_check = CheckBox("", page)
        stroke2_enabled_check.setToolTip("启用或关闭描边 2")
        inherits_stroke2 = inherits_script or subject == "ruby"
        if inherits_stroke2:
            stroke2_enabled_check.setTristate(True)
            stroke2_enabled_check.setToolTip("半选表示跟随上一级字体槽（0）")
        stroke2_enabled_check.setFixedWidth(28)
        stroke2_enabled_check.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        stroke2_width_spin = self._spin_factory(0, 120, suffix=" px")
        stroke_width_spin.valueChanged.connect(
            lambda value, field=stroke_width_field, inherit=inherits_script:
            host._update_style(**{field: None if inherit and value == 0 else value})
        )
        if inherits_stroke2:
            stroke2_enabled_check.stateChanged.connect(
                lambda state, field=stroke2_enabled_field, spin=stroke2_width_spin:
                host._on_font_stroke2_state_changed(field, spin, state)
            )
        else:
            stroke2_enabled_check.toggled.connect(
                lambda checked, field=stroke2_enabled_field, spin=stroke2_width_spin:
                host._on_font_stroke2_toggled(field, spin, checked)
            )
        stroke2_width_spin.valueChanged.connect(
            lambda value, field=stroke2_width_field, inherit=inherits_script:
            host._update_style(**{field: None if inherit and value == 0 else value})
        )
        host._font_stroke_controls[(subject, script)] = (
            stroke_width_spin,
            stroke2_enabled_check,
            stroke2_width_spin,
        )
        attr_prefix = {
            ("main", "japanese"): "",
            ("main", "latin"): "latin_",
            ("ruby", "japanese"): "ruby_",
            ("ruby", "latin"): "ruby_latin_",
        }[(subject, script)]
        setattr(host, f"_{attr_prefix}stroke_width_spin", stroke_width_spin)
        setattr(host, f"_{attr_prefix}stroke2_enabled_check", stroke2_enabled_check)
        setattr(host, f"_{attr_prefix}stroke2_width_spin", stroke2_width_spin)

        layout.addWidget(property_field("字体", font_combo))
        row = QWidget(page)
        row_layout = QGridLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setHorizontalSpacing(8)
        row_layout.addWidget(property_field("字号", size_spin), 0, 0)
        row_layout.addWidget(property_field("字重", weight_combo), 0, 1)
        row_layout.setColumnStretch(0, 1)
        row_layout.setColumnStretch(1, 1)
        layout.addWidget(row)

        # 英数字号本来就有"0 = 跟随上一级"的语义，但它藏在一个数字里，改了日文
        # 字号还得记得回来把英数也改一遍。这里把它摆成一个勾选框，且默认勾上。
        follow_label = {
            ("main", "latin"): "字号跟随主文字日文",
            ("ruby", "latin"): "字号跟随注音日文",
        }.get((subject, script))
        if follow_label is not None:
            follow_check = CheckBox(follow_label, page)
            follow_check.setChecked(True)
            follow_check.setToolTip("勾选后，改日文字号时英数字号跟着一起变。")
            follow_check.toggled.connect(
                lambda checked, current_slot=slot:
                host._on_font_size_follow_toggled(current_slot, checked)
            )
            host._font_size_follow_checks[slot] = follow_check
            layout.addWidget(follow_check)

        stroke_row = QWidget(page)
        stroke_layout = QGridLayout(stroke_row)
        stroke_layout.setContentsMargins(0, 0, 0, 0)
        stroke_layout.setHorizontalSpacing(8)
        stroke_width_widget = property_field("描边宽度", stroke_width_spin)
        stroke2_control = QWidget(stroke_row)
        stroke2_control_layout = QHBoxLayout(stroke2_control)
        stroke2_control_layout.setContentsMargins(0, 0, 0, 0)
        stroke2_control_layout.setSpacing(2)
        stroke2_control_layout.addWidget(stroke2_enabled_check, 0)
        stroke2_control_layout.addWidget(stroke2_width_spin, 1)
        stroke2_widget = property_field("描边 2", stroke2_control)
        setattr(host, f"_{attr_prefix}stroke_width_field", stroke_width_widget)
        setattr(host, f"_{attr_prefix}stroke2_field", stroke2_widget)
        # 兼容现有内部引用；开关与宽度现在属于同一个组合字段。
        setattr(host, f"_{attr_prefix}stroke2_enabled_field", stroke2_widget)
        setattr(host, f"_{attr_prefix}stroke2_width_field", stroke2_widget)
        stroke_layout.addWidget(stroke_width_widget, 0, 0)
        stroke_layout.addWidget(stroke2_widget, 0, 1)
        for column in range(2):
            stroke_layout.setColumnStretch(column, 1)
        layout.addWidget(stroke_row)
        return page

