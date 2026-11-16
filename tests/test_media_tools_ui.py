from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QWidget

from krok_helper import media_tools, media_tools_ui
from krok_helper.errors import MissingMediaToolError


def _wait_idle(controller):
    for _ in range(200):
        QTest.qWait(10)
        if not controller.is_busy():
            return
    pytest.fail("FFmpeg worker did not finish")


@pytest.fixture
def setup_window(qapp, monkeypatch):
    statuses = tuple(media_tools.ToolStatus(t, f"D:/工具/bin/{t}.exe", f"{t} version 9.0") for t in media_tools.TOOLS)
    monkeypatch.setattr(media_tools_ui, "inspect_tools", lambda *_, **__: statuses)
    monkeypatch.setattr(media_tools_ui, "install_ffmpeg", lambda log: (log("安装测试日志"), statuses)[1])
    host = QWidget()
    applied = []
    controller = media_tools_ui.MediaToolsController(host, directory=lambda: "", apply_directory=applied.append)
    host.show()
    yield host, controller, applied, statuses
    _wait_idle(controller)
    if controller._dialog is not None:
        controller._dialog.close()
    if controller._prompt is not None:
        controller._prompt.reject()
    host.close()
    host.deleteLater()
    qapp.processEvents()


def test_installation_applies_verified_directory_without_restart(setup_window):
    host, controller, applied, _ = setup_window
    controller.open()
    _wait_idle(controller)
    assert not applied  # Opening settings alone must not replace configuration.
    controller.install()
    _wait_idle(controller)
    assert applied == [Path("D:/工具/bin")]
    assert "安装测试日志" in controller._log.toPlainText()
    assert "立即应用" in controller._log.toPlainText()
    assert host.isEnabled()
    assert not controller._dialog.isModal()


def test_partial_install_does_not_overwrite_configuration(setup_window, monkeypatch):
    _, controller, applied, statuses = setup_window
    monkeypatch.setattr(media_tools_ui, "inspect_tools", lambda *_, **__: (
        statuses[0], media_tools.ToolStatus("ffprobe", error="missing"), statuses[2],
    ))
    controller.open()
    _wait_idle(controller)
    controller.check(apply=True)
    _wait_idle(controller)
    assert not applied
    assert "尚未检测到" in controller._log.toPlainText()


def test_failed_install_reenables_retry_controls(setup_window, monkeypatch):
    _, controller, applied, _ = setup_window
    def fail(_log):
        raise RuntimeError("安装已取消")
    monkeypatch.setattr(media_tools_ui, "install_ffmpeg", fail)
    controller.open()
    _wait_idle(controller)
    controller.install()
    _wait_idle(controller)
    assert not applied
    assert "安装已取消" in controller._log.toPlainText()
    assert controller._check_button.isEnabled()
    assert controller._choose_button.isEnabled()


def test_missing_prompt_coalesces_and_opens_shared_setup(setup_window):
    _, controller, _, _ = setup_window
    for _ in range(10):
        media_tools.report_missing_tool(MissingMediaToolError("ffprobe"))
    QTest.qWait(20)
    prompt = controller._prompt
    assert prompt is not None
    assert prompt.yesButton.text() == "前往安装 / 配置"
    prompt.yesButton.click()
    _wait_idle(controller)
    assert controller._dialog.isVisible()
    dialog = controller._dialog
    controller.open()
    assert controller._dialog is dialog


@pytest.mark.parametrize("frontend", ["workbench", "subtitle"])
def test_both_error_frontends_offer_the_same_installer(setup_window, frontend):
    host, controller, _, _ = setup_window
    if frontend == "workbench":
        from krok_helper.qfluent_compat import show_fluent_error
        show_fluent_error(host, str(MissingMediaToolError("ffmpeg")))
    else:
        from krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs import fluent_error
        fluent_error(host, "加载视频失败", str(MissingMediaToolError("ffprobe")))
    assert controller._prompt is not None
    assert controller._prompt.yesButton.text() == "前往安装 / 配置"


def test_background_task_reports_missing_tool_to_gui(setup_window):
    from krok_helper.background import BackgroundTask
    _, controller, _, _ = setup_window
    def runner(_log):
        raise MissingMediaToolError("ffmpeg")
    task = BackgroundTask(runner)
    errors = []
    task.task_failed.connect(errors.append)
    task.start()
    assert task.wait(2000)
    QTest.qWait(20)
    assert errors and "ffmpeg" in errors[0]
    assert controller._prompt is not None
    task.deleteLater()


def test_system_detection_discards_a_previous_manual_selection(setup_window, monkeypatch):
    _, controller, applied, statuses = setup_window
    calls = []
    monkeypatch.setattr(media_tools_ui, "inspect_tools", lambda directory, **_: (calls.append(directory), statuses)[1])
    controller.open()
    _wait_idle(controller)
    controller._selected_directory = "D:/broken"
    controller._system_button.click()
    _wait_idle(controller)
    assert calls[-1] is None
    assert applied == [Path("D:/工具/bin")]


def test_host_exit_and_update_wait_for_media_tools_work(qapp, monkeypatch):
    from types import SimpleNamespace
    from PyQt6.QtGui import QCloseEvent
    from krok_helper import gui_qt

    messages = []
    monkeypatch.setattr(gui_qt, "show_fluent_info", lambda _, message: messages.append(message))
    host = SimpleNamespace(_settings_dialogs=SimpleNamespace(media_tools_busy=lambda: True))
    event = QCloseEvent()
    gui_qt.KrokHelperQtApp.closeEvent(host, event)
    assert not event.isAccepted()
    assert "FFmpeg" in messages[0]
    assert gui_qt.KrokHelperQtApp._active_update_task_labels(host) == ["FFmpeg 安装或检测中"]


@pytest.mark.parametrize("operation", ["probe", "handoff", "roll_video"])
def test_missing_ffprobe_does_not_escape_media_load_slots(qapp, monkeypatch, operation):
    from types import SimpleNamespace
    from krok_helper.subtitle_render.frontend import main_window

    messages = []
    monkeypatch.setattr(main_window, "fluent_error", lambda _, title, message: messages.append(message))
    def missing():
        raise MissingMediaToolError("ffprobe")
    window = SimpleNamespace(_resolve_ffprobe_path=missing)
    if operation == "probe":
        assert main_window.SubtitleRenderWindow._probe(window, Path("video.mp4"), "视频") is None
    elif operation == "handoff":
        main_window.SubtitleRenderWindow.load_media_async(window, Path("video.mp4"), as_video=True)
    else:
        main_window.SubtitleRenderWindow._start_roll_media_load(window, "pre", "video", Path("video.mp4"))
    assert len(messages) == 1
    assert "找不到 ffprobe" in messages[0]


def test_download_page_theme_connection_is_released_with_page(qapp, monkeypatch):
    from PyQt6 import sip
    from PyQt6.QtCore import QEvent
    from krok_helper.settings import AppSettings
    from krok_helper.theme_workbench import theme
    from krok_helper.video_download.video_download_page import VideoDownloadPage

    monkeypatch.setattr(VideoDownloadPage, "_ensure_qr_login", lambda _: None)
    page = VideoDownloadPage(AppSettings(), lambda: None)
    page.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(page)
    theme.changed.emit()
    QTest.qWait(250)  # Must not schedule a timer on a deleted page.
