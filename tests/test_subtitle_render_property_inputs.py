"""Focused contracts for shared subtitle property input controls."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QPointF, QSize, Qt
from PyQt6.QtGui import QFont, QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QWidget

from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    DynamicStackedWidget,
    GrowingPlainTextEdit,
    NoWheelSpinBox,
    TimecodeEdit,
    WheelFocusedComboBox,
    WheelFocusedDoubleSpinBox,
    WheelFocusedFontComboBox,
    WheelFocusedSpinBox,
    _FilterableFontMenu,
)


def test_growing_property_text_edit_increases_height_for_new_paragraphs(qapp) -> None:
    editor = GrowingPlainTextEdit()
    initial_height = editor.height()

    editor.setPlainText("第一行\n第二行\n第三行")

    assert editor.document().blockCount() == 3
    assert editor.height() > initial_height


def test_dynamic_property_stack_reports_only_current_page_hints(qapp) -> None:
    class HintPage(QWidget):
        def __init__(self, hint: QSize, minimum: QSize) -> None:
            super().__init__()
            self._hint = hint
            self._minimum = minimum

        def sizeHint(self) -> QSize:  # noqa: N802
            return self._hint

        def minimumSizeHint(self) -> QSize:  # noqa: N802
            return self._minimum

    stack = DynamicStackedWidget()
    first = HintPage(QSize(100, 40), QSize(80, 30))
    second = HintPage(QSize(300, 200), QSize(200, 120))
    stack.addWidget(first)
    stack.addWidget(second)

    stack.setCurrentWidget(first)
    assert stack.sizeHint() == QSize(100, 40)
    assert stack.minimumSizeHint() == QSize(80, 30)

    stack.setCurrentWidget(second)
    assert stack.sizeHint() == QSize(300, 200)
    assert stack.minimumSizeHint() == QSize(200, 120)


def test_wheel_focused_property_combo_preserves_positional_user_data(qapp) -> None:
    combo = WheelFocusedComboBox()

    combo.addItem("布局", 3)

    assert combo.itemText(0) == "布局"
    assert combo.itemData(0) == 3


def test_no_wheel_property_spin_ignores_page_scroll_input(qapp) -> None:
    class WheelEvent:
        ignored = False

        def ignore(self) -> None:
            self.ignored = True

    event = WheelEvent()
    spin = NoWheelSpinBox()

    spin.wheelEvent(event)

    assert event.ignored is True


def test_property_timecode_input_preserves_value_format_clamp_and_step(qapp) -> None:
    edit = TimecodeEdit(0, 10_000)
    changes: list[int] = []
    edit.valueChanged.connect(changes.append)

    assert edit.submit_text("3.5") is True
    assert edit.value() == 3_500
    assert edit.text() == "0:03.500"

    edit.stepBy(1)
    edit.stepBy(-1, fine=True)
    assert edit.value() == 4_490

    assert edit.submit_text("15") is True
    assert edit.value() == 10_000
    assert edit.text() == "0:10.000"
    assert changes == [3_500, 4_500, 4_490, 10_000]


def test_property_timecode_input_restores_invalid_partial_text(qapp) -> None:
    edit = TimecodeEdit(0, 10_000)
    edit.setValue(2_500)

    assert edit.submit_text("1:") is False
    assert edit.value() == 2_500
    assert edit.text() == "0:02.500"


def test_property_spin_inputs_keep_units_out_of_selection(qapp) -> None:
    spin = WheelFocusedSpinBox()
    spin.setRange(0, 200)
    spin.setSuffix(" px")
    spin.setValue(75)
    editor = spin.lineEdit()

    editor.setSelection(0, len(editor.text()))
    qapp.processEvents()

    assert editor.selectedText() == "75"


def test_property_double_spin_input_preserves_typed_text_on_commit(qapp) -> None:
    spin = WheelFocusedDoubleSpinBox(commit_delay_ms=1)
    spin.setRange(0.0, 100.0)
    spin.setDecimals(3)
    spin.show()
    editor = spin.lineEdit()
    editor.setFocus()
    editor.selectAll()
    editor.setText("12.5")
    editor.setCursorPosition(len(editor.text()))
    spin._keyboard_commit_pending = True

    spin._commit_keyboard_edit()

    assert spin.value() == 12.5
    assert editor.text() == "12.5"
    assert editor.cursorPosition() == len("12.5")


def test_property_font_combo_uses_injected_catalog_and_canonicalizer(qapp) -> None:
    combo = WheelFocusedFontComboBox(
        font_families_provider=lambda: ("Canonical Font",),
        canonicalize_family=lambda name: (
            "Canonical Font" if name == "Saved Alias" else None
        ),
    )
    combo.enable_inheritance("跟随主文字（0）")

    combo.setCurrentFont(QFont("Saved Alias"))
    assert combo.currentText() == "Canonical Font"

    combo.setCurrentFont(QFont("Missing Font"))
    assert combo.is_inherited() is True


def _popup_font_combo(current_index: int = 0) -> WheelFocusedFontComboBox:
    families = tuple(f"Test Font {i:02d}" for i in range(20))
    combo = WheelFocusedFontComboBox(font_families_provider=lambda: families)
    combo.resize(240, 33)
    combo.setCurrentIndex(current_index)
    combo._showComboMenu()
    return combo


def _visible_font_texts(menu: _FilterableFontMenu) -> list[str]:
    view = menu.view
    return [
        view.item(row).text()
        for row in range(menu._FIRST_ITEM_ROW, view.count())
        if not view.item(row).isHidden()
    ]


def test_property_font_combo_popup_filters_families_by_search_text(qapp) -> None:
    combo = _popup_font_combo()
    menu = combo.dropMenu
    assert isinstance(menu, _FilterableFontMenu)

    menu._search.setText("02")

    assert _visible_font_texts(menu) == ["Test Font 02"]
    assert menu.view.item(menu.view.currentRow()).text() == "Test Font 02"
    # 隐藏行不影响 action 与条目的对应关系
    assert menu.action_for_item(0).text() == "Test Font 00"
    menu.action_for_item(5).trigger()
    assert combo.currentText() == "Test Font 05"

    menu.close()


def test_property_font_combo_popup_restores_full_list_when_filter_cleared(qapp) -> None:
    combo = _popup_font_combo(current_index=3)
    menu = combo.dropMenu
    menu._search.setText("19")

    assert _visible_font_texts(menu) == ["Test Font 19"]

    menu._search.clear()

    assert len(_visible_font_texts(menu)) == 20
    assert menu.view.currentRow() == menu._FIRST_ITEM_ROW + 3
    assert menu.view.item(menu._EMPTY_HINT_ROW).isHidden()

    menu.close()


def test_property_font_combo_popup_shows_hint_when_nothing_matches(qapp) -> None:
    combo = _popup_font_combo()
    menu = combo.dropMenu

    menu._search.setText("不存在的字体")

    assert _visible_font_texts(menu) == []
    assert menu.view.item(menu._EMPTY_HINT_ROW).isHidden() is False

    menu.close()


def test_property_font_combo_popup_enter_activates_first_visible_match(qapp) -> None:
    combo = _popup_font_combo()
    menu = combo.dropMenu
    menu._search.setText("07")
    event = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier
    )

    menu._search.keyPressEvent(event)

    assert combo.currentText() == "Test Font 07"


def test_property_font_menu_search_keys_move_selection_over_matches(qapp) -> None:
    combo = _popup_font_combo()
    menu = combo.dropMenu
    menu._search.setText("Test Font 1")
    first_row = menu.view.currentRow()

    menu._search.keyPressEvent(
        QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier
        )
    )

    assert menu.view.currentRow() == first_row + 1
    assert menu.view.item(menu.view.currentRow()).text() == "Test Font 11"

    menu.close()


def test_property_font_menu_pins_search_box_above_scrolling_list(qapp) -> None:
    combo = _popup_font_combo(current_index=15)
    menu = combo.dropMenu

    # 挂在 view 而非 viewport：viewport 子控件会随 contents scroll 一起挪动
    assert menu._search.parent() is menu.view
    strip = menu.view.viewportMargins().top()
    assert strip >= menu._SEARCH_TOP_INSET + menu._search.height()
    # show 之后立即就位，宽度随视口铺满（不能停在布局前的旧几何上）
    assert menu._search.width() >= 100
    y_pinned = menu._search.y()
    menu.view.scrollToBottom()
    assert menu._search.y() == y_pinned
    assert menu._search.width() <= menu.view.viewport().width()

    menu.close()


def test_property_font_combo_small_catalog_keeps_plain_popup(qapp) -> None:
    combo = WheelFocusedFontComboBox(
        font_families_provider=lambda: ("Only Font A", "Only Font B")
    )

    combo._showComboMenu()

    assert combo.dropMenu is not None
    assert not isinstance(combo.dropMenu, _FilterableFontMenu)
    combo.dropMenu.close()


def _mouse_press_event() -> QMouseEvent:
    return QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(1, 1),
        QPointF(1, 1),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def test_property_font_menu_uses_tool_window_for_ime_input(qapp) -> None:
    combo = _popup_font_combo()
    menu = combo.dropMenu
    assert isinstance(menu, _FilterableFontMenu)

    # Windows 的 IME 不附加到 Qt::Popup 窗口：筛选菜单必须用 Tool 窗口，
    # 否则搜索框只能敲英文直接按键，打不了中文。注意 Qt::Tool 的位定义
    # 本身就包含 Popup|Dialog 位，所以要按 windowType() 判断。
    assert menu.windowType() == Qt.WindowType.Tool
    assert int(menu.windowFlags()) & int(Qt.WindowType.FramelessWindowHint)

    menu.close()


def test_property_font_menu_closes_on_combo_press_without_reopen(qapp) -> None:
    combo = _popup_font_combo()
    menu = combo.dropMenu
    assert isinstance(menu, _FilterableFontMenu)

    menu.eventFilter(combo, _mouse_press_event())

    assert menu.isVisible() is False
    assert combo.dropMenu is None
    assert combo._suppress_combo_reopen is True

    # 同一次点击的 release 会再走 _showComboMenu：被置位牌吞掉，不重新弹开
    combo._showComboMenu()
    assert combo.dropMenu is None

    # 下一次点击正常打开
    combo._showComboMenu()
    assert isinstance(combo.dropMenu, _FilterableFontMenu)
    combo.dropMenu.close()


def test_property_font_menu_closes_on_press_elsewhere(qapp) -> None:
    combo = _popup_font_combo()
    menu = combo.dropMenu
    other = QWidget()

    menu.eventFilter(other, _mouse_press_event())

    assert menu.isVisible() is False
    # 非组合框区域的按下不立牌：下一次点击应正常打开
    assert combo._suppress_combo_reopen is False


def test_property_font_menu_keeps_open_for_press_inside(qapp) -> None:
    combo = _popup_font_combo()
    menu = combo.dropMenu

    menu.eventFilter(menu._search, _mouse_press_event())
    menu.eventFilter(menu.view, _mouse_press_event())

    assert menu.isVisible() is True
    menu.close()


def test_property_font_menu_ignores_window_stage_press_on_itself(qapp) -> None:
    combo = _popup_font_combo()
    menu = combo.dropMenu
    window = menu.windowHandle()

    # 真实鼠标按下先以 QWindow 为接收者经过应用级过滤器（子控件分发在其
    # 后）；窗口阶段不判定，否则菜单自家窗口上的任何点击都会被误判为
    # 「点在外部」而收起（滚动条不可点击的根因）。
    if window is not None:
        menu.eventFilter(window, _mouse_press_event())
        assert menu.isVisible() is True
    menu.close()


def test_property_font_menu_closes_on_window_deactivate(qapp) -> None:
    combo = _popup_font_combo()
    menu = combo.dropMenu

    menu.event(QEvent(QEvent.Type.WindowDeactivate))

    assert menu.isVisible() is False


def test_property_font_combo_reuses_cached_menu_across_opens(qapp) -> None:
    combo = _popup_font_combo()
    first = combo.dropMenu
    assert isinstance(first, _FilterableFontMenu)

    first.close()
    combo._showComboMenu()

    # 500+ 条目每次重建约 200ms（打开瞬间卡顿主因），重开必须复用同一实例
    assert combo.dropMenu is first


def test_property_font_combo_reopen_resets_stale_filter(qapp) -> None:
    combo = _popup_font_combo()
    menu = combo.dropMenu
    menu._search.setText("02")
    assert len(_visible_font_texts(menu)) == 1
    menu.close()

    combo._showComboMenu()

    assert combo.dropMenu is menu
    assert menu._search.text() == ""
    assert len(_visible_font_texts(menu)) == 20


def test_property_font_combo_popup_opens_without_slide_animation(qapp) -> None:
    from PyQt6.QtCore import QPropertyAnimation

    combo = _popup_font_combo()
    menu = combo.dropMenu

    # 不播滑动动画：动画从未启动，弹层直接停在方向管理器算出的终点
    # （NONE 类型会把弹层按全屏高度算尺寸并窜位，不能用它关动画）。
    assert menu.aniManager.ani.state() == QPropertyAnimation.State.Stopped
    assert menu.pos() == menu.aniManager._endPosition(menu._exec_pos)


def test_property_font_menu_reuse_does_not_stack_focus_timers(qapp) -> None:
    from PyQt6.QtCore import QTimer

    combo = _popup_font_combo()
    menu = combo.dropMenu
    for _ in range(3):
        menu.close()
        combo._showComboMenu()

    assert len(menu._search.findChildren(QTimer)) == 1


def test_property_font_combo_warmup_prebuilds_cached_menu(qapp) -> None:
    combo = _popup_font_combo()  # 等价于用户已抢先点开过
    menu = combo.dropMenu
    combo._warm_up_font_menu()  # 已有缓存时应直接跳过，不得另建实例
    assert combo._cached_font_menu is menu
    menu.close()

    fresh = WheelFocusedFontComboBox(
        font_families_provider=lambda: tuple(f"F{i:02d}" for i in range(20))
    )
    fresh._warm_up_font_menu()  # 未点开过：后台建好带全套条目的缓存

    assert fresh._cached_font_menu is not None
    # menuActions 含空提示行，比字体条目多一行
    assert len(fresh._cached_font_menu.menuActions()) == 21

    fresh._showComboMenu()
    assert fresh.dropMenu is fresh._cached_font_menu
    assert fresh.dropMenu.isVisible()
    fresh.dropMenu.close()


def test_font_picker_refreshes_after_registry_change_without_changing_selection(qapp):
    from PyQt6.QtGui import QFont
    from krok_helper.subtitle_render.frontend.properties.controls.inputs import WheelFocusedFontComboBox
    families = ["Original", "Second"]
    picker = WheelFocusedFontComboBox(font_families_provider=lambda: families,
                                     canonicalize_family=lambda name: name)
    picker.enable_inheritance("继承")
    picker.setCurrentFont(QFont("Original"))
    changes = []
    picker.currentFontChanged.connect(changes.append)
    families[:] = ["New Font", "Second"]
    qapp.fontDatabaseChanged.emit()
    picker._refresh_font_catalog()
    assert picker.findText("New Font") >= 0
    assert picker.currentText() == "Original"
    assert changes == []
    picker.setInherited()
    families.append("Another")
    qapp.fontDatabaseChanged.emit()
    picker._refresh_font_catalog()
    assert picker.is_inherited()
    assert picker.findText("Another") >= 0
    picker.deleteLater()
