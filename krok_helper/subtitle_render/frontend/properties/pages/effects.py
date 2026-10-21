"""Effects-property page construction isolated from the property panel host."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CheckBox, LineEdit as FluentLineEdit, PushButton as FluentPushButton

from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    CanvasSliderSpinBox,
    WheelFocusedComboBox,
)
from krok_helper.subtitle_render.frontend.properties.controls.layout import (
    ResponsiveControlPair,
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
    ("字距收拢", "tracking_in"),
    ("波浪上浮", "wave_in"),
    ("逐字拉伸入场", "stretch_in"),
    ("辉光浮现", "glow_in"),
    ("粒子拼接", "assemble_in"),
    ("星光闪烁", "sparkle"),
    ("涟漪光环", "ripple"),
    ("音符飘出", "note"),
    ("爱心飘出", "heart"),
    ("火焰燃起", "flame"),
    ("羽毛飘入", "feather"),
    ("闪电劈落", "lightning"),
    ("花瓣飘入", "petal"),
    ("雪花飘入·镂空", "snow"),
    ("雪花飘入·实心", "snow_solid"),
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
    ("碎散爆开", "scatter_out"),
    ("收拢消散", "converge_out"),
    ("逐字拉伸退场", "stretch_out"),
    ("辉光消散", "glow_out"),
    ("粒子消散", "dissolve_out"),
    ("星光闪烁", "sparkle"),
    ("涟漪光环", "ripple"),
    ("音符飘出", "note"),
    ("爱心飘出", "heart"),
    ("火焰烧尽", "flame"),
    ("羽毛飘散", "feather"),
    ("闪电消散", "lightning"),
    ("花瓣飘散", "petal"),
    ("雪花飘出·镂空", "snow"),
    ("雪花飘出·实心", "snow_solid"),
)

FX_PARTICLE_OPTIONS = (
    ("无", "none"),
    ("星光闪烁", "sparkle"),
    ("涟漪光环", "ripple"),
)

FX_SING_OPTIONS = (
    ("无", "none"),
    ("星光闪烁", "twinkle"),
    ("星光闪烁（旧版）", "twinkle_classic"),
    ("音符飘出", "note"),
    ("爱心飘出", "heart"),
    ("火焰燃烧", "flame"),
    ("涟漪光环", "ripple"),
    ("花瓣飘动", "petal"),
    ("羽毛飘动", "feather"),
    ("闪电闪烁", "lightning"),
    ("雪花飘动·镂空", "snow"),
    ("雪花飘动·实心", "snow_solid"),
)

FX_PARTICLE_COLOR_MODE_OPTIONS = (
    ("默认颜色", "sakura"),
    ("单独颜色", "color"),
    ("跟随字体·前后各一", "follow_mix"),
    ("跟随字体·走字前", "follow_before"),
    ("跟随字体·走字后", "follow_after"),
    ("复用配色方案", "role"),
)

FX_PARTICLE_LAYER_OPTIONS = (
    ("仅实色", "solid"),
    ("+描边", "stroke"),
    ("+装饰", "decor"),
    ("全有", "all"),
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

    def make_volume_section(self) -> QFrame:
        """Build the line-leading volume-bar module as an independent card."""
        host = self._host
        section, layout = property_section("音量柱", switch=True)
        host._volume_section = section
        host._volume_enabled_switch = section.header_switch
        host._volume_enabled_switch.toggled.connect(
            lambda checked: host._update_style(volume_enabled=checked)
        )
        host._volume_group_grids = {}

        def group(
            title: str,
            *,
            collapsed: bool = False,
            min_column_width: int = 135,
            max_columns: int = 4,
        ):
            box = SubGroup(title, collapsed=collapsed, parent=section)
            layout.addWidget(box)
            fields = ResponsiveFieldGrid(
                box,
                min_column_width=min_column_width,
                max_columns=max_columns,
            )
            box.grid.addWidget(fields, 0, 0, 1, 2)
            host._volume_group_grids[title] = fields

            def add(label: str | None, control: QWidget) -> None:
                fields.add_widget(property_field(label, control) if label is not None else control)
            return add

        add = group("时序", max_columns=3)
        self._add_spin(add, "_volume_duration_spin", "持续时间", 0, 60_000, "volume_duration_ms", suffix=" ms")
        self._add_spin(add, "_volume_waiting_time_spin", "结束等待", 0, 60_000, "volume_waiting_time_ms", suffix=" ms")
        self._add_spin(add, "_volume_time_offset_spin", "时间偏移", -60_000, 60_000, "volume_time_offset_ms", suffix=" ms")

        add = group("布局", min_column_width=220, max_columns=2)
        host._volume_appearance_mode_combo = self._combo(
            section,
            (
                ("自动配合字体", "auto"),
                ("复用配色方案", "role"),
                ("自定义", "custom"),
            ),
            "volume_appearance_mode",
        )
        host._volume_appearance_mode_combo.setToolTip(
            "自动配合字体：整体高度、柱宽按主文字字号推导（比例由「相对字号」"
            "调整）；柱体改用段首行第一个角色的完整装饰管线——填充/渐变、描边"
            "与二重描边、发光/阴影、整字放大动画与该角色同款同比缩放。对应控件"
            "停用并回显推导值，改字号或输出高度后自动跟随；"
            "复用配色方案：大小推导同自动配合字体，但装饰管线固定改用"
            "「配色来源」所选方案（全局默认/「标题」/角色方案）的配色，"
            "不随所在行的角色变化（与扫字线「复用配色方案」同口径）；"
            "自定义：全部参数独立设置"
        )
        add("外观模式", host._volume_appearance_mode_combo)
        host._volume_role_combo = WheelFocusedComboBox(section)
        compact_property_control(host._volume_role_combo)
        host._volume_role_combo.setToolTip(
            "复用配色方案模式下装饰管线引用的来源（全局默认/「标题」方案或"
            "角色方案）；在「角色」卡片编辑该来源的配色与描边/发光等装饰，"
            "音量柱即可使用渐变/拼色等效果。来源悬空时按「自动配合字体」"
            "口径回退（段首行第一个角色）"
        )
        host._volume_role_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                volume_role_name=host._volume_role_combo.currentData()
            )
        )
        host._volume_role_combo.hide()
        add("配色来源", host._volume_role_combo)
        self._add_canvas_spin(
            add,
            "_volume_auto_size_ratio_spin",
            "相对字号",
            5,
            300,
            "volume_auto_size_ratio_pct",
            "hard",
            suffix=" %",
        )
        host._volume_auto_size_ratio_spin.setToolTip(
            "auto/复用配色方案模式下整体高度相对主文字字号的百分比"
            "（默认 50%）；自定义模式下停用"
        )
        self._add_canvas_spin(
            add,
            "_volume_auto_column_ratio_spin",
            "柱宽比例",
            5,
            100,
            "volume_auto_column_ratio_pct",
            "hard",
            suffix=" %",
        )
        host._volume_auto_column_ratio_spin.setToolTip(
            "auto/复用配色方案模式下柱宽相对整体高度的百分比（默认 25%，"
            "与 N3 默认比例一致）；描边上限随柱宽推导。自定义模式下停用"
        )
        self._add_canvas_spin(add, "_volume_size_spin", "整体高度", 4, 240, "volume_size", "short_quarter", suffix=" px")
        self._add_canvas_spin(add, "_volume_column_width_spin", "柱宽", 1, 120, "volume_column_width", "short_twelfth", suffix=" px")
        self._add_canvas_spin(add, "_volume_column_count_spin", "柱数", 1, 16, "volume_column_count", "hard")
        self._add_canvas_spin(add, "_volume_column_spacing_spin", "柱间距", 0, 120, "volume_column_spacing", "short_twelfth", suffix=" px")
        self._add_canvas_spin(add, "_volume_ratio_spin", "首尾高度比", 1, 20, "volume_ratio", "hard", transform=float)
        host._volume_align_combo = self._combo(section, (("顶部", 0), ("居中", 1), ("底部", 2)), "volume_align", transform=int)
        add("垂直对齐", host._volume_align_combo)
        offset_base = getattr(host, "_offset_base_px", None)
        self._add_canvas_spin(
            add, "_volume_x_spin", "水平偏移", -100_000, 100_000, "volume_offset_x", "offset_x",
            suffix=" px", transform=offset_base,
        )
        self._add_canvas_spin(
            add, "_volume_y_spin", "垂直偏移", -100_000, 100_000, "volume_offset_y", "offset_y",
            suffix=" px", transform=offset_base,
        )
        add = group("动画", collapsed=True, max_columns=3)
        self._add_spin(add, "_volume_flash_times_spin", "闪烁次数", 1, 20, "volume_flash_times")
        self._add_spin(add, "_volume_flash_duration_spin", "闪烁占比", 0, 100, "volume_flash_duration_ratio", suffix=" %", transform=lambda value: value / 100.0)
        self._add_spin(add, "_volume_transition_ratio_spin", "覆盖过渡", 0, 100, "volume_transition_ratio_pct", suffix=" %")

        add = group("外观", max_columns=3)
        self._add_spin(add, "_volume_stroke_width_spin", "描边宽度", 0, 40, "volume_stroke_width", suffix=" px")
        self._add_spin(add, "_volume_opacity_spin", "透明度", 0, 100, "volume_opacity_pct", suffix=" %")
        self._add_color(add, "_volume_fill_btn", "柱填充色", "volume_fill_color")
        self._add_color(add, "_volume_stroke_btn", "柱描边色", "volume_stroke_color")
        self._add_color(add, "_volume_overlay_fill_btn", "覆盖填充色", "volume_overlay_fill_color")
        self._add_color(add, "_volume_overlay_stroke_btn", "覆盖描边色", "volume_overlay_stroke_color")
        section.set_expanded(False)
        return section

    def make_lit_section(self) -> QFrame:
        host = self._host
        section, layout = property_section("指示灯", switch=True)
        host._lit_section = section
        host._lit_enabled_switch = section.header_switch
        host._lit_enabled_switch.toggled.connect(
            lambda checked: host._update_style(
                lit_enabled=checked,
                **(
                    {"lit_style": host._lit_style_combo.currentData() or "circle"}
                    if checked
                    else {}
                ),
            )
        )
        host._lit_group_grids = {}

        def group(
            title: str,
            *,
            collapsed: bool = False,
            min_column_width: int = 135,
            max_columns: int = 4,
        ):
            box = SubGroup(title, collapsed=collapsed, parent=section)
            layout.addWidget(box)
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
                ("圆形", "circle"),
                ("方形", "square"),
                ("圆角", "rounded"),
                ("星型", "star"),
                ("八分音符", "note8"),
                ("十六分音符", "note16"),
                ("组合八分音符", "notepair"),
                ("图片", "image"),
            ),
            "lit_style",
        )
        add = group("布局", min_column_width=220, max_columns=2)
        add("形状", host._lit_style_combo)
        host._lit_image_path_edit = FluentLineEdit(section)
        compact_property_control(host._lit_image_path_edit)
        host._lit_image_path_edit.setPlaceholderText("形状=图片时的素材文件")
        host._lit_image_path_edit.editingFinished.connect(
            lambda: host._update_style(
                lit_image_path=host._lit_image_path_edit.text().strip()
            )
        )
        host._lit_image_browse_btn = FluentPushButton("浏览...", section)
        host._lit_image_browse_btn.setMinimumHeight(32)
        host._lit_image_browse_btn.clicked.connect(host._choose_lit_image)
        host._lit_image_clear_btn = FluentPushButton("清除", section)
        host._lit_image_clear_btn.setMinimumHeight(32)
        host._lit_image_clear_btn.clicked.connect(host._clear_lit_image)
        lit_image_row = QWidget(section)
        lit_image_layout = QHBoxLayout(lit_image_row)
        lit_image_layout.setContentsMargins(0, 0, 0, 0)
        lit_image_layout.setSpacing(4)
        lit_image_layout.addWidget(host._lit_image_path_edit, 1)
        lit_image_layout.addWidget(host._lit_image_browse_btn)
        lit_image_layout.addWidget(host._lit_image_clear_btn)
        host._lit_image_row = lit_image_row
        add("图片", lit_image_row)
        host._lit_appearance_mode_combo = self._combo(
            section,
            (
                ("自动配合字体", "auto"),
                ("复用配色方案", "role"),
                ("自定义", "custom"),
            ),
            "lit_appearance_mode",
        )
        host._lit_appearance_mode_combo.setToolTip(
            "自动配合字体：灯大小按主文字字号推导（比例由「相对字号」调整）；"
            "矢量灯改用段首行第一个角色的完整装饰管线——填充/渐变、描边与"
            "二重描边、发光/阴影与高光取该角色走字后配色并同比缩放，且不跟随"
            "行入退场动画（独立悬浮，靠自身倒计时转场淡出）。对应控件停用并"
            "回显推导值，改字号或输出高度后自动跟随；"
            "复用配色方案：大小推导同自动配合字体，但装饰管线固定改用"
            "「配色来源」所选方案（全局默认/「标题」/角色方案）的配色，"
            "不随所在行的角色变化（与扫字线「复用配色方案」同口径）；"
            "自定义：全部参数独立设置"
        )
        add("外观模式", host._lit_appearance_mode_combo)
        host._lit_role_combo = WheelFocusedComboBox(section)
        compact_property_control(host._lit_role_combo)
        host._lit_role_combo.setToolTip(
            "复用配色方案模式下装饰管线引用的来源（全局默认/「标题」方案或"
            "角色方案）；在「角色」卡片编辑该来源的配色与描边/发光等装饰，"
            "指示灯即可使用渐变/拼色等效果。来源悬空时按「自动配合字体」"
            "口径回退（段首行第一个角色）"
        )
        host._lit_role_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                lit_role_name=host._lit_role_combo.currentData()
            )
        )
        host._lit_role_combo.hide()
        add("配色来源", host._lit_role_combo)
        self._add_canvas_spin(
            add,
            "_lit_auto_size_ratio_spin",
            "相对字号",
            5,
            300,
            "lit_auto_size_ratio_pct",
            "hard",
            suffix=" %",
        )
        host._lit_auto_size_ratio_spin.setToolTip(
            "auto/复用配色方案模式下灯边长相对主文字字号的百分比"
            "（默认 50%）；自定义模式下停用"
        )
        self._add_canvas_spin(add, "_lit_number_spin", "数量", 1, 8, "lit_number", "hard")
        self._add_canvas_spin(add, "_lit_size_spin", "大小", 4, 160, "lit_size", "short_quarter", suffix=" px")
        self._add_canvas_spin(add, "_lit_tracking_spin", "间距", 0, 200, "lit_tracking", "short_twelfth", suffix=" px")
        # 偏移类：输入硬范围刻意放宽（手动输入无实际上下限），滑条给画布
        # 宽/高 ±20% 拖拽范围（见 _refresh_canvas_slider_ranges 的
        # offset_x / offset_y 档，比通用偏移类的 ±10% 更宽），更远的位置
        # 由数值输入完成。存储恒为 1080 基准（同扫字线）：编辑/显示按当前
        # 输出高度换算，提交写回基准值（_Host 桩无换算方法时按原值提交）。
        offset_base = getattr(host, "_offset_base_px", None)
        self._add_canvas_spin(
            add, "_lit_x_spin", "水平偏移", -100_000, 100_000, "lit_offset_x", "offset_x",
            suffix=" px", transform=offset_base,
        )
        self._add_canvas_spin(
            add, "_lit_y_spin", "垂直偏移", -100_000, 100_000, "lit_offset_y", "offset_y",
            suffix=" px", transform=offset_base,
        )

        add = group("时序", max_columns=3)
        self._add_spin(add, "_lit_duration_spin", "持续时间", 0, 60_000, "signals_duration_ms", suffix=" ms")
        self._add_spin(add, "_lit_waiting_time_spin", "结束等待", 0, 60_000, "lit_waiting_time_ms", suffix=" ms")
        self._add_spin(add, "_lit_time_offset_spin", "时间偏移", -60_000, 60_000, "lit_time_offset_ms", suffix=" ms")
        add = group("外观", max_columns=4)
        self._add_color(add, "_lit_fill_btn", "填充颜色", "lit_fill_color")
        self._add_color(add, "_lit_stroke_btn", "描边颜色", "lit_stroke_color")
        self._add_spin(add, "_lit_stroke_width_spin", "描边宽度", 0, 40, "lit_stroke_width", suffix=" px")
        self._add_spin(add, "_lit_opacity_spin", "透明度", 0, 100, "lit_opacity_pct", suffix=" %")
        self._add_spin(add, "_lit_edge_brightness_spin", "高光亮度", 0, 100, "lit_edge_brightness_pct", suffix=" %")
        self._add_spin(add, "_lit_stroke_soften_spin", "描边柔化", 0, 40, "lit_stroke_soften", suffix=" px")
        host._lit_shadow_check = CheckBox("启用", section)
        host._lit_shadow_check.toggled.connect(
            lambda checked: host._update_style(lit_shadow=checked)
        )
        add("阴影", host._lit_shadow_check)

        add = group("转场", collapsed=True, min_column_width=220, max_columns=2)
        host._lit_transition_mode_combo = self._combo(
            section,
            (("无", "none"), ("淡入淡出", "fade"), ("滑动", "slide")),
            "lit_transition_mode",
        )

        def _lit_transition_mode_changed(_index: int) -> None:
            if host._lit_transition_mode_combo.currentData() != "slide":
                return
            # 滑动距离为 0 时只剩透明度变化（视觉上与淡入淡出无异），
            # 切到滑动时自动补一个可见的默认位移。
            if int(host._lit_transition_distance_spin.value()) == 0:
                host._update_style(lit_transition_distance=24)

        host._lit_transition_mode_combo.currentIndexChanged.connect(
            _lit_transition_mode_changed
        )
        add("类型", host._lit_transition_mode_combo)
        self._add_spin(add, "_lit_transition_ratio_spin", "时长比例", 0, 100, "lit_transition_ratio_pct", suffix=" %")
        self._add_spin(add, "_lit_transition_angle_spin", "角度", -360, 360, "lit_transition_angle_deg", suffix=" °")
        self._add_canvas_spin(add, "_lit_transition_distance_spin", "距离", 0, 800, "lit_transition_distance", "short", suffix=" px")

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
            ("扫字线", "scanline"),
            ("utopia+扫字线", "utopia_scanline"),
            ("整字放大", "zoom_pulse"),
            ("整字放大+扫字线", "zoom_pulse_scanline"),
        ):
            host._karaoke_anim_combo.addItem(label, value)
        host._karaoke_anim_combo.setToolTip(
            "控制歌词正在着色时的逐字动画；旧项目的 Utopia 入退场会自动兼容。"
            "扫字线在走字锋面处按设定粗细高亮发光（主文字与注音同效）；"
            "整字放大在唱字期间持续放大、唱字结束后缓慢缩回，可再叠加扫字线"
        )
        host._karaoke_anim_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                karaoke_anim=host._karaoke_anim_combo.currentData()
            )
        )

        host._reverse_karaoke_anim_combo = WheelFocusedComboBox(section)
        compact_property_control(host._reverse_karaoke_anim_combo)
        for label, value in (
            ("跟随唱字特效", "inherit"),
            ("Wipe", "none"),
            ("无 Wipe", "no_wipe"),
            ("Utopia", "utopia"),
            ("扫字线", "scanline"),
            ("utopia+扫字线", "utopia_scanline"),
            ("整字放大", "zoom_pulse"),
            ("整字放大+扫字线", "zoom_pulse_scanline"),
        ):
            host._reverse_karaoke_anim_combo.addItem(label, value)
        host._reverse_karaoke_anim_combo.setToolTip(
            "仅对标记为反向唱字的歌词行生效；无 Wipe 会在区间结束时整字瞬切，"
            "扫字线档位同样叠加锋面高亮"
        )
        host._reverse_karaoke_anim_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                reverse_karaoke_anim=host._reverse_karaoke_anim_combo.currentData()
            )
        )
        host._karaoke_pair_row = self._karaoke_pair_row(
            section,
            host._karaoke_anim_combo,
            host._reverse_karaoke_anim_combo,
        )
        host._animation_grid.add_field("唱字 / 反向唱字特效", host._karaoke_pair_row)

        host._scanline_mode_combo = WheelFocusedComboBox(section)
        compact_property_control(host._scanline_mode_combo)
        for label, value in (
            ("单独颜色", "color"),
            ("底色发光", "brighten"),
            ("跟随字体·走字前", "follow_before"),
            ("跟随字体·走字后", "follow_after"),
            ("复用配色方案", "role"),
        ):
            host._scanline_mode_combo.addItem(label, value)
        host._scanline_mode_combo.setToolTip(
            "单独颜色：高亮带用设定颜色填充；"
            "底色发光：保留锋面两侧原有前后色的色相和饱和度，只提高 HSV 明度；"
            "跟随字体·走字前/后：与底色发光同一通路，但整条高亮带固定用当前行"
            "实际配色的走字前/走字后颜色（可再提亮，0 = 原色）；"
            "复用配色方案：整条高亮带用所选来源（全局默认/「标题」/角色方案）"
            "「走字后-主文字」的填充（支持渐变/拼色/图片），仅重绘字形填充层"
        )
        host._scanline_mode_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                scanline_mode=host._scanline_mode_combo.currentData()
            )
        )
        host._scanline_role_combo = WheelFocusedComboBox(section)
        compact_property_control(host._scanline_role_combo)
        host._scanline_role_combo.setToolTip(
            "复用配色方案模式下引用的来源（全局默认/「标题」方案或角色方案）；"
            "在「角色」卡片编辑该来源「走字后-主文字」的填充方式，"
            "扫字线即可使用渐变/拼色等效果"
        )
        host._scanline_role_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                scanline_role_name=host._scanline_role_combo.currentData()
            )
        )
        host._scanline_width_spin = self._spin_factory(1, 400, suffix=" px")
        host._scanline_width_spin.setToolTip("扫字线高亮带宽度（以走字锋面为中心）")
        host._scanline_width_spin.valueChanged.connect(
            lambda value: host._update_style(
                scanline_width_px=host._scanline_base_px(value)
            )
        )
        host._scanline_glow_spin = self._spin_factory(0, 200, suffix=" px")
        host._scanline_glow_spin.setToolTip(
            "扫字线字形内柔化范围；半径越大边缘越柔和，不会向字形外扩散"
        )
        host._scanline_glow_spin.valueChanged.connect(
            lambda value: host._update_style(
                scanline_glow_px=host._scanline_base_px(value)
            )
        )
        host._scanline_brightness_spin = self._spin_factory(0, 100, suffix=" %")
        host._scanline_brightness_spin.setToolTip(
            "底色发光/跟随字体的亮度提升程度；0 = 不提亮（跟随字体时为原色），"
            "100 = 提到纯白"
        )
        host._scanline_brightness_spin.valueChanged.connect(
            lambda value: host._update_style(scanline_brightness_pct=value)
        )
        host._scanline_color_btn = host._color_button(
            "scanline_color", getattr(host._style, "scanline_color", "#FFFFFF")
        )
        host._scanline_row = self._scanline_param_row(
            section,
            host._scanline_mode_combo,
            host._scanline_width_spin,
            host._scanline_color_btn,
            host._scanline_brightness_spin,
            host._scanline_role_combo,
            host._scanline_glow_spin,
        )
        host._zoom_pulse_curve_combo = WheelFocusedComboBox(section)
        compact_property_control(host._zoom_pulse_curve_combo)
        for label, value in (
            ("0级（线性）", 0),
            ("1级（匀速·默认）", 1),
            ("2级（稍快）", 2),
            ("3级（较快）", 3),
            ("4级（快）", 4),
            ("5级（极快）", 5),
        ):
            host._zoom_pulse_curve_combo.addItem(label, value)
        host._zoom_pulse_curve_combo.setToolTip(
            "整字放大速度等级：0=线性（匀速放大缩小）；1~5 为缓出/缓入曲线阶数，"
            "等级越高放大越快贴近峰值、在峰值附近停留越久（默认 1，"
            "画面与 0 级相同；觉得放大拖沓可调高）"
        )
        host._zoom_pulse_curve_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                zoom_pulse_curve_level=host._zoom_pulse_curve_combo.currentData()
            )
        )
        host._stroke_flash_check = CheckBox("唱字闪光", section)
        host._stroke_flash_check.setToolTip(
            "每个字唱到的瞬间，描边沿字形轮廓闪白脉冲约 240ms 后回落；"
            "可与任意唱字特效叠加，仅主文字生效（注音不闪）"
        )
        host._stroke_flash_check.toggled.connect(
            lambda checked: host._update_style(karaoke_stroke_flash=checked)
        )

        host._fx_sing_combo = self._combo(section, FX_SING_OPTIONS, "sing_fx")
        host._fx_sing_combo.setToolTip(
            "唱字装饰粒子：唱到哪个字就在那个字上生效——星光闪烁=按伪随机"
            "位置冒星、音符飘出=自字框升起、涟漪光环=每字两圈细环扩散"
            "（随机位置/大小/时机）"
        )
        host._fx_particle_mode_combo = WheelFocusedComboBox(section)
        compact_property_control(host._fx_particle_mode_combo)
        for label, value in FX_PARTICLE_COLOR_MODE_OPTIONS:
            host._fx_particle_mode_combo.addItem(label, value)
        host._fx_particle_mode_combo.setToolTip(
            "粒子颜色模式：默认颜色=两种樱花粉随机混发（花瓣特效的默认观感）；"
            "单独颜色=颜色一/颜色二双色槽随机混发（两色相同为单色）；"
            "跟随字体·前后各一=所在行实际配色的走字前/后主文字实色双色随机混发"
            "（渐变/拼色取平均色）；"
            "跟随字体·走字前/后=取所在行实际配色的走字前/走字后主文字"
            "完整装饰（渐变/描边按取色层级下发，渐变/拼色取平均色仅作回退）；"
            "复用配色方案=用所选来源（全局默认/「标题」/角色方案）的配色，"
            "花瓣取该来源走字前/后实色双色随机、其余粒子取「走字后-主文字」"
            "填充折算成实色，与扫字线同口径"
        )
        host._fx_particle_mode_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                fx_particle_color_mode=host._fx_particle_mode_combo.currentData()
            )
        )
        host._fx_particle_role_combo = WheelFocusedComboBox(section)
        compact_property_control(host._fx_particle_role_combo)
        host._fx_particle_role_combo.setToolTip(
            "「复用配色方案」模式下粒子引用的来源（全局默认/「标题」方案或"
            "角色方案）；在「角色」卡片编辑该来源「走字后-主文字」的填充方式，"
            "渐变/拼色折算为平均色"
        )
        host._fx_particle_role_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                fx_particle_role_name=host._fx_particle_role_combo.currentData()
            )
        )
        host._fx_particle_layers_combo = WheelFocusedComboBox(section)
        compact_property_control(host._fx_particle_layers_combo)
        for label, value in FX_PARTICLE_LAYER_OPTIONS:
            host._fx_particle_layers_combo.addItem(label, value)
        host._fx_particle_layers_combo.setToolTip(
            "取色层级（仅对从配色方案取色的四档生效：跟随字体·走字前/"
            "走字后/前后各一、复用配色方案）：仅实色=只取该态主文字色，"
            "粒子呈纯色剪影（默认）；+描边=按角色方案的描边栈（方案启用"
            "二重描边时一起加）、不加装饰；+装饰=不加描边、仅加装饰层；"
            "全有=描边栈 + 装饰层，完全按角色方案。装饰层随角色方案的"
            "装饰设置（阴影/发光）走；改一次一直沿用"
        )
        host._fx_particle_layers_combo.currentIndexChanged.connect(
            lambda _index: host._update_style(
                fx_particle_color_layers=host._fx_particle_layers_combo.currentData()
            )
        )
        host._fx_particle_row = self._fx_param_row(
            section,
            host._fx_sing_combo,
            host._fx_particle_mode_combo,
            host._fx_particle_layers_combo,
        )
        host._fx_size_spin = self._spin_factory(5, 200, suffix=" %")
        host._fx_size_spin.setToolTip(
            "唱字装饰粒子的尺寸（相对主字号的百分比）：星光/音符/花瓣按此"
            "边长，涟漪按 3.6 倍扩散；开启「入退场同用」后也作用于入退场"
            "动画粒子，否则入退场动画粒子用固定默认档"
        )
        host._fx_size_spin.valueChanged.connect(
            lambda value: host._update_style(fx_particle_size_em=value / 100.0)
        )
        host._fx_count_spin = self._spin_factory(2, 64, suffix=" 个")
        host._fx_count_spin.setToolTip(
            "唱字装饰粒子数量：闪烁/音符按比例折算为每字数量；"
            "入退场动画粒子恒用固定默认档，不受此旋钮影响"
        )
        host._fx_count_spin.valueChanged.connect(
            lambda value: host._update_style(fx_particle_count=value)
        )
        host._fx_color_btn = host._color_button(
            "fx_particle_color", getattr(host._style, "fx_particle_color", "#FFFFFF")
        )
        host._fx_color_btn.setToolTip(
            "粒子颜色一；单独颜色档下与颜色二双色随机混发"
        )
        host._fx_color_btn2 = host._color_button(
            "fx_particle_color2",
            getattr(host._style, "fx_particle_color2", "#FFFFFF"),
        )
        host._fx_color_btn2.setToolTip(
            "粒子颜色二（默认白色）：单独颜色档恒为双色随机混发"
        )
        # 双色槽容器：两颗颜色按钮并排，共占参数行的「颜色/来源」列位。
        # 窄窗下自动改竖排——两颗颜色按钮并排要 344px，会把特效页网格的
        # 最小宽度顶到 539px，360px 窄窗就再也降不到 1 列（响应式失效）。
        host._fx_color_slot = ResponsiveControlPair(section)
        host._fx_color_slot.set_widgets(host._fx_color_btn, host._fx_color_btn2)
        host._fx_apply_check = CheckBox("仅唱字", section)
        host._fx_apply_check.setToolTip(
            "勾选（默认）：粒子的颜色与尺寸仅作用于唱字装饰粒子，入场/退场"
            "动画携带的粒子用固定默认档（樱花粉双色、40% 字号，数量恒固定）；"
            "取消勾选：颜色与尺寸也应用于入场/退场动画粒子（星光/涟漪/音符/"
            "花瓣/拼接/消散，颜色模式同样生效）"
        )
        # 语义反转：勾选「仅唱字」= fx_apply_to_entry_exit 为 False。
        host._fx_apply_check.toggled.connect(
            lambda checked: host._update_style(fx_apply_to_entry_exit=not checked)
        )
        host._fx_size_count_row = self._fx_size_row(
            section,
            host._fx_size_spin,
            host._fx_count_spin,
        )
        host._fx_color_row = self._fx_color_row(
            section,
            host._fx_color_slot,
            host._fx_particle_role_combo,
            host._fx_apply_check,
        )
        # 网格行序：第 1 行 = 入场/退场，第 2 行 = 唱字对 + 段首尾区块，
        # 第 3 行 = 扫字线整行（参数永久可编辑，颜色/亮度按模式互换启用态），
        # 第 4 行 = 整字放大速度等级，第 5 行 = 装饰粒子（唱字档位 + 颜色模式
        # + 取色层级），第 6 行 = 粒子尺寸 · 数量，第 7 行 = 粒子颜色/来源
        # （双色槽与来源下拉按模式互换）+ 行尾「入退场同用」联动开关。
        # 粒子参数拆成三个字段：单行塞六控件会把网格最小宽度顶到 539px，
        # 特效页在 360px 窄窗下再也降不到 1 列（响应式失效）。

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
        # 该字段把整行四个控件都写进标签，标签本身就比行宽更长；允许折行，
        # 否则它会成为字段的最小宽度，把网格顶到 372px、窄窗右缘被裁。
        host._animation_grid.add_widget(
            property_field(
                "扫字线 / 模式 · 粗细 · 颜色/亮度/角色 · 柔化半径",
                host._scanline_row,
                wrap_label=True,
            )
        )
        host._animation_grid.add_field("整字放大速度等级", host._zoom_pulse_curve_combo)
        host._stroke_flash_row = QWidget(section)
        stroke_flash_layout = QHBoxLayout(host._stroke_flash_row)
        stroke_flash_layout.setContentsMargins(0, 0, 0, 0)
        stroke_flash_layout.setSpacing(6)
        stroke_flash_layout.addWidget(host._stroke_flash_check)
        # 用户口径：粒子参数行与唱字闪光行互换位置（粒子参数在前）。
        host._animation_grid.add_field("唱字装饰粒子 / 颜色模式", host._fx_particle_row)
        host._animation_grid.add_field("粒子尺寸 · 数量", host._fx_size_count_row)
        host._animation_grid.add_field(
            "粒子颜色 / 来源 · 仅唱字",
            host._fx_color_row,
        )
        host._animation_grid.add_field("唱字闪光", host._stroke_flash_row)
        layout.addWidget(host._animation_grid)
        return section

    @staticmethod
    def _fx_param_row(
        parent: QWidget,
        sing_combo: Any,
        mode_combo: Any,
        layers_combo: Any,
    ) -> QWidget:
        """唱字装饰粒子单行：档位 + 颜色模式 + 取色层级。"""
        row = QWidget(parent)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        row_layout.addWidget(sing_combo, 3)
        row_layout.addWidget(mode_combo, 3)
        row_layout.addWidget(layers_combo, 2)
        return row

    @staticmethod
    def _fx_size_row(
        parent: QWidget,
        size_spin: Any,
        count_spin: Any,
    ) -> QWidget:
        """粒子尺寸 · 数量单行（尺寸为字号百分比，数量按特效折算）。"""
        row = QWidget(parent)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        row_layout.addWidget(size_spin, 1)
        row_layout.addWidget(count_spin, 1)
        return row

    @staticmethod
    def _fx_color_row(
        parent: QWidget,
        color_button: QWidget,
        role_combo: Any,
        apply_check: Any,
    ) -> QWidget:
        """粒子颜色/来源单行：双色槽与来源下拉共用同一列位（按颜色模式
        互换显示，扫字线第三列同款约定）；「入退场同用」开关常驻行尾。"""
        row = QWidget(parent)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        row_layout.addWidget(color_button, 1)
        row_layout.addWidget(role_combo, 1)
        row_layout.addWidget(apply_check, 0)
        role_combo.hide()
        return row

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

    def _add_canvas_spin(
        self,
        add: Callable[[str | None, QWidget], None],
        attribute: str,
        label: str,
        minimum: int,
        maximum: int,
        model_field: str,
        range_kind: str,
        *,
        suffix: str = "",
        transform: Callable[[int], Any] = lambda value: value,
    ) -> None:
        host = self._host
        control = CanvasSliderSpinBox(
            self._spin_factory(minimum, maximum, suffix=suffix)
        )
        setattr(host, attribute, control)
        if range_kind == "hard":
            # 无量纲参数（柱数/比例/数量）：滑块直接覆盖整个硬范围，
            # 不随画布尺寸缩放，也无需注册画布刷新。
            control.set_slider_range(minimum, maximum)
        else:
            register = getattr(host, "_register_canvas_slider", None)
            if callable(register):
                register(control, range_kind)
            else:
                control.set_slider_range(minimum, maximum)
        control.valueChanged.connect(
            lambda value, field=model_field, convert=transform: host._update_style(
                **{field: convert(value)}
            )
        )
        add(label, control)

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
    def _karaoke_pair_row(
        parent: QWidget,
        karaoke_combo: Any,
        reverse_combo: Any,
    ) -> QWidget:
        row = QWidget(parent)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        row_layout.addWidget(karaoke_combo, 1)
        row_layout.addWidget(reverse_combo, 1)
        return row

    @staticmethod
    def _scanline_param_row(
        parent: QWidget,
        mode_combo: Any,
        width_spin: Any,
        color_button: QWidget,
        brightness_spin: Any,
        role_combo: Any,
        glow_spin: Any,
    ) -> QWidget:
        """扫字线参数单行：模式 · 粗细 · 颜色/亮度/来源 · 柔化半径。

        控件全部常驻；模式切换只换第三列的显示项（颜色 ↔ 亮度提升 ↔
        来源下拉；跟随字体两档复用亮度提升），不隐藏其他列、不移位。
        """

        row = QWidget(parent)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        row_layout.addWidget(mode_combo, 3)
        row_layout.addWidget(width_spin, 2)
        row_layout.addWidget(color_button, 2)
        row_layout.addWidget(brightness_spin, 2)
        row_layout.addWidget(role_combo, 2)
        row_layout.addWidget(glow_spin, 2)
        # 参数永久可编辑；宿主按模式回显：单独颜色显示颜色，底色发光与
        # 跟随字体两档显示亮度提升，复用配色方案显示来源下拉
        # （同一列位互换，行宽不变）。
        brightness_spin.hide()
        role_combo.hide()
        return row

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
