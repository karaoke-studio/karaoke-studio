from __future__ import annotations

import json
from pathlib import Path

import pytest
from PyQt6.QtCore import QObject, QProcess, pyqtSignal

from krok_helper.video_download.download_task import SOURCE_YOUTUBE
from krok_helper.video_download.youtube_login_controller import YouTubeLoginController, login_command


def export_file(path, *, token="test-only", domain=".youtube.com", expires=0):
    path.write_text(f"# Netscape HTTP Cookie File\n{domain}\tTRUE\t/\tTRUE\t{expires}\tSAPISID\t{token}\n", encoding="utf-8")
    return path


def test_generic_cookie_import_and_verified_state_survive_reload(isolated_cookie_manager, tmp_path):
    manager = isolated_cookie_manager
    source = export_file(tmp_path / "chrome-export.txt")
    source.write_text(source.read_text() + ".example.com\tTRUE\t/\tTRUE\t0\tSID\tother\n")
    manager.import_youtube_cookie_file(source)
    assert manager.youtube_cookie_status() == "已导入，待验证"
    assert not manager.check_login_status(SOURCE_YOUTUBE)
    assert {c.domain for c in manager.load_cookie_jar(SOURCE_YOUTUBE)} == {".youtube.com"}
    manager.import_youtube_cookie_file(source, verified=True)
    assert manager.youtube_cookie_status() == "已验证"
    assert manager.check_login_status(SOURCE_YOUTUBE)
    from krok_helper.video_download.cookie_manager import CookieManager
    reloaded = CookieManager()
    reloaded.default_cookie_path = manager.default_cookie_path
    assert reloaded.youtube_cookie_status() == "已验证"
    manager.import_youtube_cookie_file(source)
    assert manager.youtube_cookie_status() == "已导入，待验证"
    manager.clear(SOURCE_YOUTUBE)
    assert manager.youtube_cookie_status() == "未登录"
    assert not manager.resolved_cookie_path(SOURCE_YOUTUBE).with_suffix(".verified").exists()


@pytest.mark.parametrize("domain,expires", [(".youtube.com.attacker.test", 0), (".google.com", 0), (".youtube.com", 1)])
def test_invalid_import_does_not_replace_verified_cookie(isolated_cookie_manager, tmp_path, domain, expires):
    manager = isolated_cookie_manager
    source = export_file(tmp_path / "source.txt")
    target = manager.import_youtube_cookie_file(source, verified=True)
    before = target.read_bytes()
    export_file(source, domain=domain, expires=expires)
    with pytest.raises(ValueError):
        manager.import_youtube_cookie_file(source)
    assert target.read_bytes() == before
    assert manager.youtube_cookie_status() == "已验证"


def test_nonempty_cookie_file_is_not_login(isolated_cookie_manager):
    path = isolated_cookie_manager.resolved_cookie_path(SOURCE_YOUTUBE)
    path.write_text("not a cookie")
    assert isolated_cookie_manager.get_profile(SOURCE_YOUTUBE) is None


class FakeProcess(QObject):
    finished = pyqtSignal(int, QProcess.ExitStatus)
    errorOccurred = pyqtSignal(QProcess.ProcessError)

    def __init__(self, parent):
        super().__init__(parent)
        self.running = False

    @staticmethod
    def nullDevice():
        return QProcess.nullDevice()

    def setStandardOutputFile(self, path):
        pass

    setStandardErrorFile = setStandardOutputFile

    def start(self, program, arguments):
        self.program, self.arguments = program, arguments
        self.running = True

    def terminate(self):
        self.running = False
        # Simulate the worst race: success queued just as logout starts.
        self.finished.emit(0, QProcess.ExitStatus.NormalExit)

    kill = terminate

    def waitForFinished(self, timeout):
        return not self.running

    def state(self):
        return QProcess.ProcessState.Running if self.running else QProcess.ProcessState.NotRunning

    ProcessError = QProcess.ProcessError
    ExitStatus = QProcess.ExitStatus
    ProcessState = QProcess.ProcessState


@pytest.fixture
def controller(isolated_cookie_manager, monkeypatch):
    import krok_helper.video_download.youtube_login_controller as module
    monkeypatch.setattr(module, "QProcess", FakeProcess)
    monkeypatch.setattr(module, "proxy_url_for_app_settings", lambda _: "")
    obj = YouTubeLoginController(isolated_cookie_manager)
    yield obj
    obj.cancel()
    obj.deleteLater()


