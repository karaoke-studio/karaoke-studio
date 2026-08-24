"""Layout-property page construction isolated from the property panel host."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtWidgets import QFrame

from krok_helper.subtitle_render.frontend.property_inputs import WheelFocusedComboBox
from krok_helper.subtitle_render.frontend.property_layout import (
    ResponsiveFieldGrid,
    compact_property_control,
    property_section,
)
from krok_helper.subtitle_render.frontend.property_timing_page import timing_spin


VIEWPORT_ALIGNMENT_OPTIONS = (
    ("左上", "top_left"),
    ("中上", "top_center"),
    ("右上", "top_right"),
    ("左中", "center_left"),
    ("居中", "center"),
    ("右中", "center_right"),
    ("左下", "bottom_left"),
    ("中下", "bottom_center"),
    ("右下", "bottom_right"),
)


class LayoutPropertyPageBuilder:
    """Build layout controls while mutations remain with the panel host."""

    def __init__(
        self,
        host: Any,
        *,
        spin_factory: Callable[..., Any] = timing_spin,
    ) -> None:
        self._host = host
        self._spin_factory = spin_factory

    def make_viewport_section(self) -> QFrame:
        host = self._host
        section, layout = property_section("视图")
        host._viewport_align_combo = WheelFocusedComboBox(section)
        compact_property_control(host._viewport_align_combo)
        for label, value in VIEWPORT_ALIGNMENT_OPTIONS:
            host._viewport_align_combo.addItem(label, value)
        host._viewport_align_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                viewport_align=host._viewport_align_combo.currentData()
            )
        )

        grid = ResponsiveFieldGrid(section, min_column_width=110, max_columns=5)
        grid.add_field("对齐", host._viewport_align_combo)
        self._add_spin(grid, "_viewport_x_spin", "位置 X", -4000, 4000, "viewport_offset_x")
        self._add_spin(grid, "_viewport_y_spin", "位置 Y", -4000, 4000, "viewport_offset_y")
        self._add_spin(grid, "_viewport_scale_spin", "缩放", 10, 400, "viewport_scale_pct", suffix=" %")
        self._add_spin(grid, "_viewport_rotation_spin", "旋转", -180, 180, "viewport_rotation_deg", suffix=" °")
        layout.addWidget(grid)
        return section

    def _add_spin(
        self,
        grid: ResponsiveFieldGrid,
        attribute: str,
        label: str,
        minimum: int,
        maximum: int,
        model_field: str,
        *,
        suffix: str = "",
    ) -> None:
        host = self._host
        spin = self._spin_factory(minimum, maximum, suffix=suffix)
        setattr(host, attribute, spin)
        spin.valueChanged.connect(
            lambda value, field=model_field: host._update_style(**{field: value})
        )
        grid.add_field(label, spin)
