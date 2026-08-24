"""Background-property page construction isolated from the property panel host."""

from __future__ import annotations

from typing import Any, Callable, Iterable

from PyQt6.QtWidgets import QFrame, QGridLayout
from qfluentwidgets import CaptionLabel, ComboBox as FluentComboBox

from krok_helper.subtitle_render.frontend.property_inputs import NoWheelSpinBox
from krok_helper.subtitle_render.frontend.property_layout import (
    property_field,
    property_section,
)
from krok_helper.subtitle_render.frontend.theme import palette, themed
from krok_helper.subtitle_render.screen_settings import SCREEN_FPS_OPTIONS


class BackgroundPropertyPageBuilder:
    """Build background controls while state and file actions remain with the host."""

    def __init__(
        self,
        host: Any,
        *,
        fps_options: Iterable[int] = SCREEN_FPS_OPTIONS,
        size_spin_factory: Callable[..., Any] = NoWheelSpinBox,
        fps_combo_factory: Callable[..., Any] = FluentComboBox,
    ) -> None:
        self._host = host
        self._fps_options = tuple(int(fps) for fps in fps_options)
        self._size_spin_factory = size_spin_factory
        self._fps_combo_factory = fps_combo_factory

    def make_screen_size_section(self) -> QFrame:
        host = self._host
        section, layout = property_section("画面尺寸")
        hint = CaptionLabel("宽度 / 高度 / 帧率与预览页「画面」及导出页设置双向联动。")
        hint.setWordWrap(True)
        themed(hint, lambda: f"color: {palette().text_secondary};")
        layout.addWidget(hint)

        host._screen_size_width_spin = self._size_spin_factory(section)
        host._screen_size_width_spin.setRange(160, 7680)
        host._screen_size_width_spin.setValue(1920)
        host._screen_size_width_spin.setSingleStep(2)
        host._screen_size_width_spin.setKeyboardTracking(False)

        host._screen_size_height_spin = self._size_spin_factory(section)
        host._screen_size_height_spin.setRange(90, 4320)
        host._screen_size_height_spin.setValue(1080)
        host._screen_size_height_spin.setSingleStep(2)
        host._screen_size_height_spin.setKeyboardTracking(False)

        host._screen_size_fps_combo = self._fps_combo_factory(section)
        for fps in self._fps_options:
            host._screen_size_fps_combo.addItem(f"{fps} fps", userData=fps)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        grid.addWidget(property_field("宽度", host._screen_size_width_spin), 0, 0)
        grid.addWidget(property_field("高度", host._screen_size_height_spin), 0, 1)
        grid.addWidget(property_field("帧率", host._screen_size_fps_combo), 1, 0)
        layout.addLayout(grid)

        host._screen_size_width_spin.valueChanged.connect(
            host._on_panel_screen_size_changed
        )
        host._screen_size_height_spin.valueChanged.connect(
            host._on_panel_screen_size_changed
        )
        host._screen_size_fps_combo.currentIndexChanged.connect(
            host._on_panel_screen_size_changed
        )
        return section
