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

from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend.lyrics_list import COL_LAYOUT  # noqa: E402
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
