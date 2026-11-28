"""Focused construction contracts for the subtitle effects-property page."""

from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtWidgets import QLabel, QPushButton

from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    CanvasSliderSpinBox,
)
from krok_helper.subtitle_render.frontend.properties.controls.grouped_animation_combo import (
    ENTRY_ANIMATION_GROUPS,
    EXIT_ANIMATION_GROUPS,
    GroupedAnimationComboBox,
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

    # 与真实宿主同名的文件选择入口：builder 只做信号接线，桩里给空实现即可。
    def _choose_scanline_image(self) -> None:
        pass

    def _clear_scanline_image(self) -> None:
        pass

    def _choose_line_band_image(self) -> None:
        pass

    def _clear_line_band_image(self) -> None:
        pass

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

def test_grouped_animation_combos_categorize_and_filter_all_options(qapp) -> None:
    host = _Host()
    EffectsPropertyPageBuilder(host).make_animation_section()

    for combo, expected_groups in (
        (host._entry_anim_combo, ENTRY_ANIMATION_GROUPS),
        (host._exit_anim_combo, EXIT_ANIMATION_GROUPS),
        (host._section_head_anim_combo, ENTRY_ANIMATION_GROUPS),
        (host._section_tail_anim_combo, EXIT_ANIMATION_GROUPS),
    ):
        assert isinstance(combo, GroupedAnimationComboBox)
        assert combo.animation_groups == expected_groups

        menu = combo.build_grouped_menu()
        # All 25 animations appear in the categorized menu (none + 4 groups), plus 4 non-selectable headers.
        assert len(menu.visible_option_texts()) == 25
        assert menu.visible_row_count() == 25 + len(expected_groups)
        assert combo.findData("random_zoom") != -1
        assert combo.itemText(combo.findData("random_zoom")) == "随机缩放"

        # Filtering by each category shows only that group's animations and hides headers.
        for group in expected_groups:
            menu.set_category(group.key)
            assert menu.active_category == group.key
            assert len(menu.visible_option_texts()) == len(group.values)
            assert menu.visible_row_count() == len(group.values)
            if group.key == "char":
                assert "随机缩放" in menu.visible_option_texts()

        menu.set_category("all")
        assert len(menu.visible_option_texts()) == 25
        menu.close()
        menu.deleteLater()

    # Clicking an action whose grouped order differs from underlying item order
    # still routes to the exact model field value.
    entry_menu = host._entry_anim_combo.build_grouped_menu()
    glow_idx = host._entry_anim_combo.findData("glow_in")
    action = entry_menu.action_for_combo_index(glow_idx)
    assert action is not None
    action.trigger()
    assert host._entry_anim_combo.currentData() == "glow_in"
    assert host.updates[-1] == {"entry_anim": "glow_in"}
    entry_menu.close()
    entry_menu.deleteLater()
    qapp.processEvents()


def test_animation_preview_covers_all_entry_and_exit_options_with_sample_lyric(qapp) -> None:
    """全部 25 种入场与 25 种退场动画均具备元数据与跨相位可见变化，示例歌词固定。"""
    from krok_helper.subtitle_render.frontend.properties.controls.animation_preview import (
        ANIMATION_PREVIEW_LYRIC,
        AnimationPreviewCanvas,
        ENTRY_ANIMATION_META,
        EXIT_ANIMATION_META,
    )
    from krok_helper.subtitle_render.frontend.properties.pages.effects import (
        ENTRY_ANIMATION_OPTIONS,
        EXIT_ANIMATION_OPTIONS,
    )

    assert ANIMATION_PREVIEW_LYRIC == "渇いた 心で駆け抜ける"

    for direction, options, meta_table in (
        ("entry", ENTRY_ANIMATION_OPTIONS, ENTRY_ANIMATION_META),
        ("exit", EXIT_ANIMATION_OPTIONS, EXIT_ANIMATION_META),
    ):
        option_keys = {key for _label, key in options}
        assert option_keys <= set(meta_table)
        canvas = AnimationPreviewCanvas(direction, "fade")
        for _label, key in options:
            canvas.set_animation(direction, key)
            # Entry: phase 0.03 = empty, 0.34 = mid-entry, 0.76 = hold.
            # Exit:  phase 0.12 = hold,  0.52 = mid-exit,  0.85 = empty.
            phase_a, phase_b = (0.34, 0.76) if direction == "entry" else (0.12, 0.52)
            if key == "none":
                phase_a, phase_b = (0.03, 0.76) if direction == "entry" else (0.12, 0.85)
            canvas.set_phase(phase_a)
            img_a = canvas.grab().toImage()
            canvas.set_phase(phase_b)
            img_b = canvas.grab().toImage()
            assert not img_a.isNull() and not img_b.isNull()
            assert img_a != img_b, f"{direction}/{key} should animate visibly across phases"
        canvas.deleteLater()
        qapp.processEvents()


def test_grouped_animation_menu_hover_shows_switches_and_hides_preview(qapp) -> None:
    """悬停下拉列表项弹出无焦点动画预览卡片，切换/分类/关闭菜单时同步联动并清理。"""
    from PyQt6.QtCore import Qt

    host = _Host()
    EffectsPropertyPageBuilder(host).make_animation_section()

    for combo, expected_dir, sample_key_a, sample_key_b in (
        (host._entry_anim_combo, "entry", "random_zoom", "petal"),
        (host._exit_anim_combo, "exit", "scatter_out", "flame"),
    ):
        menu = combo.build_grouped_menu()
        try:
            assert menu.direction == expected_dir
            menu.show()
            qapp.processEvents()

            row_header = next(
                row for row in range(menu.view.count()) if menu.row_animation_key(row) == ""
            )
            row_a = next(
                row for row in range(menu.view.count()) if menu.row_animation_key(row) == sample_key_a
            )
            row_b = next(
                row for row in range(menu.view.count()) if menu.row_animation_key(row) == sample_key_b
            )

            # Group header rows do not open a preview popover.
            assert menu.preview_for_row(row_header) is None
            assert menu.preview_popover() is None

            # Hovering an animation row opens the parentless, mouse-transparent popover.
            pop = menu.preview_for_row(row_a)
            qapp.processEvents()
            assert pop is not None
            assert pop.parent() is None
            assert pop.isVisible()
            assert pop.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            assert pop.canvas.direction == expected_dir
            assert pop.canvas.anim_key == sample_key_a
            assert pop.canvas.is_animating()

            # Moving to another option updates the same popover immediately.
            pop_b = menu.preview_for_row(row_b)
            qapp.processEvents()
            assert pop_b is pop
            assert pop.canvas.anim_key == sample_key_b
            assert pop.isVisible()

            # Switching category hides the active preview.
            menu.set_category("particle")
            qapp.processEvents()
            assert not pop.isVisible()
            assert not pop.canvas.is_animating()

            # Re-showing then closing the menu hides and stops the animation cleanly.
            menu.set_category("all")
            menu.preview_for_row(row_a)
            qapp.processEvents()
            assert pop.isVisible()
            menu.close()
            qapp.processEvents()
            assert not pop.isVisible()
            assert not pop.canvas.is_animating()
        finally:
            menu.close()
            menu.deleteLater()
            qapp.processEvents()


def test_grouped_animation_menu_hides_preview_when_cursor_moves_away(qapp, monkeypatch) -> None:
    """即使 QMenu 弹窗吞掉 viewport Leave 事件，鼠标移出列表或移到分组头也会立即收起预览。"""
    from PyQt6.QtCore import QPoint
    from krok_helper.subtitle_render.frontend.properties.controls import (
        grouped_animation_combo as gac,
    )

    host = _Host()
    EffectsPropertyPageBuilder(host).make_animation_section()
    menu = host._entry_anim_combo.build_grouped_menu()
    try:
        menu.show()
        qapp.processEvents()

        row_fade = next(
            row for row in range(menu.view.count()) if menu.row_animation_key(row) == "fade"
        )
        row_header = next(
            row for row in range(menu.view.count()) if menu.row_animation_key(row) == ""
        )
        fade_center = menu._row_global_rect(row_fade).center()
        header_center = menu._row_global_rect(row_header).center()
        outside_pos = QPoint(menu.x() - 200, menu.y() - 200)

        # Cursor over fade row -> preview opens and tracks cursor.
        monkeypatch.setattr(gac.QCursor, "pos", staticmethod(lambda: fade_center))
        pop = menu.preview_for_row(row_fade, track_cursor=True)
        qapp.processEvents()
        assert pop is not None and pop.isVisible()
        assert menu._preview_cursor_timer.isActive()

        # Cursor moves outside menu without delivering Leave -> cursor sync hides preview.
        monkeypatch.setattr(gac.QCursor, "pos", staticmethod(lambda: outside_pos))
        menu._sync_preview_with_cursor()
        qapp.processEvents()
        assert not pop.isVisible()
        assert not pop.canvas.is_animating()
        assert not menu._preview_cursor_timer.isActive()

        # Pending preview canceled if cursor already moved onto a non-animation header.
        menu._pending_preview_row = row_fade
        monkeypatch.setattr(gac.QCursor, "pos", staticmethod(lambda: header_center))
        menu._show_pending_preview()
        qapp.processEvents()
        assert not pop.isVisible()
    finally:
        menu.close()
        menu.deleteLater()
        qapp.processEvents()


def test_zoom_pulse_curve_combo_hover_previews_all_levels_and_hides_on_leave(
    qapp, monkeypatch
) -> None:
    """整字放大速度等级（0~5级）下拉项均支持悬停动画预览，且移出即收起。"""
    import pytest
    from PyQt6.QtCore import QPoint, Qt
    from krok_helper.subtitle_render.frontend.properties.controls import (
        animation_preview as ap,
    )

    host = _Host()
    EffectsPropertyPageBuilder(host).make_animation_section()
    combo = host._zoom_pulse_curve_combo
    assert isinstance(combo, ap.ZoomPulseCurveComboBox)

    # Shared singing timing matches the timed God knows... line 1 (with multi-mora checkpoints & breath pause).
    assert "".join(spec.char for spec in ap.ANIMATION_PREVIEW_SING_CHARS) == ap.ANIMATION_PREVIEW_LYRIC
    assert ap.ANIMATION_PREVIEW_SING_CHARS[0].checkpoints_ms == (0, 468)
    assert ap.ANIMATION_PREVIEW_SING_CHARS[0].end_ms == 710
    assert ap.ANIMATION_PREVIEW_SING_CHARS[4].checkpoints_ms == (2607, 2839, 3067)
    assert ap.ANIMATION_PREVIEW_SING_CHARS[4].end_ms == 3273
    assert ap.ANIMATION_PREVIEW_SING_DURATION_MS == 5561
    assert ap.sample_sing_wipe_progress(468, ap.ANIMATION_PREVIEW_SING_CHARS[0]) == pytest.approx(0.5)
    assert ap.sample_sing_wipe_progress(2839, ap.ANIMATION_PREVIEW_SING_CHARS[4]) == pytest.approx(1.0 / 3.0)
    # Per-kana ruby timing matches temp.sug (渇=かわ, 心=こころ, 駆=か, 抜=ぬ).
    assert ap.sample_ruby_kana_progress(234, ap.ANIMATION_PREVIEW_SING_CHARS[0], 0, 2) == pytest.approx(0.5)
    assert ap.sample_ruby_kana_progress(234, ap.ANIMATION_PREVIEW_SING_CHARS[0], 1, 2) == pytest.approx(0.0)
    assert ap.sample_ruby_kana_progress(589, ap.ANIMATION_PREVIEW_SING_CHARS[0], 0, 2) == pytest.approx(1.0)
    assert ap.sample_ruby_kana_progress(589, ap.ANIMATION_PREVIEW_SING_CHARS[0], 1, 2) == pytest.approx(0.5)
    assert [
        (spec.char, spec.ruby)
        for spec in ap.ANIMATION_PREVIEW_SING_CHARS
        if spec.ruby
    ] == [("渇", "かわ"), ("心", "こころ"), ("駆", "か"), ("抜", "ぬ")]

    # All 6 curve levels render distinct frames across phases and visibly differ between linear & high order.
    canvas = ap.ZoomPulseCurvePreviewCanvas(0, 140)
    level_imgs = {}
    for level in range(6):
        canvas.set_curve(level, peak_pct=140)
        canvas.set_phase(0.01)
        img_idle = canvas.grab().toImage()
        canvas.set_phase(0.10)  # Inside the 793ms held 「渇」 syllable where 0 vs 5 diverges sharply.
        img_active = canvas.grab().toImage()
        assert not img_idle.isNull() and not img_active.isNull()
        assert img_idle != img_active
        level_imgs[level] = img_active
    assert level_imgs[0] != level_imgs[5]
    canvas.deleteLater()

    # Hovering menu items shows the preview with live peak_pct and hides when cursor leaves.
    host._zoom_pulse_peak_control.setValue(150)
    menu = combo.build_preview_menu()
    try:
        menu.show()
        qapp.processEvents()
        assert [menu.row_curve_level(i) for i in range(6)] == [0, 1, 2, 3, 4, 5]

        row3_center = menu._row_global_rect(3).center()
        monkeypatch.setattr(ap.QCursor, "pos", staticmethod(lambda: row3_center))
        pop = menu.preview_for_row(3, track_cursor=True)
        qapp.processEvents()
        assert pop is not None
        assert pop.parent() is None
        assert pop.isVisible()
        assert pop.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        assert pop.canvas.curve_level == 3
        assert pop.canvas.peak_ratio == 1.5
        assert pop.canvas.is_animating()

        # Switch to level 5 immediately.
        pop5 = menu.preview_for_row(5, track_cursor=True)
        qapp.processEvents()
        assert pop5 is pop
        assert pop.canvas.curve_level == 5

        # Applying a real global default Style updates the preview lyric colors (before & after).
        from krok_helper.subtitle_render.domain.models import Style
        from krok_helper.subtitle_render.domain.paint import KaraokeColors, KaraokeColorState, PaintFill

        pop.canvas.set_phase(0.36)
        default_img = pop.canvas.grab().toImage()
        host._style = Style(
            karaoke_colors=KaraokeColors(
                before=KaraokeColorState(
                    text=PaintFill(mode="gradient_vertical", start_color="#FFF7AD", end_color="#FFA9F9"),
                    stroke=PaintFill(color="#1E1B4B"),
                ),
                after=KaraokeColorState(
                    text=PaintFill(mode="gradient_vertical", start_color="#38BDF8", end_color="#34D399"),
                    stroke=PaintFill(color="#0F172A"),
                ),
            ),
            stroke_width_px=6,
            stroke2_enabled=True,
            stroke2_width_px=4,
            decoration_kind="glow",
            glow_after_radius_px=10,
        )
        menu.preview_for_row(5, track_cursor=True)
        pop.canvas.set_phase(0.36)
        qapp.processEvents()
        styled_img = pop.canvas.grab().toImage()
        assert styled_img != default_img

        # Moving cursor outside hides preview even without viewport Leave event.
        outside = QPoint(menu.x() - 200, menu.y() - 200)
        monkeypatch.setattr(ap.QCursor, "pos", staticmethod(lambda: outside))
        menu._sync_preview_with_cursor()
        qapp.processEvents()
        assert not pop.isVisible()
        assert not pop.canvas.is_animating()
        assert not menu._preview_cursor_timer.isActive()
    finally:
        menu.close()
        menu.deleteLater()
        qapp.processEvents()


def test_effects_animation_builder_preserves_options_and_layout(qapp) -> None:
    host = _Host()
    section = EffectsPropertyPageBuilder(host).make_animation_section()

    assert section.header.text() == "入退场/唱字动画"
    assert host._animation_grid._max_columns == 2
    assert host._entry_anim_combo.count() == 25
    assert host._entry_anim_combo.itemData(5) == "char_drip"
    assert host._entry_anim_combo.itemData(9) == "tracking_in"
    assert host._exit_anim_combo.count() == 25
    for combo in (host._entry_anim_combo, host._exit_anim_combo):
        assert combo.itemText(combo.findData("noise")) == "噪点溶解"
        assert combo.findData("noise") == combo.findData("utopia") + 1
        assert [combo.itemData(i) for i in range(21, 25)] == [
            "petal", "snow", "snow_solid", "random_zoom"
        ]
    assert host._exit_anim_combo.itemData(2) == "slide_out"
    assert host._exit_anim_combo.itemData(9) == "scatter_out"
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
    for selector in (host._fx_target_pill, host._scanline_scope_pill):
        assert selector.width() <= 300
        for item in selector.items.values():
            assert item.height() == 28
            assert item.icon().isNull()
    # 扫字线形态：普通高亮/烈焰棒/水泡/自定义图片（独立形态替换高亮带）。
    assert [
        host._scanline_shape_combo.itemData(index)
        for index in range(host._scanline_shape_combo.count())
    ] == ["band", "flame", "bubble", "image"]
    assert [
        host._scanline_shape_combo.itemText(index)
        for index in range(host._scanline_shape_combo.count())
    ] == ["普通高亮", "烈焰棒", "水泡", "自定义图片"]
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
    # 整字放大倍数：紧随速度等级入网，与粒子速度同款 slider+输入框复合
    # 控件，100~200%，默认 125（历史 1.25×）。
    assert isinstance(host._zoom_pulse_peak_control, CanvasSliderSpinBox)
    assert host._zoom_pulse_peak_control.input_range() == (100, 200)
    assert host._zoom_pulse_peak_control.slider_range() == (100, 200)
    assert host._zoom_pulse_peak_control.value() == 125
    assert host._zoom_pulse_peak_control.toolTip()
    peak_labels = [label.text() for label in section.findChildren(QLabel)]
    assert "整字放大倍数" in peak_labels
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
    assert host._section_head_anim_combo.count() == 25
    assert host._section_tail_anim_combo.count() == 25
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


def test_core_effect_combos_hover_previews_render_and_hide_on_leave(
    qapp, monkeypatch
) -> None:
    """特效页全部 7 个核心视觉效果下拉框均支持悬停动态演示，且移出即收起。"""
    from PyQt6.QtCore import QPoint, Qt
    from krok_helper.subtitle_render.domain.models import Style
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColors,
        KaraokeColorState,
        PaintFill,
    )
    from krok_helper.subtitle_render.frontend.properties.controls import (
        animation_preview as ap,
        effect_preview as ep,
    )

    host = _Host()
    builder = EffectsPropertyPageBuilder(host)
    builder.make_animation_section()
    builder.make_lit_section()

    combos = [
        (host._karaoke_anim_combo, "karaoke_anim", ep.KARAOKE_ANIM_META, False),
        (host._reverse_karaoke_anim_combo, "reverse_karaoke_anim", ep.REVERSE_KARAOKE_ANIM_META, False),
        (host._scanline_shape_combo, "scanline_shape", ep.SCANLINE_SHAPE_META, False),
        (host._scanline_effect_combo, "scanline_effect", ep.SCANLINE_EFFECT_META, False),
        (host._fx_sing_combo, "sing_fx", ep.SING_FX_META, False),
        (host._lit_style_combo, "lit_style", ep.LIT_STYLE_META, True),
        (host._lit_transition_mode_combo, "lit_transition_mode", ep.LIT_TRANSITION_META, True),
    ]

    # Verify every single option across all 7 categories has metadata & renders distinct animated frames.
    sing_canvas = ep.SingingEffectPreviewCanvas()
    lit_canvas = ep.LitIndicatorPreviewCanvas()
    try:
        for combo, category, meta_table, is_lit in combos:
            assert isinstance(combo, ap.HoverPreviewComboBox)
            for idx in range(combo.count()):
                key = str(combo.itemData(idx))
                assert key in meta_table, f"Missing preview meta for {category}:{key}"
                if is_lit:
                    lit_canvas.set_effect(category, key, style=host._style)
                    lit_canvas.set_phase(0.02)
                    img_a = lit_canvas.grab().toImage()
                    lit_canvas.set_phase(0.36)
                    img_b = lit_canvas.grab().toImage()
                else:
                    sing_canvas.set_effect(category, key, style=host._style)
                    sing_canvas.set_phase(0.01)
                    img_a = sing_canvas.grab().toImage()
                    sing_canvas.set_phase(0.24)
                    img_b = sing_canvas.grab().toImage()
                assert not img_a.isNull() and not img_b.isNull()
                assert img_a != img_b, f"Static preview for {category}:{key}"
    finally:
        sing_canvas.deleteLater()
        lit_canvas.deleteLater()

    # Verify live menu hover, global default color scheme sync, inherit tracking & cursor-leave cleanup.
    host._karaoke_anim_combo.setCurrentIndex(host._karaoke_anim_combo.findData("zoom_pulse"))
    for combo, category, _meta_table, is_lit in combos:
        menu = combo.build_preview_menu()
        try:
            menu.show()
            qapp.processEvents()
            target_row = min(1, combo.count() - 1)
            row_center = menu._row_global_rect(target_row).center()
            monkeypatch.setattr(ap.QCursor, "pos", staticmethod(lambda pt=row_center: pt))
            pop = menu.preview_for_row(target_row, track_cursor=True)
            qapp.processEvents()
            assert pop is not None
            assert pop.parent() is None
            assert pop.isVisible()
            assert pop.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            assert pop.canvas.is_animating()
            assert pop.canvas.category == category
            assert pop.canvas.effect_key == str(combo.itemData(target_row))

            if category == "reverse_karaoke_anim":
                # Row 0 is "inherit" and should reflect current _karaoke_anim_combo ("zoom_pulse").
                row0_center = menu._row_global_rect(0).center()
                monkeypatch.setattr(ap.QCursor, "pos", staticmethod(lambda pt=row0_center: pt))
                menu.preview_for_row(0, track_cursor=True)
                assert pop.canvas._effective_karaoke_mode() == "zoom_pulse"

            if category == "karaoke_anim":
                pop.canvas.set_phase(0.26)
                before_img = pop.canvas.grab().toImage()
                host._style = Style(
                    karaoke_colors=KaraokeColors(
                        before=KaraokeColorState(
                            text=PaintFill(mode="gradient_vertical", start_color="#FEF08A", end_color="#F472B6"),
                            stroke=PaintFill(color="#1E1B4B"),
                        ),
                        after=KaraokeColorState(
                            text=PaintFill(mode="gradient_vertical", start_color="#38BDF8", end_color="#22C55E"),
                            stroke=PaintFill(color="#0F172A"),
                        ),
                    ),
                    stroke_width_px=6,
                )
                menu.preview_for_row(target_row, track_cursor=True)
                pop.canvas.set_phase(0.26)
                qapp.processEvents()
                after_img = pop.canvas.grab().toImage()
                assert after_img != before_img

            outside = QPoint(menu.x() - 220, menu.y() - 220)
            monkeypatch.setattr(ap.QCursor, "pos", staticmethod(lambda pt=outside: pt))
            menu._sync_preview_with_cursor()
            qapp.processEvents()
            assert not pop.isVisible()
            assert not pop.canvas.is_animating()
            assert not menu._preview_cursor_timer.isActive()
        finally:
            menu.close()
            menu.deleteLater()
            qapp.processEvents()


