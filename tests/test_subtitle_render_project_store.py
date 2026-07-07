"""A11 项目文件（.yurika）读写与 standalone 新建/保存/打开往返。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu  # noqa: E402
from qfluentwidgets.components.widgets.menu import MenuAnimationType  # noqa: E402

from krok_helper.subtitle_render.frontend import main_window as mw  # noqa: E402
from krok_helper.subtitle_render.frontend import lyrics_list  # noqa: E402
from krok_helper.subtitle_render.models import Style, TitleOverlay  # noqa: E402
from krok_helper.subtitle_render.project_store import (  # noqa: E402
    PROJECT_SCHEMA_VERSION,
    load_render_project,
    save_render_project,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_window(qapp, monkeypatch):
    monkeypatch.setattr(mw.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(
        mw.SubtitleRenderWindow, "_resolve_ffprobe_path", lambda self: "ffprobe"
    )
    return mw.SubtitleRenderWindow(embedded=False)


def test_save_render_project_round_trip(tmp_path):
    path = tmp_path / "demo.yurika"
    data = {"style": {"font_size_px": 80}, "selected_scheme_key": "global"}
    save_render_project(path, data)
    assert path.is_file()
    loaded = load_render_project(path)
    assert loaded["schema_version"] == PROJECT_SCHEMA_VERSION
    assert loaded["style"]["font_size_px"] == 80


def test_load_render_project_rejects_bad_json(tmp_path):
    path = tmp_path / "bad.yurika"
    path.write_text("not json {", encoding="utf-8")
    with pytest.raises(ValueError):
        load_render_project(path)


def test_project_bar_present_in_both_modes(qapp, monkeypatch):
    # 项目命令栏与快捷键在 standalone 与嵌入模式下都提供。
    standalone = _make_window(qapp, monkeypatch)
    assert standalone._project_bar is not None
    assert hasattr(standalone, "_project_shortcuts")

    embedded = mw.SubtitleRenderWindow(embedded=True)
    assert embedded._project_bar is not None
    assert hasattr(embedded, "_project_shortcuts")


def test_window_save_new_open_round_trip(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)

    # 改样式 → 标脏
    win._style = Style(font_size_px=88, title_overlay=TitleOverlay(enabled=True))
    win._property_panel.set_style(win._style)
    win._export_crf_spin.setValue(23)
    win._export_native_check.setChecked(True)
    win._mark_project_dirty()
    assert win._project_dirty is True

    # 保存
    path = tmp_path / "song.yurika"
    assert win._write_project(path) is True
    assert win._project_dirty is False
    assert win._project_path == path

    # 新建重置为默认
    win._new_project()
    assert win._style.font_size_px == Style().font_size_px
    assert win._project_path is None
    assert win._project_dirty is False

    # 打开恢复
    data = load_render_project(path)
    win._apply_project_data(data)
    assert win._style.font_size_px == 88
    assert win._style.title_overlay is not None and win._style.title_overlay.enabled
    assert win._export_crf_spin.value() == 23
    assert win._export_native_check.isChecked() is False
    # 加载过程中不应把项目标脏
    assert win._project_dirty is False


def test_apply_project_data_does_not_mark_dirty(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    win._project_dirty = False
    win._apply_project_data({"style": {"font_size_px": 64}})
    assert win._project_dirty is False
    assert win._style.font_size_px == 64


def test_new_project_clears_loaded_media(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    lrc = tmp_path / "demo.lrc"
    lrc.write_bytes(
        b"\xef\xbb\xbf" + "[00:01:00]a[00:01:50]b[00:02:00]\r\n\r\n@Title=Foo\r\n".encode("utf-8")
    )
    assert win.load_from_lrc(lrc) is not None
    assert win._lyrics_panel.is_populated()
    assert win._preview_panel.is_populated()

    win._project_dirty = False  # 避开放弃确认弹窗
    win._new_project()
    assert win._timing_track is None
    assert win._subtitle_path is None and win._video_path is None
    assert not win._lyrics_panel.is_populated()
    assert not win._preview_panel.is_populated()


def test_preview_canvas_does_not_swallow_drops(qapp, monkeypatch):
    # 预览画布（QGraphicsView）默认会吞拖拽；必须关掉它，让拖拽冒泡到 DropPanel，
    # 这样预览被填充后仍能往播放区拖入新视频。
    win = _make_window(qapp, monkeypatch)
    canvas = win._preview_panel.canvas
    assert canvas.acceptDrops() is False
    if hasattr(canvas, "viewport"):
        assert canvas.viewport().acceptDrops() is False
    # 外层 DropPanel 仍接受拖拽
    assert win._preview_panel.acceptDrops() is True


def _write_demo_lrc(path, text="[00:01:00]あ[00:01:50]い[00:02:00]\r\n"):
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    return path


def test_extra_subtitle_sources_round_trip(qapp, monkeypatch, tmp_path):
    """副字幕源（N3 多歌词文件）随 .yurika 保存/恢复：路径、每行布局、逐字角色。"""
    win = _make_window(qapp, monkeypatch)
    main_lrc = _write_demo_lrc(tmp_path / "main.lrc")
    chorus_lrc = _write_demo_lrc(tmp_path / "chorus.lrc", "[00:10:00]ラ[00:11:00]ラ[00:12:00]\r\n")
    assert win.load_from_lrc(main_lrc) is not None

    from krok_helper.subtitle_render.frontend.main_window import ExtraSubtitleSource
    from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc

    chorus_track = load_nicokara_lrc(chorus_lrc)
    chorus_track.lines[0].layout_index = 1
    for ch in chorus_track.lines[0].chars:
        ch.role_label = "コーラス配色"
    win._extra_sources.append(
        ExtraSubtitleSource(name="コーラス1", path=chorus_lrc, track=chorus_track)
    )

    data = win._current_project_data()
    extras = data.get("extra_subtitle_sources")
    assert extras and extras[0]["name"] == "コーラス1"
    assert extras[0]["line_layout_indices"] == [1]
    assert extras[0]["char_role_labels"] == [["コーラス配色", "コーラス配色"]]

    # 恢复到新窗口（布局数量需覆盖 layout_index=1）
    from dataclasses import replace as dc_replace
    from krok_helper.subtitle_render.models import LyricsLayout, style_to_dict

    data["style"] = style_to_dict(
        dc_replace(win._style, layouts=[LyricsLayout(name="コーラス")])
    )
    win2 = _make_window(qapp, monkeypatch)
    win2._apply_project_data(data)
    assert len(win2._extra_sources) == 1
    restored = win2._extra_sources[0]
    assert restored.name == "コーラス1"
    assert restored.track.lines[0].layout_index == 1
    assert [c.role_label for c in restored.track.lines[0].chars] == ["コーラス配色", "コーラス配色"]
    combo = win2._lyrics_panel._source_combo
    assert [combo.itemText(i) for i in range(combo.count())] == ["主字幕", "コーラス1"]


def test_lyrics_combo_menu_disables_racy_popup_animation(qapp, monkeypatch):
    """菜单提前关闭时不得留下访问已删除 view 的动画回调。"""
    seen = {}

    def fake_exec(self, pos, ani=True, aniType=MenuAnimationType.DROP_DOWN):
        seen["animation"] = aniType

    monkeypatch.setattr(ComboBoxMenu, "exec", fake_exec)
    combo = lyrics_list._StableFluentComboBox()
    menu = combo._createComboMenu()
    menu.exec(QPoint(0, 0), aniType=MenuAnimationType.DROP_DOWN)

    assert seen["animation"] == MenuAnimationType.NONE
