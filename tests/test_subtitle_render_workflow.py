from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper import gui_qt  # noqa: E402
from krok_helper.gui_qt import (  # noqa: E402
    KrokHelperQtApp,
    WORKFLOW_HIRES_MIX,
    WORKFLOW_SUBTITLE_RENDER,
)
from krok_helper.subtitle_render.frontend.main_window import (  # noqa: E402
    SubtitleRenderWindow,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_prepare_subtitle_render_exports_sug_project_to_lrc_and_loads_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, object] = {}
    project = SimpleNamespace(
        metadata=SimpleNamespace(title="Song:Title"),
        singers=[
            SimpleNamespace(id="s1", name="主唱"),
            SimpleNamespace(id="s2", name="和声"),
        ],
    )

    class FakeResult:
        success = True
        file_path = str(tmp_path / "Song_Title.lrc")
        error_message = None

    class FakeExportService:
        def export(self, project_arg, format_name, file_path, **kwargs):
            calls["project"] = project_arg
            calls["format_name"] = format_name
            calls["file_path"] = Path(file_path)
            calls["kwargs"] = kwargs
            Path(file_path).write_text("@Title=Song\r\n", encoding="utf-8")
            return FakeResult()

    class FakeSubtitlePage:
        def load_from_lrc(self, path: Path):
            calls["loaded_lrc"] = path
            return object()

    monkeypatch.setattr(gui_qt, "ExportService", FakeExportService, raising=False)
    monkeypatch.setattr(gui_qt.QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(gui_qt.QMessageBox, "critical", lambda *args, **kwargs: None)

    app = SimpleNamespace(
        lyrics_timing_page=SimpleNamespace(
            _store=SimpleNamespace(project=project, save_path=tmp_path / "song.sug")
        ),
        subtitle_render_page=FakeSubtitlePage(),
        _show_module=lambda module_id: calls.setdefault("shown", module_id),
    )

    result = KrokHelperQtApp._prepare_subtitle_render_from_workflow(app)

    assert result == tmp_path / "Song_Title.lrc"
    assert calls["project"] is project
    assert calls["format_name"] == "Nicokara (带注音)"
    assert calls["file_path"] == tmp_path / "Song_Title.lrc"
    assert calls["kwargs"] == {
        "singer_ids": None,
        "insert_singer_tags": True,
        "insert_singer_each_line": False,
        "singer_map": {"s1": "主唱", "s2": "和声"},
    }
    assert calls["loaded_lrc"] == tmp_path / "Song_Title.lrc"
    assert calls["shown"] == WORKFLOW_SUBTITLE_RENDER


def test_entering_subtitle_render_prepares_workflow_once() -> None:
    calls: list[str] = []
    widget = object()

    app = SimpleNamespace(
        module_pages={WORKFLOW_SUBTITLE_RENDER: widget},
        active_module="lyrics_timing",
        page_stack=SimpleNamespace(setCurrentWidget=lambda page: calls.append(f"page:{page is widget}")),
        workflow_stepper=SimpleNamespace(setCurrentModule=lambda module: calls.append(f"step:{module}")),
        _sync_page_stack_margins=lambda module: calls.append(f"margin:{module}"),
        _sync_workflow_shortcut_scope=lambda: calls.append("shortcuts"),
        _prepare_subtitle_render_from_workflow=lambda: calls.append("prepare") or Path("song.lrc"),
    )

    KrokHelperQtApp._show_module(app, WORKFLOW_SUBTITLE_RENDER)

    assert calls == [
        "prepare",
        f"margin:{WORKFLOW_SUBTITLE_RENDER}",
        "page:True",
        f"step:{WORKFLOW_SUBTITLE_RENDER}",
        "shortcuts",
    ]


def test_subtitle_render_success_passes_output_to_workflow_context(
    qapp, tmp_path: Path
) -> None:
    received: dict[str, Path] = {}

    class Context:
        def accept_subtitle_video(self, path: Path) -> None:
            received["path"] = path

    window = SubtitleRenderWindow(embedded=True, workflow_context=Context())
    output = tmp_path / "subtitle.mp4"

    window._finish_render_success(output)

    assert received["path"] == output


def test_accept_subtitle_video_fills_hires_video_and_switches_page(tmp_path: Path) -> None:
    output = tmp_path / "subtitle.mp4"
    calls: list[object] = []

    app = SimpleNamespace(
        set_video_path=lambda path: calls.append(("video", path)),
        _show_module=lambda module_id: calls.append(("show", module_id)),
    )

    KrokHelperQtApp.accept_subtitle_video(app, output)

    assert calls == [("video", output), ("show", WORKFLOW_HIRES_MIX)]