def test_stroke_flash_checkbox_hover_preview_and_tooltip_suppression(qapp) -> None:
    """唱字闪光复选框悬停时弹出走字闪白演示卡片，并屏蔽重复文本 tooltip。"""
    from PyQt6.QtCore import QEvent, Qt
    from krok_helper.subtitle_render.frontend.properties.controls import (
        animation_preview as ap,
        effect_preview as ep,
    )

    host = _Host()
    section = EffectsPropertyPageBuilder(host).make_animation_section()
    try:
        section.show()
        qapp.processEvents()

        hover_filter = host._stroke_flash_hover_filter
        assert isinstance(hover_filter, ap.WidgetPreviewHoverFilter)

        # Native tooltip event is absorbed so it never doubles up behind the animated card.
        tooltip_event = QEvent(QEvent.Type.ToolTip)
        assert hover_filter.eventFilter(host._stroke_flash_check, tooltip_event) is True

        hover_filter.show_popover()
        qapp.processEvents()
        pop = hover_filter.popover()
        assert pop is not None
        assert pop.parent() is None
        assert pop.isVisible()
        assert pop.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        assert isinstance(pop.canvas, ep.SingingEffectPreviewCanvas)
        assert pop.canvas.category == "stroke_flash"
        assert pop.canvas.is_animating()

        # Compare pre-sing idle frame vs flash-hit frame right after char 0 start.
        pop.canvas.set_phase(0.01)
        idle_img = pop.canvas.grab().toImage()
        pop.canvas.set_phase(0.05)  # ~320 ms (100 ms into char 0 -> active white stroke flash)
        flash_img = pop.canvas.grab().toImage()
        assert idle_img != flash_img

        hover_filter.hide_popover()
        qapp.processEvents()
        assert not pop.isVisible()
        assert not pop.canvas.is_animating()
    finally:
        section.close()
        section.deleteLater()
        qapp.processEvents()
