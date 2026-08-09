from __future__ import annotations

import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop, QObject, QThread, QTimer, Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QStyle,
    QStyleOptionButton,
    QWidget,
)

from krok_helper.gui_qt import (  # noqa: E402
    AlignmentHandoffDialog,
    BackgroundTask,
    KrokHelperQtApp,
    WORKFLOW_HIRES_MIX,
    WORKFLOW_SUBTITLE_RENDER,
)
from krok_helper.hires.page import HiResPage  # noqa: E402
from krok_helper.subtitle_render.frontend.main_window import (  # noqa: E402
    SubtitleProjectState,
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


def test_subtitle_project_state_is_mirrored_to_workflow_step(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None]] = []
    app = SimpleNamespace(
        workflow_stepper=SimpleNamespace(
            setStepStatus=lambda module, text: calls.append((module, text))
        )
    )
    state = SubtitleProjectState(
        display_name="song.yurika",
        path=tmp_path / "song.yurika",
        has_project=True,
        dirty=True,
        saving=False,
        save_error=None,
        exporting=False,
        recovery_path=None,
    )

    KrokHelperQtApp._on_subtitle_project_state_changed(app, state)

    assert calls == [(WORKFLOW_SUBTITLE_RENDER, "song.yurika · 未保存")]


def test_missing_asset_state_is_mirrored_to_workflow_step(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None]] = []
    app = SimpleNamespace(
        workflow_stepper=SimpleNamespace(
            setStepStatus=lambda module, text: calls.append((module, text))
        )
    )
    state = SubtitleProjectState(
        display_name="song.yurika",
        path=tmp_path / "song.yurika",
        has_project=True,
        dirty=False,
        saving=False,
        save_error=None,
        exporting=False,
        recovery_path=None,
        missing_resources=(("主字幕", tmp_path / "missing.lrc"),),
    )

    KrokHelperQtApp._on_subtitle_project_state_changed(app, state)

    assert calls == [
        (WORKFLOW_SUBTITLE_RENDER, "song.yurika · 素材缺失 1 项")
    ]


def test_pending_subtitle_recovery_switches_module_before_prompt() -> None:
    calls: list[str] = []
    page = SimpleNamespace(
        has_pending_crash_recovery=lambda: True,
        check_crash_recovery=lambda **_kwargs: calls.append("prompt"),
    )
    app = SimpleNamespace(
        subtitle_render_page=page,
        _show_module=lambda module: calls.append(f"show:{module}"),
    )

    KrokHelperQtApp._check_subtitle_render_crash_recovery(app)

    assert calls == [f"show:{WORKFLOW_SUBTITLE_RENDER}", "prompt"]


def test_yurika_startup_project_opens_in_subtitle_render_module(tmp_path: Path) -> None:
    project_path = tmp_path / "subtitle project.yurika"
    project_path.write_text("{}", encoding="utf-8")
    opened: list[Path] = []
    shown_modules: list[str] = []
    page = SimpleNamespace(open_initial_project=opened.append)
    app = SimpleNamespace(
        subtitle_render_page=page,
        _show_module=shown_modules.append,
    )

    KrokHelperQtApp.open_subtitle_render_project(app, project_path)

    assert shown_modules == [WORKFLOW_SUBTITLE_RENDER]
    assert opened == [project_path]

    dispatched: list[Path] = []
    dispatcher = SimpleNamespace(
        open_lyrics_timing_project=lambda _path: None,
        open_subtitle_render_project=dispatched.append,
    )
    KrokHelperQtApp.open_project_file(dispatcher, project_path)
    assert dispatched == [project_path]


