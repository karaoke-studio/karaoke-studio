"""共用控件的血统。

``gui_qt`` 里 ``QComboBox`` / ``QPushButton`` 这些名字是 qfluentwidgets 的别名。
把控件搬进 ``ui_kit`` 时很容易顺手从 ``PyQt6.QtWidgets`` 导同名的原生类 ——
代码照跑、测试全绿，界面上却退回系统外观。这条测试就是拦它的。
"""

from __future__ import annotations

from qfluentwidgets import ComboBox
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu

from krok_helper.ui_kit import StyledComboBox, WhiteComboBoxMenu


def test_styled_combo_box_is_a_fluent_combo() -> None:
    assert issubclass(StyledComboBox, ComboBox)


def test_white_combo_menu_is_a_fluent_menu() -> None:
    assert issubclass(WhiteComboBoxMenu, ComboBoxMenu)


def test_styled_combo_box_uses_the_white_menu() -> None:
    """下拉面板的白底样式靠这个覆写生效。"""
    assert StyledComboBox._createComboMenu is not ComboBox._createComboMenu
