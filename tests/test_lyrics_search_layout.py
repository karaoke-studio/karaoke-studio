"""歌词检索页的浮动「导入到打轴」按钮。

这个按钮不在布局里，是浮在预览面板右上角的，位置靠面板的 Resize/Show 事件重排。
页面对象化时接收这些事件的 ``eventFilter`` 一度没跟着搬过来，按钮就停在
面板左上角 (0, 0)，压在「歌词预览」标题上 —— 而且只有选中歌曲后才会被摆正，
所以"搜过歌就正常"，一进页面反而是歪的。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from krok_helper.lyrics_search.page import LyricsSearchPage
from krok_helper.settings import AppSettings


@pytest.fixture
def page():
    app = QApplication.instance() or QApplication([])
    widget = LyricsSearchPage(
        host=SimpleNamespace(
            settings=AppSettings(),
            track_background_task=lambda task: task,
            install_single_click_combo_behavior=lambda combo: None,
            import_current_lyrics_to_timing=lambda: None,
        )
    )
    widget.resize(760, 640)
    widget.show()
    app.processEvents()
    yield widget, app
    widget.close()
    widget.deleteLater()


def _settle(app) -> None:
    # 重排走的是 QTimer.singleShot(0)，多转几圈让它落定。
    for _ in range(6):
        app.processEvents()


def test_the_import_button_sits_at_the_panel_top_right_before_any_search(page) -> None:
    """什么都没搜索时按钮也该在右上角（页面里它一直可见，只是禁用）。"""
    widget, app = page
    _settle(app)

    button = widget.import_lyrics_to_timing_button
    panel = widget.lyrics_preview_panel

    assert button.pos().x() > 0, "按钮停在左上角 —— 面板的重排事件没送到"
    assert panel.width() - (button.x() + button.width()) < 40, "按钮应当贴着面板右缘"


def test_the_import_button_does_not_cover_the_preview_title(page) -> None:
    widget, app = page
    _settle(app)

    button = widget.import_lyrics_to_timing_button
    title = widget.lyrics_preview_title_label

    assert not button.geometry().intersects(title.geometry())


def test_the_button_is_repositioned_after_a_resize(page) -> None:
    """这条才是那次回归的守卫：漏掉 ``eventFilter`` 时按钮不会跟着重排。"""
    widget, app = page
    _settle(app)
    button = widget.import_lyrics_to_timing_button

    widget.resize(1100, 640)
    _settle(app)

    panel = widget.lyrics_preview_panel
    assert panel.width() - (button.x() + button.width()) < 40
