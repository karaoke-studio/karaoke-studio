"""「自动合并音视频」「下载封面」两个勾选框要记住上次的选择。

它们本身是**逐任务**的（每个 ``DownloadTask`` 各带一份），但用户的习惯是固定的：
每加一个视频都得重勾一遍很烦。所以勾选状态同时写进设置，作为后续新任务和下次
启动时的默认值。
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from krok_helper.settings import AppSettings
from krok_helper.video_download.download_task import SOURCE_BILIBILI, VideoInfo
from krok_helper.video_download.video_download_page import VideoDownloadPage


@pytest.fixture
def page(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(VideoDownloadPage, "_refresh_cookie_status", lambda _self: None)
    monkeypatch.setattr(VideoDownloadPage, "_refresh_youtube_cookie_status", lambda _self: None)
    monkeypatch.setattr(VideoDownloadPage, "_ensure_qr_login", lambda _self: None)

    def _build(settings: AppSettings):
        saved: list[int] = []
        widget = VideoDownloadPage(settings, lambda: saved.append(1))
        widget._saved = saved
        return widget

    built: list[VideoDownloadPage] = []

    def factory(settings: AppSettings | None = None) -> VideoDownloadPage:
        widget = _build(settings or AppSettings())
        built.append(widget)
        return widget

    yield factory
    for widget in built:
        widget.close()
        widget.deleteLater()
    app.processEvents()


def _video_info() -> VideoInfo:
    return VideoInfo(
        url="https://example.invalid/1",
        source=SOURCE_BILIBILI,
        title="标题",
        uploader="作者",
        duration=90.0,
    )


def test_toggling_the_boxes_is_remembered_in_settings(page) -> None:
    settings = AppSettings()
    widget = page(settings)

    widget.per_video_thumbnail_checkbox.setChecked(True)
    widget.per_video_merge_checkbox.setChecked(False)

    assert settings.video_download_download_thumbnail is True
    assert settings.video_download_merge_video_audio is False
    assert widget._saved, "改了偏好应该落一次盘"


def test_a_new_task_starts_from_the_remembered_choice(page) -> None:
    """记住的值要真的用在下一个视频上 —— 封面那项原先是写死 False 的。"""
    widget = page(
        AppSettings(
            video_download_merge_video_audio=False,
            video_download_download_thumbnail=True,
        )
    )

    task = widget._create_download_task(_video_info())

    assert task.download_thumbnail is True
    assert task.merge_video_audio is False


def test_the_boxes_open_on_the_remembered_choice_with_no_task(page) -> None:
    """还没有任务时，两个框显示的也该是上次的选择，不是写死的默认值。"""
    widget = page(
        AppSettings(
            video_download_merge_video_audio=False,
            video_download_download_thumbnail=True,
        )
    )

    widget._sync_per_video_controls(None)

    assert widget.per_video_merge_checkbox.isChecked() is False
    assert widget.per_video_thumbnail_checkbox.isChecked() is True


def test_an_unchanged_toggle_does_not_write_to_disk(page) -> None:
    """勾成和设置里一样的值不该反复写盘。"""
    settings = AppSettings(video_download_merge_video_audio=True)
    widget = page(settings)
    widget._saved.clear()

    widget._remember_per_video_toggles()

    assert not widget._saved
