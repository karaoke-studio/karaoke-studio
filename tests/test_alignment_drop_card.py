"""波形对齐素材卡片的冒烟测试。

这个类原来长在 ``gui_qt._build_alignment_page()`` 方法体里，构造它就得先
构造整个主窗口，于是一直没有测试覆盖。搬成独立模块后这里补上：构造、
选/清文件、主题切换、以及自绘 QSS 不被 qfluentwidgets 抹掉。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from qfluentwidgets import FluentIcon as FIF, Theme, qconfig, setTheme

from krok_helper.alignment import AlignmentDropCard


@pytest.fixture
def card():
    widget = AlignmentDropCard(
        media_label="字幕视频",
        title="选择字幕视频",
        hint="支持 mkv / mp4 / mov / avi",
        extensions={".mkv", ".mp4"},
        icon=FIF.VIDEO,
        theme="red",
    )
    yield widget
    widget.deleteLater()


def test_empty_card_shows_accent_styling(card) -> None:
    """空态：标题与提示行用主题强调色，图标带底色 —— 不是黑白默认样式。"""
    accent = card._theme_palette["accent"]

    assert card.title_label.text() == "字幕视频"
    assert card.file_name_label.text() == "未选择文件"
    assert accent in card.title_label.styleSheet()
    assert accent in card.action_label.styleSheet()
    assert card._theme_palette["icon_background"] in card.icon_button.styleSheet()


def test_accepts_only_known_extensions(tmp_path: Path, card) -> None:
    video = tmp_path / "a.mkv"
    video.write_bytes(b"")
    other = tmp_path / "a.txt"
    other.write_bytes(b"")

    assert card.accepts(video)
    assert not card.accepts(other)
    assert not card.accepts(tmp_path / "missing.mkv")


def test_set_and_clear_path(tmp_path: Path, card) -> None:
    video = tmp_path / "song.mkv"
    video.write_bytes(b"")

    card.set_path(video)
    assert card.path == video
    assert card.file_name_label.text() == "song.mkv"

    card.clear_path()
    assert card.path is None
    assert card.file_name_label.text() == "未选择文件"


def test_duration_text_change_emits_signal(card) -> None:
    """时长文案变化要通知宿主刷新导出面板（原来是直接回调宿主私有方法）。"""
    seen: list[int] = []
    card.durationTextChanged.connect(lambda: seen.append(1))

    card.detail_label.setText("字幕视频: 03:21")

    assert seen, "detail_label.setText 应当触发 durationTextChanged"


def test_custom_qss_survives_fluent_theme_reapply(card) -> None:
    """回归：卡片自绘 QSS 不得被 qfluentwidgets 的主题刷新抹掉。

    历史 bug —— 启动后卡片是黑白样式，鼠标划一下才恢复；根因是这些 Fluent
    子控件仍受 ``styleSheetManager`` 托管，主题刷新会整块重写它们的样式表。
    """
    original_theme = qconfig.themeMode.value
    accent = card._theme_palette["accent"]
    try:
        setTheme(Theme.DARK, lazy=False)
        setTheme(Theme.LIGHT, lazy=True)

        for widget in (card.title_label, card.action_label, card.icon_button):
            assert not widget.property("dirty-qss")
        assert accent in card.title_label.styleSheet()
        assert card._theme_palette["icon_background"] in card.icon_button.styleSheet()
    finally:
        setTheme(original_theme, lazy=False)


def test_theme_refresh_reapplies_styles(card) -> None:
    """主题切换回调：重算 palette 并把样式重新写进控件。"""
    card.title_label.setStyleSheet("")
    card.icon_button.setStyleSheet("")

    card._apply_theme_refresh()

    assert card._theme_palette["accent"] in card.title_label.styleSheet()
    assert card._theme_palette["icon_background"] in card.icon_button.styleSheet()