def test_subtitle_render_success_prompts_for_post_export_action(
    qapp, monkeypatch, tmp_path: Path
) -> None:
    received: list[Path] = []
    opened: list[Path] = []
    prompts: list[tuple] = []
    sounds: list[bool] = []
    # 「打开文件夹」是不关闭弹窗的 sticky 按钮，只会通过 handler 生效。
    choices = iter((2, 1, 2))

    class Context:
        def accept_subtitle_video(self, path: Path) -> None:
            received.append(path)

    window = SubtitleRenderWindow(embedded=True, workflow_context=Context())
    output = tmp_path / "subtitle.mp4"
    monkeypatch.setattr(window, "_open_export_folder", opened.append)

    def choose(*args, **kwargs):
        prompts.append((args, kwargs))
        if len(prompts) == 1:  # 首次弹窗模拟先点「打开文件夹」再点「取消」
            kwargs["sticky"][0]()
        return next(choices)

    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.main_window.fluent_choice",
        choose,
    )
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.main_window.play_completion_sound",
        lambda: sounds.append(True),
    )
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.main_window.time",
        SimpleNamespace(monotonic=lambda: 185.0),
    )
    window._export_started_monotonic = 60.0

    window._finish_render_success(output)
    window._finish_render_success(output)
    window._finish_render_success(output)

    assert opened == [output]
    assert received == [output]
    assert sounds == [True, True, True]
    assert len(prompts) == 3
    args, kwargs = prompts[0]
    assert args[0] is window
    assert args[1] == "视频导出完成"
    assert str(output) in args[2]
    assert "本次导出耗时：2 分 5 秒" in args[2]
    assert args[3] == ("打开文件夹", "进入下一步", "取消")
    assert kwargs["default"] == 1
    assert set(kwargs["sticky"]) == {0}
    assert set(kwargs) == {"default", "sticky"}


def test_fluent_choice_sticky_button_keeps_dialog_open(qapp, monkeypatch) -> None:
    from krok_helper.subtitle_render.frontend import fluent_dialogs

    ran: list[str] = []
    finished: list[int] = []
    finished_after_sticky: list[int] = []

    def fake_exec(self) -> int:
        self.finished.connect(finished.append)
        self.yesButton.click()
        finished_after_sticky.extend(finished)
        self.cancelButton.click()
        return 0

    monkeypatch.setattr(fluent_dialogs.FluentMessageDialog, "exec", fake_exec)

    choice = fluent_dialogs.fluent_choice(
        None,
        "视频导出完成",
        "内容",
        ("打开文件夹", "进入下一步", "取消"),
        default=1,
        sticky={0: lambda: ran.append("open")},
    )

    assert ran == ["open"]
    assert finished_after_sticky == []  # sticky 按钮点完弹窗仍在
    assert finished  # 取消照常关闭
    assert choice == 2


def test_hires_success_plays_sound_and_uses_fluent_dialog(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple] = []
    output = tmp_path / "hires.mp4"

    class ValueWidget:
        def setRange(self, minimum: int, maximum: int) -> None:
            calls.append(("range", minimum, maximum))

        def setValue(self, value: int) -> None:
            calls.append(("value", value))

        def setEnabled(self, enabled: bool) -> None:
            calls.append(("enabled", enabled))

        def setText(self, text: str) -> None:
            calls.append(("text", text))

    app = SimpleNamespace(
        _hires_cancel_requested=False,
        _hires_process=object(),
        hires_progress=ValueWidget(),
        hires_start_button=ValueWidget(),
        hires_cancel_button=ValueWidget(),
        hires_status_label=ValueWidget(),
        _set_hires_status_color=lambda color: calls.append(("color", color)),
        _reset_hires_cancel_state=lambda: calls.append(("reset",)),
    )
    monkeypatch.setattr(
        "krok_helper.hires.page.play_completion_sound",
        lambda: calls.append(("sound",)),
    )

    def show_info(*args, **kwargs) -> None:
        calls.append(("dialog", args, kwargs))

    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.fluent_dialogs.fluent_info",
        show_info,
    )

    HiResPage._finish_hires_success(app, [output])

    assert ("sound",) in calls
    dialog_call = next(call for call in calls if call[0] == "dialog")
    args, kwargs = dialog_call[1], dialog_call[2]
    assert args[0] is app
    assert args[1] == "Hi-Res 导出完成"
    assert str(output) in args[2]
    assert kwargs == {"ok_text": "确定", "copyable": True}


