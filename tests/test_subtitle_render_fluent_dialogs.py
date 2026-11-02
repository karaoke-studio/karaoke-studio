"""图片序列源帧率筛选输入控件与弹窗测试。"""

from __future__ import annotations

from fractions import Fraction

import pytest

from PyQt6.QtWidgets import QApplication

from krok_helper.subtitle_render.domain.frame_rate import frame_rate
from krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs import (
    FilterableFpsCombo,
    FluentFpsInputDialog,
    format_sequence_fps_text,
    parse_sequence_fps_text,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_parse_sequence_fps_text_accepts_decimal_and_fraction_forms():
    assert parse_sequence_fps_text("60") == 60
    assert parse_sequence_fps_text(" 24 ") == 24
    assert parse_sequence_fps_text("0.5") == 0.5
    assert parse_sequence_fps_text("23.976") == 23.976
    assert parse_sequence_fps_text("1/30") == float(Fraction(1, 30))
    assert parse_sequence_fps_text("1/2") == 0.5


def test_parse_sequence_fps_text_rejects_invalid_values():
    assert parse_sequence_fps_text("") is None
    assert parse_sequence_fps_text("abc") is None
    assert parse_sequence_fps_text("0") is None
    assert parse_sequence_fps_text("-5") is None
    assert parse_sequence_fps_text("1/0") is None
    assert parse_sequence_fps_text("500") is None  # 超上限 240
    assert parse_sequence_fps_text("1/2400") is None  # 低于下限 0.001


def test_parse_sequence_fps_text_accepts_documented_bounds():
    assert parse_sequence_fps_text("0.001") == 0.001
    assert parse_sequence_fps_text("1/1000") == 0.001
    assert parse_sequence_fps_text("240") == 240


def test_format_sequence_fps_text_prefers_compact_decimal():
    assert format_sequence_fps_text(60) == "60"
    assert format_sequence_fps_text(0.5) == "0.5"
    assert format_sequence_fps_text(0.25) == "0.25"
    assert format_sequence_fps_text(23.976) == "23.976"
    assert format_sequence_fps_text(float(Fraction(1, 30))) == "1/30"


def test_filterable_fps_combo_lists_presets(qapp):
    combo = FilterableFpsCombo()

    items = [combo.itemText(i) for i in range(combo.count())]
    assert items == [
        "0.25", "0.5", "1", "2", "3", "5", "8", "10", "12",
        "15", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60", "120",
    ]
    assert combo.value() == 60
    assert combo.text() == "60"


def test_filterable_fps_combo_submit_accepts_custom_values(qapp):
    combo = FilterableFpsCombo()

    assert combo.submit_text("0.5") is True
    assert combo.value() == 0.5
    assert combo.text() == "0.5"

    assert combo.submit_text("1/30") is True
    assert frame_rate(combo.value()) == Fraction(1, 30)
    assert combo.text() == "1/30"

    assert combo.submit_text("24.0") is True
    assert combo.value() == 24
    assert combo.text() == "24"


def test_filterable_fps_combo_reverts_invalid_input(qapp):
    combo = FilterableFpsCombo(value=23.976)

    for bad in ("abc", "0", "-5", "1/0", "500", ""):
        assert combo.submit_text(bad) is False
        assert combo.value() == 23.976
        assert combo.text() == "23.976"


def test_filterable_fps_combo_emits_only_for_user_commits(qapp):
    combo = FilterableFpsCombo()
    emitted: list[object] = []
    combo.fpsValidated.connect(emitted.append)

    combo.setValue(0.5)
    assert emitted == []
    assert combo.value() == 0.5

    combo.submit_text("24")
    assert emitted == [24]

    combo.submit_text("bogus")
    assert emitted == [24]


def test_filterable_fps_combo_commit_timer_validates_typing(qapp):
    combo = FilterableFpsCombo()
    emitted: list[object] = []
    combo.fpsValidated.connect(emitted.append)

    combo.setText("0.25")
    combo.textEdited.emit("0.25")
    # 文本变化（含程序设值）必须挂起提交定时器——这是提交链路的唯一入口。
    assert combo._commit_timer.isActive()
    combo._commit_timer.stop()  # 不等 200ms，直接触发一次提交语义
    combo._commit_typing()

    assert combo.value() == 0.25
    assert emitted == [0.25]


def test_filterable_fps_combo_return_key_commits_without_adding_item(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    combo = FilterableFpsCombo(value=23.976)
    preset_count = combo.count()

    combo.setText("0.5")
    QTest.keyClick(combo, Qt.Key.Key_Return)

    assert combo.value() == 0.5
    assert combo.count() == preset_count  # 自定义值不进预设列表

    combo.setText("zz")
    QTest.keyClick(combo, Qt.Key.Key_Return)

    # 非法输入回退到上一个合法值（此处是刚提交的 0.5，而非初始 23.976）。
    assert combo.value() == 0.5
    assert combo.text() == "0.5"
    assert combo.count() == preset_count


def test_filterable_fps_combo_bounds_programmatic_values(qapp):
    combo = FilterableFpsCombo(value=1000)

    assert combo.value() == 240

    combo.setValue(0.00001)
    assert combo.value() == 0.001


def test_fluent_fps_input_dialog_value_round_trip(qapp):
    dialog = FluentFpsInputDialog("图片序列帧率", "源帧率", value=29.97)

    assert dialog.value() == 29.97
    assert dialog.control.text() == "29.97"

    assert dialog.control.submit_text("1/30") is True
    assert dialog.value() == float(Fraction(1, 30))


def test_fluent_fps_input_dialog_preselects_initial_text(qapp):
    dialog = FluentFpsInputDialog("图片序列帧率", "源帧率", value=60)

    assert dialog.control.hasSelectedText()
    assert dialog.control.selectedText() == "60"
