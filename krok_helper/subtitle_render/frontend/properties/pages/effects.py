"""Effects-property page construction isolated from the property panel host."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CheckBox

from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    WheelFocusedComboBox,
)
from krok_helper.subtitle_render.frontend.properties.controls.layout import (
    ResponsiveFieldGrid,
    compact_property_control,
    property_field,
    property_section,
)
from krok_helper.subtitle_render.frontend.properties.pages.timing import timing_spin
from krok_helper.subtitle_render.frontend.properties.controls.widgets import SubGroup


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

    def make_lit_section(self) -> QFrame:
        host = self._host
        section, layout = property_section("指示灯", switch=True)
        host._lit_section = section
        host._lit_enabled_switch = section.header_switch
        host._lit_enabled_switch.toggled.connect(
            lambda checked: host._update_style(lit_enabled=checked)
        )
        host._lit_volume_groups = []
        host._lit_shape_groups = []
        host._lit_group_grids = {}

        def group(
            title: str,
            category: str | None,
            *,
            collapsed: bool = False,
            min_column_width: int = 135,
            max_columns: int = 4,
        ):
            box = SubGroup(title, collapsed=collapsed, parent=section)
            layout.addWidget(box)
            if category == "volume":
                host._lit_volume_groups.append(box)
            elif category == "shape":
                host._lit_shape_groups.append(box)
            fields = ResponsiveFieldGrid(
                box,
                min_column_width=min_column_width,
                max_columns=max_columns,
            )
            box.grid.addWidget(fields, 0, 0, 1, 2)
            host._lit_group_grids[title] = fields

            def add(label: str | None, control: QWidget) -> None:
                fields.add_widget(
                    property_field(label, control) if label is not None else control
                )

            return add

        host._lit_style_combo = self._combo(
            section,
            (
                ("音量柱", "volume"),
                ("圆形", "circle"),
                ("方形", "square"),
                ("圆角", "rounded"),
            ),
            "lit_style",
        )
        add = group("通用", None, min_column_width=130, max_columns=5)
        add("样式", host._lit_style_combo)
        self._add_spin(add, "_lit_duration_spin", "持续", 0, 60_000, "signals_duration_ms", suffix=" ms")
        self._add_spin(add, "_lit_waiting_time_spin", "等待", 0, 60_000, "lit_waiting_time_ms", suffix=" ms")
        self._add_spin(add, "_lit_stroke_width_spin", "描边宽度", 0, 40, "lit_stroke_width", suffix=" px")
        self._add_spin(add, "_lit_opacity_spin", "透明度", 0, 100, "lit_opacity_pct", suffix=" %")

        add = group("音量柱 · 布局", "volume", max_columns=4)
        self._add_spin(add, "_volume_size_spin", "整体大小", 4, 240, "volume_size", suffix=" px")
        self._add_spin(add, "_volume_column_width_spin", "柱条宽度", 1, 120, "volume_column_width", suffix=" px")
        self._add_spin(add, "_volume_column_count_spin", "柱条数量", 1, 16, "volume_column_count")
        self._add_spin(add, "_volume_column_spacing_spin", "柱条间距", 0, 120, "volume_column_spacing", suffix=" px")
        self._add_spin(add, "_volume_ratio_spin", "前后比率", 1, 20, "volume_ratio", transform=float)
        host._volume_align_combo = self._combo(
            section,
            (("顶部", 0), ("居中", 1), ("底部", 2)),
            "volume_align",
            transform=int,
        )
        add("柱条对齐", host._volume_align_combo)
        self._add_spin(add, "_volume_x_spin", "X", -4000, 4000, "volume_offset_x")
        self._add_spin(add, "_volume_y_spin", "Y", -4000, 4000, "volume_offset_y")

        add = group("音量柱 · 动画", "volume", collapsed=True, max_columns=3)
        self._add_spin(add, "_volume_flash_times_spin", "闪烁次数", 1, 20, "volume_flash_times")
        self._add_spin(
            add,
            "_volume_flash_duration_spin",
            "闪烁占比",
            0,
            100,
            "volume_flash_duration_ratio",
            suffix=" %",
            transform=lambda value: value / 100.0,
        )
        self._add_spin(add, "_volume_transition_ratio_spin", "覆盖过渡", 0, 100, "volume_transition_ratio_pct", suffix=" %")

        add = group("音量柱 · 颜色", "volume", max_columns=4)
        self._add_color(add, "_volume_fill_btn", "柱填充色", "volume_fill_color")
        self._add_color(add, "_volume_stroke_btn", "柱描边色", "volume_stroke_color")
        self._add_color(add, "_volume_overlay_fill_btn", "覆盖填充色", "volume_overlay_fill_color")
        self._add_color(add, "_volume_overlay_stroke_btn", "覆盖描边色", "volume_overlay_stroke_color")

        add = group("形状灯 · 布局", "shape", max_columns=5)
        self._add_spin(add, "_lit_number_spin", "数量", 1, 8, "lit_number")
        self._add_spin(add, "_lit_size_spin", "大小", 4, 160, "lit_size", suffix=" px")
        self._add_spin(add, "_lit_tracking_spin", "间距", 0, 200, "lit_tracking", suffix=" px")
        self._add_spin(add, "_lit_x_spin", "X", -4000, 4000, "lit_offset_x")
        self._add_spin(add, "_lit_y_spin", "Y", -4000, 4000, "lit_offset_y")

        add = group("形状灯 · 外观", "shape", max_columns=5)
        self._add_color(add, "_lit_fill_btn", "填充颜色", "lit_fill_color")
        self._add_color(add, "_lit_stroke_btn", "描边颜色", "lit_stroke_color")
        self._add_spin(add, "_lit_edge_brightness_spin", "边缘亮度", 0, 100, "lit_edge_brightness_pct", suffix=" %")
        self._add_spin(add, "_lit_stroke_soften_spin", "描边柔化", 0, 40, "lit_stroke_soften", suffix=" px")
        host._lit_shadow_check = CheckBox("启用", section)
        host._lit_shadow_check.toggled.connect(
            lambda checked: host._update_style(lit_shadow=checked)
        )
        add("阴影", host._lit_shadow_check)

        add = group("形状灯 · 转场", "shape", collapsed=True, max_columns=4)
        host._lit_transition_mode_combo = self._combo(
            section,
            (("无", "none"), ("淡入淡出", "fade"), ("滑动", "slide")),
            "lit_transition_mode",
        )
        add("类型", host._lit_transition_mode_combo)
        self._add_spin(add, "_lit_transition_ratio_spin", "时长比例", 0, 100, "lit_transition_ratio_pct", suffix=" %")
        self._add_spin(add, "_lit_transition_angle_spin", "角度", -360, 360, "lit_transition_angle_deg", suffix=" °")
        self._add_spin(add, "_lit_transition_distance_spin", "距离", 0, 800, "lit_transition_distance", suffix=" px")

        host._sync_lit_style_visibility()
        section.set_expanded(False)
        return section

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
        for label, value in (
            ("无", "none"),
            ("无 Wipe", "no_wipe"),
            ("utopia", "utopia"),
        ):
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

        host._reverse_karaoke_anim_combo = WheelFocusedComboBox(section)
        compact_property_control(host._reverse_karaoke_anim_combo)
        for label, value in (
            ("跟随唱字特效", "inherit"),
            ("Wipe", "none"),
            ("无 Wipe", "no_wipe"),
            ("Utopia", "utopia"),
        ):
            host._reverse_karaoke_anim_combo.addItem(label, value)
        host._reverse_karaoke_anim_combo.setToolTip(
            "仅对标记为反向唱字的歌词行生效；无 Wipe 会在区间结束时整字瞬切"
        )
        host._reverse_karaoke_anim_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                reverse_karaoke_anim=host._reverse_karaoke_anim_combo.currentData()
            )
        )
        host._animation_grid.add_field(
            "反向唱字特效", host._reverse_karaoke_anim_combo
        )

        host._section_edge_check = CheckBox("段首尾独立动画", section)
        host._section_edge_check.toggled.connect(host._on_section_edge_toggled)
        host._section_head_anim_combo = self._animation_combo(
            section,
            ENTRY_ANIMATION_OPTIONS,
            "section_head_anim",
        )
        host._section_head_anim_combo.setToolTip(
            "段首页各行替换全局入场动画；单页段两侧都替换"
        )
        host._section_tail_anim_combo = self._animation_combo(
            section,
            EXIT_ANIMATION_OPTIONS,
            "section_tail_anim",
        )
        host._section_tail_anim_combo.setToolTip(
            "段尾页各行替换全局退场动画；单页段两侧都替换"
        )
        host._section_edge_both_check = CheckBox("同时设置出入场", section)
        host._section_edge_both_check.setToolTip(
            "开启后段首页与段尾页同时替换入场和退场动画；默认各页只替换自己一侧"
        )
        host._section_edge_both_check.toggled.connect(
            host._on_section_edge_both_toggled
        )
        host._section_edge_row = self._section_edge_block(
            section,
            host._section_edge_check,
            host._section_head_anim_combo,
            host._section_tail_anim_combo,
            host._section_edge_both_check,
        )
        host._animation_grid.add_widget(host._section_edge_row)
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

    def _combo(
        self,
        parent: QWidget,
        options: tuple[tuple[str, Any], ...],
        model_field: str,
        *,
        transform: Callable[[Any], Any] = lambda value: value,
    ) -> WheelFocusedComboBox:
        host = self._host
        combo = WheelFocusedComboBox(parent)
        compact_property_control(combo)
        for label, value in options:
            combo.addItem(label, value)
        combo.currentIndexChanged.connect(
            lambda _index, field=model_field, control=combo, convert=transform: (
                host._update_style(**{field: convert(control.currentData())})
            )
        )
        return combo

    def _add_spin(
        self,
        add: Callable[[str | None, QWidget], None],
        attribute: str,
        label: str,
        minimum: int,
        maximum: int,
        model_field: str,
        *,
        suffix: str = "",
        transform: Callable[[int], Any] = lambda value: value,
    ) -> None:
        host = self._host
        spin = self._spin_factory(minimum, maximum, suffix=suffix)
        setattr(host, attribute, spin)
        spin.valueChanged.connect(
            lambda value, field=model_field, convert=transform: host._update_style(
                **{field: convert(value)}
            )
        )
        add(label, spin)

    def _add_color(
        self,
        add: Callable[[str | None, QWidget], None],
        attribute: str,
        label: str,
        model_field: str,
    ) -> None:
        host = self._host
        button = host._color_button(model_field, getattr(host._style, model_field))
        setattr(host, attribute, button)
        add(label, button)

    @staticmethod
    def _section_edge_block(
        parent: QWidget,
        check: CheckBox,
        head_combo: WheelFocusedComboBox,
        tail_combo: WheelFocusedComboBox,
        both_check: CheckBox,
    ) -> QWidget:
        block = QWidget(parent)
        block_layout = QVBoxLayout(block)
        block_layout.setContentsMargins(0, 0, 0, 0)
        block_layout.setSpacing(4)
        row = QWidget(block)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        row_layout.addWidget(check, 0)
        row_layout.addWidget(head_combo, 1)
        row_layout.addWidget(tail_combo, 1)
        block_layout.addWidget(row)
        block_layout.addWidget(both_check)
        # 主开关关闭时子选项与两个下拉一起失效（回显时由宿主按样式同步）。
        head_combo.setEnabled(False)
        tail_combo.setEnabled(False)
        both_check.setEnabled(False)
        return block

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