def test_accept_subtitle_video_fills_hires_video_and_switches_page(tmp_path: Path) -> None:
    """渲染完成即定稿，下一步就是混流 —— 这条链路要跳页，与对齐/分离不同。"""
    output = tmp_path / "subtitle.mp4"
    calls: list[object] = []

    app = SimpleNamespace(
        set_video_path=lambda path: calls.append(("video", path)),
        _show_module=lambda module_id: calls.append(("show", module_id)),
        _notify_handoff=lambda title, content: calls.append(("toast", title)),
    )

    KrokHelperQtApp.accept_subtitle_video(app, output)

    assert calls == [
        ("video", output),
        ("show", WORKFLOW_HIRES_MIX),
        ("toast", "成片已交给下一步"),
    ]


@pytest.mark.parametrize(
    ("is_video_target", "subtitle_text", "hires_text"),
    [
        (
            True,
            "将导出的对齐视频作为字幕渲染背景素材",
            "将用于对齐的原唱音源作为 Hi-Res 混流原唱音源",
        ),
        (
            False,
            "将用于对齐的视频作为字幕渲染背景素材",
            "将导出的对齐音频作为 Hi-Res 混流原唱音源",
        ),
    ],
)
def test_alignment_handoff_dialog_defaults_both_options_to_checked(
    qapp,
    tmp_path: Path,
    is_video_target: bool,
    subtitle_text: str,
    hires_text: str,
) -> None:
    parent = QWidget()
    parent.resize(900, 700)
    dialog = AlignmentHandoffDialog(
        is_video_target=is_video_target,
        output_path=tmp_path / ("aligned.mp4" if is_video_target else "aligned.wav"),
        parent=parent,
    )

    assert dialog.subtitle_check.text() == subtitle_text
    assert dialog.hires_check.text() == hires_text
    assert dialog.selections() == (True, True)
    assert dialog.yesButton.text() == "确认"
    assert dialog.cancelButton.text() == "取消"
    dialog.close()
    parent.close()


def test_alignment_handoff_dialog_keeps_gui_responsive(
    qapp, tmp_path: Path
) -> None:
    calls: list[tuple[str, Path]] = []

    class HandoffHost(QWidget):
        def __init__(self):
            super().__init__()
            self.resize(900, 700)
            self.subtitle_render_page = SimpleNamespace(
                load_video=lambda path: calls.append(("subtitle", path))
            )

        def set_on_vocal_path(self, path):
            calls.append(("hires", path))

        def _notify_handoff(self, title, content):
            pass  # 提示条另有专测（test_handoff_toasts.py）

        def _apply_alignment_handoff(self):
            KrokHelperQtApp._apply_alignment_handoff(self)

        def _clear_alignment_handoff_dialog(self, result):
            KrokHelperQtApp._clear_alignment_handoff_dialog(self, result)

    host = HandoffHost()
    host.show()
    output = tmp_path / "aligned.wav"
    source_video = tmp_path / "source.mp4"
    source_audio = tmp_path / "source.flac"
    KrokHelperQtApp._offer_alignment_handoff(
        host,
        is_video_target=False,
        output_path=output,
        source_video_path=source_video,
        source_audio_path=source_audio,
    )
    dialog = host._alignment_handoff_dialog
    assert dialog is not None
    assert dialog.isVisible()
    assert host.isEnabled()
    assert not dialog.isModal()
    assert QApplication.activeModalWidget() is None

    heartbeat_count = 0
    heartbeat = QTimer(host)
    heartbeat.setInterval(5)

    def tick():
        nonlocal heartbeat_count
        heartbeat_count += 1

    heartbeat.timeout.connect(tick)
    heartbeat.start()
    for _ in range(10):
        time.sleep(0.005)
        qapp.processEvents()
    heartbeat.stop()
    assert heartbeat_count > 0

    option = QStyleOptionButton()
    option.initFrom(dialog.subtitle_check)
    indicator = dialog.subtitle_check.style().subElementRect(
        QStyle.SubElement.SE_CheckBoxIndicator,
        option,
        dialog.subtitle_check,
    )
    QTest.mouseClick(
        dialog.subtitle_check,
        Qt.MouseButton.LeftButton,
        pos=indicator.center(),
    )
    assert not dialog.subtitle_check.isChecked()

    finished = QEventLoop()
    dialog.finished.connect(lambda _result: finished.quit())
    QTest.mouseClick(
        dialog.yesButton,
        Qt.MouseButton.LeftButton,
        pos=dialog.yesButton.rect().center(),
    )
    if dialog.isVisible():
        QTimer.singleShot(500, finished.quit)
        finished.exec()

    assert host._alignment_handoff_dialog is None
    assert not dialog.isVisible()
    assert calls == [("hires", output)]
    host.close()


