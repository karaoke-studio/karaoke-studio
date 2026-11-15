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
    COL_CONTENT,
    COL_EFFECT,
    COL_LANE,
    COL_LAYOUT,
    COL_ROLE,
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

    # 只改色点颜色：不在编辑当下刷新，攒到停手后走角色列窄刷新，
    # 不再触发整表重绘（避免 5 列全部重写 + 整视口重绘打断输入）。
    panel.set_style(
        replace(style, font_size_px=style.font_size_px + 10, fill_color="#123456")
    )
    assert calls == []
    assert panel._swatch_refresh_timer.isActive()
    panel._flush_swatch_refresh()
    assert calls == []

    # 布局档字段立即生效但走窄刷新（轨标/对齐/布局名），不再整表；
    # 同批的色点变化由色点窄刷新就地吸收，不留下待处理刷新。
    panel.set_style(
        replace(style, fill_color="#123456", line_alignments=["left", "center", "right"])
    )
    assert calls == []  # 整表未触发
    assert not panel._swatch_refresh_timer.isActive()  # 色点已被吸收

    panel.deleteLater()
    qapp.processEvents()


def test_swatch_flush_updates_role_column_only(qapp):
    """色点停手刷新是窄刷新：只重画角色列图标，其余列一律不碰。"""
    from PyQt6.QtCore import QPoint

    panel = LyricsPanel()
    style = Style()
    panel.set_style(style)
    panel.set_track(_track())

    lyric_row = next(
        row
        for row, item in enumerate(panel._presentation_rows)
        if item.kind == "lyric"
    )

    # 哨兵：内容 / 特效列在窄刷新后必须原样保留。
    content_item = panel.table_widget.item(lyric_row, COL_CONTENT)
    effect_item = panel.table_widget.item(lyric_row, COL_EFFECT)
    content_item.setText("sentinel-content")
    effect_item.setText("sentinel-effect")
    role_item = panel.table_widget.item(lyric_row, COL_ROLE)

    def icon_center_color() -> QColor:
        image = role_item.icon().pixmap(24, 24).toImage()
        return image.pixelColor(QPoint(12, 12))

    before = icon_center_color()

    panel.set_style(replace(style, fill_color="#FF0000"))
    panel._flush_swatch_refresh()

    after = icon_center_color()
    # 无方案行取全局 fill_color 作代表色：色点应变红。
    assert after.name().lower() == "#ff0000"
    assert before.name().lower() != after.name().lower()
    assert content_item.text() == "sentinel-content"
    assert effect_item.text() == "sentinel-effect"

    # 窄刷新与整表刷新共用同一取色口径：整表刷新（布局签名变化）后色点颜色一致。
    panel.set_style(
        replace(style, fill_color="#FF0000", line_alignments=["left", "left"])
    )
    assert icon_center_color().name().lower() == after.name().lower()

    panel.deleteLater()
    qapp.processEvents()


def test_gradient_swatch_refresh_invalidates_cached_icons_without_rebuilding_rows(qapp):
    from krok_helper.subtitle_render.domain.models import KaraokeColors, KaraokeColorState, PaintFill
    from krok_helper.subtitle_render.frontend.editor.lyrics_list import _mixed_swatch_icon

    panel = LyricsPanel()
    fill = PaintFill(mode="gradient_horizontal", color="#FF5A6F",
                     gradient_stops=[(0, "#FF0000"), (100, "#0000FF")])
    style = Style(karaoke_colors=KaraokeColors(after=KaraokeColorState(text=fill)))
    panel.set_style(style)
    panel.set_track(_track())
    role_item = panel.table_widget.item(2, COL_ROLE)
    content_item = panel.table_widget.item(2, COL_CONTENT)
    content_item.setText("sentinel")
    first = role_item.icon().pixmap(24, 24).toImage()
    first_mixed = _mixed_swatch_icon([fill, None])

    changed = replace(fill, gradient_stops=[(0, "#00FF00"), (100, "#0000FF")])
    panel.set_style(replace(style, karaoke_colors=KaraokeColors(after=KaraokeColorState(text=changed))))
    panel._flush_swatch_refresh()
    current = role_item.icon().pixmap(24, 24).toImage()
    assert current.pixelColor(7, 12).green() > first.pixelColor(7, 12).green() + 100
    assert "#00FF00" in role_item.toolTip()
    assert panel.table_widget.item(2, COL_CONTENT) is content_item
    assert content_item.text() == "sentinel"
    assert _mixed_swatch_icon([changed, None]).cacheKey() != first_mixed.cacheKey()
    assert _mixed_swatch_icon([changed, None]).cacheKey() == _mixed_swatch_icon([changed, None]).cacheKey()


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
    assert dialog._blank_enabled.text() == "空行识别为分段"
    assert dialog._blank_page_enabled.text() == "空行识别为分页"
    assert dialog._blank_page_enabled.toolTip()
    # 新默认：空行识别为分页开、分段关；两个方向都验证互斥联动。
    assert dialog._blank_page_enabled.isChecked()
    assert not dialog._blank_enabled.isChecked()
    dialog._blank_enabled.setChecked(True)
    assert not dialog._blank_page_enabled.isChecked()
    dialog._blank_enabled.setChecked(False)
    assert not dialog._blank_enabled.isChecked()
    dialog._blank_page_enabled.setChecked(True)
    assert dialog._blank_page_enabled.isChecked()
    assert not dialog._blank_enabled.isChecked()
    dialog._blank_enabled.setChecked(True)
    assert not dialog._blank_page_enabled.isChecked()
    dialog._blank_page_enabled.setChecked(True)
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


