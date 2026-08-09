"""波形对齐的交接弹窗。

这些控件必须是 qfluentwidgets 那一套：``gui_qt`` 里 ``QCheckBox`` /
``QPushButton`` 是 ``CheckBox`` / ``PushButton`` 的别名，把类搬进独立模块时
很容易顺手从 ``PyQt6.QtWidgets`` 导入同名的原生控件 —— 界面上就表现为
「取消」比「确认」矮一截（原生 26px vs Fluent 32px），勾选框也不是一套画风。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from qfluentwidgets import CheckBox, PrimaryPushButton, PushButton

from krok_helper.alignment import AlignmentHandoffDialog


@pytest.fixture
def dialog():
    d = AlignmentHandoffDialog(is_video_target=True, output_path=Path("D:/tmp/对齐后.mp4"))
    yield d
    d.deleteLater()


def test_buttons_are_fluent_widgets(dialog) -> None:
    assert isinstance(dialog.yesButton, PrimaryPushButton)
    assert isinstance(dialog.cancelButton, PushButton)


def test_checkboxes_are_fluent_widgets(dialog) -> None:
    assert isinstance(dialog.subtitle_check, CheckBox)
    assert isinstance(dialog.hires_check, CheckBox)


def test_the_two_buttons_are_the_same_height(dialog) -> None:
    dialog.resize(660, 320)
    dialog.show()
    try:
        assert dialog.cancelButton.height() == dialog.yesButton.height()
        assert dialog.cancelButton.width() == dialog.yesButton.width()
    finally:
        dialog.close()


def test_both_targets_are_ticked_by_default(dialog) -> None:
    assert dialog.selections() == (True, True)


def test_selections_follow_the_checkboxes(dialog) -> None:
    dialog.subtitle_check.setChecked(False)
    assert dialog.selections() == (False, True)
    dialog.hires_check.setChecked(False)
    assert dialog.selections() == (False, False)


@pytest.mark.parametrize(
    ("is_video_target", "subtitle_text", "hires_text"),
    [
        (True, "将导出的对齐视频作为字幕渲染背景素材", "将用于对齐的原唱音源作为 Hi-Res 混流原唱音源"),
        (False, "将用于对齐的视频作为字幕渲染背景素材", "将导出的对齐音频作为 Hi-Res 混流原唱音源"),
    ],
)
def test_wording_follows_the_export_target(is_video_target, subtitle_text, hires_text) -> None:
    d = AlignmentHandoffDialog(is_video_target=is_video_target, output_path=Path("D:/tmp/out"))
    try:
        assert d.subtitle_check.text() == subtitle_text
        assert d.hires_check.text() == hires_text
    finally:
        d.deleteLater()
