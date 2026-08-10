"""三个可调高度的面板压到最矮时不能把内容挤坏。

每张卡片原来都写死了一个最小高度（145 / 260 / 180），全都**小于**它自己布局的
真实最小值（176 / 352 / 218）。``QSplitter`` 认显式的 ``minimumHeight``，于是能把
卡片压得比内容还矮，表现是：

* 顶部卡片：提示文字被压进输入框、右侧「解析 / 清空」挤到看不见；
* 中间卡片：缩略图和文字互相重叠；
* 下载列表：表格被压成 0，一条任务都看不见。

数字不该手写 —— 交给布局算。
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QToolButton

from krok_helper.settings import AppSettings
from krok_helper.video_download.video_download_page import (
    SPLITTER_SAVE_DELAY_MS,
    VideoDownloadPage,
)

PANEL_KEYS = ("input", "info", "download")


@pytest.fixture
def page(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(VideoDownloadPage, "_refresh_cookie_status", lambda _self: None)
    monkeypatch.setattr(
        VideoDownloadPage, "_refresh_youtube_cookie_status", lambda _self: None
    )
    monkeypatch.setattr(VideoDownloadPage, "_ensure_qr_login", lambda _self: None)
    widget = VideoDownloadPage(AppSettings(), lambda: None)
    widget.resize(1400, 1000)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def _cards(page: VideoDownloadPage) -> list:
    return [page.content_splitter.widget(index) for index in range(3)]


def test_no_card_declares_a_minimum_below_its_layout(page) -> None:
    """写死的下限必须让位给布局算出来的下限。"""
    for key, card in zip(PANEL_KEYS, _cards(page)):
        assert card.minimumHeight() <= card.minimumSizeHint().height(), key


def test_squeezing_everything_never_overflows_a_card(page) -> None:
    """把窗口压到装不下三块 —— 每张卡片仍要装得下自己的内容。

    光 ``setSizes([1,1,1])`` 是压不动的：分割器只会在**总高度不够**时才真的去挤
    每一块，所以先把页面本身缩矮。
    """
    page.resize(1400, 560)
    page.content_splitter.setSizes([1, 1, 1])
    QApplication.instance().processEvents()

    for key, card in zip(PANEL_KEYS, _cards(page)):
        needed = card.minimumSizeHint().height()
        assert card.height() >= needed, f"{key} 被压到 {card.height()}，需要 {needed}"


def test_the_parse_buttons_survive_the_squeeze(page) -> None:
    """右侧「解析 / 清空」被挤到看不见是最早发现的症状。"""
    page.resize(1400, 560)
    page.content_splitter.setSizes([1, 1, 1])
    QApplication.instance().processEvents()

    for button in (page.parse_button, page.clear_input_button):
        assert button.width() > 0 and button.height() > 0
        assert button.isVisible()


def test_the_download_table_keeps_a_header_and_a_row(page) -> None:
    """拖到最矮时至少还看得见一条任务，而不是只剩一条表头线。"""
    page.resize(1400, 560)
    page.content_splitter.setSizes([1, 1, 1])
    QApplication.instance().processEvents()

    header = page.download_table.horizontalHeader().sizeHint().height()
    assert page.download_table.minimumHeight() > header
    assert page.download_table.height() > header


def test_new_users_get_compact_top_cards_by_default(page) -> None:
    cards = _cards(page)

    assert cards[0].height() == cards[0].minimumSizeHint().height()
    assert cards[1].height() == cards[1].minimumSizeHint().height()
    assert cards[2].height() > cards[2].minimumSizeHint().height()


def test_saved_region_sizes_are_restored_after_show(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(VideoDownloadPage, "_refresh_cookie_status", lambda _self: None)
    monkeypatch.setattr(VideoDownloadPage, "_refresh_youtube_cookie_status", lambda _self: None)
    monkeypatch.setattr(VideoDownloadPage, "_ensure_qr_login", lambda _self: None)
    original = VideoDownloadPage(AppSettings(), lambda: None)
    original.resize(1400, 1000)
    original.show()
    app.processEvents()
    main_available = sum(original.main_splitter.sizes())
    content_available = sum(original.content_splitter.sizes())
    expected_main = [280, main_available - 280]
    expected_content = [190, 360, content_available - 550]
    original.main_splitter.setSizes(expected_main)
    original.content_splitter.setSizes(expected_content)
    app.processEvents()
    settings = AppSettings(
        video_download_main_splitter_sizes=original.main_splitter.sizes(),
        video_download_content_splitter_sizes=original.content_splitter.sizes(),
    )
    original.close()
    original.deleteLater()
    app.processEvents()

    restored = VideoDownloadPage(settings, lambda: None)
    restored.resize(1400, 1000)
    restored.show()
    app.processEvents()
    try:
        assert restored.main_splitter.sizes() == expected_main
        assert restored.content_splitter.sizes() == expected_content
    finally:
        restored.close()
        restored.deleteLater()
        app.processEvents()


def test_dragged_region_sizes_are_saved_with_debounce(page, qtbot) -> None:
    saved: list[tuple[list[int], list[int]]] = []
    page._save_settings = lambda: saved.append(
        (
            page.settings.video_download_main_splitter_sizes.copy(),
            page.settings.video_download_content_splitter_sizes.copy(),
        )
    )

    page.main_splitter.splitterMoved.emit(300, 1)
    page.content_splitter.splitterMoved.emit(200, 1)

    assert page.settings.video_download_main_splitter_sizes == page.main_splitter.sizes()
    assert page.settings.video_download_content_splitter_sizes == page.content_splitter.sizes()
    qtbot.wait(SPLITTER_SAVE_DELAY_MS + 50)
    assert saved == [
        (
            page.main_splitter.sizes(),
            page.content_splitter.sizes(),
        )
    ]


def test_icon_only_buttons_are_tool_buttons(page) -> None:
    """``PushButton(icon, "")`` 会按"图标 + 文字"整体居中，空文字仍占位，图标于是偏右。"""
    assert isinstance(page.delete_current_task_button, QToolButton)


def test_the_row_delete_button_is_a_tool_button_too(page) -> None:
    from krok_helper.video_download import video_download_page as module

    button = module._create_delete_button("删除")
    try:
        assert isinstance(button, QToolButton)
    finally:
        button.deleteLater()


# ── 「第 N 个 …」导航行 ──────────────────────────────────────


def _show_details(page: VideoDownloadPage) -> None:
    page.video_details_stack.setCurrentIndex(1)
    page._sync_task_switch_row_visibility()
    QApplication.instance().processEvents()


def _center_y(page: VideoDownloadPage, widget) -> int:
    return widget.mapTo(page, widget.rect().center()).y()


def test_the_task_switch_row_sits_on_the_card_title_line(page) -> None:
    """它是"当前在看第几个视频"的导航，属于标题的一部分，不该另起一行。"""
    _show_details(page)

    offset = abs(
        _center_y(page, page.task_switch_row)
        - _center_y(page, page.delete_current_task_button)
    )

    assert offset <= 4, f"导航行和标题行差了 {offset}px"


def test_the_task_switch_row_hides_without_a_video(page) -> None:
    assert page.video_details_stack.currentIndex() == 0
    assert not page.task_switch_row.isVisible()


def test_collapsing_hides_the_task_switch_row(page) -> None:
    _show_details(page)

    page._toggle_panel_collapsed("info")
    QApplication.instance().processEvents()

    assert not page.task_switch_row.isVisible()


def test_expanding_restores_it_only_when_there_is_a_video(page) -> None:
    """展开是无差别 show 的，空状态下不能把导航行也放出来。"""
    page._toggle_panel_collapsed("info")
    QApplication.instance().processEvents()

    page._toggle_panel_collapsed("info")
    QApplication.instance().processEvents()

    assert page.video_details_stack.currentIndex() == 0
    assert not page.task_switch_row.isVisible()

    _show_details(page)

    assert page.task_switch_row.isVisible()