def test_subtitle_loading_settings_round_trips_keep_singer_label_text_flag() -> None:
    from krok_helper.subtitle_render.domain.models import (
        subtitle_loading_settings_from_dict,
        subtitle_loading_settings_to_dict,
    )

    payload = subtitle_loading_settings_to_dict(
        SubtitleLoadingSettings(keep_singer_label_text=True)
    )
    assert payload["keep_singer_label_text"] is True
    assert subtitle_loading_settings_from_dict(payload) == SubtitleLoadingSettings(
        keep_singer_label_text=True
    )

    # 旧项目快照没有该字段时回落默认值「关闭」，保证升级后既有行为不变。
    legacy = dict(payload)
    legacy.pop("keep_singer_label_text")
    assert (
        subtitle_loading_settings_from_dict(legacy).keep_singer_label_text is False
    )


def test_loading_settings_dialog_edits_keep_singer_label_text(qapp):
    parent = QWidget()
    dialog = _SubtitleLoadingSettingsDialog(
        mode="global",
        effective=SubtitleLoadingSettings(keep_singer_label_text=True),
        global_defaults=SubtitleLoadingSettings(keep_singer_label_text=True),
        anchor=None,
        parent=parent,
    )

    assert dialog._keep_singer_label_check.text() == "保留歌词中【xxx】演唱者名"
    assert dialog._keep_singer_label_check.toolTip()
    assert dialog._keep_singer_label_check.isChecked()
    dialog._keep_singer_label_check.setChecked(False)
    assert dialog.result_value() == ("global", SubtitleLoadingSettings())
    dialog._keep_singer_label_check.setChecked(True)
    assert dialog.result_value() == (
        "global",
        SubtitleLoadingSettings(keep_singer_label_text=True),
    )
    dialog.deleteLater()
    parent.deleteLater()
    qapp.processEvents()


def test_layout_scope_and_animation_scope_use_narrow_refresh(qapp):
    """布局档/动画档各自窄刷新：不触发整表，且产出的单元格值与整表一致。"""
    panel = LyricsPanel()
    style = Style()
    panel.set_style(style)
    panel.set_track(_track())

    full_calls: list[int] = []
    original_full = panel._refresh_presentation
    panel._refresh_presentation = lambda *a, **k: (  # type: ignore[method-assign]
        full_calls.append(1),
        original_full(*a, **k),
    )[1]

    # 动画档（entry_anim 类型）：窄刷特效列，不整表。
    panel.set_style(replace(style, entry_anim="fade"))
    assert full_calls == []
    lyric_row = next(
        row
        for row, item in enumerate(panel._presentation_rows)
        if item.kind == "lyric"
    )
    effect_text_narrow = panel.table_widget.item(lyric_row, COL_EFFECT).text()
    assert effect_text_narrow  # 摘要非空

    # 布局档（对齐）：窄刷轨标/对齐/布局名，不整表。
    panel.set_style(replace(style, entry_anim="fade", line_alignments=["left", "right", "center"]))
    assert full_calls == []
    lane_visible = not panel.table_widget.isColumnHidden(COL_LANE)
    assert lane_visible  # 双行布局轨列可见

    # 与整表对照：强制整表后，特效摘与轨标文本一致。
    panel._refresh_presentation()
    assert (
        panel.table_widget.item(lyric_row, COL_EFFECT).text()
        == effect_text_narrow
    )

    # 布局 + 动画同时变化（如预设切换）仍走整表。
    panel.set_style(
        replace(
            style,
            entry_anim="fade",
            line_alignments=["left", "right", "center"],
            dual_line_layout=not style.dual_line_layout,
        )
    )
    assert full_calls == [1]

    panel.deleteLater()
    qapp.processEvents()
