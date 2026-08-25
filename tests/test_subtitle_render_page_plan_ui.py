from __future__ import annotations

from dataclasses import replace
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from krok_helper.subtitle_render.engine.layout.page.plan import (
    project_page_plan_to_legacy_fields,
)
from krok_helper.subtitle_render.frontend.editor.lyrics_list import (
    COL_LANE,
    COL_LAYOUT,
    LyricsPanel,
)
from krok_helper.subtitle_render.frontend.main_window import (
    _SubtitleLoadingSettingsDialog,
)
from krok_helper.subtitle_render.domain.models import (
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


def test_boundary_drag_mapping_allows_adjacent_sections(qapp):
    track = _track()
    track.page_plan = TrackPagePlan(
        [
            TrackSection([TrackPage(1, "builtin-1")]),
            TrackSection([TrackPage(1, "builtin-1")]),
            TrackSection([TrackPage(2, "builtin-2")]),
        ]
    )
    project_page_plan_to_legacy_fields(track, Style())
    panel = LyricsPanel()
    panel.set_style(Style())
    panel.set_track(track)

    lyric_rows = [
        row
        for row, item in enumerate(panel._presentation_rows)
        if item.track_line_index is not None
    ]
    assert panel._page_boundary_move_for_drag(lyric_rows[1], lyric_rows[0]) == (
        0,
        0,
        1,
    )
    assert panel._page_boundary_move_for_drag(lyric_rows[0], lyric_rows[1]) == (
        0,
        0,
        -1,
    )

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

    # 只改色点颜色：不在编辑当下刷新，攒到停手后刷一次，避免整表重绘打断输入。
    panel.set_style(
        replace(style, font_size_px=style.font_size_px + 10, fill_color="#123456")
    )
    assert calls == []
    assert panel._swatch_refresh_timer.isActive()
    panel._flush_swatch_refresh()
    assert calls == [1]

    # 影响行内容/列语义的字段仍然立即刷新，并且不留下待处理的色点刷新。
    panel.set_style(
        replace(style, fill_color="#123456", line_alignments=["left", "center", "right"])
    )
    assert calls == [1, 1]
    assert not panel._swatch_refresh_timer.isActive()

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
    assert dialog._actual_rows_layout.text() == "根据实际行数分配布局"
    assert dialog._actual_rows_layout.toolTip()
    assert not dialog._actual_rows_layout.isChecked()
    assert dialog._sug_offset_check.text() == "读取 .sug 时应用打轴模块的软件导出补偿"
    assert dialog._sug_offset_check.toolTip()
    assert dialog._sug_offset_check.isChecked()
    dialog._sug_offset_check.setChecked(False)
    assert dialog.result_value() == (
        "global",
        SubtitleLoadingSettings(apply_sug_export_compensation=False),
    )
    dialog._sug_offset_check.setChecked(True)
    assert dialog._mode_combo.toolTip()
    assert dialog.result_value() == ("global", defaults)
    dialog.deleteLater()
    parent.deleteLater()
    qapp.processEvents()


def test_sug_software_compensation_reads_timing_module_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """补偿值来自宿主 lyrics_timing 命名空间的 export.software_compensation_ms。"""
    from types import SimpleNamespace

    from krok_helper.subtitle_render.frontend import main_window as render_window

    def fake_settings(value):
        def load():
            return SimpleNamespace(lyrics_timing=value)

        return load

    cases = [
        ({"export": {"software_compensation_ms": -300}}, -300),
        ({"export": {"software_compensation_ms": 240}}, 240),
        # 字段缺失 / 命名空间缺失 / 值脏 → 0（SUG 侧同款默认）。
        ({"export": {}}, 0),
        ({}, 0),
        ({"export": {"software_compensation_ms": "abc"}}, 0),
    ]
    for payload, expected in cases:
        monkeypatch.setattr(
            render_window, "load_app_settings", fake_settings(payload)
        )
        assert render_window._sug_software_compensation_ms() == expected, payload

    def broken():
        raise OSError("settings unreadable")

    monkeypatch.setattr(render_window, "load_app_settings", broken)
    assert render_window._sug_software_compensation_ms() == 0
