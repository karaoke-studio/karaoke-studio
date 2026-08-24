"""Effects-property page construction isolated from the property panel host."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtWidgets import QFrame, QHBoxLayout, QWidget

from krok_helper.subtitle_render.frontend.property_inputs import (
    WheelFocusedComboBox,
)
from krok_helper.subtitle_render.frontend.property_layout import (
    ResponsiveFieldGrid,
    compact_property_control,
    property_section,
)
from krok_helper.subtitle_render.frontend.property_timing_page import timing_spin


ENTRY_ANIMATION_OPTIONS = (
    ("无", "none"),
    ("淡入", "fade"),
    ("滑入", "slide_in"),
    ("上移", "rise"),
    ("逐文字渐显", "char_fade"),
    ("文字垂下", "char_drip"),
    ("旋转翻转", "spin_flip"),
    ("utopia", "utopia"),
)

EXIT_ANIMATION_OPTIONS = (
    ("无", "none"),
    ("淡出", "fade"),
    ("滑出", "slide_out"),
    ("上移", "rise"),
    ("逐文字渐隐", "char_fade"),
    ("文字垂出", "char_drip"),
    ("旋转翻转", "spin_flip"),
    ("utopia", "utopia"),
)


class EffectsPropertyPageBuilder:
    """Build effect controls while leaving style transitions with the host."""

    def __init__(
        self,
        host: Any,
        *,
        spin_factory: Callable[..., Any] = timing_spin,
    ) -> None:
        self._host = host
        self._spin_factory = spin_factory

    def make_animation_section(self) -> QFrame:
        host = self._host
        section, layout = property_section("入退场动画")
        host._animation_grid = ResponsiveFieldGrid(
            section,
            min_column_width=260,
            max_columns=2,
        )

        host._entry_anim_combo = self._animation_combo(
            section,
            ENTRY_ANIMATION_OPTIONS,
            "entry_anim",
        )
        host._entry_lead_spin = self._spin_factory(0, 3000, suffix=" ms")
        host._entry_lead_spin.valueChanged.connect(
            lambda value: host._update_style(entry_lead_ms=value)
        )
        host._entry_animation_row = self._animation_row(
            section,
            host._entry_anim_combo,
            host._entry_lead_spin,
            "入场动画时长",
        )
        host._animation_grid.add_field(
            "入场动画 / 时长",
            host._entry_animation_row,
        )

        host._exit_anim_combo = self._animation_combo(
            section,
            EXIT_ANIMATION_OPTIONS,
            "exit_anim",
        )
        host._exit_fade_spin = self._spin_factory(0, 3000, suffix=" ms")
        host._exit_fade_spin.valueChanged.connect(
            lambda value: host._update_style(exit_fade_ms=value)
        )
        host._exit_animation_row = self._animation_row(
            section,
            host._exit_anim_combo,
            host._exit_fade_spin,
            "退场动画时长",
        )
        host._animation_grid.add_field(
            "退场动画 / 时长",
            host._exit_animation_row,
        )

        host._karaoke_anim_combo = WheelFocusedComboBox(section)
        compact_property_control(host._karaoke_anim_combo)
        for label, value in (("无", "none"), ("utopia", "utopia")):
            host._karaoke_anim_combo.addItem(label, value)
        host._karaoke_anim_combo.setToolTip(
            "控制歌词正在着色时的逐字动画；旧项目的 Utopia 入退场会自动兼容"
        )
        host._karaoke_anim_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                karaoke_anim=host._karaoke_anim_combo.currentData()
            )
        )
        host._animation_grid.add_field("唱字特效", host._karaoke_anim_combo)
        layout.addWidget(host._animation_grid)
        return section

    def _animation_combo(
        self,
        parent: QWidget,
        options: tuple[tuple[str, str], ...],
        model_field: str,
    ) -> WheelFocusedComboBox:
        host = self._host
        combo = WheelFocusedComboBox(parent)
        compact_property_control(combo)
        for label, value in options:
            combo.addItem(label, value)
        combo.currentIndexChanged.connect(
            lambda _index, field=model_field, control=combo: host._update_style(
                **{field: control.currentData()}
            )
        )
        return combo

    @staticmethod
    def _animation_row(
        parent: QWidget,
        combo: WheelFocusedComboBox,
        duration_spin: Any,
        tooltip: str,
    ) -> QWidget:
        row = QWidget(parent)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        row_layout.addWidget(combo, 2)
        duration_spin.setToolTip(tooltip)
        row_layout.addWidget(duration_spin, 1)
        return row
