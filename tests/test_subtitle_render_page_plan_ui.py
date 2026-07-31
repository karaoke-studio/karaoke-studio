from __future__ import annotations

from dataclasses import replace
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from krok_helper.subtitle_render.engine.page_plan import (
    project_page_plan_to_legacy_fields,
)
from krok_helper.subtitle_render.frontend.lyrics_list import (
    COL_LANE,
    COL_LAYOUT,
    LyricsPanel,
)
from krok_helper.subtitle_render.frontend.main_window import (
    _SubtitleLoadingSettingsDialog,
)
from krok_helper.subtitle_render.models import (
    Style,
    SubtitleLoadingSettings,
    TimingChar,
    TimingLine,
    TimingTrack,
    TrackPage,
    TrackPagePlan,
    TrackSection,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _track() -> TimingTrack:
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(str(index), index * 1000)],
                end_ms=index * 1000 + 500,
            )
            for index in range(4)
        ]
    )
    track.page_plan = TrackPagePlan(
        [TrackSection([TrackPage(2, "default"), TrackPage(2, "default")])]
    )
    project_page_plan_to_legacy_fields(track, Style())
    return track


def test_page_markers_layout_column_and_boundary_drag_mapping(qapp):
    panel = LyricsPanel()
    panel.set_style(Style())
    panel.set_track(_track())
    table = panel.table_widget

    assert table.columnCount() == 5
    assert [table.item(row, COL_LANE).text() for row in range(table.rowCount())] == [
        "♪♪♪ S1 ♪♪♪",
        "♪ S1 · P1 ♪",
        "T1",
        "T2",
        "♪ S1 · P2 ♪",
        "T1",
        "T2",
    ]
    assert table.item(2, COL_LAYOUT).text() == "2 行布局（默认）"
    assert panel._page_boundary_move_for_drag(5, 3) == (0, 0, 1)

    panel._on_cell_clicked(1, COL_LANE)
    selected = {item.row() for item in table.selectedItems()}
    assert {1, 2, 3} <= selected
    panel.deleteLater()
    qapp.processEvents()


def test_render_only_style_change_skips_full_table_refresh(qapp):
    """字号这类只作用于画面的字段不能触发整表刷新。

    整表刷新要为每行重建单元格、算对齐并画角色色点图标；属性面板里绝大多数
    控件都跟本表无关，跟着刷新只是白烧 GUI 线程。
    """
    panel = LyricsPanel()
    style = Style()
    panel.set_style(style)
    panel.set_track(_track())

    calls: list[int] = []
    original = panel._refresh_presentation
    panel._refresh_presentation = lambda *a, **k: (  # type: ignore[method-assign]
        calls.append(1),
        original(*a, **k),
    )[1]

    panel.set_style(replace(style, font_size_px=style.font_size_px + 10))
    assert calls == []

    # 真正影响呈现的字段照旧刷新。
    panel.set_style(replace(style, font_size_px=style.font_size_px + 10, fill_color="#123456"))
    assert calls == [1]

    panel.deleteLater()
    qapp.processEvents()


def test_loading_settings_card_is_isolated_and_fully_described(qapp):
    parent = QWidget()
    defaults = SubtitleLoadingSettings()
    dialog = _SubtitleLoadingSettingsDialog(
        mode="global",
        effective=defaults,
        global_defaults=defaults,
        anchor=None,
        parent=parent,
    )

    assert dialog.windowTitle() == "加载字幕设置"
    assert dialog._rows_spin.minimum() == 1
    assert dialog._rows_spin.maximum() == 4
    assert dialog._gap_enabled.toolTip()
    assert dialog._gap_spin.toolTip()
    assert dialog._blank_enabled.toolTip()
    assert "也使用 3 行默认布局" in dialog._rows_spin.toolTip()
    assert dialog._mode_combo.toolTip()
    assert dialog.result_value() == ("global", defaults)
    dialog.deleteLater()
    parent.deleteLater()
    qapp.processEvents()
