"""选中多行后直接点「角色 / 布局」单元格 → 选区保住、改动作用于整个选区。

以前只能走右键菜单：单击这两列会被 QTableWidget 在**按下**时把选区重置成点中的
那一行（``cellClicked`` 要等松开才发，那时多选早没了），于是只改得动一行。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend.editor import lyrics_list  # noqa: E402
from krok_helper.subtitle_render.frontend.editor.lyrics_list import (  # noqa: E402
    COL_CONTENT,
    COL_LAYOUT,
    COL_ROLE,
    LyricsPanel,
)
from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
)


def _track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(c, row * 1_000 + i * 100) for i, c in enumerate(text)],
                end_ms=row * 1_000 + 900,
            )
            for row, text in enumerate(["鬼さんこちら", "命短し", "大概歴史には"])
        ]
    )


@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication([])
    widget = LyricsPanel()
    widget.set_style(Style())
    widget.set_track(_track())
    widget.set_role_options(["和声"])
    widget.resize(900, 400)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def _display_rows_for(panel: LyricsPanel, track_rows: list[int]) -> list[int]:
    return [
        row
        for row in (panel._display_row_for_track_line(t) for t in track_rows)
        if row is not None
    ]


def _select_track_rows(panel: LyricsPanel, track_rows: list[int]) -> None:
    from PyQt6.QtCore import QItemSelectionModel

    table = panel.table_widget
    selection = table.selectionModel()
    selection.clearSelection()
    flags = (
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    )
    for display_row in _display_rows_for(panel, track_rows):
        selection.select(table.model().index(display_row, 0), flags)
    QApplication.instance().processEvents()


def _cell_point(panel: LyricsPanel, display_row: int, column: int) -> QPoint:
    item = panel.table_widget.item(display_row, column)
    assert item is not None, (display_row, column)
    return panel.table_widget.visualItemRect(item).center()


def _collect_menu_actions(panel: LyricsPanel, call) -> list:
    """把选择器里加进菜单的 Action 抓出来，不真的弹窗。"""
    actions: list = []
    original_add = lyrics_list._StableRoundMenu.addAction
    original_exec = lyrics_list._StableRoundMenu.exec
    lyrics_list._StableRoundMenu.addAction = lambda self, action: actions.append(action)
    lyrics_list._StableRoundMenu.exec = lambda self, *a, **k: None
    try:
        call()
    finally:
        lyrics_list._StableRoundMenu.addAction = original_add
        lyrics_list._StableRoundMenu.exec = original_exec
    return actions


# ── 选区判定 ────────────────────────────────────────────────


def test_the_picker_targets_the_whole_selection(panel) -> None:
    _select_track_rows(panel, [0, 1])

    assert sorted(panel._picker_target_rows(0)) == [0, 1]


def test_clicking_outside_the_selection_only_targets_that_row(panel) -> None:
    """点选区外的行仍然只改它自己，否则“点一行却改了别处”很怪。"""
    _select_track_rows(panel, [0, 1])

    assert panel._picker_target_rows(2) == [2]


# ── 按下拦截 ────────────────────────────────────────────────


def test_a_plain_click_on_a_selected_role_cell_is_consumed(panel) -> None:
    _select_track_rows(panel, [0, 1])
    point = _cell_point(panel, _display_rows_for(panel, [0])[0], COL_ROLE)

    consumed = panel._consume_press_for_batch_picker(
        point, Qt.KeyboardModifier.NoModifier
    )

    assert consumed is True
    # 吃掉按下 = 表格没机会重置选区
    assert sorted(panel._selected_track_rows()) == [0, 1]


@pytest.mark.parametrize("column", [COL_ROLE, COL_LAYOUT])
def test_modified_clicks_are_left_alone(panel, column: int) -> None:
    """Ctrl / Shift 点选必须照常工作。"""
    _select_track_rows(panel, [0, 1])
    point = _cell_point(panel, _display_rows_for(panel, [0])[0], column)

    for modifier in (
        Qt.KeyboardModifier.ControlModifier,
        Qt.KeyboardModifier.ShiftModifier,
    ):
        assert panel._consume_press_for_batch_picker(point, modifier) is False


def test_other_columns_are_left_alone(panel) -> None:
    """只拦这两列，别把内容列的框选起点也吃掉。"""
    _select_track_rows(panel, [0, 1])
    point = _cell_point(panel, _display_rows_for(panel, [0])[0], COL_CONTENT)

    assert (
        panel._consume_press_for_batch_picker(point, Qt.KeyboardModifier.NoModifier)
        is False
    )


def test_a_single_row_selection_goes_through_the_old_path(panel) -> None:
    """只选了一行时没必要特殊对待，走原来的 cellClicked。"""
    _select_track_rows(panel, [0])
    point = _cell_point(panel, _display_rows_for(panel, [0])[0], COL_ROLE)

    assert (
        panel._consume_press_for_batch_picker(point, Qt.KeyboardModifier.NoModifier)
        is False
    )


def test_clicking_an_unselected_row_goes_through_the_old_path(panel) -> None:
    _select_track_rows(panel, [0, 1])
    point = _cell_point(panel, _display_rows_for(panel, [2])[0], COL_ROLE)

    assert (
        panel._consume_press_for_batch_picker(point, Qt.KeyboardModifier.NoModifier)
        is False
    )


# ── 真的批量改 ──────────────────────────────────────────────


def test_the_layout_picker_emits_every_selected_row(panel) -> None:
    seen: list[tuple[list, int]] = []
    panel.layoutChangeRequested.connect(lambda rows, idx: seen.append((list(rows), idx)))
    _select_track_rows(panel, [0, 1])

    display_row = _display_rows_for(panel, [0])[0]
    actions = _collect_menu_actions(
        panel, lambda: panel._show_layout_picker(0, display_row)
    )
    triggerable = [a for a in actions if a.isEnabled() and a.isCheckable()]

    assert triggerable, "布局菜单是空的"
    triggerable[-1].trigger()

    assert seen and sorted(seen[-1][0]) == [0, 1]


def test_the_role_picker_applies_to_every_selected_row(panel) -> None:
    seen: list[tuple[list, str]] = []
    panel.roleChangeRequested.connect(lambda rows, name: seen.append((list(rows), name)))
    _select_track_rows(panel, [0, 1])

    actions = _collect_menu_actions(panel, lambda: panel._show_role_picker(0))
    triggerable = [a for a in actions if a.isEnabled() and a.isCheckable()]

    assert triggerable, "角色菜单是空的"
    triggerable[-1].trigger()

    assert seen and sorted(seen[-1][0]) == [0, 1]


def test_a_mixed_layout_selection_checks_nothing(panel) -> None:
    """选区里布局不一致时不该勾任何一项 —— 勾了就是在说谎。"""
    panel._track.lines[0].layout_index = 0
    panel._track.lines[1].layout_index = 2
    _select_track_rows(panel, [0, 1])

    display_row = _display_rows_for(panel, [0])[0]
    actions = _collect_menu_actions(
        panel, lambda: panel._show_layout_picker(0, display_row)
    )

    assert not any(a.isCheckable() and a.isChecked() for a in actions)
    assert any("多个布局" in a.text() for a in actions), [a.text() for a in actions]


def test_a_multi_row_picker_says_how_many_rows_it_will_touch(panel) -> None:
    _select_track_rows(panel, [0, 1])

    actions = _collect_menu_actions(panel, lambda: panel._show_role_picker(0))

    assert any("应用到所选 2 行" in a.text() for a in actions), [a.text() for a in actions]


def test_the_event_filter_swallows_the_press(panel) -> None:
    """拦截必须真的接在 viewport 的事件过滤器上，光有方法不算数。"""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QMouseEvent

    _select_track_rows(panel, [0, 1])
    point = _cell_point(panel, _display_rows_for(panel, [0])[0], COL_LAYOUT)
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(point),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    handled = panel.eventFilter(panel.table_widget.viewport(), event)

    assert handled is True, "按下没被吃掉，表格会把多选重置成一行"
    assert sorted(panel._selected_track_rows()) == [0, 1]


def test_the_event_filter_lets_other_columns_through(panel) -> None:
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QMouseEvent

    _select_track_rows(panel, [0, 1])
    point = _cell_point(panel, _display_rows_for(panel, [0])[0], COL_CONTENT)
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(point),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    assert panel.eventFilter(panel.table_widget.viewport(), event) is not True