@pytest.mark.parametrize(
    ("is_video_target", "expected_background", "expected_vocal"),
    [
        (True, "output", "audio"),
        (False, "video", "output"),
    ],
)
def test_alignment_handoff_maps_assets_without_switching_modules(
    monkeypatch,
    tmp_path: Path,
    is_video_target: bool,
    expected_background: str,
    expected_vocal: str,
) -> None:
    video = tmp_path / "source.mp4"
    audio = tmp_path / "source.flac"
    output = tmp_path / ("aligned.mp4" if is_video_target else "aligned.wav")
    paths = {"video": video, "audio": audio, "output": output}
    calls: list[tuple[str, Path]] = []

    class FakeSignal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self, *args):
            for slot in self.slots:
                slot(*args)

    class AcceptedDialog:
        def __init__(self, **_kwargs):
            self.accepted = FakeSignal()
            self.finished = FakeSignal()

        def show(self):
            self.accepted.emit()
            self.finished.emit(QDialog.DialogCode.Accepted)

        def raise_(self):
            pass

        def activateWindow(self):
            pass

        def selections(self):
            return True, True

    monkeypatch.setattr("krok_helper.alignment.page.AlignmentHandoffDialog", AcceptedDialog)
    app = SimpleNamespace(
        subtitle_render_page=SimpleNamespace(
            load_video=lambda path: calls.append(("subtitle", path))
        ),
        set_on_vocal_path=lambda path: calls.append(("hires", path)),
        # 提示条另有专测（test_handoff_toasts.py），这里只关心素材映射
        _notify_handoff=lambda title, content: None,
    )
    app._apply_alignment_handoff = lambda: KrokHelperQtApp._apply_alignment_handoff(app)
    app._clear_alignment_handoff_dialog = (
        lambda result: KrokHelperQtApp._clear_alignment_handoff_dialog(app, result)
    )

    KrokHelperQtApp._offer_alignment_handoff(
        app,
        is_video_target=is_video_target,
        output_path=output,
        source_video_path=video,
        source_audio_path=audio,
    )

    assert calls == [
        ("subtitle", paths[expected_background]),
        ("hires", paths[expected_vocal]),
    ]


