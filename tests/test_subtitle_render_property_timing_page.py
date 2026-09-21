"""Focused construction contracts for the subtitle timing-property page."""

from __future__ import annotations

from krok_helper.subtitle_render.frontend.properties.pages.timing import (
    TimingPropertyPageBuilder,
)


class _Host:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self.sync_refreshes = 0

    def _update_style(self, **changes) -> None:
        self.updates.append(changes)

    def _sync_sync_each_page_enabled(self) -> None:
        self.sync_refreshes += 1


def test_timing_property_builder_preserves_section_and_control_contracts(qapp) -> None:
    host = _Host()
    tooltip_calls: list[tuple[object, int]] = []
    builder = TimingPropertyPageBuilder(
        host,
        tooltip_installer=lambda widget, *, show_delay: tooltip_calls.append(
            (widget, show_delay)
        ),
    )

    section = builder.make_section()

    assert section.header.text() == "时间"
    assert host._line_lead_spin.minimum() == 0
    assert host._line_lead_spin.maximum() == 10_000
    assert host._line_offset_spin.minimum() == -10_000
    assert host._section_gap_spin.maximum() == 60_000
    assert host._section_gap_spin.isHidden()
    assert host._section_ending_combo.count() == 2
    assert host._section_ending_combo.itemData(0) == "hold"
    assert host._section_ending_combo.itemData(1) == "clear"
    assert host._lane_gap_spin.maximum() == 5_000
    assert host._entry_anim_protect_spin.minimum() == 0
    assert host._entry_anim_protect_spin.maximum() == 10_000
    assert host._exit_anim_protect_spin.minimum() == 0
    assert host._exit_anim_protect_spin.maximum() == 10_000
    # 轴下拉初始只有「主字幕」；跟随开关默认隐藏交给宿主 scope 状态控制。
    assert host._timing_scope_combo.count() == 1
    assert host._timing_scope_combo.itemText(0) == "主字幕"
    assert host._timing_follow_check.text() == "跟随主字幕时间策略"
    # 受跟随开关只读管控的控件共 14 个（下拉与跟随开关不在此列）。
    assert len(host._timing_scope_managed_controls) == 14
    assert not host._sync_each_page_check.isEnabled()
    assert len(tooltip_calls) == 6
    assert {delay for _widget, delay in tooltip_calls} == {300}


def test_timing_property_builder_routes_controls_to_style_fields(qapp) -> None:
    host = _Host()
    builder = TimingPropertyPageBuilder(
        host,
        tooltip_installer=lambda *_args, **_kwargs: None,
    )
    builder.make_section()

    host._line_lead_spin.setValue(250)
    host._line_offset_spin.setValue(-120)
    host._entry_anim_protect_spin.setValue(600)
    host._exit_anim_protect_spin.setValue(140)
    host._section_ending_combo.setCurrentIndex(1)
    host._sync_entry_check.setChecked(True)
    host._ruby_main_reading_units_check.setChecked(True)
    host._allow_animation_overlap_check.setChecked(True)
    host._auto_fill_section_time_check.setChecked(True)

    assert host.updates == [
        {"line_lead_in_ms": 250},
        {"timing_offset_ms": -120},
        {"entry_anim_protect_ms": 600},
        {"exit_anim_protect_ms": 140},
        {"section_ending_mode": "clear"},
        {"sync_entry": True},
        {"ruby_main_progress_mode": "reading_units"},
        {"allow_entry_exit_animation_overlap": True},
        {"auto_fill_section_time": True},
    ]
    assert host.sync_refreshes == 1


def test_timing_overlap_builder_groups_switch_and_fallback_capsule(qapp) -> None:
    """「重叠设置」随时间卡片落位：控件契约与轴只读管控一并建立。"""

    host = _Host()
    tooltip_calls: list[tuple[object, int]] = []
    builder = TimingPropertyPageBuilder(
        host,
        tooltip_installer=lambda widget, *, show_delay: tooltip_calls.append(
            (widget, show_delay)
        ),
    )
    builder.make_section()

    section = builder.make_overlap_section()

    assert section.header.text() == "重叠设置"
    assert host._allow_inter_page_line_overlap_check.text() == "启用行间重叠"
    tooltip = host._allow_inter_page_line_overlap_check.toolTip()
    assert "「入场动画保护时间」" in tooltip
    assert "「出场动画保护时间」" in tooltip
    # tooltip 指明编辑对象为当前选中的字幕轴。
    assert "编辑对象为顶部选中的字幕轴" in tooltip
    # 胶囊（WorkspaceSwitcher）默认旧方案，两档与模型枚举一致，初始可用。
    capsule = host._overlap_fallback_switch
    assert capsule.currentRouteKey() == "lift"
    assert set(capsule._items) == {"lift", "displace"}
    assert capsule.isEnabled() is True
    capsule_tooltip = capsule.toolTip()
    assert "抬升避让" in capsule_tooltip
    assert "吃掉走字时长" in capsule_tooltip
    # 两个控件并入跟随态整体只读清单（14 个时间控件 + 重叠开关 + 胶囊）。
    assert len(host._timing_scope_managed_controls) == 16
    assert host._timing_scope_managed_controls[-2:] == (
        host._allow_inter_page_line_overlap_check,
        host._overlap_fallback_switch,
    )
    # make_section 的 6 个 + 重叠开关 1 个；最后一个必须是重叠开关本体。
    assert len(tooltip_calls) == 7
    assert tooltip_calls[-1][0] is host._allow_inter_page_line_overlap_check
    assert tooltip_calls[-1][1] == 300


def test_timing_overlap_builder_routes_switch_and_fallback_mode(qapp) -> None:
    host = _Host()
    builder = TimingPropertyPageBuilder(
        host,
        tooltip_installer=lambda *_args, **_kwargs: None,
    )
    builder.make_section()
    builder.make_overlap_section()

    host._overlap_fallback_switch._items["displace"].click()
    host._allow_inter_page_line_overlap_check.setChecked(True)

    assert host.updates == [
        {"overlap_fallback_mode": "displace"},
        {"allow_inter_page_line_overlap": True},
    ]
    # 勾选「启用行间重叠」后不存在跨页避让，收尾策略胶囊随之失效。
    assert host._overlap_fallback_switch.isEnabled() is False
    host._allow_inter_page_line_overlap_check.setChecked(False)
    assert host._overlap_fallback_switch.isEnabled() is True
