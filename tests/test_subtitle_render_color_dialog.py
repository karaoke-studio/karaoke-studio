"""Color editor interactions, RGBA fidelity and shared-dialog lifecycle."""

import pytest
from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QDragEnterEvent, QDropEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QColorDialog, QDialog, QLabel, QSpinBox, QWidget

from krok_helper.subtitle_render.frontend.properties import color_controls as colors
from krok_helper.subtitle_render.frontend.properties.color_picker_widgets import (
    HueSaturationPlane,
)


@pytest.fixture
def qapp():
    return QApplication.instance()


@pytest.fixture
def dialog(qapp):
    colors.clear_recent_colors()
    widget = colors._ColorDialog(QColor("#804093E9"))
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()
    colors.clear_recent_colors()


def rgba(color):
    return color.name(QColor.NameFormat.HexArgb).upper()


@pytest.mark.parametrize("value", ["#00000000", "#00123456", "#FFFFFFFF", "#804093E9", "#01010203", "#FEFEDCBA"])
def test_dialog_load_and_alpha_only_edits_keep_exact_rgb(dialog, value):
    dialog.setCurrentColor(QColor(value))
    assert rgba(dialog.currentColor()) == value
    dialog._set_current_alpha(73)
    assert rgba(dialog.currentColor()) == "#49" + value[3:]
    assert dialog._alpha_slider.alpha == 73
    assert dialog._hex_edit.text() == "#49" + value[3:]


def test_dialog_uses_painted_surfaces_and_chinese_fluent_controls(dialog):
    assert not isinstance(dialog, QColorDialog)
    assert isinstance(dialog._color_plane, HueSaturationPlane)
    assert dialog.findChildren(QColorDialog) == []
    label_texts = {
        label.text() for label in dialog.findChildren(QLabel)
        if not dialog.titleBar.isAncestorOf(label)
    }
    assert {"最近使用", "原色", "新色"} <= label_texts
    assert label_texts.isdisjoint({"选择颜色", "色相 / 饱和度", "颜色对比", "常用颜色", "明度", "不透明度", "色号"})
    assert dialog.findChildren(QSpinBox) == []
    assert not any(label.text().endswith("%") for label in dialog.findChildren(QLabel))
    assert dialog._original_swatch.height() == dialog._current_swatch.height() == 24
    assert dialog._original_swatch.width() > dialog._original_swatch.height() * 4
    assert dialog._original_swatch.mapTo(dialog, QPoint()).y() == dialog._current_swatch.mapTo(dialog, QPoint()).y()
    assert dialog._original_swatch.mapTo(dialog, QPoint()).x() < dialog._current_swatch.mapTo(dialog, QPoint()).x()
    for caption, swatch in (("原色", dialog._original_swatch), ("新色", dialog._current_swatch)):
        label = next(label for label in dialog.findChildren(QLabel) if label.text() == caption)
        assert label.mapTo(dialog, QPoint()).y() < swatch.mapTo(dialog, QPoint()).y()
        assert label.mapTo(dialog, QPoint()).x() == swatch.mapTo(dialog, QPoint()).x()
    assert dialog._delete_recent_button.text() == "删除当前色"
    assert not hasattr(dialog, "_save_recent_button")
    assert dialog._ok_button.text() == "确定"
    assert dialog._cancel_button.text() == "取消"
    assert dialog._screen_button.text() == "从屏幕取色"
    assert all("?" not in label.text() for label in dialog.findChildren(QLabel))
    for widget in (dialog._color_plane, dialog._alpha_slider, dialog._hex_edit, dialog._recent_list):
        assert widget.isVisible()
        assert dialog.rect().contains(widget.mapTo(dialog, widget.rect().bottomRight()))