def test_alignment_handoff_respects_unchecked_options_and_cancel(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, Path]] = []
    dialog_results = iter(
        (
            (QDialog.DialogCode.Accepted, (False, True)),
            (QDialog.DialogCode.Rejected, (True, True)),
        )
    )

    class FakeSignal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self, *args):
            for slot in self.slots:
                slot(*args)

    class ControlledDialog:
        def __init__(self, **_kwargs):
            self.result, self.selected = next(dialog_results)
            self.accepted = FakeSignal()
            self.finished = FakeSignal()

        def show(self):
            if self.result == QDialog.DialogCode.Accepted:
                self.accepted.emit()
            self.finished.emit(self.result)

        def raise_(self):
            pass

        def activateWindow(self):
            pass

        def selections(self):
            return self.selected

    monkeypatch.setattr("krok_helper.alignment.page.AlignmentHandoffDialog", ControlledDialog)
    app = SimpleNamespace(
        subtitle_render_page=SimpleNamespace(
            load_video=lambda path: calls.append(("subtitle", path))
        ),
        set_on_vocal_path=lambda path: calls.append(("hires", path)),
        # 提示条另有专测（test_handoff_toasts.py），这里只关心素材映射
        _notify_handoff=lambda title, content: None,
    )
    app._apply_alignment_handoff = lambda: KrokHelperQtApp._apply_alignment_handoff(app)
    app._clear_alignment_handoff_dialog = (
        lambda result: KrokHelperQtApp._clear_alignment_handoff_dialog(app, result)
    )
    kwargs = {
        "is_video_target": True,
        "output_path": tmp_path / "aligned.mp4",
        "source_video_path": tmp_path / "source.mp4",
        "source_audio_path": tmp_path / "source.flac",
    }

    KrokHelperQtApp._offer_alignment_handoff(app, **kwargs)
    KrokHelperQtApp._offer_alignment_handoff(app, **kwargs)

    assert calls == [("hires", tmp_path / "source.flac")]


def test_alignment_export_success_opens_handoff_with_frozen_source_paths(
    tmp_path: Path,
) -> None:
    calls: list[object] = []

    class ValueWidget:
        def setRange(self, minimum, maximum):
            calls.append(("range", minimum, maximum))

        def setValue(self, value):
            calls.append(("value", value))

        def setEnabled(self, enabled):
            calls.append(("enabled", enabled))

        def setText(self, text):
            calls.append(("text", text))

    video = tmp_path / "source.mp4"
    audio = tmp_path / "source.flac"
    output = tmp_path / "aligned.mp4"
    handoffs: list[dict] = []
    app = SimpleNamespace(
        _align_export_cancel_requested=False,
        _align_export_process=object(),
        align_progress=ValueWidget(),
        align_analyze_button=ValueWidget(),
        align_status_label=ValueWidget(),
        _refresh_alignment_preview_controls=lambda: calls.append(("refresh",)),
        _reset_align_export_state=lambda: calls.append(("reset",)),
        _offer_alignment_handoff=lambda **kwargs: handoffs.append(kwargs),
    )

    KrokHelperQtApp._finish_aligned_export(
        app,
        True,
        "",
        [output],
        "对齐视频",
        is_video_target=True,
        source_video_path=video,
        source_audio_path=audio,
    )

    assert handoffs == [
        {
            "is_video_target": True,
            "output_path": output,
            "source_video_path": video,
            "source_audio_path": audio,
        }
    ]


def test_alignment_export_completion_is_queued_to_gui_thread(
    qapp, tmp_path: Path
) -> None:
    output = tmp_path / "aligned.wav"
    source_video = tmp_path / "source.mp4"
    source_audio = tmp_path / "source.flac"
    observed: list[tuple[QThread, tuple[object, ...], dict[str, object]]] = []

    class CompletionProbe(QObject):
        _finish_aligned_export_success = KrokHelperQtApp._finish_aligned_export_success

        def __init__(self) -> None:
            super().__init__()
            self._align_export_handoff_context = (
                False,
                source_video,
                source_audio,
                "对齐音频",
            )

        def _finish_aligned_export(self, *args, **kwargs) -> None:
            observed.append((QThread.currentThread(), args, kwargs))

    probe = CompletionProbe()
    task = BackgroundTask(lambda _logger: [output])
    task.task_succeeded.connect(probe._finish_aligned_export_success)
    task.start()
    deadline = time.monotonic() + 3.0
    while not observed and time.monotonic() < deadline:
        time.sleep(0.005)
        qapp.processEvents()
    task.wait()

    assert observed == [
        (
            qapp.thread(),
            (True, "", [output], "对齐音频"),
            {
                "is_video_target": False,
                "source_video_path": source_video,
                "source_audio_path": source_audio,
            },
        )
    ]


