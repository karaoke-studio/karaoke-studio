"""Focused construction contracts for the subtitle effects-property page."""

from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtWidgets import QLabel, QPushButton

from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    CanvasSliderSpinBox,
)
from krok_helper.subtitle_render.frontend.properties.pages.effects import (
    EffectsPropertyPageBuilder,
)


class _Host:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self._style = SimpleNamespace(
            volume_fill_color="#101010",
            volume_stroke_color="#202020",
            volume_overlay_fill_color="#303030",
            volume_overlay_stroke_color="#404040",
            lit_fill_color="#505050",
            lit_stroke_color="#606060",
        )

    def _update_style(self, _force_global: bool = False, **changes) -> None:
        # 与真实宿主同签名：``_force_global`` 是编辑作用域开关，不算样式字段。
        self.updates.append(changes)

    def _scanline_base_px(self, value: int) -> int:
        # 真实宿主按当前输出高度把编辑值折算回 1080 基准;桩默认 1080 画布,
        # 换算为恒等,路由契约测试只关心字段名与数值流向。
        return value

    def _choose_lit_image(self) -> None:
        self.updates.append({"lit_image_path": "chosen"})

    def _clear_lit_image(self) -> None:
        self.updates.append({"lit_image_path": ""})

    def _on_section_edge_toggled(self, checked: bool) -> None:
        # 真实宿主还读 self._style；builder 契约测试只关心启用联动与字段路由。
        self._section_head_anim_combo.setEnabled(checked)
        self._section_tail_anim_combo.setEnabled(checked)
        self._section_edge_both_check.setEnabled(checked)
        self._update_style(section_edge_anim_enabled=checked)

    def _on_section_edge_both_toggled(self, checked: bool) -> None:
        self._update_style(section_edge_both_animations=checked)

    def _on_fx_target_changed(self, target: str) -> None:
        self.updates.append({"_fx_target": target})

    def _on_fx_color_mode_changed(self, mode) -> None:
        self._update_style(fx_target_color_mode=mode)

    def _on_fx_role_name_changed(self, name) -> None:
        self._update_style(fx_target_role_name=name)

    def _on_fx_layers_changed(self, layers) -> None:
        self._update_style(fx_target_layers=layers)

    def _on_fx_size_changed(self, value: int) -> None:
        self._update_style(fx_target_size=value)

    def _on_fx_count_changed(self, value: int) -> None:
        self._update_style(fx_target_count=value)

    def _on_fx_speed_changed(self, value: int) -> None:
        self._update_style(fx_target_speed=value)

    def _fx_color_button(self, slot: str, color: str):
        button = QPushButton(color)
        button.setObjectName(slot)
        return button

    def _color_button(self, field: str, color: str):
        button = QPushButton(color)
        button.setObjectName(field)
        return button

