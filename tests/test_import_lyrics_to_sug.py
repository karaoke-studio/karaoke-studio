from __future__ import annotations

from types import SimpleNamespace

import pytest

import krok_helper  # noqa: F401 - installs bundled SUG src path
from krok_helper.gui_qt import KrokHelperQtApp, WORKFLOW_LYRICS_TIMING
from krok_helper.lyrics_search.page import LyricsSearchPage
from krok_helper.lyrics import (
    LYRICS_LANGUAGE_TRANSLATION,
    LYRICS_PREVIEW_LINE,
    LyricsSearchCandidate,
)


class _FakeTimingPage:
    def __init__(self) -> None:
        self.imported_text = ""

    def import_lyrics_from_text(self, content: str) -> bool:
        self.imported_text = content
        return True


class _FakeFileLoader:
    def __init__(self) -> None:
        self.loaded_text = ""

    def load_lyrics_from_text(self, content: str) -> None:
        self.loaded_text = content


def test_import_current_lyrics_to_timing_uses_filtered_preview() -> None:
    candidate = LyricsSearchCandidate(
        provider_id="qm",
        provider_name="QQ音乐",
        track_id="1",
        title="Song",
        artist="Artist",
        album="Album",
        duration_seconds=None,
        line_lyrics="[00:01.00]original",
        translation_lyrics="[00:01.00]translated",
        lyrics_loaded=True,
    )
    timing_page = _FakeTimingPage()
    shown_modules: list[str] = []
    # "有没有可导入的歌词"由检索页判断，"交给打轴"才是外壳的事 —— 两边各搭一个替身。
    lyrics_page = SimpleNamespace(
        lyrics_selected_candidate=candidate,
        lyrics_strip_intro_checkbox=SimpleNamespace(isChecked=lambda: True),
        _current_lyrics_preview_mode=lambda: LYRICS_PREVIEW_LINE,
        _current_lyrics_language=lambda: LYRICS_LANGUAGE_TRANSLATION,
        _refresh_lyrics_import_button=lambda _preview: None,
    )
    lyrics_page._build_current_lyrics_preview = (
        lambda selected: LyricsSearchPage._build_current_lyrics_preview(lyrics_page, selected)
    )
    lyrics_page.current_lyrics_for_timing = (
        lambda: LyricsSearchPage.current_lyrics_for_timing(lyrics_page)
    )
    app = SimpleNamespace(
        lyrics_page=lyrics_page,
        lyrics_timing_page=timing_page,
        _show_module=shown_modules.append,
    )

    KrokHelperQtApp._import_current_lyrics_to_timing(app)

    assert timing_page.imported_text == "[00:01.00]translated"
    assert shown_modules == [WORKFLOW_LYRICS_TIMING]


def test_sug_public_import_api_routes_text_to_editor_loader() -> None:
    pytest.importorskip("numpy")
    from strange_uta_game.frontend.main_window import MainWindow as LyricsTimingMainWindow

    loader = _FakeFileLoader()
    editor = SimpleNamespace(_file_loader=loader)
    switched_to: list[object] = []
    window = SimpleNamespace(
        editorInterface=editor,
        switchTo=switched_to.append,
    )

    imported = LyricsTimingMainWindow.import_lyrics_from_text(window, "hello\nworld")

    assert imported is True
    assert loader.loaded_text == "hello\nworld"
    assert switched_to == [editor]


class _FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def disconnect(self, callback) -> None:
        self.callbacks.remove(callback)

    def emit(self, *args) -> None:
        for callback in list(self.callbacks):
            callback(*args)


def _make_import_app(timing_page, lyrics_text: str = "[00:01.00]new lyrics"):
    shown_modules: list[str] = []
    app = SimpleNamespace(
        lyrics_page=SimpleNamespace(current_lyrics_for_timing=lambda: lyrics_text),
        lyrics_timing_page=timing_page,
        _show_module=shown_modules.append,
    )
    return app, shown_modules


def test_import_lyrics_prompts_with_three_options_even_when_saved(monkeypatch) -> None:
    prompt_calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_choice(_parent, title, _content, buttons, **_kwargs):
        prompt_calls.append((title, tuple(buttons)))
        return 2  # 取消

    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs.fluent_choice",
        fake_choice,
    )

    imported_texts: list[str] = []
    timing_page = SimpleNamespace(
        has_unsaved_changes=lambda: False,
        _store=SimpleNamespace(
            project=SimpleNamespace(sentences=[object()]),
            save_path="song.sug",
            is_temp_save_path=lambda: False,
            audio_path=None,
            original_media_path=None,
        ),
        editorInterface=SimpleNamespace(_file_loader=SimpleNamespace(_lyric_thread=None)),
        import_lyrics_from_text=lambda text: imported_texts.append(text) or True,
    )
    app, shown_modules = _make_import_app(timing_page)

    KrokHelperQtApp._import_current_lyrics_to_timing(app)

    assert prompt_calls == [
        ("打轴模块已有数据", ("保存后新建项目", "直接覆盖", "取消")),
    ]
    assert imported_texts == []
    assert shown_modules == []