def test_success_imports_candidate_and_cleans_private_directory(controller):
    seen = []
    controller.succeeded.connect(seen.append)
    controller.start("firefox")
    directory = Path(controller._temporary.name)
    process = controller._process
    assert "firefox" in process.arguments
    assert json.loads((directory / "request.json").read_text()) == {"proxy": ""}
    export_file(directory / "cookies.txt")
    process.finished.emit(0, QProcess.ExitStatus.NormalExit)
    assert seen and controller.manager.youtube_cookie_status() == "已验证"
    assert not directory.exists() and not controller.is_running()


def test_logout_rejects_late_success(controller, tmp_path):
    source = export_file(tmp_path / "prior.txt")
    controller.manager.import_youtube_cookie_file(source, verified=True)
    controller.start()
    directory = Path(controller._temporary.name)
    export_file(directory / "cookies.txt", token="late")
    controller.cancel()
    controller.manager.clear(SOURCE_YOUTUBE)
    assert controller.manager.youtube_cookie_status() == "未登录"
    assert not directory.exists()


@pytest.mark.parametrize("code,status", [(1, QProcess.ExitStatus.NormalExit), (9, QProcess.ExitStatus.CrashExit), (0, QProcess.ExitStatus.NormalExit)])
def test_cancel_crash_or_missing_output_preserves_previous(controller, tmp_path, code, status):
    path = controller.manager.import_youtube_cookie_file(export_file(tmp_path / "prior.txt"), verified=True)
    before = path.read_bytes()
    controller.start()
    directory = Path(controller._temporary.name)
    controller._process.finished.emit(code, status)
    assert path.read_bytes() == before
    assert controller.manager.youtube_cookie_status() == "已验证"
    assert not directory.exists()


def test_launch_failure_releases_busy_state(controller):
    states = []
    controller.busyChanged.connect(states.append)
    controller.start()
    controller._process.errorOccurred.emit(QProcess.ProcessError.FailedToStart)
    assert states == [True, False]


def test_request_write_failure_removes_private_directory(controller, monkeypatch):
    paths = []

    def fail_write(path, *args, **kwargs):
        paths.append(path.parent)
        raise OSError("write failed")

    monkeypatch.setattr(Path, "write_text", fail_write)
    with pytest.raises(OSError, match="write failed"):
        controller.start()
    assert paths and not paths[0].exists()
    assert controller._temporary is None
    assert not controller.is_running()


def test_frozen_and_source_launch_commands(monkeypatch, tmp_path):
    import sys
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    program, args = login_command(tmp_path / "request.json", "default")
    assert Path(args[0]).name == "app.py"
    monkeypatch.setattr(sys, "frozen", True)
    frozen_program, frozen_args = login_command(tmp_path / "request.json", "default")
    assert frozen_program == program == sys.executable
    assert frozen_args[0] == "--youtube-web-login"


def test_formal_page_file_import_and_logout(monkeypatch, tmp_path):
    from PyQt6.QtWidgets import QApplication, QFileDialog
    from krok_helper.settings import AppSettings
    from krok_helper.video_download.video_download_page import VideoDownloadPage
    monkeypatch.setattr(VideoDownloadPage, "_refresh_cookie_status", lambda self: None)
    monkeypatch.setattr(VideoDownloadPage, "_ensure_qr_login", lambda self: None)
    page = VideoDownloadPage(AppSettings(), lambda: None)
    try:
        page.cookie_manager.default_cookie_path = lambda platform=SOURCE_YOUTUBE: tmp_path / "saved.txt"
        page._refresh_youtube_cookie_status()
        source = export_file(tmp_path / "edge-export.txt")
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(source), ""))
        page.import_youtube_cookie_button.click()
        assert page.youtube_status_text_label.text() == "已导入，待验证"
        assert page.youtube_status_row.status_label.text() == "已导入，待验证"
        page.cookie_manager.import_youtube_cookie_file(source, verified=True)
        page._handle_youtube_cookie_import_succeeded(str(source))
        assert page.youtube_status_text_label.text() == "已验证"
        page.logout_youtube_cookie_button.click()
        assert page.youtube_status_text_label.text() == "未登录"
        assert not (tmp_path / "saved.txt").exists()
    finally:
        page.close()
        page.deleteLater()
        QApplication.instance().processEvents()
