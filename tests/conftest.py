"""测试套件级 Qt 应用生命周期管理。

各测试文件惯用 ``QApplication.instance() or QApplication([])`` 自建应用，且引用
只活在 fixture / 测试局部。模块跑完引用被 GC 时，PyQt6 会随 C++ QApplication 的
销毁**连带删除所有 QObject**——包括 qfluentwidgets 的全局 ``qconfig`` 单例。之后
同进程里任何再创建 qfluentwidgets 控件的测试都会撞上::

    RuntimeError: wrapped C/C++ object of type QConfig has been deleted

（此前表现为多个测试文件合跑时互相污染、单跑各自全过。）

这里在任何测试运行前创建一个进程级 QApplication，并用模块全局钉住引用；后续
所有 ``QApplication.instance() or ...`` 都命中它，谁也不会把它送去 GC。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

_PINNED_QAPP = None


@pytest.fixture(scope="session", autouse=True)
def _pinned_qapp():
    global _PINNED_QAPP
    from PyQt6.QtWidgets import QApplication

    _PINNED_QAPP = QApplication.instance() or QApplication([])
    yield _PINNED_QAPP
