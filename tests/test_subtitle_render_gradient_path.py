from __future__ import annotations

import json
import math
from dataclasses import replace

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QDialog

from krok_helper.subtitle_render.domain.gradient_path import (
    anchor_route, anchored_samples, bake_path, disk_point, hue_route, path_samples, replace_path_stops, rgba,
    smooth_points,
)
from krok_helper.subtitle_render.domain.paint import GradientPath, PaintFill
from krok_helper.subtitle_render.serialization.paint import paint_fill_from_dict, paint_fill_to_dict


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def _fill(start="#9383CA", end="#F6E9BF"):
    return PaintFill(mode="gradient_horizontal", start_color=start, end_color=end,
                     gradient_stops=[(0, start), (100, end)])


@pytest.mark.parametrize("long", [False, True])
@pytest.mark.parametrize("ends", [("#9383CA", "#F6E9BF"), ("#FF0000", "#00FFFF"),
                                  ("#00FFFF00", "#804080FF"), ("#000000", "#FFFFFF")])
def test_baking_preserves_exact_endpoints_and_approximates_entire_color_route(long, ends):
    points = hue_route(*ends, long=long)
    route = GradientPath(points)
    stops = bake_path(route, *ends)
    assert rgba(stops[0][1]) == rgba(ends[0])
    assert rgba(stops[-1][1]) == rgba(ends[1])
    assert len(stops) <= 256
    samples = path_samples(points, *ends)
    index = 0
    for i, color in enumerate(samples):
        p = i / (len(samples) - 1) * 100
        while index < len(stops) - 2 and p > stops[index + 1][0]:
            index += 1
        a, b = stops[index:index + 2]
        t = (p - a[0]) / (b[0] - a[0])
        assert max(abs(c - (x + (y - x) * t)) for c, x, y in zip(color, rgba(a[1]), rgba(b[1]))) < 2.01


def test_short_and_long_routes_take_different_colors_without_gray_midpoint():
    short = path_samples(hue_route("#FF0000", "#00FFFF"), "#FF0000", "#00FFFF")[512]
    long = path_samples(hue_route("#FF0000", "#00FFFF", long=True), "#FF0000", "#00FFFF")[512]
    assert short != long
    assert max(short[:3]) - min(short[:3]) > 250
    assert max(long[:3]) - min(long[:3]) > 250


def test_path_sampling_ignores_mouse_pauses_and_event_density():
    sparse = [(1., 0.), (0., 1.)]
    dense = [(1., 0.)] * 20 + [(1 - i / 100, i / 100) for i in range(101)] + [(0., 1.)] * 30
    assert path_samples(sparse, "#FF0000", "#00FF00") == path_samples(dense, "#FF0000", "#00FF00")


def test_curve_smoothing_keeps_endpoints_and_stays_inside_disk():
    points = [(1., 0.), (.6, .1), (.3, .7), (0., 1.)]
    smoothed = smooth_points(points)
    assert smoothed[0] == points[0] and smoothed[-1] == points[-1]
    assert all(math.hypot(x, y) <= 1 for x, y in smoothed)


def test_segment_replacement_keeps_other_colors_and_fractional_positions():
    stops = [(0, "#123456"), (12.125, "#FF0000"), (65.75, "#00FFFF"), (88.5, "#002244"), (100, "#FFFFFF")]
    route = GradientPath(hue_route(stops[1][1], stops[2][1]), 12.125, 65.75)
    baked = replace_path_stops(stops, route)
    assert baked[0] == stops[0] and baked[-2:] == stops[-2:]
    assert (12.125, "#FF0000") in baked and (65.75, "#00FFFF") in baked
    assert len(baked) > len(stops)


def test_segment_preserves_adjacent_sides_of_duplicate_position_stops():
    stops = [(0, "#000000"), (20, "#112233"), (20, "#FF0000"),
             (80, "#00FFFF"), (80, "#445566"), (100, "#FFFFFF")]
    route = GradientPath(hue_route("#FF0000", "#00FFFF"), 20, 80)
    baked = replace_path_stops(stops, route)
    assert baked[:3] == stops[:3]
    assert baked[-3:] == stops[-3:]


def test_path_round_trip_keeps_editing_route_and_baked_stops(tmp_path):
    from krok_helper.subtitle_render.domain.models import Style, style_from_dict, style_to_dict
    from krok_helper.subtitle_render.domain.paint import KaraokeColors
    from krok_helper.subtitle_render.project.store import save_render_project, load_render_project

    fill = _fill()
    route = GradientPath(hue_route(fill.start_color, fill.end_color))
    fill = replace(fill, gradient_path=route, gradient_stops=replace_path_stops(fill.gradient_stops, route))
    colors = KaraokeColors()
    colors.after.text = fill
    project = tmp_path / "勾勒渐变.yurika"
    save_render_project(project, {"style": style_to_dict(Style(karaoke_colors=colors))})
    restored = style_from_dict(load_render_project(project)["style"])
    assert restored.karaoke_colors.after.text.gradient_path == route
    assert restored.karaoke_colors.after.text.gradient_stops == fill.gradient_stops
    assert paint_fill_from_dict(json.loads(json.dumps(paint_fill_to_dict(fill)))).gradient_path == route
    assert "gradient_path" not in paint_fill_to_dict(_fill())


@pytest.mark.parametrize("metadata", [None, {}, {"version": 2},
    {"version": 1, "points": [[0, 0], [float("nan"), 0]], "start_pct": 0, "end_pct": 100},
    {"version": 1, "points": [[0, 0], [2, 0]], "start_pct": 0, "end_pct": 100},
    {"version": 1, "points": [[0, 0]] * 1025, "start_pct": 0, "end_pct": 100},
    {"version": 1, "points": [[0, 0], [1, 0]], "start_pct": 100, "end_pct": 0}])
