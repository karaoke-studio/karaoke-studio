from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import krok_helper.gui_qt as gui_qt
from krok_helper.gui_qt import (
    KrokHelperQtApp,
    WORKFLOW_HIRES_MIX,
    WORKFLOW_LYRICS_TIMING,
    WORKFLOW_WAVEFORM_ALIGN,
)


class _FakeShortcut:
    def __init__(self) -> None:
        self.enabled: bool | None = None

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt-style API
        self.enabled = enabled


class _FakeTimingPage:
    def __init__(self) -> None:
        self.save_count = 0
        self.flush_count = 0
        self.opened_projects: list[str] = []
        self.editorInterface = _FakeEditor()

    def trigger_save(self) -> None:
        self.save_count += 1

    def open_initial_project(self, project_path: str) -> None:
        self.opened_projects.append(project_path)

    def flush_unsaved(self) -> None:
        self.flush_count += 1


class _FakeProjectPage:
    def __init__(self, *, dirty: bool = True, save_result: bool = True) -> None:
        self.dirty = dirty
        self.save_result = save_result
        self.save_count = 0
        self.discard_count = 0
        self.flush_count = 0

    def has_unsaved_changes(self) -> bool:
        return self.dirty

    def trigger_save(self) -> bool:
        self.save_count += 1
        if self.save_result:
            self.dirty = False
        return self.save_result

    def discard_unsaved(self) -> None:
        self.discard_count += 1
        self.dirty = False

    def flush_unsaved(self) -> None:
        self.flush_count += 1


class _FakeCloseEvent:
    def __init__(self) -> None:
        self.ignored = False

    def ignore(self) -> None:
        self.ignored = True


class _FakeStore:
    def __init__(self) -> None:
        self.cleanup_count = 0

    def cleanup_temp_files(self) -> None:
        self.cleanup_count += 1


class _FakeEditor:
    def __init__(self) -> None:
        self.release_count = 0

    def release_resources(self) -> None:
        self.release_count += 1


class _FakeAudioEngine:
    def __init__(self) -> None:
        self.release_count = 0

    def release(self) -> None:
        self.release_count += 1


def _fake_app(module_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        active_module=module_id,
        shortcut_space=_FakeShortcut(),
        shortcut_auto=_FakeShortcut(),
        shortcut_drag_mode=_FakeShortcut(),
        shortcut_export=_FakeShortcut(),
    )


def test_host_space_shortcut_is_disabled_on_embedded_timing_page() -> None:
    app = _fake_app(WORKFLOW_LYRICS_TIMING)

    KrokHelperQtApp._sync_workflow_shortcut_scope(app)

    assert app.shortcut_space.enabled is False
    assert app.shortcut_auto.enabled is False
    assert app.shortcut_drag_mode.enabled is False
    assert app.shortcut_export.enabled is True


def test_host_alignment_shortcuts_only_enabled_on_alignment_page() -> None:
    app = _fake_app(WORKFLOW_WAVEFORM_ALIGN)

    KrokHelperQtApp._sync_workflow_shortcut_scope(app)

    assert app.shortcut_space.enabled is True
    assert app.shortcut_auto.enabled is True
    assert app.shortcut_drag_mode.enabled is True
    assert app.shortcut_export.enabled is True


def test_host_shortcuts_do_not_consume_unrelated_pages() -> None:
    app = _fake_app(WORKFLOW_HIRES_MIX)

    KrokHelperQtApp._sync_workflow_shortcut_scope(app)

    assert app.shortcut_space.enabled is False
    assert app.shortcut_auto.enabled is False
    assert app.shortcut_drag_mode.enabled is False
    assert app.shortcut_export.enabled is False


def test_ctrl_s_routes_to_embedded_sug_save() -> None:
    timing_page = _FakeTimingPage()
    app = SimpleNamespace(
        active_module=WORKFLOW_LYRICS_TIMING,
        lyrics_timing_page=timing_page,
    )

    KrokHelperQtApp._handle_export_or_save_shortcut(app)

    assert timing_page.save_count == 1


def test_open_sug_project_switches_to_embedded_timing_page(tmp_path: Path) -> None:
    project_path = tmp_path / "song.sug"
    project_path.write_text("{}", encoding="utf-8")
    timing_page = _FakeTimingPage()
    shown_modules: list[str] = []
    app = SimpleNamespace(
        lyrics_timing_page=timing_page,
        _show_module=shown_modules.append,
    )

    KrokHelperQtApp.open_lyrics_timing_project(app, project_path)

    assert shown_modules == [WORKFLOW_LYRICS_TIMING]
    assert timing_page.opened_projects == [str(project_path)]


def test_force_update_exit_flushes_and_releases_sug_resources_once() -> None:
    timing_page = _FakeTimingPage()
    calls: list[str] = []
    app = SimpleNamespace(
        _update_exit_prepared=False,
        lyrics_timing_page=timing_page,
        _stop_alignment_preview=lambda **_kwargs: calls.append("stop-preview"),
        _save_all_settings=lambda: calls.append("save-settings"),
    )

    KrokHelperQtApp._prepare_force_quit_for_update(app)
    KrokHelperQtApp._prepare_force_quit_for_update(app)

    assert calls == ["stop-preview", "save-settings"]
    assert timing_page.flush_count == 1
    assert timing_page.editorInterface.release_count == 1


