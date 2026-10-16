"""共用控件的血统。

``gui_qt`` 里 ``QComboBox`` / ``QPushButton`` 这些名字是 qfluentwidgets 的别名。
把控件搬进 ``ui_kit`` 时很容易顺手从 ``PyQt6.QtWidgets`` 导同名的原生类 ——
代码照跑、测试全绿，界面上却退回系统外观。这条测试就是拦它的。
"""

from __future__ import annotations

import sys

import pytest
from PyQt6.QtCore import QChar
from PyQt6.QtGui import QFont, QFontDatabase, QTextLayout
from qfluentwidgets import ComboBox
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu

from krok_helper.ui_kit import StyledComboBox, WhiteComboBoxMenu
from krok_helper.ui_kit import configure_korean_font_fallback


def test_styled_combo_box_is_a_fluent_combo() -> None:
    assert issubclass(StyledComboBox, ComboBox)


def test_white_combo_menu_is_a_fluent_menu() -> None:
    assert issubclass(WhiteComboBoxMenu, ComboBoxMenu)


def test_styled_combo_box_uses_the_white_menu() -> None:
    """下拉面板的白底样式靠这个覆写生效。"""
    assert StyledComboBox._createComboMenu is not ComboBox._createComboMenu


def test_korean_fallback_overrides_competitor_but_preserves_primary_font() -> None:
    installed = QFontDatabase.families()
    competitor = next((f for f in ("NanumBarunGothic", "Source Han Sans SC", "Gulim", "Batang") if f in installed), None)
    if sys.platform != "win32" or competitor is None or not all(f in installed for f in ("Malgun Gothic", "Microsoft YaHei")):
        pytest.skip("Windows Korean and Chinese system fonts required")

    def faces(text, family="Microsoft YaHei", weight=QFont.Weight.Normal):
        layout = QTextLayout(text, QFont(family, 30, weight))
        layout.beginLayout()
        line = layout.createLine()
        line.setLineWidth(10000)
        layout.endLayout()
        return [(run.rawFont().familyName(), run.rawFont().styleName(), tuple(run.glyphIndexes())) for run in line.glyphRuns()]

    script = QChar.Script.Script_Hangul
    previous = QFontDatabase.applicationFallbackFontFamilies(script)
    try:
        QFontDatabase.setApplicationFallbackFontFamilies(script, [competitor])
        assert {f[0] for f in faces("한글")} == {competitor}
        unaffected = {text: faces(text) for text in ("0123456789", "中文", "あいう")}
        assert configure_korean_font_fallback()
        assert configure_korean_font_fallback()  # idempotent: no duplicate candidates
        assert QFontDatabase.applicationFallbackFontFamilies(script) == ["Malgun Gothic", competitor]
        for weight in (QFont.Weight.Normal, QFont.Weight.Bold):
            assert {f[0] for f in faces("한글", weight=weight)} == {"Malgun Gothic"}
        assert {f[0] for f in faces("한글", family=competitor)} == {competitor}
        assert {text: faces(text) for text in unaffected} == unaffected
    finally:
        QFontDatabase.setApplicationFallbackFontFamilies(script, previous)


def test_missing_korean_font_keeps_existing_fallback_policy(monkeypatch) -> None:
    monkeypatch.setattr(QFontDatabase, "families", lambda: [])
    monkeypatch.setattr(
        QFontDatabase, "setApplicationFallbackFontFamilies",
        lambda *args: pytest.fail("missing system font must not change the fallback policy"),
    )
    assert configure_korean_font_fallback() is False


def test_preview_bold_keeps_primary_and_default_fallback(qapp):
    from PyQt6.QtGui import QFont
    from strange_uta_game.frontend.font_utils import DEFAULT_FONT_FAMILY
    from strange_uta_game.frontend.editor.timing.karaoke_preview import KaraokePreview

    from krok_helper.sug_compat import _patch_preview_font_families
    _patch_preview_font_families()
    _patch_preview_font_families()
    preview = KaraokePreview()
    try:
        preview.set_font_sizes(18, main_font="Arial", ruby_font="Arial")
        expected = ["Arial", DEFAULT_FONT_FAMILY]
        assert preview._font_current.families() == expected
        assert preview._font_context.families() == expected
        assert preview._font_ruby.families() == expected
        assert preview._font_current.weight() == QFont.Weight.Bold
        assert preview._font_context.weight() == QFont.Weight.Normal
        preview.set_font_sizes(18, main_font=DEFAULT_FONT_FAMILY)
        assert preview._font_current.families() == [DEFAULT_FONT_FAMILY]
    finally:
        preview.close()