def test_invalid_path_metadata_does_not_lose_rendered_stops(metadata):
    payload = paint_fill_to_dict(_fill()) | {"gradient_path": metadata}
    restored = paint_fill_from_dict(payload)
    assert restored.gradient_path is None
    assert restored.gradient_stops == _fill().gradient_stops


def test_manual_changes_discard_stale_route_but_orientation_keeps_it():
    from krok_helper.subtitle_render.frontend.properties.property_panel import _replace_fill
    fill = replace(_fill(), gradient_path=GradientPath(hue_route("#9383CA", "#F6E9BF")))
    assert _replace_fill(fill, mode="gradient_vertical").gradient_path == fill.gradient_path
    for changes in ({"start_color": "#112233"}, {"gradient_stops": [(0, "#000000"), (100, "#FFFFFF")]},
                    {"color": "#888888"}):
        assert _replace_fill(fill, **changes).gradient_path is None


def test_render_cache_signature_uses_baked_colors_and_ignores_editing_points():
    from krok_helper.subtitle_render.engine.value_signature import value_signature
    fill = _fill()
    route = GradientPath(hue_route(fill.start_color, fill.end_color))
    editable = replace(fill, gradient_path=route)
    assert value_signature(fill) == value_signature(editable)
    editable.gradient_stops = replace_path_stops(fill.gradient_stops, route)
    assert value_signature(fill) != value_signature(editable)


