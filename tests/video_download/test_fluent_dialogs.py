from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDialog
from qfluentwidgets import CheckBox, PushButton

from krok_helper.qfluent_compat import HostFluentMessageDialog
from krok_helper.settings import AppSettings
from krok_helper.video_download.download_task import (
    DownloadTask,
    TASK_STATUS_DOWNLOADING,
    VideoInfo,
)
from krok_helper.video_download.video_download_page import VideoDownloadPage


MODULE_PATH = Path(__file__).resolve().parents[2] / "krok_helper" / "video_download"


@pytest.fixture
def page(monkeypatch):
    app = QApplication.instance() or QApplication([])
    for name in ("_refresh_cookie_status", "_refresh_youtube_cookie_status", "_ensure_qr_login"):
        monkeypatch.setattr(VideoDownloadPage, name, lambda _self: None)
    widget = VideoDownloadPage(AppSettings(), lambda: None)
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_module_has_no_native_message_dialogs() -> None:
    """整个模块不该再出现原生弹窗（QFileDialog 例外，qfluentwidgets 没有对应实现）。"""
    offenders = []
    for path in MODULE_PATH.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in ("QMessageBox", "QInputDialog", "QErrorMessage", "QProgressDialog"):
            if name in text:
                offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_module_has_no_mask_dialogs() -> None:
    offenders = []
    for path in MODULE_PATH.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "MessageBoxBase" in text:
            offenders.append(path.name)
    assert offenders == []


def test_delete_running_task_asks_with_fluent_box(page, monkeypatch) -> None:
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        HostFluentMessageDialog,
        "exec",
        lambda self: seen.append((self.titleLabel.text(), self.yesButton.text(), self.cancelButton.text())) or 0,
    )
    task = DownloadTask(task_id="t1", url="u", title="标题", source="Bilibili", status=TASK_STATUS_DOWNLOADING)
    page._tasks.append(task)
    page._task_index["t1"] = task

    page._delete_task("t1")

    assert seen == [("删除下载任务", "删除", "取消")]
    # exec 返回 0（用户取消）时任务必须留着
    assert "t1" in page._task_index


def test_clear_list_while_downloading_shows_fluent_info(page, monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        "krok_helper.qfluent_compat.show_modeless_dialog",
        lambda dialog: seen.append(dialog.titleLabel.text()) or dialog,
    )
    page._running_workers = {"t1": object()}
    page._tasks.append(DownloadTask(task_id="t1", url="u", title="标题", source="Bilibili"))

    page._clear_task_list()

    assert seen == ["视频下载"]
    assert page._tasks, "有任务在跑时不该清空列表"


def test_settings_dialog_is_mask_free_and_modeless(page, monkeypatch) -> None:
    seen: list[tuple[str, str]] = []

    def reject_modeless(dialog: QDialog) -> int:
        buttons = dialog.findChildren(PushButton)
        seen.append((dialog.windowTitle(), ",".join(button.text() for button in buttons)))
        assert dialog.windowModality() == Qt.WindowModality.NonModal
        assert page.window().isEnabled()
        return 0

    monkeypatch.setattr(
        "krok_helper.video_download.video_download_page.exec_modeless_dialog",
        reject_modeless,
    )

    page._open_download_settings_dialog()

    assert seen == [("下载设置", "更新 yt-dlp,保存,取消")]


def test_settings_dialog_cancel_keeps_settings(page, monkeypatch) -> None:
    monkeypatch.setattr(
        "krok_helper.video_download.video_download_page.exec_modeless_dialog",
        lambda _dialog: 0,
    )
    page.settings.video_download_concurrent_count = 5
    persisted: list[bool] = []
    monkeypatch.setattr(page, "_persist_settings", lambda *a, **k: persisted.append(True))

    page._open_download_settings_dialog()

    assert persisted == [], "取消时不该写回设置"
    assert page.settings.video_download_concurrent_count == 5


def test_part_picker_is_mask_free_and_returns_selection(page, monkeypatch) -> None:
    infos = [
        VideoInfo(url=f"u{i}", source="Bilibili", title=f"P{i}", uploader="", duration=1.0)
        for i in range(3)
    ]

    def reject_modeless(dialog: QDialog) -> int:
        assert dialog.windowTitle() == "选择 Bilibili 分 P"
        assert dialog.windowModality() == Qt.WindowModality.NonModal
        assert page.window().isEnabled()
        return 0

    monkeypatch.setattr(
        "krok_helper.video_download.video_download_page.exec_modeless_dialog",
        reject_modeless,
    )
    assert page._choose_bilibili_parts(infos) == []

    # 确认：默认只勾第一个分 P
    def accept_modeless(dialog: QDialog) -> int:
        checks = dialog.findChildren(CheckBox)
        assert [check.isChecked() for check in checks] == [True, False, False]
        return 1

    monkeypatch.setattr(
        "krok_helper.video_download.video_download_page.exec_modeless_dialog",
        accept_modeless,
    )
    chosen = page._choose_bilibili_parts(infos)
    assert [info.title for info in chosen] == ["P0"]
