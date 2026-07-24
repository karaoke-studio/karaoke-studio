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


# 这些模块专门测设置文件的路径解析 / 原子写，会自行用 APPDATA 等做隔离；
# 强制 KARAOKE_STUDIO_SETTINGS_DIR 会盖掉它们要验证的解析逻辑。
_SETTINGS_PATH_AWARE_MODULES = frozenset({"test_settings_atomic_io"})


@pytest.fixture(autouse=True)
def _isolated_app_settings(request, tmp_path_factory, monkeypatch):
    """给每个测试一份独立的设置目录。

    ``SubtitleRenderWindow`` 等窗口在构造和交互时会读写真实的
    ``%APPDATA%\\Karaoke Studio\\settings.json``：跑一次测试就把用例里的导出
    参数（CRF / 编码器 / 画布尺寸 / 输出目录…）写进用户的真实配置；反过来用户
    的配置又会让断言默认值的用例失败。测试永远不该碰真实配置。

    自己 ``monkeypatch.setenv`` 设置目录的测试照旧覆盖这里的值。
    """
    if request.module.__name__ in _SETTINGS_PATH_AWARE_MODULES:
        yield
        return
    from krok_helper.settings import SETTINGS_DIR_ENV

    monkeypatch.setenv(
        SETTINGS_DIR_ENV, str(tmp_path_factory.mktemp("app-settings"))
    )
    yield
