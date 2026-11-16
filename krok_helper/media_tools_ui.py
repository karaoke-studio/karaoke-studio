"""Shared FFmpeg setup window and actionable missing-tool prompts for Lin-K."""

from __future__ import annotations

from pathlib import Path
import sys
import time
from typing import Callable
import weakref

from PyQt6 import sip
from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal as Signal, pyqtSlot as Slot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, PrimaryPushButton, PushButton, SubtitleLabel

from krok_helper.background import BackgroundTask
from krok_helper.media_tools import (
    ToolStatus, inspect_tools, install_ffmpeg, missing_tool, register_missing_tool_reporter,
)
from krok_helper.qfluent_compat import HostFluentMessageDialog, ModelessDialog, show_modeless_dialog
from krok_helper.settings import load_app_settings, save_app_settings


class MediaToolsController(QObject):
    missing = Signal(str)
    configured = Signal(str)

    def __init__(
        self, parent: QWidget | None = None, *,
        directory: Callable[[], str] | None = None,
        apply_directory: Callable[[Path], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._window = parent
        self._directory = directory or (lambda: load_app_settings().ffmpeg_dir)
        self._apply_directory = apply_directory or self._save_directory
        self._dialog: ModelessDialog | None = None
        self._prompt: HostFluentMessageDialog | None = None
        self._task: BackgroundTask | None = None
        self._last_offer = 0.0
        self._selected_directory: str | None = None
        self._apply_after_check = False
        self.missing.connect(self.offer_install)
        register_missing_tool_reporter(self._report_missing)
        app = QApplication.instance()
        if app is not None:
            app._link_media_tools_controller = weakref.ref(self)

    def _report_missing(self, message: str) -> None:
        self.missing.emit(message)

    @staticmethod
    def _save_directory(path: Path) -> None:
        settings = load_app_settings()
        settings.ffmpeg_dir = str(path)
        save_app_settings(settings)

    def is_busy(self) -> bool:
        return self._task is not None

    @Slot(str)
    def offer_install(self, message: str) -> None:
        if not missing_tool(message):
            return
        if self._dialog is not None and self._dialog.isVisible():
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        if self._prompt is not None or time.monotonic() - self._last_offer < 15:
            return
        self._last_offer = time.monotonic()
        prompt = HostFluentMessageDialog(
            "需要安装或配置 FFmpeg",
            message + "\n\nFFmpeg 工具包包含 FFmpeg、FFprobe 和 FFplay。", self._window,
        )
        self._prompt = prompt
        prompt.yesButton.setText("前往安装 / 配置")
        prompt.cancelButton.setText("稍后再说")
        prompt.accepted.connect(self.open)
        prompt.finished.connect(self._prompt_closed)
        show_modeless_dialog(prompt)

    def _prompt_closed(self, _result: int) -> None:
        self._prompt = None

    def open(self) -> None:
        if self._dialog is None:
            self._build_dialog()
        elif not self.is_busy():
            self._selected_directory = None
            self.check()
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()

    def _build_dialog(self) -> None:
        dialog = ModelessDialog(self._window)
        self._dialog = dialog
        dialog.setWindowTitle("FFmpeg 安装与检测")
        dialog.resize(760, 580)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(SubtitleLabel("FFmpeg 安装与检测", dialog))
        description = BodyLabel(
            "FFmpeg 用于音视频转换，FFprobe 用于读取媒体信息，FFplay 用于波形试听。\n"
            "安装或配置成功后将立即应用到工作台，原操作请重新尝试。", dialog,
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        self._status_labels = {}
        for tool in ("ffmpeg", "ffprobe", "ffplay"):
            label = BodyLabel(f"{tool}：等待检测", dialog)
            label.setWordWrap(True)
            self._status_labels[tool] = label
            layout.addWidget(label)
        buttons = QHBoxLayout()
        self._install_button = PrimaryPushButton("一键安装", dialog)
        self._install_button.setEnabled(sys.platform in ("win32", "darwin"))
        self._install_button.clicked.connect(self.install)
        self._check_button = PushButton("重新检测并应用", dialog)
        self._check_button.clicked.connect(lambda: self.check(apply=True))
        self._choose_button = PushButton("选择目录…", dialog)
        self._choose_button.clicked.connect(self._choose_directory)
        for button in (self._install_button, self._check_button, self._choose_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self._system_button = PushButton("检测系统 PATH", dialog)
        self._system_button.clicked.connect(self._check_system)
        layout.addWidget(self._system_button)
        help_row = QHBoxLayout()
        for text, url in (
            ("手动下载 FFmpeg", "https://ffmpeg.org/download.html"),
            ("安装 Homebrew" if sys.platform == "darwin" else "安装 WinGet",
             "https://brew.sh/" if sys.platform == "darwin" else "https://aka.ms/getwinget"),
        ):
            button = PushButton(text, dialog)
            button.clicked.connect(lambda _checked=False, target=url: QDesktopServices.openUrl(QUrl(target)))
            help_row.addWidget(button)
        layout.addLayout(help_row)
        self._log = QPlainTextEdit(dialog)
        self._log.setReadOnly(True)
        self._log.document().setMaximumBlockCount(1500)
        layout.addWidget(self._log, 1)
        close_button = PushButton("关闭", dialog)
        close_button.clicked.connect(dialog.close)
        layout.addWidget(close_button)
        from krok_helper.theme_workbench import palette, themed

        themed(dialog, lambda: f"QDialog {{ background: {palette().shell_bg}; }}"
               f"QPlainTextEdit {{ background: {palette().input_bg}; color: {palette().text_primary};"
               f" border: 1px solid {palette().input_border}; border-radius: 6px; }}")
        QTimer.singleShot(0, self.check)

    def _choose_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self._dialog, "选择 FFmpeg 所在目录", self._directory())
        if path:
            self._selected_directory = path
            self.check(apply=True)

    def _run(self, runner, *, apply: bool) -> None:
        if self.is_busy():
            return
        self._set_busy(True)
        self._apply_after_check = apply
        task = BackgroundTask(runner)
        task.setParent(self)
        self._task = task
        task.log_message.connect(self._log.appendPlainText)
        task.task_succeeded.connect(self._task_succeeded)
        task.task_failed.connect(self._failed)
        task.finished.connect(self._finished)
        task.finished.connect(task.deleteLater)
        task.start()

    def _set_busy(self, busy: bool) -> None:
        self._install_button.setEnabled(not busy and sys.platform in ("win32", "darwin"))
        self._check_button.setEnabled(not busy)
        self._choose_button.setEnabled(not busy)
        self._system_button.setEnabled(not busy)

    def _task_succeeded(self, statuses: object) -> None:
        self._checked(statuses, apply=self._apply_after_check)

    def _check_system(self) -> None:
        self._selected_directory = ""
        self.check(apply=True)

    def check(self, *, apply: bool = False) -> None:
        directory_text = self._directory() if self._selected_directory is None else self._selected_directory
        directory = Path(directory_text).expanduser() if directory_text.strip() else None
        self._run(lambda _log: inspect_tools(directory, refresh=True, activate=apply), apply=apply)

    def install(self) -> None:
        self._selected_directory = None
        self._run(install_ffmpeg, apply=True)

    def _checked(self, statuses: tuple[ToolStatus, ...], *, apply: bool) -> None:
        for status in statuses:
            label = {"ffmpeg": "FFmpeg", "ffprobe": "FFprobe", "ffplay": "FFplay"}[status.tool]
            version = status.version.removeprefix(f"{status.tool} version ")
            detail = f"可用 · {version}\n{status.path}" if status.available else status.error
            self._status_labels[status.tool].setText(f"{label}：{detail}")
        ready = all(s.available for s in statuses if s.tool in ("ffmpeg", "ffprobe"))
        if not ready:
            self._log.appendPlainText("尚未检测到可用的 FFmpeg 和 FFprobe，请安装工具包或选择有效目录。")
            return
        if apply:
            path = Path(next(s.path for s in statuses if s.tool == "ffmpeg")).parent
            try:
                self._apply_directory(path)
            except Exception as exc:
                self._failed(f"保存 FFmpeg 配置失败：{exc}")
                return
            self._selected_directory = str(path)
            self.configured.emit(str(path))
            self._last_offer = 0.0
            self._log.appendPlainText("FFmpeg 和 FFprobe 已检测通过并立即应用，请重试原操作。")
        else:
            self._log.appendPlainText("检测完成，FFmpeg 和 FFprobe 可用。")

    def _failed(self, message: str) -> None:
        self._log.appendPlainText(message)

    def _finished(self) -> None:
        self._task = None
        self._set_busy(False)


def _registered_controller() -> MediaToolsController | None:
    app = QApplication.instance()
    reference = getattr(app, "_link_media_tools_controller", None)
    controller = reference() if reference is not None else None
    return controller if controller is not None and not sip.isdeleted(controller) else None


def offer_media_tools(parent: QWidget | None, error: object) -> bool:
    """Replace a missing-tool error with a setup action, preserving other errors."""
    if not missing_tool(error):
        return False
    controller = _registered_controller()
    if controller is None:
        window = parent.window() if parent is not None else None
        controller = MediaToolsController(window)
        if window is not None:
            window._link_media_tools_controller = controller
        else:
            # Retain a standalone prompt controller until its window closes.
            QApplication.instance()._link_standalone_media_tools_controller = controller
    controller.offer_install(str(error))
    return True
