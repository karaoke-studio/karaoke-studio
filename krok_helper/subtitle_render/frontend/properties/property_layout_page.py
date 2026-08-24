"""Layout-property page construction isolated from the property panel host."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CheckBox

from krok_helper.subtitle_render.frontend.properties.property_inputs import WheelFocusedComboBox
from krok_helper.subtitle_render.frontend.properties.property_layout import (
    ResponsiveFieldGrid,
    compact_property_control,
    inline_property_section,
    property_field,
    property_section,
)
from krok_helper.subtitle_render.frontend.properties.property_timing_page import timing_spin
from krok_helper.subtitle_render.frontend.theme import palette, themed


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
POSITION_SEGMENT_OPTIONS = (
    ("top", "pos_top", "顶部"),
    ("center", "pos_middle", "居中"),
    ("bottom", "pos_bottom", "底部"),
)


class LayoutPropertyPageBuilder:
    """Build layout controls while mutations remain with the panel host."""

    def __init__(
        self,
        host: Any,
        *,
        spin_factory: Callable[..., Any] = timing_spin,
        plain_card_factory: Callable[..., Any] | None = None,
        glyph_segment_factory: Callable[..., Any] | None = None,
        layout_schematic_factory: Callable[..., Any] | None = None,
        schematic_board_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._host = host
        self._spin_factory = spin_factory
        self._plain_card_factory = plain_card_factory
        self._glyph_segment_factory = glyph_segment_factory
        self._layout_schematic_factory = layout_schematic_factory
        self._schematic_board_factory = schematic_board_factory

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

    def make_ruby_section(
        self,
        parent: QWidget | None = None,
        *,
        inline: bool = False,
    ) -> QWidget:
        host = self._host
        section, layout = (
            inline_property_section("注音", parent)
            if inline
            else property_section("注音")
        )
        grid = ResponsiveFieldGrid(section, min_column_width=90, max_columns=3)

        host._ruby_gap_spin = self._spin_factory(
            -LAYOUT_SIZE_MAX_PX,
            LAYOUT_SIZE_MAX_PX,
            suffix=" px",
        )
        host._ruby_gap_spin.valueChanged.connect(
            lambda value: host._update_layout_field(ruby_gap_px=value)
        )
        grid.add_field("与正文间距", host._ruby_gap_spin)

        host._ruby_interval_spin = self._spin_factory(
            -LAYOUT_SIZE_MAX_PX,
            LAYOUT_SIZE_MAX_PX,
            suffix=" px",
        )
        host._ruby_interval_spin.setToolTip(
            "注音字符之间的最小间距（N3 ルビ間隔），可为负让注音字符收紧。\n"
            "注意这是「下限」：注音比正文窄、均等分布摊出的间距大于此值时，"
            "调整它看不到变化；对超出正文宽度的长注音效果最明显。"
        )
        host._ruby_interval_spin.valueChanged.connect(
            lambda value: host._update_layout_field(ruby_interval_px=value)
        )
        grid.add_field("字间距", host._ruby_interval_spin)

        host._ruby_alignment_combo = WheelFocusedComboBox(section)
        compact_property_control(host._ruby_alignment_combo)
        for label, value in (
            ("自动", "auto"),
            ("居中", "center"),
            ("均等分布", "equal_space"),
        ):
            host._ruby_alignment_combo.addItem(label, value)
        host._ruby_alignment_combo.setToolTip(
            "注音相对正文范围的排布（N3 ルビ配置）：自动 = 正文或注音全为英数时居中、"
            "否则均等分布。"
        )
        host._ruby_alignment_combo.currentIndexChanged.connect(
            lambda _index: host._update_layout_field(
                ruby_alignment=host._ruby_alignment_combo.currentData()
            )
        )
        grid.add_field("排布", host._ruby_alignment_combo)
        layout.addWidget(grid)
        return section

    def make_row_structure_section(self) -> QFrame:
        host = self._host
        if any(
            factory is None
            for factory in (
                self._plain_card_factory,
                self._glyph_segment_factory,
                self._layout_schematic_factory,
                self._schematic_board_factory,
            )
        ):
            raise RuntimeError("row-structure widget factories are required")

        section, layout = self._plain_card_factory()
        host._layout_section = section
        navigation = host._make_layout_navigation(section)
        assignment_actions = host._make_layout_assignment_actions(section)

        host._line_position_seg = self._glyph_segment_factory(
            POSITION_SEGMENT_OPTIONS,
            section,
        )
        host._line_position_seg.setValue("bottom")
        host._line_position_seg.valueChanged.connect(host._on_line_position_changed)
        host._line_position_field = property_field(
            "上下配置",
            host._line_position_seg,
        )

        host._horizontal_margin_spin = self._spin_factory(
            -LAYOUT_SIZE_MAX_PX,
            LAYOUT_SIZE_MAX_PX,
            suffix=" px",
        )
        host._horizontal_margin_spin.setFixedWidth(120)
        host._horizontal_margin_spin.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        host._horizontal_margin_spin.setToolTip(
            "左右余白（N3 左右余白）：左对齐行的左缘贴此值，右对齐行的右缘贴"
            "「画面宽 − 此值」。"
        )
        host._horizontal_margin_spin.valueChanged.connect(
            host._on_horizontal_margin_changed
        )
        host._horizontal_margin_field = property_field(
            "左右余白",
            host._horizontal_margin_spin,
        )

        host._smart_horizontal_field = host._make_smart_horizontal_field(section)
        host._left_layout_controls = QWidget(section)
        host._left_layout_controls.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        left_controls_layout = QVBoxLayout(host._left_layout_controls)
        left_controls_layout.setContentsMargins(0, 0, 0, 0)
        left_controls_layout.setSpacing(8)
        left_controls_layout.addWidget(
            host._smart_horizontal_field,
            0,
            Qt.AlignmentFlag.AlignLeft,
        )
        left_controls_layout.addWidget(
            host._horizontal_margin_field,
            0,
            Qt.AlignmentFlag.AlignRight,
        )
        host._character_layout_group = host._make_character_layout_group(section)

        host._allow_biting_check = CheckBox("启用文字咬合", section)
        host._allow_biting_check.setToolTip(
            "允许斜体和部分标点使用负字形边距，效果更接近 NicokaraMaker3。"
        )
        host._allow_biting_check.toggled.connect(
            lambda checked: host._update_layout_field(allow_biting=checked)
        )

        host._layout_schematic = self._layout_schematic_factory(section)
        host._layout_schematic.setFixedWidth(round(150 * 16 / 9))
        host._line_margin_spin = self._spin_factory(
            -LAYOUT_SIZE_MAX_PX,
            LAYOUT_SIZE_MAX_PX,
            suffix=" px",
        )
        host._line_margin_spin.setFixedWidth(120)
        host._line_margin_spin.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        host._line_margin_spin.setToolTip(
            "顶部锚定 = 画面上端到最上行的余白；底部锚定 = 画面下端到最下行的"
            "余白；居中时忽略（N3 上/下余白）。"
        )
        host._line_margin_spin.valueChanged.connect(
            lambda value: host._update_layout_field(line_y_margin_px=value)
        )

        host._vertical_margin_field = QWidget(section)
        retain_policy = host._vertical_margin_field.sizePolicy()
        retain_policy.setRetainSizeWhenHidden(True)
        host._vertical_margin_field.setSizePolicy(retain_policy)
        vertical_margin_layout = QHBoxLayout(host._vertical_margin_field)
        vertical_margin_layout.setContentsMargins(0, 0, 0, 0)
        vertical_margin_layout.setSpacing(8)
        host._vertical_margin_label = QLabel(
            "下余白",
            host._vertical_margin_field,
        )
        themed(
            host._vertical_margin_label,
            lambda: f"color: {palette().text_secondary}; font-size: 9pt;",
        )
        vertical_margin_layout.addWidget(host._vertical_margin_label)
        vertical_margin_layout.addWidget(host._line_margin_spin)

        host._schematic_board = self._schematic_board_factory(
            QWidget(section),
            host._layout_schematic,
            host._vertical_margin_field,
            host._make_line_alignments_box(section),
            section,
            header_left=navigation,
            header_right=assignment_actions,
            top_left=host._left_layout_controls,
            top_center=host._line_position_field,
            bottom_left=host._character_layout_group,
            bottom_right=host._allow_biting_check,
        )
        layout.addWidget(host._schematic_board)
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
