"""根 conftest 的 ``_reap_stray_toplevel_widgets``：测试新建的无父窗口必须在测试结束后立刻销毁。

此前泄漏窗口累积到模块级 ``qapp`` teardown 一次性批量关闭，单次清账最多
308s；这里锁定按测试粒度回收的两个关键行为——本测试新建的要回收、
更高作用域 fixture 的不误回收。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

_ORPHAN: QWidget | None = None


def test_orphan_created_during_test_is_tracked():
    global _ORPHAN
    _ORPHAN = QWidget()
    QApplication.processEvents()
    assert _ORPHAN is not None


def test_orphan_from_previous_test_was_reaped():
    # 回收器在上个测试结束后已 close+deleteLater 并冲刷 DeferredDelete，
    # C++ 对象已删，再触碰 wrapper 会抛 RuntimeError。
    assert _ORPHAN is not None
    with pytest.raises(RuntimeError):
        _ORPHAN.isVisible()


@pytest.fixture(scope="module")
def surviving_panel():
    return QWidget()


def test_module_fixture_window_exists(surviving_panel):
    assert surviving_panel is not None


def test_module_fixture_window_survives_reaper(surviving_panel):
    # 模块级 fixture 先于函数级回收器实例化，其窗口必须落在快照里，
    # 跨测试存活（若被误回收，这里访问会抛 RuntimeError 而非返回 False）。
    assert surviving_panel.isVisible() is False
