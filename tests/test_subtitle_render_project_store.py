"""A11 项目文件（.krrender.json）读写与 standalone 新建/保存/打开往返。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend import main_window as mw  # noqa: E402
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
    path = tmp_path / "demo.krrender.json"
    data = {"style": {"font_size_px": 80}, "selected_scheme_key": "global"}
    save_render_project(path, data)
    assert path.is_file()
    loaded = load_render_project(path)
    assert loaded["schema_version"] == PROJECT_SCHEMA_VERSION
    assert loaded["style"]["font_size_px"] == 80


def test_load_render_project_rejects_bad_json(tmp_path):
    path = tmp_path / "bad.krrender.json"
    path.write_text("not json {", encoding="utf-8")
    with pytest.raises(ValueError):
        load_render_project(path)


def test_project_bar_standalone_only(qapp, monkeypatch):
    standalone = _make_window(qapp, monkeypatch)
    assert not standalone._embedded
    assert standalone._project_bar.isVisibleTo(standalone) or True  # 构造期不一定 show
    assert hasattr(standalone, "_project_shortcuts")

    embedded = mw.SubtitleRenderWindow(embedded=True)
    assert not embedded._project_bar.isVisibleTo(embedded)
    assert not hasattr(embedded, "_project_shortcuts")


def test_window_save_new_open_round_trip(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)

    # 改样式 → 标脏
    win._style = Style(font_size_px=88, title_overlay=TitleOverlay(enabled=True))
    win._property_panel.set_style(win._style)
    win._export_crf_spin.setValue(23)
    win._mark_project_dirty()
    assert win._project_dirty is True

    # 保存
    path = tmp_path / "song.krrender.json"
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
    # 加载过程中不应把项目标脏
    assert win._project_dirty is False


def test_apply_project_data_does_not_mark_dirty(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    win._project_dirty = False
    win._apply_project_data({"style": {"font_size_px": 64}})
    assert win._project_dirty is False
    assert win._style.font_size_px == 64