def test_lyrics_timing_export_loads_saved_sug_into_subtitle_render(
    tmp_path: Path,
) -> None:
    source = tmp_path / "song.sug"
    source.write_text("{}", encoding="utf-8")
    video = tmp_path / "song.mp4"
    video.write_bytes(b"video")
    project = object()
    tags = {
        "title": "曲名",
        "artist": "歌手",
        "custom": ["@Emoji=主唱"],
    }
    calls: list[object] = []

    class RenderPage:
        def load_from_sug(self, path):
            calls.append(("track", path))
            return object()

        def load_video(self, path):
            calls.append(("video", path))

        def load_audio(self, path):
            calls.append(("audio", path))

    app = SimpleNamespace(
        lyrics_timing_page=SimpleNamespace(
            export_to_next_payload=lambda: {
                "project": project,
                "nicokara_tags": tags,
                "source_path": str(source),
                "media_path": str(video),
                "media_kind": "video",
                "audio_path": None,
            }
        ),
        subtitle_render_page=RenderPage(),
        _show_module=lambda module: calls.append(("show", module)),
    )

    KrokHelperQtApp._export_lyrics_timing_to_next(app)

    assert calls == [
        ("track", source),
        ("video", video),
        ("show", WORKFLOW_SUBTITLE_RENDER),
    ]


def test_lyrics_timing_export_falls_back_to_audio(tmp_path: Path) -> None:
    source = tmp_path / "song.sug"
    source.write_text("{}", encoding="utf-8")
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"audio")
    calls: list[object] = []
    app = SimpleNamespace(
        lyrics_timing_page=SimpleNamespace(
            export_to_next_payload=lambda: {
                "project": object(),
                "nicokara_tags": {},
                "source_path": str(source),
                "media_path": None,
                "media_kind": None,
                "audio_path": str(audio),
            }
        ),
        subtitle_render_page=SimpleNamespace(
            load_from_sug=lambda path: calls.append(("track", path)) or object(),
            load_audio=lambda path: calls.append(("audio", path)),
        ),
        _show_module=lambda module: calls.append(("show", module)),
    )

    KrokHelperQtApp._export_lyrics_timing_to_next(app)

    assert calls == [
        ("track", source),
        ("audio", audio),
        ("show", WORKFLOW_SUBTITLE_RENDER),
    ]


def test_lyrics_timing_export_without_project_uses_fluent_warning(
    monkeypatch,
) -> None:
    dialogs: list[tuple[object, str, str]] = []
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.fluent_dialogs.fluent_warning",
        lambda parent, title, content: dialogs.append((parent, title, content)),
    )
    app = SimpleNamespace(
        lyrics_timing_page=SimpleNamespace(export_to_next_payload=lambda: None),
        subtitle_render_page=object(),
    )

    KrokHelperQtApp._export_lyrics_timing_to_next(app)

    assert dialogs == [
        (app, "无法导出到下一步", "当前没有可导出的打轴项目。")
    ]


def test_lyrics_timing_export_read_failure_uses_fluent_error(monkeypatch) -> None:
    dialogs: list[tuple[object, str, str]] = []
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.fluent_dialogs.fluent_error",
        lambda parent, title, content: dialogs.append((parent, title, content)),
    )

    def fail():
        raise RuntimeError("读取异常")

    app = SimpleNamespace(
        lyrics_timing_page=SimpleNamespace(export_to_next_payload=fail),
        subtitle_render_page=object(),
    )

    KrokHelperQtApp._export_lyrics_timing_to_next(app)

    assert dialogs == [
        (app, "导出到下一步失败", "无法读取当前打轴项目：\n读取异常")
    ]


class _WorkflowSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def disconnect(self, callback):
        self.callbacks.remove(callback)

    def emit(self, value):
        for callback in list(self.callbacks):
            callback(value)