def test_effects_animation_builder_preserves_options_and_layout(qapp) -> None:
    host = _Host()
    section = EffectsPropertyPageBuilder(host).make_animation_section()

    assert section.header.text() == "入退场/唱字动画"
    assert host._animation_grid._max_columns == 2
    assert host._entry_anim_combo.count() == 23
    assert host._entry_anim_combo.itemData(5) == "char_drip"
    assert host._entry_anim_combo.itemData(8) == "tracking_in"
    assert host._exit_anim_combo.count() == 23
    for combo in (host._entry_anim_combo, host._exit_anim_combo):
        assert [combo.itemData(i) for i in range(20, 23)] == [
            "petal", "snow", "snow_solid"
        ]
    assert host._exit_anim_combo.itemData(2) == "slide_out"
    assert host._exit_anim_combo.itemData(8) == "scatter_out"
    assert host._entry_lead_spin.maximum() == 3000
    assert host._exit_fade_spin.maximum() == 3000
    assert host._entry_lead_spin.toolTip() == "入场动画时长"
    assert host._exit_fade_spin.toolTip() == "退场动画时长"
    assert [
        host._karaoke_anim_combo.itemData(index)
        for index in range(host._karaoke_anim_combo.count())
    ] == [
        "none", "no_wipe", "utopia", "zoom_pulse"
    ]
    assert [
        host._reverse_karaoke_anim_combo.itemData(index)
        for index in range(host._reverse_karaoke_anim_combo.count())
    ] == [
        "inherit", "none", "no_wipe", "utopia",
        "zoom_pulse"
    ]
    assert [
        host._scanline_mode_combo.itemData(index)
        for index in range(host._scanline_mode_combo.count())
    ] == [
        "default",
        "color",
        "brighten",
        "follow_before",
        "follow_after",
        "role",
    ]
    # 扫字线特效：沿锋面发射的装饰粒子档位。
    assert [
        host._scanline_effect_combo.itemData(index)
        for index in range(host._scanline_effect_combo.count())
    ] == [
        "none", "sakura", "feather", "snow", "snow_solid", "star", "note",
    ]
    # 粒子设置对象药丸：入退场 / 唱字装饰 / 扫字线三类独立配色。
    assert [
        host._fx_target_pill.items[key].text()
        for key in host._fx_target_pill.items
    ] == ["入退场动画", "唱字装饰粒子", "扫字线特效"]
    assert host._fx_target_pill.currentRouteKey() == "sing"
    # 扫字线形态：普通高亮/烈焰棒/水泡（独立形态替换高亮带）。
    assert [
        host._scanline_shape_combo.itemData(index)
        for index in range(host._scanline_shape_combo.count())
    ] == ["band", "flame", "bubble"]
    assert [
        host._scanline_shape_combo.itemText(index)
        for index in range(host._scanline_shape_combo.count())
    ] == ["普通高亮", "烈焰棒", "水泡"]
    # 下方粒子/闪光单独 3 列网格，和扫字线参数分栏。
    assert host._fx_grid._max_columns == 3
    # 粒子速度调整：项目通用 slider+输入框复合控件，-1000~1000%，默认 0。
    assert isinstance(host._fx_speed_control, CanvasSliderSpinBox)
    assert host._fx_speed_control.input_range() == (-1000, 1000)
    assert host._fx_speed_control.slider_range() == (-1000, 1000)
    assert host._fx_speed_control.value() == 0
    assert host._fx_speed_control.toolTip()
    particle_labels = [
        label.text() for label in host._particle_section.findChildren(QLabel)
    ]
    assert "唱字粒子样式" not in particle_labels
    assert "唱字闪光" not in particle_labels
    assert host._particle_section.header.text() == "粒子设置"
    animation_labels = [label.text() for label in section.findChildren(QLabel)]
    assert "唱字粒子样式" in animation_labels
    assert "唱字闪光" in animation_labels
    assert host._scanline_section.header.text() == "扫字线"
    assert host._scanline_enabled_switch.isChecked() is False
    assert host._scanline_scope_pill.currentRouteKey() == "forward"
    # 整字放大速度等级：0（线性）~5 六档下拉，每档带速度说明；默认 1。
    assert [
        host._zoom_pulse_curve_combo.itemData(index)
        for index in range(host._zoom_pulse_curve_combo.count())
    ] == [0, 1, 2, 3, 4, 5]
    assert host._zoom_pulse_curve_combo.itemText(0) == "0级（线性）"
    assert host._zoom_pulse_curve_combo.itemText(1) == "1级（匀速·默认）"
    assert host._zoom_pulse_curve_combo.itemText(3) == "3级（较快）"
    assert host._zoom_pulse_curve_combo.itemText(5) == "5级（极快）"
    # 扫字线独占一整行（与出入场动画两栏同宽）；参数永久激活，亮度与角色
    # 下拉默认隐藏（默认单独颜色模式），宿主回显按模式互换第三列。
    assert host._scanline_row is not None
    for control in (
        host._scanline_mode_combo,
        host._scanline_width_spin,
        host._scanline_color_btn,
        host._scanline_role_combo,
        host._scanline_glow_spin,
    ):
        assert control.isEnabled()
    assert not host._scanline_color_btn.isHidden()
    assert host._scanline_brightness_spin.isHidden()
    assert host._scanline_role_combo.isHidden()
    # 网格行序：唱字对（第 2 行第 1 栏）→ 段首尾区块（第 2 行第 2 栏）
    # → 扫字线整行（第 3 行）。
    items = host._animation_grid._items

    def grid_index(widget) -> int:
        for index, item in enumerate(items):
            node = widget
            while node is not None:
                if node is item:
                    return index
                node = node.parent()
        return -1

    assert (
        grid_index(host._karaoke_pair_row)
        < grid_index(host._section_edge_row)
    )
    assert host._scanline_section.header.text() == "扫字线"
    node = host._scanline_row
    while node is not None and node is not host._scanline_section:
        node = node.parent()
    assert node is host._scanline_section


