"""波形对齐素材卡片的冒烟测试。

这个类原来长在 ``gui_qt._build_alignment_page()`` 方法体里，构造它就得先
构造整个主窗口，于是一直没有测试覆盖。搬成独立模块后这里补上：构造、
选/清文件、主题切换、以及自绘 QSS 不被 qfluentwidgets 抹掉。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication
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


def test_action_buttons_are_fluent_widgets(card) -> None:
    """「更换」「移除」必须是 Fluent 按钮 —— ``gui_qt`` 里 ``QPushButton`` 是
    ``PushButton`` 的别名，搬模块时导成 PyQt 原生按钮会矮 6px、画风也不一致。"""
    from qfluentwidgets import PushButton

    assert isinstance(card.replace_button, PushButton)
    assert isinstance(card.remove_button, PushButton)


LONG_NAME = "『アークナイツ：エンドフィールド』リーノEP - 『Mirairo Rider (Japanese Ver)』 [216].mp4"


def _chip_with_long_name(card, width: int) -> str:
    """把卡片摆成收起态、给定宽度，返回标题栏上真正显示的那行字。"""
    card.set_path(Path("D:/tmp") / LONG_NAME)
    card.detail_label.setText("字幕视频: 3:36.120")
    card.set_display_mode("chip")
    card.resize(width, 60)
    card.show()
    QApplication.instance().processEvents()
    return card.title_label.text()


def test_a_long_file_name_is_elided_but_the_duration_survives(card):
    """收起态是「文件名 · 时长」一行，文件名长起来会把时长从右边挤没。

    QLabel 是从右边截的，正好切掉时长 —— 而时长才是这一行真正要看的信息。
    所以只截文件名，时长整段留着。
    """
    text = _chip_with_long_name(card, 520)

    assert text.endswith("3:36.120"), f"时长被挤掉了：{text}"
    assert "…" in text, f"文件名没截断：{text}"
    assert len(text) < len(LONG_NAME), "截了个寂寞"


def test_the_duration_still_survives_when_the_card_gets_narrower(card):
    """窄到只剩几个字也不能牺牲时长。"""
    text = _chip_with_long_name(card, 300)

    assert text.endswith("3:36.120"), f"时长被挤掉了：{text}"


def test_a_wider_card_shows_more_of_the_name(card):
    """宽度变了要重新截 —— 布局给标签多少宽度只有排完版才知道。"""
    narrow = _chip_with_long_name(card, 420)
    card.resize(900, 60)
    QApplication.instance().processEvents()
    wide = card.title_label.text()

    assert len(wide) > len(narrow), f"拉宽了却没多显示：{narrow!r} -> {wide!r}"
    assert wide.endswith("3:36.120")


def test_a_short_name_is_not_elided(card):
    card.set_path(Path("D:/tmp/GO GHOST.mp4"))
    card.detail_label.setText("字幕视频: 2:51.367")
    card.set_display_mode("chip")
    card.resize(520, 60)
    card.show()
    QApplication.instance().processEvents()

    assert card.title_label.text() == "GO GHOST.mp4 · 2:51.367"


def test_the_expanded_card_keeps_showing_the_media_label(card):
    """展开态标题还是「字幕视频」这种固定文案，别被收起态的逻辑带跑。"""
    card.set_path(Path("D:/tmp") / LONG_NAME)
    card.set_display_mode("ready")
    QApplication.instance().processEvents()

    assert card.title_label.text() == "字幕视频"