def _mouse(wheel, kind, point, *, pressed=True):
    event = QMouseEvent(kind, wheel.to_screen(point), Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseMove else Qt.MouseButton.NoButton,
                        Qt.MouseButton.LeftButton if pressed else Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(wheel, event)


def test_drawing_uses_new_endpoints_and_adjusting_keeps_them_while_clamping(qapp):
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    dialog = GradientPathDialog(_fill("#FF0000", "#00FFFF"))
    dialog.resize(500, 680)
    dialog.show()
    qapp.processEvents()
    wheel = dialog.wheel
    old_start = wheel.points[0]
    _mouse(wheel, QEvent.Type.MouseButtonPress, (.9, .1))
    for point in ((.8, .5), (0., 2.), (-.8, .5), (-.9, .1)):
        _mouse(wheel, QEvent.Type.MouseMove, point)
    _mouse(wheel, QEvent.Type.MouseButtonRelease, (-1., 0.), pressed=False)
    dialog._refresh()
    assert wheel.points[0] == pytest.approx((.9, .1))
    assert wheel.points[0] != old_start
    assert wheel.points[-1] == pytest.approx((-1., 0.))
    endpoints = wheel.points[0], wheel.points[-1]
    assert all(math.hypot(*p) <= 1.000001 for p in wheel.points)
    assert dialog.apply_button.isEnabled()
    assert len(dialog.stops()) > 2
    dialog.mode_switch.setCurrentItem("adjust")
    index = len(wheel.points) // 2
    original = wheel.points[index]
    _mouse(wheel, QEvent.Type.MouseButtonPress, original)
    _mouse(wheel, QEvent.Type.MouseMove, (.2, .3))
    _mouse(wheel, QEvent.Type.MouseButtonRelease, (.2, .3), pressed=False)
    assert (.2, .3) == pytest.approx(wheel.points[index])
    assert (wheel.points[0], wheel.points[-1]) == endpoints
    dialog.reject()


def test_dialog_short_long_reverse_undo_and_cancel_do_not_modify_original(qapp):
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    fill = _fill()
    original = paint_fill_to_dict(fill)
    dialog = GradientPathDialog(fill)
    assert not dialog.apply_button.isEnabled()
    dialog.short_button.click()
    short = dialog.stops()
    dialog.long_button.click()
    assert dialog.stops() != short
    dialog.undo_button.click()
    assert dialog.stops() == short
    dialog.reverse_button.click()
    reversed_stops = dialog.stops()
    assert reversed_stops[0][1] == fill.end_color
    assert reversed_stops[-1][1] == fill.start_color
    dialog.reject()
    assert paint_fill_to_dict(fill) == original


def test_reopening_saved_route_restores_whole_gradient_without_rebaking(qapp):
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    stops = [(0, "#000000"), (20, "#FF0000"), (80, "#00FFFF"), (100, "#FFFFFF")]
    fill = replace(_fill(), gradient_stops=stops)
    dialog = GradientPathDialog(fill)
    dialog.long_button.click()
    baked, route = dialog.stops(), dialog.route()
    dialog.reject()
    restored = paint_fill_from_dict(json.loads(json.dumps(paint_fill_to_dict(replace(fill, gradient_stops=baked, gradient_path=route)))))
    reopened = GradientPathDialog(restored)
    assert reopened.route() == route
    assert reopened.stops() == baked
    assert not reopened.apply_button.isEnabled()
    reopened.reject()


@pytest.mark.parametrize("size", [(480, 660), (480, 480), (480, 432)])
def test_small_dialog_shrinks_wheel_and_keeps_all_controls_visible_without_scrolling(qapp, size):
    from PyQt6.QtWidgets import QScrollArea
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    dialog = GradientPathDialog(replace(_fill(), gradient_stops=[
        (0, "#112233"), (30, "#FF0000"), (70, "#00FFFF"), (100, "#AABBCC")]))
    dialog.resize(*size)
    dialog.long_button.click()
    dialog.show()
    qapp.processEvents()
    assert dialog.findChild(QScrollArea) is None
    for widget in (dialog.mode_switch, dialog.wheel, dialog.short_button, dialog.long_button,
                   dialog.undo_button, dialog.reverse_button, dialog.preview, dialog.apply_button,
                   dialog.close_button, dialog.point_editor.hex_edit, dialog.point_editor.point_combo):
        assert widget.visibleRegion().boundingRect().height() == widget.height(), widget.accessibleName()
    assert dialog.height() <= size[1]
    dialog.reject()


def test_panel_applies_without_closing_persists_and_manual_edit_becomes_authoritative(qapp):
    from krok_helper.subtitle_render.frontend.properties.property_panel import PropertyPanel
    from krok_helper.subtitle_render.domain.models import style_from_dict, style_to_dict

    panel = PropertyPanel()
    panel._fill_mode_combo.setCurrentIndex(panel._fill_mode_combo.findData("gradient_horizontal"))
    emitted = []
    panel.styleChanged.connect(emitted.append)
    panel._gradient_path_btn.click()
    dialog = panel._gradient_path_dialog
    dialog.long_button.click()
    dialog.apply_button.click()
    assert dialog.isVisible()
    assert not dialog.apply_button.isEnabled()
    assert len(emitted) == 1
    fill = emitted[-1].karaoke_colors.after.text
    assert fill.gradient_path is not None
    assert len(fill.gradient_stops) > 2
    restored = style_from_dict(style_to_dict(emitted[-1])).karaoke_colors.after.text
    assert restored.gradient_path == fill.gradient_path
    assert restored.gradient_stops == fill.gradient_stops
    panel._gradient_editor.set_selected_color("#123456")
    assert emitted[-1].karaoke_colors.after.text.gradient_path is None
    dialog.close()


def test_panel_close_unapplied_draft_does_not_emit_or_mutate(qapp):
    from krok_helper.subtitle_render.frontend.properties.property_panel import PropertyPanel
    panel = PropertyPanel()
    panel._fill_mode_combo.setCurrentIndex(panel._fill_mode_combo.findData("gradient_vertical"))
    emitted = []
    panel.styleChanged.connect(emitted.append)
    panel._gradient_path_btn.click()
    dialog = panel._gradient_path_dialog
    dialog.short_button.click()
    dialog.close_button.click()
    assert not emitted
    assert panel._current_paint_fill().gradient_path is None
    assert panel._gradient_path_dialog is None


def test_modeless_dialog_cannot_apply_to_a_different_color_layer(qapp):
    from krok_helper.subtitle_render.frontend.properties import property_panel as pp

    panel = pp.PropertyPanel()
    panel._fill_mode_combo.setCurrentIndex(panel._fill_mode_combo.findData("gradient_horizontal"))
    panel._gradient_path_btn.click()
    dialog = panel._gradient_path_dialog
    dialog.short_button.click()
    panel._color_layer_combo.setCurrentIndex(panel._color_layer_combo.findData("stroke"))
    dialog.apply_button.click()
    assert "已变化" in dialog.status_label.text()
    assert panel._current_karaoke_colors().after.text.gradient_path is None
    assert panel._current_karaoke_colors().after.stroke.gradient_path is None
    dialog.close()


def test_modes_use_workspace_switcher_and_all_gradients_edit_whole_range(qapp):
    from qfluentwidgets import ComboBox
    from krok_helper.workspace_switcher import WorkspaceSwitcher
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    whole = GradientPathDialog(_fill())
    assert isinstance(whole.mode_switch, WorkspaceSwitcher)
    assert [item.accessibleName() for item in whole.mode_switch._items.values()] == ["调整模式", "绘制模式"]
    assert whole.findChildren(ComboBox) == [whole.point_editor.point_combo]
    whole.mode_switch.setCurrentItem("adjust")
    assert whole.wheel.adjusting
    whole.mode_switch.setCurrentItem("draw")
    assert not whole.wheel.adjusting
    segmented = GradientPathDialog(replace(_fill(), gradient_stops=[
        (0, "#FF0000"), (50, "#00FF00"), (100, "#0000FF")]))
    assert segmented.findChildren(ComboBox) == [segmented.point_editor.point_combo]
    assert (segmented.route().start_pct, segmented.route().end_pct) == (0, 100)
    whole.reject()
    segmented.reject()


def test_hex_typing_preserves_alpha_and_commits_exact_rgb_without_replacing_input(qapp):
    from PyQt6.QtTest import QTest
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    fill = _fill("#804080C0", "#F6E9BF")
    dialog = GradientPathDialog(fill)
    dialog.show()
    qapp.processEvents()
    entry = dialog.point_editor.hex_edit
    entry.setFocus()
    entry.selectAll()
    QTest.keyClicks(entry, "123456")
    assert entry.text() == "123456"
    assert rgba(dialog.stops()[0][1]) == (0x12, 0x34, 0x56, 0x80)
    QTest.keyClick(entry, Qt.Key.Key_Return)
    assert entry.text() == "#80123456"
    assert dialog.stops()[-1][1] == fill.end_color
    assert fill.gradient_stops[0][1] == "#804080C0"
    dialog.reject()


def test_invalid_hex_keeps_draft_but_blocks_apply_until_repaired(qapp):
    from PyQt6.QtTest import QTest
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    dialog = GradientPathDialog(_fill())
    dialog.long_button.click()
    saved = dialog.stops()
    dialog.show()
    qapp.processEvents()
    dialog._select_anchor(1)
    entry = dialog.point_editor.hex_edit
    entry.setFocus()
    entry.selectAll()
    QTest.keyClicks(entry, "oops")
    assert entry.text() == "oops"
    assert not dialog.apply_button.isEnabled()
    assert dialog.stops() == saved
    assert entry.text() == "oops"
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    entry.selectAll()
    QTest.keyClicks(entry, "#123456")
    assert dialog.apply_button.isEnabled()
    assert rgba(dialog.stops()[-1][1]) == (0x12, 0x34, 0x56, 255)
    dialog.reject()


@pytest.mark.parametrize("endpoint", [0, 1])
def test_endpoints_are_directly_draggable_in_adjust_mode(qapp, endpoint):
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    from PyQt6.QtGui import QColor
    fill = _fill("#804080C0", "#20FF6000")
    dialog = GradientPathDialog(fill)
    dialog.long_button.click()
    dialog.mode_switch.setCurrentItem("adjust")
    points_before = list(dialog.wheel.points)
    colors_before = dialog._colors()
    position = dialog.wheel.points[0 if endpoint == 0 else -1]
    _mouse(dialog.wheel, QEvent.Type.MouseButtonPress, position)
    _mouse(dialog.wheel, QEvent.Type.MouseMove, (0., 1.))
    _mouse(dialog.wheel, QEvent.Type.MouseButtonRelease, (0., 1.), pressed=False)
    colors_after = dialog._colors()
    previous = QColor(colors_before[endpoint])
    assert rgba(colors_after[endpoint]) == (round(previous.value() / 2), previous.value(), 0, previous.alpha())
    assert colors_after[1 - endpoint] == colors_before[1 - endpoint]
    assert dialog.wheel.points != points_before
    dialog.stops()
    assert QColor(dialog.point_editor.hex_edit.text()) == QColor(colors_after[endpoint])
    dialog.undo_button.click()
    assert dialog.wheel.points == points_before
    assert dialog._colors() == colors_before
    dialog.reject()


def test_coincident_endpoint_handles_can_be_dragged_independently(qapp):
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    dialog = GradientPathDialog(_fill("#000000", "#FFFFFF"))
    dialog.mode_switch.setCurrentItem("adjust")
    wheel = dialog.wheel
    assert wheel.endpoint_position(0) != wheel.endpoint_position(1)
    event = QMouseEvent(QEvent.Type.MouseButtonPress, wheel.endpoint_position(0),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(wheel, event)
    _mouse(wheel, QEvent.Type.MouseMove, (1., 0.))
    _mouse(wheel, QEvent.Type.MouseButtonRelease, (1., 0.), pressed=False)
    assert rgba(dialog.stops()[0][1]) == (0, 0, 0, 255)
    assert rgba(dialog.stops()[-1][1]) == (255, 255, 255, 255)
    dialog.reject()


def test_hex_reanchors_hand_drawn_path_without_replacing_middle_and_saves(qapp):
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    dialog = GradientPathDialog(_fill())
    middle = [(0.2, -0.1), (.7, .1), (.2, .3)]
    dialog.wheel.set_points([dialog.wheel.points[0]] + middle + [dialog.wheel.points[-1]])
    dialog._schedule_refresh()
    dialog._select_anchor(1)
    entry = dialog.point_editor.hex_edit
    entry.setText("#800080")
    entry.textEdited.emit(entry.text())
    assert dialog.wheel.points[1:-1] == middle
    baked, route = dialog.stops(), dialog.route()
    restored = paint_fill_from_dict(json.loads(json.dumps(paint_fill_to_dict(
        replace(_fill(), gradient_stops=baked, gradient_path=route)))))
    reopened = GradientPathDialog(restored)
    assert reopened.wheel.points == dialog.wheel.points
    assert rgba(reopened._colors()[1]) == (128, 0, 128, 255)
    dialog.reject()
    reopened.reject()


def test_each_freehand_stroke_replaces_old_route_and_samples_its_own_ends(qapp):
    from PyQt6.QtGui import QColor
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    dialog = GradientPathDialog(_fill("#80112233", "#20445566"))
    wheel = dialog.wheel
    for start, end in (((.2, .2), (-.2, -.3)), ((.7, 0.), (.5, .5))):
        _mouse(wheel, QEvent.Type.MouseButtonPress, start)
        assert wheel.points == [pytest.approx(start), pytest.approx(start)]
        _mouse(wheel, QEvent.Type.MouseMove, (.3, .4))
        _mouse(wheel, QEvent.Type.MouseButtonRelease, end, pressed=False)
        assert wheel.points[0] == pytest.approx(start)
        assert wheel.points[-1] == pytest.approx(end)
        for color, point, alpha in zip(dialog._colors(), (start, end), (128, 32)):
            hue = math.atan2(point[1], point[0]) / math.tau % 1
            expected = QColor.fromHsvF(hue, math.hypot(*point), 1)
            assert rgba(color) == (*expected.getRgb()[:3], alpha)
        assert not wheel.adjusting
    original_route = dialog.route()
    dialog.mode_switch.setCurrentItem("adjust")
    assert wheel.adjusting
    assert dialog.route() == original_route
    dialog.reject()


def test_applied_gradients_can_be_undone_immediately_without_closing_dialog(qapp):
    from copy import deepcopy
    from krok_helper.subtitle_render.frontend.properties.property_panel import PropertyPanel
    panel = PropertyPanel()
    panel._fill_mode_combo.setCurrentIndex(panel._fill_mode_combo.findData("gradient_horizontal"))
    original = deepcopy(panel._current_paint_fill())
    emitted = []
    panel.styleChanged.connect(emitted.append)
    panel._gradient_path_btn.click()
    dialog = panel._gradient_path_dialog
    dialog.short_button.click()
    dialog.apply_button.click()
    first = deepcopy(panel._current_paint_fill())
    dialog.long_button.click()
    dialog.apply_button.click()
    second = deepcopy(panel._current_paint_fill())
    assert first != second != original
    assert len(emitted) == 2
    assert dialog.isVisible()
    dialog.undo_button.click()
    assert panel._current_paint_fill() == first
    assert dialog.route() == first.gradient_path
    assert dialog.isVisible()
    dialog.undo_button.click()
    assert panel._current_paint_fill() == original
    assert dialog.stops() == original.gradient_stops
    assert not dialog.undo_button.isEnabled()
    assert dialog.isVisible()
    assert len(emitted) == 4
    dialog.close()


def test_undoing_unapplied_edits_keeps_last_applied_subtitle_and_close_retains_it(qapp):
    from copy import deepcopy
    from krok_helper.subtitle_render.frontend.properties.property_panel import PropertyPanel
    panel = PropertyPanel()
    panel._fill_mode_combo.setCurrentIndex(panel._fill_mode_combo.findData("gradient_vertical"))
    emitted = []
    panel.styleChanged.connect(emitted.append)
    panel._gradient_path_btn.click()
    dialog = panel._gradient_path_dialog
    dialog.short_button.click()
    dialog.apply_button.click()
    applied = deepcopy(panel._current_paint_fill())
    dialog.long_button.click()
    assert panel._current_paint_fill() == applied
    dialog.undo_button.click()
    assert panel._current_paint_fill() == applied
    assert len(emitted) == 1
    # Reopening the action while the window is open reuses the same session.
    panel._gradient_path_btn.click()
    assert panel._gradient_path_dialog is dialog
    dialog.reverse_button.click()
    dialog.close_button.click()
    assert panel._current_paint_fill() == applied
    assert panel._gradient_path_dialog is None


def test_applied_undo_will_not_overwrite_external_color_changes(qapp):
    from copy import deepcopy
    from krok_helper.subtitle_render.frontend.properties.property_panel import PropertyPanel
    panel = PropertyPanel()
    panel._fill_mode_combo.setCurrentIndex(panel._fill_mode_combo.findData("gradient_horizontal"))
    panel._gradient_path_btn.click()
    dialog = panel._gradient_path_dialog
    dialog.long_button.click()
    dialog.apply_button.click()
    panel._gradient_editor.set_selected_color("#123456")
    external = deepcopy(panel._current_paint_fill())
    dialog.undo_button.click()
    assert panel._current_paint_fill() == external
    assert "已变化" in dialog.status_label.text()
    assert not dialog.apply_button.isEnabled()
    dialog.close()


def test_older_segment_metadata_loads_colors_but_new_edit_covers_the_whole_gradient(qapp):
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    stops = [(0, "#123456"), (20, "#FF0000"), (80, "#00FFFF"), (100, "#ABCDEF")]
    old_route = GradientPath(hue_route("#FF0000", "#00FFFF"), 20, 80)
    original = replace(_fill(), gradient_stops=replace_path_stops(stops, old_route), gradient_path=old_route)
    dialog = GradientPathDialog(original)
    assert dialog.stops() == original.gradient_stops
    assert (dialog.route().start_pct, dialog.route().end_pct) == (0, 100)
    dialog.long_button.click()
    assert dialog.stops()[0] == (0, "#123456")
    assert dialog.stops()[-1] == (100, "#ABCDEF")
    dialog.reject()


@pytest.mark.parametrize("endpoint", [0, 1])
def test_endpoint_eyedroppers_reuse_screen_picker_and_update_only_picked_endpoint(qapp, monkeypatch, endpoint):
    from PyQt6.QtGui import QColor
    from krok_helper.subtitle_render.frontend.properties.color_controls import ScreenColorPicker
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    monkeypatch.setattr(ScreenColorPicker, "start", lambda self: None)
    dialog = GradientPathDialog(_fill("#80112233", "#20445566"))
    original = dialog._colors()
    dialog._select_anchor(endpoint)
    editor = dialog.point_editor
    assert not editor.screen_picker_button.icon().isNull()
    assert editor.layout().indexOf(editor.screen_picker_button) > editor.layout().indexOf(editor.hex_edit)
    editor.screen_picker_button.click()
    picker = dialog._screen_picker
    assert isinstance(picker, ScreenColorPicker)
    picker.colorHovered.emit(QColor("#ABCDEF"))
    assert dialog._colors() == original
    assert not dialog.undo_button.isEnabled()
    assert rgba(editor.hex_edit.text())[:3] == (171, 205, 239)
    picker.colorPicked.emit(QColor("#1256AB"))
    picker._finish()
    assert dialog._screen_picker is None
    assert rgba(dialog._colors()[endpoint]) == (18, 86, 171, 128 if endpoint == 0 else 32)
    assert dialog._colors()[1 - endpoint] == original[1 - endpoint]
    assert dialog.mode_switch.currentRouteKey() == "adjust"
    dialog.stops()
    assert rgba(editor.hex_edit.text()) == rgba(dialog._colors()[endpoint])
    dialog.undo_button.click()
    assert dialog._colors() == original
    dialog.close()


def test_cancelled_endpoint_screen_pick_restores_unfinished_hex_without_editing_route(qapp, monkeypatch):
    from PyQt6.QtGui import QColor
    from krok_helper.subtitle_render.frontend.properties.color_controls import ScreenColorPicker
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    monkeypatch.setattr(ScreenColorPicker, "start", lambda self: None)
    dialog = GradientPathDialog(_fill())
    colors, points = dialog._colors(), list(dialog.wheel.points)
    editor = dialog.point_editor
    editor.hex_edit.setText("#12")
    editor.screen_picker_button.click()
    picker = dialog._screen_picker
    picker.colorHovered.emit(QColor("#ABCDEF"))
    picker.cancel()
    assert dialog._screen_picker is None
    assert editor.hex_edit.text() == "#12"
    assert dialog._colors() == colors
    assert dialog.wheel.points == points
    assert not dialog.undo_button.isEnabled()
    assert not dialog.apply_button.isEnabled()
    dialog.close()


def test_closing_gradient_dialog_cancels_active_screen_picker(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.properties.color_controls import ScreenColorPicker
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    monkeypatch.setattr(ScreenColorPicker, "start", lambda self: None)
    dialog = GradientPathDialog(_fill())
    dialog.show()
    qapp.processEvents()
    dialog.point_editor.screen_picker_button.click()
    picker = dialog._screen_picker
    assert picker._active
    dialog.close()
    assert not picker._active
    assert dialog._screen_picker is None


def test_destroying_gradient_dialog_releases_parentless_screen_picker(qapp, monkeypatch):
    from PyQt6.QtCore import QCoreApplication
    from krok_helper.subtitle_render.frontend.properties.color_controls import ScreenColorPicker
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    monkeypatch.setattr(ScreenColorPicker, "start", lambda self: None)
    dialog = GradientPathDialog(_fill())
    dialog.point_editor.screen_picker_button.click()
    picker = dialog._screen_picker
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
    assert not picker._active


@pytest.mark.parametrize("long", [False, True])
@pytest.mark.parametrize("colors", [
    ["#FF0000", "#FF00FF", "#00FFFF"],
    ["#80CC1122", "#10101010", "#E010A0C0", "#FFFEDC"],
    ["#000000", "#FFFFFF", "#00000000", "#80444444"],
    ["#123456", "#123456", "#123456"],
    ["#9383CA", "#F6E9BF", "#80FF55", "#4400FF"]])
def test_generated_waypoints_keep_exact_rgba_and_baking_error_bounded(colors, long):
    from krok_helper.subtitle_render.domain.gradient_path import MAX_BAKED_STOPS
    route = anchor_route(colors, long=long)
    assert [color for _, color in route.anchors] == colors
    assert all(math.hypot(*point) <= 1.000001 for point in route.points)
    for index, color in route.anchors:
        assert route.points[index] == pytest.approx(disk_point(color), abs=1e-9)
    stops = bake_path(route, colors[0], colors[-1])
    assert len(stops) <= MAX_BAKED_STOPS
    samples, positions, mandatory = anchored_samples(route)
    for i in mandatory:
        position = round(positions[i] * 100, 6)
        assert rgba(next(c for p, c in stops if p == position)) == samples[i]
    assert [rgba(next(c for p, c in stops if p == round(positions[i] * 100, 6))) for i in sorted(mandatory)] == [rgba(c) for c in colors]
    cursor = 0
    for color, position in zip(samples, positions):
        p = position * 100
        while cursor < len(stops) - 2 and stops[cursor + 1][0] < p:
            cursor += 1
        a, b = stops[cursor:cursor + 2]
        t = (p - a[0]) / (b[0] - a[0])
        assert max(abs(c - (x + (y - x)*t)) for c, x, y in zip(color, rgba(a[1]), rgba(b[1]))) < 2.02


def test_waypoint_metadata_round_trip_preserves_generated_route_and_exact_colors(tmp_path):
    from krok_helper.subtitle_render.domain.models import Style, style_from_dict, style_to_dict
    from krok_helper.subtitle_render.domain.paint import KaraokeColors
    from krok_helper.subtitle_render.project.store import load_render_project, save_render_project
    route = anchor_route(["#FF0000", "#804080C0", "#00FFFF"], long=True)
    colors = KaraokeColors()
    colors.after.text = replace(_fill(), gradient_path=route, gradient_stops=bake_path(route, "#FF0000", "#00FFFF"))
    project = tmp_path / "waypoints.yurika"
    save_render_project(project, {"style": style_to_dict(Style(karaoke_colors=colors))})
    fill = style_from_dict(load_render_project(project)["style"]).karaoke_colors.after.text
    assert fill.gradient_path == route
    assert fill.gradient_stops == colors.after.text.gradient_stops


@pytest.mark.parametrize("anchors", [[[0, "#FF0000"]], [[0, "bad"], [1, "#FFFFFF"]],
    [[0, "#000000"], [0, "#FFFFFF"]], [[0, "#000000"], [999, "#FFFFFF"]],
    [[False, "#000000"], [1, "#FFFFFF"]], "invalid"])
def test_invalid_waypoint_metadata_falls_back_to_baked_stops(anchors):
    payload = paint_fill_to_dict(replace(_fill(), gradient_path=GradientPath([(1., 0.), (0., 1.)])))
    payload["gradient_path"]["anchors"] = anchors
    fill = paint_fill_from_dict(payload)
    assert fill.gradient_path is None
    assert fill.gradient_stops == _fill().gradient_stops


def _add_waypoint(dialog, fraction=.5):
    dialog.mode_switch.setCurrentItem("adjust")
    segment = max(0, min(len(dialog.wheel.points) - 2, int((len(dialog.wheel.points) - 1)*fraction)))
    a, b = dialog.wheel.points[segment:segment + 2]
    point = ((a[0] + b[0])/2, (a[1] + b[1])/2)
    _mouse(dialog.wheel, QEvent.Type.MouseButtonDblClick, point)
    return dialog._selected


def test_double_click_adds_selects_drags_and_deletes_waypoint(qapp):
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    dialog = GradientPathDialog(_fill())
    dialog.short_button.click()
    index = _add_waypoint(dialog)
    assert index == 1
    assert len(dialog.route().anchors) == 3
    assert dialog.point_editor.point_combo.currentText() == "途经点 1"
    assert not dialog.point_editor.delete_button.isHidden()
    before = dialog.route()
    vertex, color = before.anchors[index]
    _mouse(dialog.wheel, QEvent.Type.MouseButtonPress, before.points[vertex])
    _mouse(dialog.wheel, QEvent.Type.MouseMove, (.7, .2))
    _mouse(dialog.wheel, QEvent.Type.MouseButtonRelease, (.7, .2), pressed=False)
    assert dialog._selected == index
    assert dialog.route() != before
    assert rgba(dialog.route().anchors[index][1])[3] == rgba(color)[3]
    # The route is regenerated from the picked 8-bit RGB color.
    assert dialog.route().points[dialog.route().anchors[index][0]] == pytest.approx((.7, .2), abs=.004)
    dialog.point_editor.delete_button.click()
    assert len(dialog.route().anchors) == 2
    assert dialog.point_editor.delete_button.isHidden()
    dialog.undo_button.click()
    assert len(dialog.route().anchors) == 3
    dialog.close()


def test_waypoint_hex_and_eyedropper_update_same_anchor_and_route(qapp, monkeypatch):
    from PyQt6.QtGui import QColor
    from PyQt6.QtTest import QTest
    from krok_helper.subtitle_render.frontend.properties.color_controls import ScreenColorPicker
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    monkeypatch.setattr(ScreenColorPicker, "start", lambda self: None)
    dialog = GradientPathDialog(_fill())
    dialog.long_button.click()
    index = _add_waypoint(dialog)
    dialog.show()
    qapp.processEvents()
    entry = dialog.point_editor.hex_edit
    entry.setFocus()
    entry.selectAll()
    QTest.keyClicks(entry, "#804080C0")
    QTest.keyClick(entry, Qt.Key.Key_Tab)
    assert rgba(dialog.route().anchors[index][1]) == (64, 128, 192, 128)
    before = dialog.route()
    ends = dialog._colors()
    dialog.point_editor.screen_picker_button.click()
    picker = dialog._screen_picker
    picker.colorPicked.emit(QColor("#FF8899"))
    picker._finish()
    assert rgba(dialog.route().anchors[index][1]) == (255, 136, 153, 128)
    assert dialog._colors() == ends
    assert dialog.point_editor.point_combo.currentText() == "途经点 1"
    assert dialog.route() != before
    dialog.undo_button.click()
    assert dialog.route() == before
    dialog.close()


def test_regenerate_and_reverse_keep_all_waypoints_and_freehand_clears_them(qapp):
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    dialog = GradientPathDialog(_fill())
    dialog.short_button.click()
    _add_waypoint(dialog, .3)
    _add_waypoint(dialog, .7)
    colors = [c for _, c in dialog.route().anchors]
    assert len(colors) == 4
    dialog.long_button.click()
    long = dialog.route()
    assert [c for _, c in long.anchors] == colors
    dialog.short_button.click()
    assert [c for _, c in dialog.route().anchors] == colors
    assert dialog.route().points != long.points
    dialog.reverse_button.click()
    assert [c for _, c in dialog.route().anchors] == list(reversed(colors))
    dialog.mode_switch.setCurrentItem("draw")
    _mouse(dialog.wheel, QEvent.Type.MouseButtonPress, (.8, 0.))
    _mouse(dialog.wheel, QEvent.Type.MouseMove, (.4, .4))
    _mouse(dialog.wheel, QEvent.Type.MouseButtonRelease, (0., .8), pressed=False)
    assert len(dialog.route().anchors) == 2
    dialog.undo_button.click()
    assert [c for _, c in dialog.route().anchors] == list(reversed(colors))
    dialog.close()


def test_waypoint_apply_reopen_delete_and_undo_update_live_panel(qapp):
    from krok_helper.subtitle_render.frontend.properties.property_panel import PropertyPanel
    panel = PropertyPanel()
    panel._fill_mode_combo.setCurrentIndex(panel._fill_mode_combo.findData("gradient_horizontal"))
    panel._gradient_path_btn.click()
    dialog = panel._gradient_path_dialog
    dialog.short_button.click()
    index = _add_waypoint(dialog)
    dialog.point_editor.hex_edit.setText("#804080C0")
    dialog.point_editor.hex_edit.textEdited.emit("#804080C0")
    dialog.apply_button.click()
    saved = panel._current_paint_fill().gradient_path
    assert len(saved.anchors) == 3
    assert dialog.isVisible()
    dialog.close()
    panel._gradient_path_btn.click()
    reopened = panel._gradient_path_dialog
    assert reopened.route() == saved
    reopened.mode_switch.setCurrentItem("adjust")
    reopened.point_editor.point_combo.setCurrentIndex(index)
    assert reopened.point_editor.hex_edit.text() == "#804080C0"
    reopened.point_editor.delete_button.click()
    reopened.apply_button.click()
    assert len(panel._current_paint_fill().gradient_path.anchors) == 2
    reopened.undo_button.click()
    assert panel._current_paint_fill().gradient_path == saved
    reopened.close()


def test_click_selects_waypoint_without_editing_or_creating_undo_history(qapp):
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    dialog = GradientPathDialog(_fill())
    dialog.short_button.click()
    _add_waypoint(dialog)
    saved = dialog.route()
    history_length = len(dialog._history)
    dialog.point_editor.point_combo.setCurrentIndex(0)
    position = dialog.wheel.endpoint_position(1)
    for event_type in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        event = QMouseEvent(event_type, position, Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(dialog.wheel, event)
    assert dialog._selected == 1
    assert dialog.route() == saved
    assert len(dialog._history) == history_length
    dialog.close()


def test_maximum_waypoints_keep_bounded_points_and_baked_stops(qapp):
    from krok_helper.subtitle_render.domain.gradient_path import MAX_ANCHORS, MAX_BAKED_STOPS, MAX_PATH_POINTS
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    dialog = GradientPathDialog(_fill())
    dialog.short_button.click()
    while len(dialog.route().anchors) < MAX_ANCHORS:
        _add_waypoint(dialog, .5)
    saved = dialog.route()
    assert len(saved.points) <= MAX_PATH_POINTS
    assert len(dialog.stops()) <= MAX_BAKED_STOPS
    _add_waypoint(dialog, .5)
    assert dialog.route() == saved
    dialog.close()


def test_gray_value_and_alpha_waypoints_get_visible_gradient_space():
    for colors in (["#000000", "#FFFFFF", "#000000"],
                   ["#00FFFFFF", "#FFFFFFFF", "#00FFFFFF"]):
        route = anchor_route(colors)
        samples, positions, mandatory = anchored_samples(route)
        assert positions[sorted(mandatory)[1]] == pytest.approx(.5)
        stops = bake_path(route, colors[0], colors[-1])
        assert rgba(next(c for p, c in stops if p == 50)) == rgba(colors[1])


def test_waypoint_numbers_follow_creation_order_when_inserted_before_existing_point(qapp):
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    dialog = GradientPathDialog(_fill())
    dialog.short_button.click()
    _add_waypoint(dialog, .75)
    assert dialog.point_editor.point_combo.currentText() == "途经点 1"
    first_color = dialog.route().anchors[1][1]
    _add_waypoint(dialog, .15)
    assert dialog.point_editor.point_combo.currentText() == "途经点 2"
    assert dialog.route().waypoint_ids == [2, 1]
    assert dialog.wheel.anchor_labels == ["起", "2", "1", "终"]
    assert dialog.route().anchors[2][1] == first_color
    dialog.long_button.click()
    assert dialog.route().waypoint_ids == [2, 1]
    dialog.reverse_button.click()
    assert dialog.route().waypoint_ids == [1, 2]
    dialog.undo_button.click()
    assert dialog.route().waypoint_ids == [2, 1]
    dialog.close()


def test_waypoint_creation_numbers_survive_save_delete_and_reopen(qapp):
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    dialog = GradientPathDialog(_fill())
    dialog.short_button.click()
    _add_waypoint(dialog, .75)
    _add_waypoint(dialog, .15)
    baked, route = dialog.stops(), dialog.route()
    payload = json.loads(json.dumps(paint_fill_to_dict(replace(_fill(), gradient_stops=baked, gradient_path=route))))
    restored = GradientPathDialog(paint_fill_from_dict(payload))
    assert restored.route().waypoint_ids == [2, 1]
    restored.mode_switch.setCurrentItem("adjust")
    restored.point_editor.point_combo.setCurrentIndex(1)
    assert restored.point_editor.point_combo.currentText() == "途经点 2"
    restored.point_editor.delete_button.click()
    assert restored.route().waypoint_ids == [1]
    _add_waypoint(restored, .15)
    assert restored.point_editor.point_combo.currentText() == "途经点 2"
    restored.undo_button.click()
    assert restored.route().waypoint_ids == [1]
    restored.undo_button.click()
    assert restored.route().waypoint_ids == [2, 1]
    for _ in range(2):
        restored.point_editor.point_combo.setCurrentIndex(1)
        restored.point_editor.delete_button.click()
    assert restored.route().waypoint_ids == []
    _add_waypoint(restored)
    assert restored.point_editor.point_combo.currentText() == "途经点 1"
    dialog.close()
    restored.close()


def test_reverse_flips_colors_geometry_numbers_and_selection_together(qapp):
    from krok_helper.subtitle_render.frontend.properties.gradient_path_dialog import GradientPathDialog
    dialog = GradientPathDialog(_fill())
    dialog.short_button.click()
    _add_waypoint(dialog, .25)
    _add_waypoint(dialog, .85)
    assert dialog.route().waypoint_ids == [1, 2]
    dialog.point_editor.point_combo.setCurrentIndex(1)
    before = dialog.route()
    selected_color = dialog.point_editor.hex_edit.text()
    dialog.reverse_button.click()
    after = dialog.route()
    assert after.points == list(reversed(before.points))
    assert [color for _, color in after.anchors] == list(reversed([color for _, color in before.anchors]))
    assert after.waypoint_ids == [2, 1]
    assert dialog.wheel.anchor_labels == ["起", "2", "1", "终"]
    assert [dialog.point_editor.point_combo.itemText(i) for i in range(4)] == ["起点", "途经点 2", "途经点 1", "终点"]
    assert dialog.point_editor.point_combo.currentText() == "途经点 1"
    assert dialog.point_editor.hex_edit.text() == selected_color
    dialog.reverse_button.click()
    assert dialog.route() == before
    dialog.undo_button.click()
    assert dialog.route() == after
    dialog.close()