def test_color_dialog_has_a_real_fluent_window_without_a_mask(dialog):
    from qfluentwidgets import FluentTitleBar, MaskDialogBase
    from qframelesswindow import FramelessDialog

    assert isinstance(dialog, FramelessDialog)
    assert isinstance(dialog.titleBar, FluentTitleBar)
    assert not isinstance(dialog, MaskDialogBase)
    assert dialog.isWindow()
    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert dialog.titleBar.isVisible()
    assert not dialog.titleBar.minBtn.isVisible()
    assert not dialog.titleBar.maxBtn.isVisible()
    assert dialog._color_plane.mapTo(dialog, QPoint()).y() >= dialog.titleBar.height()
    dialog.setWindowTitle("选择描边颜色")
    assert dialog.titleBar.titleLabel.text() == "选择描边颜色"
    dialog.resize(dialog.width() + 80, dialog.height() + 40)
    assert dialog.titleBar.width() == dialog.width()
    assert dialog.titleBar.canDrag(QPoint(180, 16))
    assert not dialog.titleBar.canDrag(dialog.titleBar.closeBtn.geometry().center())


def test_titlebar_close_rejects_the_dialog_without_recording_a_color(dialog):
    dialog.setCurrentColor(QColor("#60123456"))
    QTimer.singleShot(10, dialog.titleBar.closeBtn.click)
    assert dialog.exec() == QDialog.DialogCode.Rejected
    assert not dialog.selectedColor().isValid()
    assert colors.recent_colors() == []


def test_reopening_after_mouse_close_clears_pressed_state_and_allows_dragging(dialog, qapp):
    from qframelesswindow.titlebar.title_bar_buttons import TitleBarButtonState

    for _ in range(3):
        # click() skips the library's mousePressEvent and cannot reproduce this.
        QTest.mouseClick(dialog.titleBar.closeBtn, Qt.MouseButton.LeftButton)
        assert not dialog.isVisible()
        dialog.show()
        qapp.processEvents()
        assert dialog.titleBar.closeBtn._state == TitleBarButtonState.NORMAL
        assert not dialog.titleBar.closeBtn.isPressed()
        assert not dialog.titleBar.closeBtn.isDown()
        assert dialog.titleBar.canDrag(QPoint(180, 16))


def test_active_close_press_still_blocks_drag_and_hiding_clears_it(dialog, qapp):
    button = dialog.titleBar.closeBtn
    QTest.mousePress(button, Qt.MouseButton.LeftButton)
    assert button.isPressed() and button.isDown()
    assert not dialog.titleBar.canDrag(QPoint(180, 16))
    dialog.hide()
    assert not button.isPressed() and not button.isDown()
    dialog.show()
    qapp.processEvents()
    assert dialog.titleBar.canDrag(QPoint(180, 16))


def test_titlebar_drag_dispatch_works_immediately_after_mouse_close(dialog, qapp, monkeypatch):
    import importlib
    import sys
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QMouseEvent

    method = dialog.titleBar.mouseMoveEvent if sys.platform == "win32" else dialog.titleBar.mousePressEvent
    module = importlib.import_module(method.__func__.__module__)
    moves = []
    monkeypatch.setattr(module, "startSystemMove", lambda window, point: moves.append((window, point)))
    QTest.mouseClick(dialog.titleBar.closeBtn, Qt.MouseButton.LeftButton)
    dialog.show()
    qapp.processEvents()
    local = QPoint(180, 16)
    global_pos = dialog.titleBar.mapToGlobal(local)
    event_type = QEvent.Type.MouseMove if sys.platform == "win32" else QEvent.Type.MouseButtonPress
    event = QMouseEvent(event_type, QPointF(local), QPointF(global_pos),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    method(event)
    assert moves == [(dialog, global_pos)]


def test_shared_fluent_dialog_can_be_reowned_and_reopened(qapp):
    first, second = QWidget(), QWidget()
    first.show()
    second.show()
    colors.clear_recent_colors()
    try:
        for host in (first, second):
            def close_dialog():
                dialog = colors._shared_color_dialog
                assert dialog.parentWidget() is host
                assert dialog.titleBar.isVisible()
                assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
                assert dialog.titleBar.canDrag(QPoint(180, 16))
                QTest.mouseClick(dialog.titleBar.closeBtn, Qt.MouseButton.LeftButton)

            QTimer.singleShot(20, close_dialog)
            result = colors._select_color(QColor("#804093E9"), host, "选择颜色")
            assert not result.isValid()
            assert colors._shared_color_dialog.parentWidget() is None
    finally:
        first.close()
        second.close()
        colors.clear_recent_colors()


def test_plane_mouse_keyboard_and_black_recovery_preserve_chosen_hue(dialog):
    plane = dialog._color_plane
    rect = plane.surface_rect()
    QTest.mouseClick(plane, Qt.MouseButton.LeftButton, pos=QPoint(round(rect.left() + rect.width() / 3), round(rect.top())))
    assert dialog.currentColor().green() == 233  # The plane preserves the current value.
    assert dialog.currentColor().red() <= 5  # Integer mouse coordinates round the hue slightly.
    assert dialog.currentColor().alpha() == 128
    hue = dialog._hue
    QTest.keyClick(plane, Qt.Key.Key_Right)
    assert dialog._hue == pytest.approx(hue + 1)
    QTest.keyClick(plane, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert dialog._saturation == pytest.approx(0.9)

    # Value down to black, change the hue there, then return to full brightness.
    QTest.keyClick(dialog._value_slider, Qt.Key.Key_Home)
    assert dialog.currentColor().value() == 0
    QTest.keyClick(plane, Qt.Key.Key_Right)
    remembered = dialog._hue
    QTest.keyClick(dialog._value_slider, Qt.Key.Key_End)
    assert dialog._hue == remembered
    assert dialog.currentColor().value() == 255
    assert dialog.currentColor().alpha() == 128


def test_alpha_slider_endpoints_and_keyboard_keep_rgb(dialog):
    slider = dialog._alpha_slider
    rect = slider.surface_rect()
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=rect.topLeft().toPoint())
    assert rgba(dialog.currentColor()) == "#004093E9"
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=rect.topRight().toPoint())
    assert rgba(dialog.currentColor()) == "#FF4093E9"
    QTest.keyClick(slider, Qt.Key.Key_Left)
    assert rgba(dialog.currentColor()) == "#FE4093E9"
    QTest.keyClick(slider, Qt.Key.Key_Home)
    assert dialog._alpha_slider.alpha == 0