def test_effects_section_edge_builder_defaults_and_order(qapp) -> None:
    host = _Host()
    EffectsPropertyPageBuilder(host).make_animation_section()

    assert host._section_edge_check.text() == "段首尾独立动画"
    assert host._section_edge_both_check.text() == "同时设置出入场"
    assert not host._section_edge_check.isChecked()
    assert not host._section_edge_both_check.isChecked()
    assert host._section_head_anim_combo.count() == 23
    assert host._section_tail_anim_combo.count() == 23
    # 主开关默认关：两个下拉与子开关全部禁用；选项表含默认动画（具体显示值
    # 由 set_style 回显）。
    assert not host._section_head_anim_combo.isEnabled()
    assert not host._section_tail_anim_combo.isEnabled()
    assert not host._section_edge_both_check.isEnabled()
    assert host._section_head_anim_combo.findData("fade") != -1
    assert host._section_tail_anim_combo.findData("fade") != -1

    # 唱字特效在第三行（退场动画之后），段首尾区块最后。
    items = host._animation_grid._items

    def grid_index(widget) -> int:
        for index, item in enumerate(items):
            node = widget
            while node is not None:
                if node is item:
                    return index
                node = node.parent()
        return -1

    assert (
        grid_index(host._exit_anim_combo)
        < grid_index(host._karaoke_anim_combo)
        < grid_index(host._section_edge_row)
    )


def test_effects_section_edge_builder_routes_controls_to_style_fields(qapp) -> None:
    host = _Host()
    EffectsPropertyPageBuilder(host).make_animation_section()

    host._section_edge_check.setChecked(True)
    host._section_edge_both_check.setChecked(True)
    host._section_head_anim_combo.setCurrentIndex(3)
    host._section_tail_anim_combo.setCurrentIndex(2)

    assert host.updates == [
        {"section_edge_anim_enabled": True},
        {"section_edge_both_animations": True},
        {"section_head_anim": "rise"},
        {"section_tail_anim": "slide_out"},
    ]


def test_effects_animation_builder_routes_controls_to_style_fields(qapp) -> None:
    host = _Host()
    EffectsPropertyPageBuilder(host).make_animation_section()

    host._entry_anim_combo.setCurrentIndex(1)
    host._entry_lead_spin.setValue(250)
    host._exit_anim_combo.setCurrentIndex(2)
    host._exit_fade_spin.setValue(300)
    host._karaoke_anim_combo.setCurrentIndex(1)
    host._reverse_karaoke_anim_combo.setCurrentIndex(2)
    host._scanline_mode_combo.setCurrentIndex(
        host._scanline_mode_combo.findData("brighten")
    )
    host._scanline_width_spin.setValue(24)
    host._scanline_glow_spin.setValue(9)
    host._scanline_brightness_spin.setValue(75)
    host._scanline_shape_combo.setCurrentIndex(
        host._scanline_shape_combo.findData("flame")
    )
    host._scanline_effect_combo.setCurrentIndex(
        host._scanline_effect_combo.findData("snow")
    )
    host._fx_target_pill.setCurrentItem("scanline")
    host._fx_particle_mode_combo.setCurrentIndex(
        host._fx_particle_mode_combo.findData("sakura")
    )
    host._fx_size_spin.setValue(45)
    host._fx_count_spin.setValue(16)
    host._fx_speed_control.setValue(250)

    assert host.updates == [
        {"entry_anim": "fade"},
        {"entry_lead_ms": 250},
        {"exit_anim": "slide_out"},
        {"exit_fade_ms": 300},
        {"karaoke_anim": "no_wipe"},
        {"reverse_karaoke_anim": "no_wipe"},
        {"scanline_mode": "brighten"},
        {"scanline_width_px": 24},
        {"scanline_glow_px": 9},
        {"scanline_brightness_pct": 75},
        {"scanline_shape": "flame"},
        {"scanline_effect": "snow"},
        {"_fx_target": "scanline"},
        {"fx_target_size": 45},
        {"fx_target_count": 16},
        {"fx_target_speed": 250},
    ]


