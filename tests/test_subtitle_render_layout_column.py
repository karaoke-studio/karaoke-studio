"""歌词表格的「布局」列要跟着布局改动走。

右键「应用布局 → 3 行布局」之后，菜单里的单选点已经落在新布局上、预览也按新布局
渲染，但表格那一列还写着旧的 —— 因为布局是写在 **track**（``line.layout_index``）
上而不是 ``Style`` 上，宿主却靠 ``set_style`` 顺带触发重绘，而 ``set_style`` 后来
加了"样式签名没变就直接返回"的优化。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend.editor.lyrics_list import (  # noqa: E402
    COL_CONTENT,
    COL_LAYOUT,
)
from krok_helper.subtitle_render.frontend.main_window import (  # noqa: E402
    SubtitleRenderWindow,
)
from krok_helper.subtitle_render.models import (  # noqa: E402
    TimingChar,
    TimingLine,
    TimingTrack,
)


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    widget = SubtitleRenderWindow.for_embedding(settings_provider=None)
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(c, i * 300) for i, c in enumerate("速く駆けるよ")],
                end_ms=2_000,
            ),
            TimingLine(
                chars=[TimingChar(c, 2_000 + i * 300) for i, c in enumerate("君のもとへ")],
                end_ms=4_000,
            ),
        ]
    )
    widget._timing_track = track
    widget._lyrics_panel.set_track(track)
    app.processEvents()
    yield widget, track
    widget.close()
    widget.deleteLater()
    app.processEvents()


def _layout_cells(widget) -> list[str]:
    table = widget._lyrics_panel.table_widget
    cells = []
    for row in range(table.rowCount()):
        item = table.item(row, COL_LAYOUT)
        cells.append(item.text() if item is not None else "")
    return cells


def test_the_layout_column_follows_the_applied_layout(window) -> None:
    widget, track = window
    before = _layout_cells(widget)
    target = 3  # 「3 行布局」

    widget._on_layout_change_requested([0], target)
    QApplication.instance().processEvents()

    assert [line.layout_index for line in track.lines] == [target, target]
    after = _layout_cells(widget)
    assert after != before, f"布局列没刷新，还停在 {before}"
    expected = widget._style.layouts[target - 1].name
    assert all(cell == expected for cell in after if cell), after


def test_switching_back_updates_the_column_again(window) -> None:
    """来回切也要跟得上，不能只在第一次生效。"""
    widget, track = window

    widget._on_layout_change_requested([0], 3)
    QApplication.instance().processEvents()
    widget._on_layout_change_requested([0], 0)
    QApplication.instance().processEvents()

    assert [line.layout_index for line in track.lines] == [0, 0]
    cells = [cell for cell in _layout_cells(widget) if cell]
    assert all("2 行布局" in cell for cell in cells), cells


# ---------------------------------------------------------------------------
# 内容|布局 边界把手：Qt 把这条边界映射给 Stretch 的内容列（不可拖），
# 面板接管后应改为调整布局列。


def _send_header_mouse(viewport, etype, x, buttons=Qt.MouseButton.NoButton):
    global_pos = viewport.mapToGlobal(viewport.rect().topLeft())
    event = QMouseEvent(
        etype,
        QPointF(x, viewport.height() / 2),
        QPointF(global_pos.x() + x, global_pos.y() + viewport.height() / 2),
        Qt.MouseButton.LeftButton,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.instance().sendEvent(viewport, event)
    QApplication.instance().processEvents()


def _boundary_x(widget) -> int:
    header = widget._lyrics_panel.table_widget.horizontalHeader()
    return (
        header.sectionViewportPosition(COL_CONTENT)
        + header.sectionSize(COL_CONTENT)
    )


def _section_sizes(widget) -> list[int]:
    header = widget._lyrics_panel.table_widget.horizontalHeader()
    return [header.sectionSize(i) for i in range(5)]


def test_boundary_drag_resizes_layout_column(window) -> None:
    widget, _track = window
    widget.resize(1080, 640)
    widget.show()
    QApplication.instance().processEvents()
    header = widget._lyrics_panel.table_widget.horizontalHeader()
    viewport = header.viewport()
    boundary = _boundary_x(widget)
    before = _section_sizes(widget)
    minimum = widget._lyrics_panel._column_minimum_width(COL_LAYOUT)
    step = min(40, before[COL_LAYOUT] - minimum - 5)
    assert step >= 10, f"布局列太窄，压不出空间：{before} / min={minimum}"

    # 往右拖：压窄布局列（内容列吸收）
    _send_header_mouse(viewport, QEvent.Type.MouseButtonPress, boundary, Qt.MouseButton.LeftButton)
    _send_header_mouse(viewport, QEvent.Type.MouseMove, boundary + step, Qt.MouseButton.LeftButton)
    _send_header_mouse(viewport, QEvent.Type.MouseButtonRelease, boundary + step, Qt.MouseButton.LeftButton)
    narrowed = _section_sizes(widget)
    assert narrowed[COL_LAYOUT] == before[COL_LAYOUT] - step, (before, narrowed, step)
    assert abs(sum(narrowed) - sum(before)) <= 1, (before, narrowed)

    # 再往左拖回去：布局列放宽（钳制上限可能因 Stretch 取整差 1px）
    boundary2 = _boundary_x(widget)
    _send_header_mouse(viewport, QEvent.Type.MouseButtonPress, boundary2, Qt.MouseButton.LeftButton)
    _send_header_mouse(viewport, QEvent.Type.MouseMove, boundary2 - step, Qt.MouseButton.LeftButton)
    _send_header_mouse(viewport, QEvent.Type.MouseButtonRelease, boundary2 - step, Qt.MouseButton.LeftButton)
    restored = _section_sizes(widget)
    assert abs(restored[COL_LAYOUT] - before[COL_LAYOUT]) <= 1, (before, restored)
    assert abs(sum(restored) - sum(before)) <= 1, (before, restored)
    assert COL_LAYOUT in widget._lyrics_panel._user_sized_columns


def test_boundary_drag_right_clamps_at_minimum(window) -> None:
    widget, _track = window
    widget.resize(1080, 640)
    widget.show()
    QApplication.instance().processEvents()
    header = widget._lyrics_panel.table_widget.horizontalHeader()
    viewport = header.viewport()
    boundary = _boundary_x(widget)

    _send_header_mouse(viewport, QEvent.Type.MouseButtonPress, boundary, Qt.MouseButton.LeftButton)
    _send_header_mouse(viewport, QEvent.Type.MouseMove, boundary + 500, Qt.MouseButton.LeftButton)
    _send_header_mouse(viewport, QEvent.Type.MouseButtonRelease, boundary + 500, Qt.MouseButton.LeftButton)

    minimum = widget._lyrics_panel._column_minimum_width(COL_LAYOUT)
    assert header.sectionSize(COL_LAYOUT) == minimum


def test_press_outside_handle_not_hijacked(window) -> None:
    widget, _track = window
    widget.resize(1080, 640)
    widget.show()
    QApplication.instance().processEvents()
    header = widget._lyrics_panel.table_widget.horizontalHeader()
    viewport = header.viewport()
    boundary = _boundary_x(widget)
    before = _section_sizes(widget)

    # 内容列中部（远离把手区）按下并拖动：面板不应劫持，列宽不变
    inside_content = boundary - 60
    _send_header_mouse(viewport, QEvent.Type.MouseButtonPress, inside_content, Qt.MouseButton.LeftButton)
    _send_header_mouse(viewport, QEvent.Type.MouseMove, inside_content - 30, Qt.MouseButton.LeftButton)
    _send_header_mouse(viewport, QEvent.Type.MouseButtonRelease, inside_content - 30, Qt.MouseButton.LeftButton)

    assert widget._lyrics_panel._header_drag is None
    assert _section_sizes(widget) == before