def test_dirty_saved_sug_confirms_before_saving_for_export(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[object] = []
    choices: list[tuple[object, ...]] = []
    source = tmp_path / "saved-project.sug"
    source.write_text("{}", encoding="utf-8")
    timing_page = SimpleNamespace(
        has_unsaved_changes=lambda: True,
        export_to_next_payload=lambda: {
            "project": object(),
            "nicokara_tags": {},
            "source_path": str(source),
            "media_path": None,
            "media_kind": None,
            "audio_path": None,
        },
    )
    app = SimpleNamespace(
        lyrics_timing_page=timing_page,
        subtitle_render_page=object(),
        _save_lyrics_timing_then_export=lambda page, **kwargs: calls.append(
            ("save", page, kwargs)
        ),
    )
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.fluent_dialogs.fluent_choice",
        lambda parent, title, content, buttons, default=0: (
            choices.append((parent, title, content, tuple(buttons), default)) or 0
        ),
    )

    KrokHelperQtApp._export_lyrics_timing_to_next(app)

    assert choices == [
        (
            app,
            "保存打轴项目",
            "当前项目包含未保存的修改。保存到 .sug 文件后再进入下一步吗？",
            ("保存并进入下一步", "取消"),
            0,
        )
    ]
    assert calls == [("save", timing_page, {"force_save_as": False})]


def test_dirty_saved_sug_cancel_does_not_save_or_export(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "saved-project.sug"
    source.write_text("{}", encoding="utf-8")
    calls: list[object] = []
    timing_page = SimpleNamespace(
        has_unsaved_changes=lambda: True,
        export_to_next_payload=lambda: {
            "project": object(),
            "source_path": str(source),
        },
    )
    app = SimpleNamespace(
        lyrics_timing_page=timing_page,
        subtitle_render_page=object(),
        _save_lyrics_timing_then_export=lambda *args, **kwargs: calls.append(
            (args, kwargs)
        ),
    )
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.fluent_dialogs.fluent_choice",
        lambda *_args, **_kwargs: 1,
    )

    KrokHelperQtApp._export_lyrics_timing_to_next(app)

    assert calls == []


def test_missing_saved_sug_forces_save_as_before_export(tmp_path: Path) -> None:
    missing = tmp_path / "moved.sug"
    calls: list[object] = []
    timing_page = SimpleNamespace(
        has_unsaved_changes=lambda: False,
        export_to_next_payload=lambda: {
            "project": object(),
            "source_path": str(missing),
        },
    )
    app = SimpleNamespace(
        lyrics_timing_page=timing_page,
        subtitle_render_page=object(),
        _save_lyrics_timing_then_export=lambda page, **kwargs: calls.append(
            (page, kwargs)
        ),
    )

    KrokHelperQtApp._export_lyrics_timing_to_next(app)

    assert calls == [(timing_page, {"force_save_as": True})]


def test_dirty_sug_save_waits_for_success_before_loading_file(
    qapp, monkeypatch, tmp_path: Path
) -> None:
    finished = _WorkflowSignal()
    failed = _WorkflowSignal()
    state = {"dirty": True}
    calls: list[object] = []
    source = tmp_path / "saved.sug"
    source.write_text("old", encoding="utf-8")

    def trigger_save() -> bool:
        calls.append("save")
        source.write_text("{}", encoding="utf-8")
        state["dirty"] = False
        finished.emit(str(source))
        return True

    timing_page = SimpleNamespace(
        has_unsaved_changes=lambda: state["dirty"],
        trigger_save=trigger_save,
        project_save_finished=finished,
        project_save_failed=failed,
        export_to_next_payload=lambda: {
            "project": object(),
            "nicokara_tags": {},
            "source_path": str(source),
            "media_path": None,
            "media_kind": None,
            "audio_path": None,
        },
    )
    app = SimpleNamespace(
        lyrics_timing_page=timing_page,
        subtitle_render_page=SimpleNamespace(
            load_from_sug=lambda path: calls.append(("load", path)) or object()
        ),
        _show_module=lambda module: calls.append(("show", module)),
        _save_lyrics_timing_then_export=lambda page, **kwargs: (
            KrokHelperQtApp._save_lyrics_timing_then_export(app, page, **kwargs)
        ),
    )
    app._export_lyrics_timing_to_next = lambda: (
        KrokHelperQtApp._export_lyrics_timing_to_next(app)
    )
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.fluent_dialogs.fluent_choice",
        lambda *_args, **_kwargs: 0,
    )

    KrokHelperQtApp._export_lyrics_timing_to_next(app)
    qapp.processEvents()

    assert calls == [
        "save",
        ("load", source),
        ("show", WORKFLOW_SUBTITLE_RENDER),
    ]
    assert finished.callbacks == []
    assert failed.callbacks == []


