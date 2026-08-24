"""Timing-property page construction isolated from the property panel host."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout
from qfluentwidgets import CheckBox

from krok_helper.qfluent_compat import install_fluent_tooltip
from krok_helper.subtitle_render.frontend.property_inputs import (
    WheelFocusedComboBox,
    WheelFocusedSpinBox,
)
from krok_helper.subtitle_render.frontend.property_layout import (
    ResponsiveFieldGrid,
    compact_property_control,
    property_section,
)


def timing_spin(
    minimum: int,
    maximum: int,
    *,
    suffix: str = "",
) -> WheelFocusedSpinBox:
    """Create the compact integer input used by timing properties."""
    spin = WheelFocusedSpinBox()
    spin.setRange(minimum, maximum)
    spin.setSuffix(suffix)
    spin.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    compact_property_control(spin)
    spin._sync_text_minimum()
    return spin


class TimingPropertyPageBuilder:
    """Build timing controls while leaving style transitions with the host."""

    def __init__(
        self,
        host: Any,
        *,
        spin_factory: Callable[..., Any] = timing_spin,
        tooltip_installer: Callable[..., None] = install_fluent_tooltip,
    ) -> None:
        self._host = host
        self._spin_factory = spin_factory
        self._tooltip_installer = tooltip_installer

    def make_section(self) -> QFrame:
        host = self._host
        section, layout = property_section("时间")
        grid = ResponsiveFieldGrid(section, min_column_width=130, max_columns=4)

        host._line_lead_spin = self._add_spin(
            grid,
            "提前入场",
            0,
            10_000,
            "line_lead_in_ms",
        )
        host._line_tail_spin = self._add_spin(
            grid,
            "延迟退场",
            0,
            10_000,
            "line_tail_ms",
        )
        host._line_offset_spin = self._add_spin(
            grid,
            "偏移",
            -10_000,
            10_000,
            "timing_offset_ms",
        )

        host._section_gap_spin = self._spin_factory(0, 60_000, suffix=" ms")
        host._section_gap_spin.valueChanged.connect(
            lambda value: host._update_style(section_gap_ms=value)
        )
        # This source-loading option remains available for state synchronization,
        # but its visible editor lives in the lyrics-list toolbar settings.
        host._section_gap_spin.setVisible(False)

        host._section_ending_combo = WheelFocusedComboBox(section)
        compact_property_control(host._section_ending_combo)
        for label, value in (("保持", "hold"), ("段末清屏", "clear")):
            host._section_ending_combo.addItem(label, value)
        host._section_ending_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                section_ending_mode=host._section_ending_combo.currentData()
            )
        )
        grid.add_field("段落结束", host._section_ending_combo)

        host._lane_gap_spin = self._add_spin(
            grid,
            "同轨间隔",
            0,
            5_000,
            "line_lane_gap_ms",
        )
        host._lane_gap_spin.setToolTip("同一显示轨上相邻两句之间保留的时间间隔。")
        layout.addWidget(grid)

        sync_row = QHBoxLayout()
        sync_row.setContentsMargins(0, 0, 0, 0)
        host._sync_entry_check = CheckBox("同步入场", section)
        host._sync_entry_check.setToolTip(
            "未手工调整上屏时间的 T 会尽量延长到同步页的最早边界；默认只处理段首页，\n"
            "开启“每句同步”后处理每一页。"
            "发生像素碰撞时，各个 T 独立按先压缩前句退场、再压缩自己入场的"
            "顺序处理；不会改动未参与该次碰撞的页内兄弟行。"
        )
        host._sync_entry_check.toggled.connect(
            lambda checked: host._update_style(sync_entry=checked)
        )
        host._sync_entry_check.toggled.connect(
            lambda _checked: host._sync_sync_each_page_enabled()
        )
        sync_row.addWidget(host._sync_entry_check)

        host._sync_ending_check = CheckBox("同步退场", section)
        host._sync_ending_check.setToolTip(
            "未手工调整消失时间的 T 会尽量延长到同步页的最晚边界；默认只处理段尾页，\n"
            "开启“每句同步”后处理每一页。"
            "发生像素碰撞时，各个 T 独立按先压缩前句退场、再压缩后句入场的"
            "顺序处理；不会改动未参与该次碰撞的页内兄弟行。"
        )
        host._sync_ending_check.toggled.connect(
            lambda checked: host._update_style(sync_ending=checked)
        )
        host._sync_ending_check.toggled.connect(
            lambda _checked: host._sync_sync_each_page_enabled()
        )
        sync_row.addWidget(host._sync_ending_check)

        host._sync_each_page_check = CheckBox("每句同步", section)
        host._sync_each_page_check.setToolTip(
            "关闭时，同步入场只作用于每段第一页，同步退场只作用于每段最后一页；\n"
            "开启时，每一页都会分别执行同步入场和同步退场。"
        )
        host._sync_each_page_check.toggled.connect(
            lambda checked: host._update_style(sync_each_page=checked)
        )
        host._sync_each_page_check.setEnabled(False)
        sync_row.addWidget(host._sync_each_page_check)
        sync_row.addStretch(1)
        layout.addLayout(sync_row)

        host._ruby_main_reading_units_check = CheckBox(
            "正文按注音字符切分（N3 式）",
            section,
        )
        host._ruby_main_reading_units_check.setToolTip(
            "正文内部已有时间点时，两种模式都会保留正文逐字时钟；"
            "缺失时，开启按注音可视字符数映射，"
            "关闭按注音内部时间点形成的时间段数均分正文。"
        )
        host._ruby_main_reading_units_check.toggled.connect(
            lambda checked: host._update_style(
                ruby_main_progress_mode=(
                    "reading_units" if checked else "checkpoint_segments"
                )
            )
        )
        host._n3_style_row = QHBoxLayout()
        host._n3_style_row.setContentsMargins(0, 0, 0, 0)
        host._n3_style_row.addWidget(host._ruby_main_reading_units_check)

        host._allow_animation_overlap_check = CheckBox(
            "允许出入场动画重叠",
            section,
        )
        host._allow_animation_overlap_check.setToolTip(
            "开启时，同轨间隔只约束主文字的稳定显示段，入场和退场动画可以互相重叠；\n"
            "关闭时，完整的入场、稳定显示和退场窗口都必须满足同轨间隔。"
        )
        host._allow_animation_overlap_check.toggled.connect(
            lambda checked: host._update_style(
                allow_entry_exit_animation_overlap=checked
            )
        )
        host._n3_style_row.addWidget(host._allow_animation_overlap_check)

        host._auto_fill_section_time_check = CheckBox(
            "自动填充段内时间",
            section,
        )
        host._auto_fill_section_time_check.setToolTip(
            "开启时，非段尾页的每句按主文字行盒高度匹配下一页最近的行，并延长到"
            "该行入场前的同轨间隔；段尾页填充到本页自然结束。\n"
            "关闭时，每句仅保留自己的退场窗口。"
        )
        host._auto_fill_section_time_check.toggled.connect(
            lambda checked: host._update_style(auto_fill_section_time=checked)
        )
        host._n3_style_row.addWidget(host._auto_fill_section_time_check)

        for tooltip_button in (
            host._sync_entry_check,
            host._sync_ending_check,
            host._sync_each_page_check,
            host._ruby_main_reading_units_check,
            host._allow_animation_overlap_check,
            host._auto_fill_section_time_check,
        ):
            self._tooltip_installer(tooltip_button, show_delay=300)
        host._n3_style_row.addStretch(1)
        layout.addLayout(host._n3_style_row)
        return section

    def _add_spin(
        self,
        grid: ResponsiveFieldGrid,
        label: str,
        minimum: int,
        maximum: int,
        model_field: str,
    ) -> Any:
        spin = self._spin_factory(minimum, maximum, suffix=" ms")
        spin.valueChanged.connect(
            lambda value, field=model_field: self._host._update_style(
                **{field: value}
            )
        )
        grid.add_field(label, spin)
        return spin