def test_hex_edits_update_alpha_and_preview_without_rounding(dialog):
    dialog._hex_edit.setFocus()
    dialog._hex_edit.selectAll()
    QTest.keyClicks(dialog._hex_edit, "#20123456")
    assert rgba(dialog.currentColor()) == "#20123456"
    assert dialog._alpha_slider.alpha == 32
    assert dialog._hex_edit.text() == "#20123456"
    assert rgba(dialog._current_swatch.color) == "#20123456"


def test_invalid_hex_cannot_be_accepted_and_enter_does_not_dismiss(dialog):
    dialog._hex_edit.setFocus()
    dialog._hex_edit.selectAll()
    QTest.keyClicks(dialog._hex_edit, "#12ZZ")
    assert not dialog._ok_button.isEnabled()
    dialog.accept()
    assert dialog.isVisible()
    assert not dialog.selectedColor().isValid()
    dialog._hex_edit.selectAll()
    QTest.keyClicks(dialog._hex_edit, "#ABCDEF")
    QTest.keyClick(dialog._hex_edit, Qt.Key.Key_Return)
    assert dialog.isVisible()
    assert colors.recent_colors() == []
    assert rgba(dialog.currentColor()) == "#FFABCDEF"
    dialog._ok_button.click()
    assert rgba(dialog.selectedColor()) == "#FFABCDEF"


def test_color_sliders_use_the_full_editor_width(dialog):
    for slider in (dialog._value_slider, dialog._alpha_slider):
        assert slider.width() == dialog._hex_edit.width()
        assert slider.mapTo(dialog, QPoint()).x() == dialog._hex_edit.mapTo(dialog, QPoint()).x()


def test_dragging_plane_and_alpha_tracks_the_cursor(dialog):
    plane = dialog._color_plane
    rect = plane.surface_rect()
    QTest.mousePress(plane, Qt.MouseButton.LeftButton, pos=rect.topLeft().toPoint())
    QTest.mouseMove(plane, rect.bottomRight().toPoint())
    QTest.mouseRelease(plane, Qt.MouseButton.LeftButton, pos=rect.bottomRight().toPoint())
    assert dialog.currentColor().saturation() == 0
    slider = dialog._alpha_slider
    rect = slider.surface_rect()
    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=rect.topLeft().toPoint())
    QTest.mouseMove(slider, rect.topRight().toPoint())
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=rect.topRight().toPoint())
    assert dialog.currentColor().alpha() == 255


