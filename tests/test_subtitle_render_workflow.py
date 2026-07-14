from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

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


def test_entering_subtitle_render_only_switches_page() -> None:
    calls: list[str] = []
    widget = object()

    app = SimpleNamespace(
        module_pages={WORKFLOW_SUBTITLE_RENDER: widget},
        active_module="lyrics_timing",
        page_stack=SimpleNamespace(setCurrentWidget=lambda page: calls.append(f"page:{page is widget}")),
        workflow_stepper=SimpleNamespace(setCurrentModule=lambda module: calls.append(f"step:{module}")),
        _sync_page_stack_margins=lambda module: calls.append(f"margin:{module}"),
        _sync_workflow_shortcut_scope=lambda: calls.append("shortcuts"),
        _prepare_subtitle_render_from_workflow=lambda: calls.append("prepare"),
    )

    KrokHelperQtApp._show_module(app, WORKFLOW_SUBTITLE_RENDER)

    assert calls == [
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


# ---------------------------------------------------------------------------
# 字幕轨道显示/隐藏时间编辑的撤销 / 重做（Ctrl+Z / Ctrl+Y）
# ---------------------------------------------------------------------------


def _bind_undo_host(track, extra_sources=()):
    """把撤销相关真实方法绑到轻量宿主上，避免构造完整窗口。"""
    from krok_helper.subtitle_render.frontend.main_window import (
        SubtitleRenderWindow as W,
    )

    host = SimpleNamespace(
        _undo_stack=[],
        _redo_stack=[],
        _timing_track=track,
        _extra_sources=list(extra_sources),
        refreshed=[],
    )
    for name in (
        "_on_display_window_edited",
        "_undo_edit",
        "_redo_edit",
        "_restore_display_override",
        "_restore_animation_overrides",
        "_on_line_animation_override_requested",
        "_track_by_index",
        "_clear_undo_history",
    ):
        setattr(host, name, getattr(W, name).__get__(host))
    host._refresh_after_display_edit = host.refreshed.append
    host._active_source_index = 0
    host._lyrics_panel = SimpleNamespace(refresh_row_effect=lambda row: None)
    return host


def _undo_track():
    from krok_helper.subtitle_render.models import TimingChar, TimingLine, TimingTrack

    line = TimingLine(chars=[TimingChar("あ", 1000)], end_ms=2000)
    return TimingTrack(lines=[line])


def test_undo_redo_display_window_edit() -> None:
    track = _undo_track()
    host = _bind_undo_host(track)
    line = track.lines[0]

    # 模拟一次拖动：视图已写入新值后上报旧值 → 新值
    line.display_start_override_ms = 300
    host._on_display_window_edited(0, 0, (None, None), (300, None))
    assert len(host._undo_stack) == 1
    assert host.refreshed == [0]

    host._undo_edit()
    assert line.display_start_override_ms is None
    assert line.display_end_override_ms is None
    assert host._undo_stack == []
    assert len(host._redo_stack) == 1

    host._redo_edit()
    assert line.display_start_override_ms == 300
    assert len(host._undo_stack) == 1
    assert host._redo_stack == []


def test_new_edit_clears_redo_stack() -> None:
    track = _undo_track()
    host = _bind_undo_host(track)
    line = track.lines[0]

    line.display_start_override_ms = 300
    host._on_display_window_edited(0, 0, (None, None), (300, None))
    host._undo_edit()
    assert len(host._redo_stack) == 1

    line.display_end_override_ms = 5000
    host._on_display_window_edited(0, 0, (None, None), (None, 5000))
    assert host._redo_stack == []


def test_undo_skips_stale_entries() -> None:
    track = _undo_track()
    host = _bind_undo_host(track)
    line = track.lines[0]

    line.display_start_override_ms = 300
    host._on_display_window_edited(0, 0, (None, None), (300, None))
    # 一条指向已移除副字幕源的失效记录压在栈顶
    host._undo_stack.append((5, 0, (None, None), (111, None)))

    host._undo_edit()
    # 失效记录被丢弃，继续撤销到有效记录
    assert line.display_start_override_ms is None
    assert host._undo_stack == []


def test_line_animation_batch_edit_supports_undo_redo() -> None:
    from krok_helper.subtitle_render.models import LineAnimationOverride, TimingChar, TimingLine, TimingTrack

    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("あ", 1000)], end_ms=2000),
            TimingLine(chars=[TimingChar("い", 3000)], end_ms=4000),
        ]
    )
    host = _bind_undo_host(track)
    override = LineAnimationOverride(
        entry_anim="slide_in",
        entry_duration_ms=450,
        exit_anim="fade",
        exit_duration_ms=300,
    )

    host._on_line_animation_override_requested([0, 1], override)
    assert [line.animation_override for line in track.lines] == [override, override]

    host._undo_edit()
    assert [line.animation_override for line in track.lines] == [None, None]

    host._redo_edit()
    assert [line.animation_override for line in track.lines] == [override, override]