def test_force_update_exit_flushes_both_project_modules_once() -> None:
    timing_page = _FakeTimingPage()
    subtitle_page = _FakeProjectPage()
    app = SimpleNamespace(
        _update_exit_prepared=False,
        lyrics_timing_page=timing_page,
        subtitle_render_page=subtitle_page,
        _stop_alignment_preview=lambda **_kwargs: None,
        _save_all_settings=lambda: None,
    )

    KrokHelperQtApp._prepare_force_quit_for_update(app)
    KrokHelperQtApp._prepare_force_quit_for_update(app)

    assert timing_page.flush_count == 1
    assert subtitle_page.flush_count == 1


def test_save_project_page_for_close_requires_dirty_state_to_clear() -> None:
    saved = _FakeProjectPage(save_result=True)
    failed = _FakeProjectPage(save_result=False)
    app = SimpleNamespace()

    assert KrokHelperQtApp._save_project_page_for_close(app, "字幕视频生成", saved)
    assert not KrokHelperQtApp._save_project_page_for_close(
        app, "字幕视频生成", failed
    )
    assert saved.save_count == 1
    assert failed.save_count == 1


def test_unsaved_project_confirmation_saves_both_modules(monkeypatch) -> None:
    dialog_call: dict[str, object] = {}

    def fake_fluent_choice(parent, title, content, buttons, *, default):
        dialog_call.update(
            parent=parent,
            title=title,
            content=content,
            buttons=tuple(buttons),
            default=default,
        )
        return 0

    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.fluent_dialogs.fluent_choice",
        fake_fluent_choice,
    )
    timing_page = _FakeProjectPage()
    subtitle_page = _FakeProjectPage()
    app = SimpleNamespace(
        lyrics_timing_page=timing_page,
        subtitle_render_page=subtitle_page,
    )
    event = _FakeCloseEvent()

    assert KrokHelperQtApp._confirm_unsaved_projects(app, event)
    assert timing_page.save_count == 1
    assert subtitle_page.save_count == 1
    assert event.ignored is False
    assert dialog_call == {
        "parent": app,
        "title": "未保存的更改",
        "content": "以下项目有未保存的更改：\n\n• 歌词打轴\n• 字幕视频生成\n\n是否在退出前全部保存？",
        "buttons": ("全部保存", "全部放弃", "取消"),
        "default": 0,
    }


def test_unsaved_confirmation_uses_explicit_project_state(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def cancel_dialog(_parent, _title, content, _buttons, **_kwargs):
        captured["content"] = content
        return 2

    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.fluent_dialogs.fluent_choice",
        cancel_dialog,
    )
    page = SimpleNamespace(
        project_state=lambda: SimpleNamespace(
            dirty=True,
            status_text=lambda: "song.yurika · 未保存 · 素材缺失 1 项",
        ),
        has_unsaved_changes=lambda: False,
    )
    app = SimpleNamespace(subtitle_render_page=page)
    event = _FakeCloseEvent()

    assert not KrokHelperQtApp._confirm_unsaved_projects(
        app, event, (("字幕视频生成", page),)
    )
    assert event.ignored is True
    assert "字幕视频生成（song.yurika · 未保存 · 素材缺失 1 项）" in str(
        captured["content"]
    )


def test_request_force_quit_closes_before_scheduling_hard_exit(monkeypatch) -> None:
    calls: list[str] = []
    app = SimpleNamespace(
        _force_quitting_for_update=False,
        close=lambda: calls.append("close"),
    )
    monkeypatch.setattr(gui_qt, "_schedule_hard_process_exit", lambda: calls.append("hard-exit"))
    monkeypatch.setattr(gui_qt.QApplication, "instance", staticmethod(lambda: None))

    KrokHelperQtApp.request_force_quit(app)
    KrokHelperQtApp.request_force_quit(app)

    assert calls == ["close", "hard-exit"]


def test_shutdown_embedded_sug_releases_editor_resources() -> None:
    store = _FakeStore()
    editor = _FakeEditor()
    timing_page = SimpleNamespace(
        _store=store,
        editorInterface=editor,
        has_unsaved_changes=lambda: False,
    )
    app = SimpleNamespace(lyrics_timing_page=timing_page)
    event = _FakeCloseEvent()

    assert KrokHelperQtApp._shutdown_lyrics_timing(app, event) is True

    assert event.ignored is False
    assert store.cleanup_count == 1
    assert editor.release_count == 1


def test_release_embedded_sug_resources_falls_back_to_audio_engine() -> None:
    engine = _FakeAudioEngine()
    timing_page = SimpleNamespace(_audio_engine=engine)

    KrokHelperQtApp._release_lyrics_timing_resources(timing_page)
    KrokHelperQtApp._release_lyrics_timing_resources(timing_page)

    assert engine.release_count == 1