@pytest.mark.parametrize("family", ["Microsoft YaHei", "Arial", "NanumBarunGothic"])
def test_lyric_bold_resolves_ascii_in_selected_family(qapp, family):
    from PyQt6.QtGui import QFont, QFontDatabase, QTextLayout
    from krok_helper.sug_compat import _patch_preview_font_families
    _patch_preview_font_families()
    from strange_uta_game.frontend.font_utils import lyrics_font

    if family not in QFontDatabase.families():
        pytest.skip("test font is not installed")
    for weight in (QFont.Weight.Normal, QFont.Weight.Bold):
        font = lyrics_font(family, 22, weight)
        assert font.styleName() in QFontDatabase.styles(family)
        layout = QTextLayout("ABCxyz0123456789", font)
        layout.beginLayout()
        line = layout.createLine()
        line.setLineWidth(10000)
        layout.endLayout()
        runs = line.glyphRuns()
        assert runs
        assert {run.rawFont().familyName() for run in runs} == {family}
        assert all(0 not in run.glyphIndexes() for run in runs)
        assert all(run.rawFont().weight() == weight.value for run in runs)


def test_click_focus_paints_ascii_with_primary_bold(qapp, monkeypatch):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QFontDatabase, QPainter, QRawFont
    from PyQt6.QtTest import QTest
    from strange_uta_game.backend.domain import Project, Sentence
    from strange_uta_game.frontend.editor.timing import karaoke_preview as module

    if "Microsoft YaHei" not in QFontDatabase.families():
        pytest.skip("Microsoft YaHei required")
    from krok_helper.sug_compat import _patch_preview_font_families
    _patch_preview_font_families()
    painted = []

    class RecordingPainter(QPainter):
        def drawText(self, *args):
            if self.font().pointSize() >= 18 and args and isinstance(args[-1], str) and args[-1] in ("A", "1", "A1"):
                raw = QRawFont.fromFont(self.font())
                painted.append((raw.familyName(), raw.weight()))
            return super().drawText(*args)

    monkeypatch.setattr(module, "QPainter", RecordingPainter)
    preview = module.KaraokePreview()
    try:
        preview.resize(800, 500)
        preview.set_font_sizes(18, main_font="Microsoft YaHei")
        project = Project()
        project.add_sentence(Sentence.from_text("test", project.get_default_singer().id))
        project.add_sentence(Sentence.from_text("A1", project.get_default_singer().id))
        preview.set_project(project)
        preview.set_current_position(0, 0)
        preview.show()
        QTest.qWait(100)
        preview.grab()
        assert ("Microsoft YaHei", 400) in painted
        rect = next(rect for rect, line, char in preview._char_hitboxes if line == 1 and char == 0)
        painted.clear()
        QTest.mouseClick(preview, Qt.MouseButton.LeftButton, pos=rect.center())
        QTest.qWait(450)
        preview.grab()
        assert preview._focus_line_idx == 1
        assert ("Microsoft YaHei", 700) in painted
        assert all(family == "Microsoft YaHei" for family, _ in painted)
    finally:
        preview.close()


def test_pinned_lyric_style_keeps_chinese_and_korean_fallback(qapp):
    from krok_helper.sug_compat import _patch_preview_font_families
    _patch_preview_font_families()
    from strange_uta_game.frontend.font_utils import lyrics_font
    if sys.platform != "win32" or not all(f in QFontDatabase.families() for f in ("Arial", "Microsoft YaHei", "Malgun Gothic")):
        pytest.skip("Windows system fonts required")
    script = QChar.Script.Script_Hangul
    previous = QFontDatabase.applicationFallbackFontFamilies(script)
    try:
        configure_korean_font_fallback()
        for weight in (QFont.Weight.Normal, QFont.Weight.Bold):
            layout = QTextLayout("A1中文한글", lyrics_font("Arial", 22, weight))
            layout.beginLayout()
            line = layout.createLine()
            line.setLineWidth(10000)
            layout.endLayout()
            assert {run.rawFont().familyName() for run in line.glyphRuns()} == {"Arial", "Microsoft YaHei", "Malgun Gothic"}
    finally:
        QFontDatabase.setApplicationFallbackFontFamilies(script, previous)