@pytest.mark.parametrize(
    "timing_page_factory",
    [
        # 1. 有未保存修改
        lambda: SimpleNamespace(
            has_unsaved_changes=lambda: True,
            _store=SimpleNamespace(
                project=SimpleNamespace(sentences=[]),
                save_path=None,
                is_temp_save_path=lambda: False,
                audio_path=None,
                original_media_path=None,
            ),
            editorInterface=SimpleNamespace(_file_loader=SimpleNamespace(_lyric_thread=None)),
        ),
        # 2. 已保存但存在正式 .sug 路径
        lambda: SimpleNamespace(
            has_unsaved_changes=lambda: False,
            _store=SimpleNamespace(
                project=SimpleNamespace(sentences=[]),
                save_path="D:/demo.sug",
                is_temp_save_path=lambda: False,
                audio_path=None,
                original_media_path=None,
            ),
            editorInterface=SimpleNamespace(_file_loader=SimpleNamespace(_lyric_thread=None)),
        ),
        # 3. 仅加载音频
        lambda: SimpleNamespace(
            has_unsaved_changes=lambda: False,
            _store=SimpleNamespace(
                project=None,
                save_path=None,
                is_temp_save_path=lambda: False,
                audio_path="D:/song.wav",
                original_media_path="D:/song.wav",
            ),
            editorInterface=SimpleNamespace(
                _audio_file_path="D:/song.wav",
                _file_loader=SimpleNamespace(_lyric_thread=None),
            ),
        ),
    ],
)
def test_lyrics_timing_has_data_detects_all_populated_states(timing_page_factory) -> None:
    assert KrokHelperQtApp._lyrics_timing_has_data(timing_page_factory()) is True


def test_lyrics_timing_has_data_is_false_for_empty_project() -> None:
    timing_page = SimpleNamespace(
        has_unsaved_changes=lambda: False,
        _store=SimpleNamespace(
            project=SimpleNamespace(sentences=[]),
            save_path="D:/cache/temp.sug",
            is_temp_save_path=lambda: True,
            audio_path=None,
            original_media_path=None,
        ),
        editorInterface=SimpleNamespace(
            _project=SimpleNamespace(sentences=[]),
            _audio_file_path=None,
            _file_loader=SimpleNamespace(_lyric_thread=None),
        ),
    )

    assert KrokHelperQtApp._lyrics_timing_has_data(timing_page) is False


def test_import_lyrics_direct_overwrite_replaces_without_saving_or_resetting(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs.fluent_choice",
        lambda *_args, **_kwargs: 1,  # 直接覆盖
    )

    events: list[object] = []
    timing_page = SimpleNamespace(
        has_unsaved_changes=lambda: True,
        _store=SimpleNamespace(
            project=SimpleNamespace(sentences=[object()]),
            save_path=None,
            is_temp_save_path=lambda: False,
            audio_path="D:/song.wav",
            original_media_path="D:/song.wav",
        ),
        editorInterface=SimpleNamespace(
            _file_loader=SimpleNamespace(
                _lyric_thread=None,
                create_fresh_project=lambda **kwargs: events.append(("fresh", kwargs)),
            )
        ),
        trigger_save=lambda: events.append("save") or True,
        import_lyrics_from_text=lambda text: events.append(("import", text)) or True,
    )
    app, shown_modules = _make_import_app(timing_page, "[00:01.00]overwrite")

    KrokHelperQtApp._import_current_lyrics_to_timing(app)

    assert events == [("import", "[00:01.00]overwrite")]
    assert shown_modules == [WORKFLOW_LYRICS_TIMING]


def test_import_lyrics_save_then_new_project_waits_for_save_and_resets(
    qapp, monkeypatch
) -> None:
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs.fluent_choice",
        lambda *_args, **_kwargs: 0,  # 保存后新建项目
    )

    finished = _FakeSignal()
    failed = _FakeSignal()
    events: list[object] = []

    def trigger_save() -> bool:
        events.append("save")
        finished.emit("D:/saved.sug")
        return True

    timing_page = SimpleNamespace(
        has_unsaved_changes=lambda: True,
        _store=SimpleNamespace(
            project=SimpleNamespace(sentences=[object()]),
            save_path="D:/saved.sug",
            is_temp_save_path=lambda: False,
            audio_path="D:/old.wav",
            original_media_path="D:/old.wav",
        ),
        editorInterface=SimpleNamespace(
            _file_loader=SimpleNamespace(
                _lyric_thread=None,
                create_fresh_project=lambda inherit_audio=False: events.append(
                    ("fresh", inherit_audio)
                ),
            )
        ),
        project_save_finished=finished,
        project_save_failed=failed,
        trigger_save=trigger_save,
        import_lyrics_from_text=lambda text: events.append(("import", text)) or True,
    )
    app, shown_modules = _make_import_app(timing_page, "[00:01.00]brand new")

    KrokHelperQtApp._import_current_lyrics_to_timing(app)
    qapp.processEvents()

    assert events == [
        "save",
        ("fresh", False),
        ("import", "[00:01.00]brand new"),
    ]
    assert shown_modules == [WORKFLOW_LYRICS_TIMING]
    assert finished.callbacks == []
    assert failed.callbacks == []
    assert app._lyrics_timing_import_waiting_for_save is False