def test_theme_switch_updates_dialog_and_keeps_current_rgba(dialog, qapp):
    from krok_helper.theme_workbench import (
        THEME_REFRESH_DELAY_MS, ThemeMode, _sync_fluent_theme, palette, theme,
    )

    previous_mode = theme.mode
    try:
        for mode, dark in ((ThemeMode.DARK, True), (ThemeMode.LIGHT, False)):
            theme.mode = mode
            _sync_fluent_theme(dark)
            # The host deliberately debounces its second Win11 polish pass.
            for _ in range(10):
                QTest.qWait(THEME_REFRESH_DELAY_MS // 2)
                if palette().panel_bg in dialog.styleSheet():
                    break
            assert palette().panel_bg in dialog.styleSheet()
            assert rgba(dialog.currentColor()) == "#804093E9"
            assert not dialog.grab().isNull()
    finally:
        theme.mode = previous_mode
        _sync_fluent_theme(theme.is_dark)
        qapp.processEvents()


def test_original_bar_restores_full_rgba_and_resets_between_opens(dialog):
    dialog.setCurrentColor(QColor("#20EF4444"))
    dialog._original_swatch.click()
    assert rgba(dialog.currentColor()) == "#804093E9"
    dialog.set_initial_color(QColor("#20665544"))
    dialog.setCurrentColor(QColor("#FFFFFF"))
    dialog._original_swatch.click()
    assert rgba(dialog.currentColor()) == "#20665544"
    assert not dialog.selectedColor().isValid()


def test_recent_colors_restore_alpha_and_hide_empty_rows(dialog):
    for value in ("#80445566", "#FFFFFF", "#60112233"):
        colors.record_recent_color(QColor(value))
    dialog.set_recent_colors(colors.recent_colors())
    assert [rgba(c) for c in colors.recent_colors()] == ["#60112233", "#FFFFFFFF", "#80445566"]
    tiles = dialog._recent_list.tiles
    assert tiles[1].isEnabled()
    assert tiles[1].isVisible()
    assert not tiles[3].isVisible()
    QTest.keyClick(dialog._alpha_slider, Qt.Key.Key_End)
    tiles[2].click()
    assert rgba(dialog.currentColor()) == "#80445566"
    assert dialog._alpha_slider.alpha == 128
    assert len(colors.recent_colors()) == 3  # Previewing history does not reorder it.
    assert tiles[2].isChecked()
    assert dialog._delete_recent_button.isEnabled()


def test_delete_recent_color_matches_rgba_and_keeps_the_preview(dialog):
    for value in ("#FF445566", "#80445566", "#123456"):
        colors.record_recent_color(QColor(value))
    dialog.set_recent_colors(colors.recent_colors())
    assert not dialog._delete_recent_button.isEnabled()
    dialog._recent_list.tiles[1].click()
    assert rgba(dialog._recent_list.selected_color) == "#80445566"
    dialog._delete_recent_button.click()
    assert [rgba(c) for c in colors.recent_colors()] == ["#FF123456", "#FF445566"]
    assert rgba(dialog.currentColor()) == "#80445566"
    assert not dialog._delete_recent_button.isEnabled()
    assert not any(tile.isChecked() for tile in dialog._recent_list.tiles)
    dialog._recent_list.tiles[1].click()
    assert dialog._delete_recent_button.isEnabled()
    dialog._delete_recent_button.click()
    dialog._recent_list.tiles[0].click()
    dialog._delete_recent_button.click()
    assert colors.recent_colors() == []
    assert dialog._recent_list._empty_hint.isVisible()
    assert all(not tile.isVisible() for tile in dialog._recent_list.tiles)


def test_editing_a_recent_color_clears_selection_without_deleting_another_entry(dialog):
    colors.record_recent_color(QColor("#80445566"))
    colors.record_recent_color(QColor("#FF445566"))
    dialog.set_recent_colors(colors.recent_colors())
    dialog._recent_list.tiles[1].click()
    QTest.keyClick(dialog._alpha_slider, Qt.Key.Key_PageDown)
    assert not dialog._delete_recent_button.isEnabled()
    dialog._delete_current_color()
    assert [rgba(c) for c in colors.recent_colors()] == ["#FF445566", "#80445566"]


@pytest.mark.parametrize("accept", [True, False])
def test_deleting_a_recent_color_stays_deleted_after_dialog_closes(qapp, monkeypatch, accept):
    colors.clear_recent_colors()
    colors.record_recent_color(QColor("#80112233"))
    host = QWidget()

    def fake_exec(self):
        self.show()
        self._recent_list.tiles[0].click()
        self._delete_recent_button.click()
        self.accept() if accept else self.reject()
        return self.result()

    monkeypatch.setattr(colors._ColorDialog, "exec", fake_exec)
    try:
        result = colors._select_color(QColor("#000000"), host, "选择颜色")
        assert result.isValid() == accept
        assert colors.recent_colors() == []
        if accept:
            assert rgba(result) == "#80112233"
        # A later session using the color again may legitimately remember it.
        dialog = colors._shared_color_dialog
        dialog.set_initial_color(QColor("#80112233"))
        dialog.remember_selected_color(QColor("#80112233"))
        assert [rgba(c) for c in colors.recent_colors()] == ["#80112233"]
    finally:
        host.close()
        colors.clear_recent_colors()


@pytest.mark.parametrize("name", ["_color_plane", "_value_slider", "_alpha_slider"])
def test_focusing_color_handles_does_not_add_a_colored_outer_ring(dialog, qapp, name):
    widget = getattr(dialog, name)
    dialog._hex_edit.setFocus()
    qapp.processEvents()
    unfocused = widget.grab().toImage()
    widget.setFocus()
    qapp.processEvents()
    assert widget.hasFocus()
    assert widget.grab().toImage() == unfocused


def test_recent_history_scrolls_to_clickable_full_width_bars_and_caps_at_20(dialog, qapp):
    for index in range(24):
        colors.record_recent_color(QColor(index, index, index, 128))
    dialog.set_recent_colors(colors.recent_colors())
    visible = [rgba(tile.color) for tile in dialog._recent_list.tiles]
    assert visible == [rgba(QColor(index, index, index, 128)) for index in range(23, 3, -1)]
    assert len(colors.recent_colors()) == 20
    qapp.processEvents()
    recent = dialog._recent_list
    scrollbar = recent.verticalScrollBar()
    assert scrollbar.maximum() > 0
    last = recent.tiles[-1]
    assert last.width() > 150 and last.height() == 30
    assert not recent.viewport().rect().contains(last.mapTo(recent.viewport(), last.rect().center()))
    recent.ensureWidgetVisible(last)
    qapp.processEvents()
    assert recent.viewport().rect().contains(last.mapTo(recent.viewport(), last.rect().center()))
    QTest.mouseClick(last, Qt.MouseButton.LeftButton)
    assert rgba(dialog.currentColor()) == rgba(last.color)
    chosen = rgba(dialog.currentColor())
    dialog._delete_recent_button.click()
    qapp.processEvents()
    assert scrollbar.value() == 0
    assert chosen not in [rgba(c) for c in colors.recent_colors()]
    assert not dialog._delete_recent_button.isEnabled()
    colors.record_recent_color(QColor())
    assert len(colors.recent_colors()) == 19


def test_recent_list_shows_empty_hint_and_expands_when_dialog_is_taller(dialog, qapp):
    recent = dialog._recent_list
    assert recent._empty_hint.isVisible()
    assert all(not tile.isVisible() for tile in recent.tiles)
    old_height = recent.height()
    dialog.resize(dialog.width(), dialog.height() + 100)
    qapp.processEvents()
    assert recent.height() >= old_height + 90
    colors.record_recent_color(dialog.currentColor())
    dialog.set_recent_colors(colors.recent_colors())
    assert not recent._empty_hint.isVisible()
    assert recent.tiles[0].isVisible()
    assert dialog._screen_button.mapTo(dialog, QPoint()).y() > dialog._hex_edit.mapTo(dialog, QPoint()).y()


@pytest.mark.parametrize("use_text", [True, False])
def test_drop_into_history_retains_rgba_and_refreshes_swatches(dialog, use_text):
    mime = QMimeData()
    if use_text:
        mime.setText("#20ABCDEF")
    else:
        mime.setColorData(QColor("#20ABCDEF"))
    enter = QDragEnterEvent(QPoint(4, 4), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    dialog._recent_list.dragEnterEvent(enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(4, 4), Qt.DropAction.CopyAction, mime,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    dialog._recent_list.dropEvent(drop)
    assert drop.isAccepted()
    assert rgba(dialog._recent_list.tiles[0].color) == "#20ABCDEF"
    dialog.reject()
    assert [rgba(c) for c in colors.recent_colors()] == ["#20ABCDEF"]


def test_dropping_on_scroll_viewport_reaches_the_history_list(dialog, qapp):
    mime = QMimeData()
    mime.setText("#40123456")
    viewport = dialog._recent_list.viewport()
    enter = QDragEnterEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    qapp.sendEvent(viewport, enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    qapp.sendEvent(viewport, drop)
    assert drop.isAccepted()
    assert rgba(dialog._recent_list.tiles[0].color) == "#40123456"


def test_shared_dialog_accept_cancel_owner_and_original_reset(qapp, monkeypatch):
    host = QWidget()
    host.show()
    colors.clear_recent_colors()
    colors.record_recent_color(QColor("#80112233"))
    opened = []
    actions = iter([True, False])

    def fake_exec(self):
        opened.append((self.parentWidget(), rgba(self._original_swatch.color)))
        self.show()
        self._recent_list.tiles[0].click()
        if next(actions):
            self.accept()
        else:
            self.setCurrentColor(QColor("#999999"))
            self.reject()
        return self.result()

    monkeypatch.setattr(colors._ColorDialog, "exec", fake_exec)
    try:
        picked = colors._select_color(QColor("#40AABBCC"), host, "选择描边颜色")
        assert rgba(picked) == "#80112233"
        picked = colors._select_color(QColor("#50DDEEFF"), host, "选择背景颜色")
        assert not picked.isValid()
        assert opened == [(host, "#40AABBCC"), (host, "#50DDEEFF")]
        assert colors._shared_color_dialog.parentWidget() is None
        assert colors._shared_color_dialog.windowTitle() == "选择背景颜色"
        assert [rgba(c) for c in colors.recent_colors()] == ["#80112233"]
    finally:
        host.close()
        colors.clear_recent_colors()


def test_escape_cancels_real_dialog_without_committing_history(dialog):
    QTimer.singleShot(10, lambda: QTest.keyClick(dialog, Qt.Key.Key_Escape))
    assert dialog.exec() == QDialog.DialogCode.Rejected
    assert not dialog.selectedColor().isValid()
    assert colors.recent_colors() == []


def test_screen_picker_hover_cancel_and_confirm(dialog, monkeypatch):
    class FakePicker(QWidget):
        colorPicked = pyqtSignal(QColor)
        colorHovered = pyqtSignal(QColor)
        finished = pyqtSignal()

        def start(self):
            pass

        def cancel(self):
            self.finished.emit()
            self.deleteLater()

    monkeypatch.setattr(colors, "ScreenColorPicker", FakePicker)
    dialog._screen_button.click()
    picker = dialog._screen_picker
    assert picker.parentWidget() is dialog
    picker.colorHovered.emit(QColor("#123456"))
    assert rgba(dialog.currentColor()) == "#FF123456"
    picker.cancel()
    assert rgba(dialog.currentColor()) == "#804093E9"
    assert colors.recent_colors() == []
    dialog._screen_button.click()
    picker = dialog._screen_picker
    colors.record_recent_color(QColor("#ABCDEF"))  # ScreenColorPicker records on confirmation.
    picker.colorPicked.emit(QColor("#ABCDEF"))
    picker.cancel()
    assert rgba(dialog.currentColor()) == "#FFABCDEF"
    assert rgba(dialog._recent_list.tiles[0].color) == "#FFABCDEF"


def test_reject_while_picking_cleans_up_overlay(dialog, monkeypatch):
    monkeypatch.setattr(colors.ScreenColorPicker, "start", lambda self: None)
    dialog._screen_button.click()
    picker = dialog._screen_picker
    dialog.reject()
    assert dialog._screen_picker is None
    assert not picker._active
    assert not picker._hover_timer.isActive()