def test_missing_sug_save_as_waits_for_new_file_before_loading(
    qapp, tmp_path: Path
) -> None:
    finished = _WorkflowSignal()
    failed = _WorkflowSignal()
    missing = tmp_path / "old-location.sug"
    saved = tmp_path / "new-location.sug"
    state = {"source_path": str(missing)}
    calls: list[object] = []

    def trigger_save_as() -> bool:
        calls.append("save_as")
        saved.write_text("{}", encoding="utf-8")
        state["source_path"] = str(saved)
        finished.emit(str(saved))
        return True

    timing_page = SimpleNamespace(
        has_unsaved_changes=lambda: False,
        trigger_save_as=trigger_save_as,
        project_save_finished=finished,
        project_save_failed=failed,
        export_to_next_payload=lambda: {
            "project": object(),
            "source_path": state["source_path"],
            "media_path": None,
            "media_kind": None,
            "audio_path": None,
        },
    )
    app = SimpleNamespace(
        lyrics_timing_page=timing_page,
        subtitle_render_page=SimpleNamespace(
            load_from_sug=lambda path: calls.append(("load", path)) or object()
        ),
        _show_module=lambda module: calls.append(("show", module)),
    )
    app._trigger_lyrics_timing_save_as = lambda page: (
        KrokHelperQtApp._trigger_lyrics_timing_save_as(page)
    )
    app._save_lyrics_timing_then_export = lambda page, **kwargs: (
        KrokHelperQtApp._save_lyrics_timing_then_export(app, page, **kwargs)
    )
    app._export_lyrics_timing_to_next = lambda: (
        KrokHelperQtApp._export_lyrics_timing_to_next(app)
    )

    KrokHelperQtApp._export_lyrics_timing_to_next(app)
    qapp.processEvents()

    assert calls == [
        "save_as",
        ("load", saved),
        ("show", WORKFLOW_SUBTITLE_RENDER),
    ]
    assert finished.callbacks == []
    assert failed.callbacks == []


def test_cancelled_required_save_as_stops_export_and_disconnects_callbacks() -> None:
    finished = _WorkflowSignal()
    failed = _WorkflowSignal()
    timing_page = SimpleNamespace(
        trigger_save_as=lambda: False,
        project_save_finished=finished,
        project_save_failed=failed,
    )
    app = SimpleNamespace()
    app._trigger_lyrics_timing_save_as = lambda page: (
        KrokHelperQtApp._trigger_lyrics_timing_save_as(page)
    )
    app._export_lyrics_timing_to_next = lambda: pytest.fail(
        "取消另存为后不应继续导出"
    )

    KrokHelperQtApp._save_lyrics_timing_then_export(
        app,
        timing_page,
        force_save_as=True,
    )

    assert app._lyrics_timing_export_waiting_for_save is False
    assert finished.callbacks == []
    assert failed.callbacks == []


def test_save_as_compat_adapter_detects_current_sug_private_save_start() -> None:
    save_started = _WorkflowSignal()
    timing_page = SimpleNamespace(
        _store=SimpleNamespace(save_started=save_started),
        editorInterface=SimpleNamespace(
            _on_save_as=lambda: save_started.emit("saved.sug")
        ),
    )

    assert KrokHelperQtApp._trigger_lyrics_timing_save_as(timing_page) is True
    assert save_started.callbacks == []


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
