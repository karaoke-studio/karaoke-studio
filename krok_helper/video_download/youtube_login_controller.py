"""Own a separate WebEngine process and import only its completed candidate."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from PyQt6.QtCore import QObject, QProcess, pyqtSignal
from PyQt6.QtWidgets import QApplication

from krok_helper.network import proxy_url_for_app_settings


def login_command(request: Path, mode: str) -> tuple[str, list[str]]:
    args = ["--youtube-web-login", "--youtube-login-request", str(request), "--youtube-login-mode", mode]
    if getattr(sys, "frozen", False):
        return sys.executable, args
    return sys.executable, [str(Path(__file__).resolve().parents[2] / "app.py"), *args]


class YouTubeLoginController(QObject):
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)
    busyChanged = pyqtSignal(bool)

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self._process = None
        self._temporary = None
        self._cancelled = False
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.cancel)

    def is_running(self) -> bool:
        return self._process is not None

    def start(self, mode="default") -> None:
        if self.is_running():
            return
        if mode not in {"default", "firefox"}:
            raise ValueError("不支持的登录模式。")
        proxy = proxy_url_for_app_settings(self.manager._settings())
        self._cancelled = False
        self._temporary = tempfile.TemporaryDirectory(prefix="link-youtube-login-")
        request = Path(self._temporary.name) / "request.json"
        try:
            request.write_text(json.dumps({"proxy": proxy}), encoding="utf-8")
            request.chmod(0o600)
        except OSError:
            self._temporary.cleanup()
            self._temporary = None
            raise
        process = QProcess(self)
        self._process = process
        # Do not forward a web page's console messages or account details.
        process.setStandardOutputFile(QProcess.nullDevice())
        process.setStandardErrorFile(QProcess.nullDevice())
        process.finished.connect(lambda code, status: self._finished(process, code, status))
        process.errorOccurred.connect(lambda error: self._error(process, error))
        program, arguments = login_command(request, mode)
        self.busyChanged.emit(True)
        process.start(program, arguments)

    def _error(self, process, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._finished(process, -1, QProcess.ExitStatus.CrashExit)

    def _finished(self, process, code, status):
        if self._process is not process:
            return
        candidate = Path(self._temporary.name) / "cookies.txt"
        success = None
        failure = None
        try:
            if not self._cancelled:
                if code == 0 and status == QProcess.ExitStatus.NormalExit:
                    success = self.manager.import_youtube_cookie_file(candidate, verified=True)
                elif status == QProcess.ExitStatus.CrashExit or code != 1:
                    failure = "登录窗口未能正常完成。请确认已安装完整客户端，或使用 Cookie 文件导入。"
        except (OSError, ValueError):
            failure = "未能导入登录状态，原有 Cookie 已保留，请重新登录或导入 Cookie 文件。"
        finally:
            self._process = None
            self._temporary.cleanup()
            self._temporary = None
            process.deleteLater()
            self.busyChanged.emit(False)
        if success is not None:
            self.succeeded.emit(str(success))
        elif failure:
            self.failed.emit(failure)

    def cancel(self):
        """Revoke import before terminating; late completion cannot undo logout."""
        self._cancelled = True
        process = self._process
        if process is None:
            return
        process.terminate()
        if not process.waitForFinished(500):
            process.kill()
            process.waitForFinished(1000)
        if self._process is process and process.state() == QProcess.ProcessState.NotRunning:
            self._finished(process, 1, QProcess.ExitStatus.NormalExit)
