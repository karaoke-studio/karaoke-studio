from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
        self.opened_projects: list[str] = []

    def trigger_save(self) -> None:
        self.save_count += 1

    def open_initial_project(self, project_path: str) -> None:
        self.opened_projects.append(project_path)


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
