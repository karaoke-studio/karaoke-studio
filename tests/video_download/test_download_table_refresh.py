from __future__ import annotations

from collections.abc import Iterator

import pytest
from PyQt6.QtWidgets import QApplication

from krok_helper.settings import AppSettings
from krok_helper.video_download.download_task import (
    TASK_STATUS_CANCELLED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_DOWNLOADING,
    TASK_STATUS_FAILED,
    TASK_STATUS_WAITING,
)
from krok_helper.video_download.video_download_page import (
    DOWNLOAD_TABLE_ACTION_COLUMN,
    VideoDownloadPage,
)

PROGRESS_COLUMN = 4


def _progress_payload(downloaded_bytes: int) -> dict:
    return {
        "status": "downloading",
        "downloaded_bytes": downloaded_bytes,
        "total_bytes": 1000,
        "total_bytes_estimate": 0,
        "speed": 1024.0,
        "eta": 10,
        "fragment_index": 0,
        "fragment_count": 0,
        "filename": "video.mp4",
    }


def _action_button_texts(page: VideoDownloadPage, row: int) -> list[str]:
    container = page.download_table.cellWidget(row, DOWNLOAD_TABLE_ACTION_COLUMN)
    return [container.layout().itemAt(index).widget().text() for index in range(container.layout().count())]


@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch, make_download_task) -> Iterator[VideoDownloadPage]:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(VideoDownloadPage, "_refresh_cookie_status", lambda _self: None)
    monkeypatch.setattr(VideoDownloadPage, "_refresh_youtube_cookie_status", lambda _self: None)
    monkeypatch.setattr(VideoDownloadPage, "_ensure_qr_login", lambda _self: None)

    widget = VideoDownloadPage(AppSettings(), lambda: None)
    for index in (1, 2):
        task = make_download_task(task_id=f"task-{index}")
        task.status = TASK_STATUS_DOWNLOADING
        widget._tasks.append(task)
        widget._task_index[task.task_id] = task
    # 隐藏期间进度刷新会转为 pending 攒批（见 background_throttle），
    # 本文件的用例验证的是可见路径的表格行为。
    widget.show()
    app.processEvents()
    widget._refresh_download_table()
    try:
        yield widget
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_progress_tick_keeps_action_widgets_alive(page: VideoDownloadPage) -> None:
    """The 取消 button must survive progress updates instead of being rebuilt."""
    before = [page.download_table.cellWidget(row, DOWNLOAD_TABLE_ACTION_COLUMN) for row in range(2)]

    page._handle_download_progress("task-1", _progress_payload(400))

    after = [page.download_table.cellWidget(row, DOWNLOAD_TABLE_ACTION_COLUMN) for row in range(2)]
    assert after[0] is before[0]
    assert after[1] is before[1]
    assert page.download_table.item(0, PROGRESS_COLUMN).text() == "40%"


def test_progress_ticks_are_coalesced(page: VideoDownloadPage) -> None:
    repaints: list[int] = []
    original = page._repaint_task_progress
    page._repaint_task_progress = lambda: (repaints.append(1), original())[1]

    for downloaded in (100, 200, 300, 400, 500):
        page._handle_download_progress("task-1", _progress_payload(downloaded))

    assert len(repaints) == 1
    assert page._progress_refresh_pending is True
    # The trailing edge still renders the newest value once the interval elapses.
    page._flush_progress_refresh()
    assert len(repaints) == 2
    assert page.download_table.item(0, PROGRESS_COLUMN).text() == "50%"


def test_progress_refresh_defers_while_page_hidden(page: VideoDownloadPage) -> None:
    """页面隐藏期间进度 tick 只攒 pending，恢复可见时补一次重绘。"""
    app = QApplication.instance()

    page.hide()
    app.processEvents()
    page._handle_download_progress("task-1", _progress_payload(400))
    assert page._progress_refresh_pending is True
    assert page.download_table.item(0, PROGRESS_COLUMN).text() != "40%"

    page.show()
    app.processEvents()
    assert page._progress_refresh_pending is False
    assert page.download_table.item(0, PROGRESS_COLUMN).text() == "40%"


def test_status_change_rebuilds_action_widget(page: VideoDownloadPage) -> None:
    assert _action_button_texts(page, 0)[0] == "取消"
    before = page.download_table.cellWidget(0, DOWNLOAD_TABLE_ACTION_COLUMN)

    page._tasks[0].status = TASK_STATUS_COMPLETED
    page._refresh_download_table()

    assert page.download_table.cellWidget(0, DOWNLOAD_TABLE_ACTION_COLUMN) is not before
    assert _action_button_texts(page, 0)[0] == "打开"


def test_download_hint_shows_completed_count(page: VideoDownloadPage) -> None:
    assert page.download_hint_label.text() == "共 2 个任务。已结束 0/2（成功 0，失败 0）"

    page._tasks[0].status = TASK_STATUS_COMPLETED
    page._refresh_download_table()

    assert page.download_hint_label.text() == "共 2 个任务。已结束 1/2（成功 1，失败 0）"


def test_download_hint_counts_failures_and_cancellations(page: VideoDownloadPage) -> None:
    page._tasks[0].status = TASK_STATUS_FAILED
    page._tasks[1].status = TASK_STATUS_CANCELLED

    page._refresh_download_table()

    assert page.download_hint_label.text() == "共 2 个任务。已结束 2/2（成功 0，失败 1，取消 1）"


def test_download_hint_shows_all_completed(page: VideoDownloadPage) -> None:
    for task in page._tasks:
        task.status = TASK_STATUS_COMPLETED

    page._refresh_download_table()

    assert page.download_hint_label.text() == "共 2 个任务。全部完成"


def test_removing_a_task_rebinds_shifted_action_widget(page: VideoDownloadPage) -> None:
    """Row 0's buttons must follow the task that moved into it, not the deleted one."""
    cancelled: list[str] = []
    page._cancel_task = cancelled.append
    # 下载中的任务删除前会弹确认框，这里换成等待中，按钮仍是「取消」。
    page._tasks[0].status = TASK_STATUS_WAITING
    page._refresh_download_table()

    page._delete_task("task-1")

    assert page.download_table.rowCount() == 1
    container = page.download_table.cellWidget(0, DOWNLOAD_TABLE_ACTION_COLUMN)
    container.layout().itemAt(0).widget().click()
    assert cancelled == ["task-2"]