def test_import_lyrics_save_then_new_project_aborts_when_save_cancelled(
    qapp, monkeypatch
) -> None:
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs.fluent_choice",
        lambda *_args, **_kwargs: 0,  # 保存后新建项目
    )

    finished = _FakeSignal()
    failed = _FakeSignal()
    events: list[object] = []

    timing_page = SimpleNamespace(
        has_unsaved_changes=lambda: True,
        _store=SimpleNamespace(
            project=SimpleNamespace(sentences=[object()]),
            save_path=None,
            is_temp_save_path=lambda: False,
            audio_path=None,
            original_media_path=None,
        ),
        editorInterface=SimpleNamespace(
            _file_loader=SimpleNamespace(
                _lyric_thread=None,
                create_fresh_project=lambda **kwargs: events.append(("fresh", kwargs)),
            )
        ),
        project_save_finished=finished,
        project_save_failed=failed,
        trigger_save=lambda: events.append("save_cancel") or False,
        import_lyrics_from_text=lambda text: events.append(("import", text)) or True,
    )
    app, shown_modules = _make_import_app(timing_page)

    KrokHelperQtApp._import_current_lyrics_to_timing(app)
    qapp.processEvents()

    assert events == ["save_cancel"]
    assert shown_modules == []
    assert finished.callbacks == []
    assert failed.callbacks == []
    assert app._lyrics_timing_import_waiting_for_save is False


def test_import_lyrics_save_then_new_project_aborts_on_save_error(
    qapp, monkeypatch
) -> None:
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs.fluent_choice",
        lambda *_args, **_kwargs: 0,  # 保存后新建项目
    )
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs.fluent_error",
        lambda _parent, title, content: errors.append((title, content)),
    )

    finished = _FakeSignal()
    failed = _FakeSignal()
    events: list[object] = []

    def trigger_save() -> bool:
        events.append("save")
        failed.emit("disk full")
        return True

    timing_page = SimpleNamespace(
        has_unsaved_changes=lambda: True,
        _store=SimpleNamespace(
            project=SimpleNamespace(sentences=[object()]),
            save_path="D:/saved.sug",
            is_temp_save_path=lambda: False,
            audio_path=None,
            original_media_path=None,
        ),
        editorInterface=SimpleNamespace(
            _file_loader=SimpleNamespace(
                _lyric_thread=None,
                create_fresh_project=lambda **kwargs: events.append(("fresh", kwargs)),
            )
        ),
        project_save_finished=finished,
        project_save_failed=failed,
        trigger_save=trigger_save,
        import_lyrics_from_text=lambda text: events.append(("import", text)) or True,
    )
    app, shown_modules = _make_import_app(timing_page)

    KrokHelperQtApp._import_current_lyrics_to_timing(app)
    qapp.processEvents()

    assert events == ["save"]
    assert errors == [("保存项目失败", "disk full")]
    assert shown_modules == []
    assert finished.callbacks == []
    assert failed.callbacks == []
    assert app._lyrics_timing_import_waiting_for_save is False


def test_import_lyrics_save_then_new_project_prepares_project_for_audio_only_state(
    qapp, monkeypatch
) -> None:
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs.fluent_choice",
        lambda *_args, **_kwargs: 0,  # 保存后新建项目
    )

    finished = _FakeSignal()
    failed = _FakeSignal()
    events: list[object] = []

    store = SimpleNamespace(
        project=None,
        save_path=None,
        is_temp_save_path=lambda: False,
        audio_path="D:/song.wav",
        original_media_path="D:/song.wav",
    )

    def create_fresh(inherit_audio: bool = False) -> None:
        events.append(("fresh", inherit_audio))
        store.project = SimpleNamespace(sentences=[])

    def trigger_save() -> bool:
        assert store.project is not None
        events.append("save")
        finished.emit("D:/audio_only.sug")
        return True

    editor = SimpleNamespace(
        _audio_file_path="D:/song.wav",
        _preserve_audio_on_project_load=False,
        _file_loader=SimpleNamespace(
            _lyric_thread=None,
            create_fresh_project=create_fresh,
        ),
    )
    timing_page = SimpleNamespace(
        has_unsaved_changes=lambda: False,
        _store=store,
        editorInterface=editor,
        project_save_finished=finished,
        project_save_failed=failed,
        trigger_save=trigger_save,
        import_lyrics_from_text=lambda text: events.append(("import", text)) or True,
    )
    app, shown_modules = _make_import_app(timing_page, "[00:01.00]after audio")

    KrokHelperQtApp._import_current_lyrics_to_timing(app)
    qapp.processEvents()

    assert events == [
        ("fresh", True),
        "save",
        ("fresh", False),
        ("import", "[00:01.00]after audio"),
    ]
    assert shown_modules == [WORKFLOW_LYRICS_TIMING]
