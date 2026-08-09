"""提示框必须真的能看见，而且不能锁住工作台。

历史问题有两个，都表现为「界面像是卡死了」：

* 原生 ``QMessageBox`` 会被 SUG 的全局非模态策略在 Show 事件里改窗口模态，
  Qt 把正在显示的窗口重新收起来 —— 屏幕上剩一个没有内容的白框；
* 带遮罩的 Fluent ``MessageBox``（``MaskDialogBase``）在工作台的堆叠页层级里
  会盖住内容并吃掉鼠标事件。

所以工作台统一用 ``qfluent_compat`` 里那套：Fluent 外观、顶层窗口、无遮罩、
无 Qt 模态。这里盯的是症状 —— 弹得出来、看得见、主界面还能用。
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QPushButton, QWidget

from krok_helper.qfluent_compat import show_fluent_error, show_fluent_info, show_fluent_warning

UI_MODULES = [
    pathlib.Path(__file__).resolve().parents[1] / "krok_helper" / name
    for name in ("gui_qt.py", "alignment/page.py", "hires/page.py", "lyrics_search/page.py")
]


@pytest.fixture
def host():
    app = QApplication.instance() or QApplication([])
    widget = QWidget()
    widget.resize(600, 400)
    widget.show()
    app.processEvents()
    yield widget
    for dialog in [d for d in app.topLevelWidgets() if isinstance(d, QDialog) and d.isVisible()]:
        dialog.close()
    widget.close()
    widget.deleteLater()


def _visible_dialogs() -> list[QDialog]:
    return [d for d in QApplication.instance().topLevelWidgets() if isinstance(d, QDialog) and d.isVisible()]


@pytest.mark.parametrize("show", [show_fluent_error, show_fluent_info, show_fluent_warning])
def test_the_dialog_is_actually_visible(host, show) -> None:
    """回归：报错框曾经是一个空白窗框 —— 窗口在、内容不在。"""
    show(host, "请先选择有效的文件: 字幕视频")
    QApplication.instance().processEvents()

    dialogs = _visible_dialogs()
    assert len(dialogs) == 1
    dialog = dialogs[0]
    assert not dialog.isHidden()
    assert any(button.isVisible() for button in dialog.findChildren(QPushButton))


def test_the_dialog_does_not_lock_the_workbench(host) -> None:
    """无遮罩、无模态：弹窗开着的时候主界面照样能点。"""
    show_fluent_error(host, "出错了")
    QApplication.instance().processEvents()

    dialog = _visible_dialogs()[0]
    assert not dialog.isModal()
    assert QApplication.instance().activeModalWidget() is None
    assert host.isEnabled()


def test_the_dialog_carries_the_message(host) -> None:
    show_fluent_info(host, "当前生成任务还在处理中，请稍等。")
    QApplication.instance().processEvents()

    dialog = _visible_dialogs()[0]
    assert "当前生成任务还在处理中" in dialog.contentLabel.text()


@pytest.mark.parametrize("module", UI_MODULES, ids=lambda p: p.name)
def test_the_ui_modules_do_not_use_native_message_boxes(module: pathlib.Path) -> None:
    """别退回原生 ``QMessageBox`` —— 它正是空白窗框的来源。"""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    assert "QMessageBox" not in names, f"{module.name} 又用回了原生 QMessageBox"
