"""点遍一整页按钮的共用装置（不是测试文件，是给测试用的工具）。

出发点见 :mod:`tests.test_alignment_click_paths` 的开头：方法级单测全绿不代表
按钮点得动，页面对象化把"页面 → 外壳"的调用改成了 ``self._host.xxx``，改错了
只有真点下去才会知道。

用法：::

    with sweep_guard(monkeypatch) as crashes:
        for name, widget in clickable(page):
            widget.click()
            assert not crashes, ...
"""

from __future__ import annotations

import sys
import traceback
from contextlib import contextmanager
from types import SimpleNamespace

from PyQt6.QtWidgets import QAbstractButton, QDialog, QFileDialog, QMenu, QWidget


def fake_popen(*_args, **_kwargs) -> SimpleNamespace:
    """撑得住 ``proc.stdout.close()`` / ``terminate()`` 这类用法的假子进程。"""
    return SimpleNamespace(
        stdout=SimpleNamespace(close=lambda: None, fileno=lambda: 0),
        stdin=SimpleNamespace(close=lambda: None, write=lambda _data: None),
        terminate=lambda: None,
        kill=lambda: None,
        wait=lambda timeout=None: 0,
        poll=lambda: None,
        returncode=None,
    )


def block_modals(monkeypatch) -> None:
    """把所有会转起事件循环 / 弹系统对话框的出口就地封住。

    漏一个的下场不是报错而是**整条扫描卡死**，还看不出卡在哪个按钮上（我在
    ``getSaveFileName`` 上栽过一次），所以这里宁可封得宽一点。
    """
    monkeypatch.setattr(QDialog, "exec", lambda self: 0)
    monkeypatch.setattr(QMenu, "exec", lambda self, *a, **k: None)
    monkeypatch.setattr(QMenu, "popup", lambda self, *a, **k: None)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("", "")))
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([], "")))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))


@contextmanager
def crash_collector():
    """接住信号槽里抛出的异常。

    Qt 不会把槽函数里的异常往上抛，而是转给 ``sys.excepthook``（线上就是被应用
    的全局钩子接住、弹成那个"发生未处理的错误"对话框）。所以 ``pytest.raises``
    在这里一点用都没有，只能换钩子。
    """
    crashes: list[str] = []
    original = sys.excepthook

    def _collect(exc_type, exc, tb) -> None:
        crashes.append(f"{exc_type.__name__}: {exc}\n" + "".join(traceback.format_tb(tb)[-4:]))

    sys.excepthook = _collect
    try:
        yield crashes
    finally:
        sys.excepthook = original


def clickable(page: QWidget, *, skip: set[str] = frozenset()) -> list[tuple[str, QAbstractButton]]:
    """页面上所有可点的按钮，带上属性名（报错时能指名道姓）。

    统一按 ``QAbstractButton`` 找：qfluentwidgets 的 PushButton / ToolButton /
    CheckBox / RadioButton 全在这条血脉上 —— 连它的 ComboBox 也是（继承自
    QPushButton），按 ``QComboBox`` 去找反而会漏掉，这个坑踩过。
    """
    named: dict[int, str] = {}
    for attr, widget in vars(page).items():
        if not isinstance(widget, QAbstractButton):
            continue
        # 不少按钮有历史别名（btn_auto_align / ExportWAVBtn ...）；报错要指得出
        # 正名，所以带页面前缀的优先，别被后来的别名盖掉。
        previous = named.get(id(widget))
        if previous is None or (previous.startswith("_") and not attr.startswith("_")):
            named[id(widget)] = attr

    found: list[tuple[str, QAbstractButton]] = []
    for widget in page.findChildren(QAbstractButton):
        name = named.get(id(widget)) or widget.objectName() or widget.text() or widget.__class__.__name__
        if name in skip:
            continue
        found.append((name, widget))
    return found