def test_effects_lit_builder_preserves_groups_ranges_and_initial_state(qapp) -> None:
    host = _Host()
    section = EffectsPropertyPageBuilder(host).make_lit_section()

    assert section.header.text() == "指示灯"
    assert section.header_switch is host._lit_enabled_switch
    assert not section.is_expanded()
    assert list(host._lit_group_grids) == ["布局", "时序", "外观", "转场"]
    # 圆形/方形/圆角/星型/三种音符 + 图片（素材模式）。
    assert host._lit_style_combo.count() == 8
    assert host._lit_transition_angle_spin.minimum() == -360
    assert host._lit_transition_distance_spin.maximum() == 800
    assert host._lit_stroke_btn.objectName() == "lit_stroke_color"
    # 外观模式（自动配合字体/复用配色方案/自定义）与 auto 专属「相对字号」
    # 比例；「配色来源」下拉常驻但默认隐藏（role 档才显示）。
    appearance_data = [
        host._lit_appearance_mode_combo.itemData(index)
        for index in range(host._lit_appearance_mode_combo.count())
    ]
    assert appearance_data == ["auto", "role", "custom"]
    assert host._lit_role_combo.isHidden()
    assert host._lit_auto_size_ratio_spin.minimum() == 5
    assert host._lit_auto_size_ratio_spin.maximum() == 300


def test_effects_volume_builder_is_independent_and_compact(qapp) -> None:
    host = _Host()
    section = EffectsPropertyPageBuilder(host).make_volume_section()

    assert section.header.text() == "音量柱"
    assert section.header_switch is host._volume_enabled_switch
    assert list(host._volume_group_grids) == ["时序", "布局", "动画", "外观"]
    assert host._volume_column_count_spin.maximum() == 16
    assert host._volume_fill_btn.objectName() == "volume_fill_color"
    # 外观模式三档与 role 档专属的「配色来源」下拉（默认隐藏）。
    volume_appearance_data = [
        host._volume_appearance_mode_combo.itemData(index)
        for index in range(host._volume_appearance_mode_combo.count())
    ]
    assert volume_appearance_data == ["auto", "role", "custom"]
    assert host._volume_role_combo.isHidden()


def test_effects_role_source_combos_route_to_style_fields(qapp) -> None:
    host = _Host()
    builder = EffectsPropertyPageBuilder(host)
    builder.make_volume_section()
    builder.make_lit_section()

    # 下拉条目由宿主按方案表重建（_refresh_role_source_combo，重建期间
    # blockSignals）；这里同样屏蔽填充信号，builder 契约只验证字段路由。
    host._volume_role_combo.blockSignals(True)
    host._lit_role_combo.blockSignals(True)
    host._volume_role_combo.addItem("全局默认", "__global__")
    host._volume_role_combo.addItem("青", "青")
    host._lit_role_combo.addItem("全局默认", "__global__")
    host._lit_role_combo.addItem("标题", "标题")
    host._volume_role_combo.blockSignals(False)
    host._lit_role_combo.blockSignals(False)
    host._volume_role_combo.setCurrentIndex(1)
    host._lit_role_combo.setCurrentIndex(1)

    assert host.updates == [
        {"volume_role_name": "青"},
        {"lit_role_name": "标题"},
    ]


def test_effects_lit_builder_routes_transformed_values(qapp) -> None:
    host = _Host()
    builder = EffectsPropertyPageBuilder(host)
    builder.make_volume_section()
    builder.make_lit_section()

    host._volume_enabled_switch.setChecked(True)
    host._lit_enabled_switch.setChecked(True)
    host._volume_ratio_spin.setValue(3)
    host._volume_flash_duration_spin.setValue(25)
    host._lit_transition_mode_combo.setCurrentIndex(2)
    host._lit_shadow_check.setChecked(True)
    host._lit_appearance_mode_combo.setCurrentIndex(
        host._lit_appearance_mode_combo.findData("custom")
    )
    host._lit_auto_size_ratio_spin.setValue(80)

    assert host.updates == [
        {"volume_enabled": True},
        {"lit_enabled": True, "lit_style": "circle"},
        {"volume_ratio": 3.0},
        {"volume_flash_duration_ratio": 0.25},
        {"lit_transition_mode": "slide"},
        # 切到滑动且距离为 0 时自动补默认位移（否则视觉上等于淡入淡出）。
        {"lit_transition_distance": 24},
        {"lit_shadow": True},
        {"lit_appearance_mode": "custom"},
        {"lit_auto_size_ratio_pct": 80},
    ]
