"""Layout-property page construction isolated from the property panel host."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QSizePolicy, QWidget
from qfluentwidgets import CheckBox

from krok_helper.subtitle_render.frontend.property_inputs import WheelFocusedComboBox
from krok_helper.subtitle_render.frontend.property_layout import (
    ResponsiveFieldGrid,
    compact_property_control,
    property_field,
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

LAYOUT_SIZE_MAX_PX = 16_384


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

    def make_vertical_section(self) -> QFrame:
        host = self._host
        section, layout = property_section("垂直与方向")
        host._line_gap_spin = self._spin_factory(
            -LAYOUT_SIZE_MAX_PX,
            LAYOUT_SIZE_MAX_PX,
            suffix=" px",
        )
        host._line_gap_spin.setFixedWidth(120)
        host._line_gap_spin.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        host._line_gap_spin.setToolTip(
            "相邻两行主文字行盒之间的间距（N3 行間），可为负让行盒重叠；"
            "不包含注音高度。"
        )
        host._line_gap_spin.valueChanged.connect(
            lambda value: host._update_layout_field(line_gap_px=value)
        )

        compact_row = QWidget(section)
        compact_layout = QHBoxLayout(compact_row)
        compact_layout.setContentsMargins(0, 0, 0, 0)
        compact_layout.setSpacing(12)
        compact_layout.addWidget(property_field("行间距", host._line_gap_spin), 0)

        host._vertical_check = CheckBox("竖排", compact_row)
        host._vertical_check.toggled.connect(
            lambda checked: host._update_style(vertical=checked)
        )
        compact_layout.addWidget(
            host._vertical_check,
            0,
            Qt.AlignmentFlag.AlignBottom,
        )
        host._rtl_check = CheckBox("从右到左", compact_row)
        host._rtl_check.toggled.connect(
            lambda checked: host._update_style(right_to_left=checked)
        )
        compact_layout.addWidget(
            host._rtl_check,
            0,
            Qt.AlignmentFlag.AlignBottom,
        )

        host._allow_inter_page_line_overlap_check = CheckBox(
            "启用行间重叠",
            compact_row,
        )
        host._allow_inter_page_line_overlap_check.setToolTip(
            "关闭时，系统按每一行不含注音、描边、阴影和发光的主文字字形"
            "像素范围检测真实跨页冲突，只缩短发生冲突的两行的提前入场和延迟"
            "退场时间，不会截断任何走字区间或改变页内上屏顺序。自动压缩可以"
            "缩短动画时段：不会把非零入场动画自动压到"
            " 250 ms 以下；若动画时长或上屏时间由用户手工设定，则保留用户值；"
            "非零退场动画自动压缩时至少保留 100 ms。是否允许入场和退场动画"
            "互相重叠，由时间设置中的“允许出入场动画重叠”单独控制。"
            "时间压缩仍无法消除冲突时，移动后进入的整页字幕。"
            "避让优先吸附到已有布局行位，再沿布局方向寻找画布"
            "内空间，跨页空隙采用被重叠页面布局的行间距；放不下时改向反方向寻找；"
            "两边都放不下则保持原布局位置和绘制优先级。页面一旦移动，会保持位置"
            "直到本页播放完毕。入场、退场和字符动画允许互相穿越，不因页面排版"
            "变化而扩大碰撞时间。开启后不执行跨页时间压缩或空间避让，允许跨页字幕"
            "直接重叠，适合需要刻意叠放的特殊效果。同一页内部的负行间距或手工重叠"
            "不受此开关影响。"
        )
        host._allow_inter_page_line_overlap_check.toggled.connect(
            lambda checked: host._update_style(
                allow_inter_page_line_overlap=checked
            )
        )
        compact_layout.addWidget(
            host._allow_inter_page_line_overlap_check,
            0,
            Qt.AlignmentFlag.AlignBottom,
        )
        compact_layout.addStretch(1)
        layout.addWidget(compact_row)
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
