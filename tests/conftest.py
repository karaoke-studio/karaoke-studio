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


def _stop_media_outputs(widgets) -> None:
    """销毁窗口前显式停掉测试懒创建的 QMediaPlayer。

    与 ``test_subtitle_render_transport._release_media_objects`` 同一套防御：
    若把 QMediaPlayer/QAudioOutput 留给解释器退出时的 Python GC 与 PyQt6
    多媒体后端 C++ 析构竞争，会段错误（Python 3.14 退出期尤甚）。
    """
    try:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtMultimedia import QMediaPlayer
    except ImportError:
        return
    for widget in widgets:
        for attr in ("_player", "_video_player"):
            player = getattr(widget, attr, None)
            if isinstance(player, QMediaPlayer):
                try:
                    player.stop()
                    player.setSource(QUrl())
                    player.setAudioOutput(None)
                    player.setVideoOutput(None)
                except (RuntimeError, TypeError):
                    pass


@pytest.fixture(autouse=True)
def _reap_stray_toplevel_widgets():
    """测试结束后立刻回收本测试新建的无父顶层窗口。

    背景：大量 GUI 测试构造 ``SubtitleRenderWindow`` / 面板后不再销毁，
    全进程的泄漏窗口会累积到 ``test_subtitle_render_property_panel`` 等
    模块级 ``qapp`` teardown 一次性 ``topLevelWidgets()`` 批量关闭——
    ``topLevelWidgets()`` 是进程级的，那一个 teardown 实测最多 308s
    （占全量 18m38s 的 27%），且随累积量超线性、量级不稳定（12s~308s），
    还让进程堆持续膨胀，拖慢其它依赖 ``gc.collect()`` 的测试。

    这里按测试粒度回收：进入测试前快照顶层窗口集合，结束后只处理新增项。
    pytest 按 scope 排序实例化 fixture：更高作用域（session/module/class）
    的 fixture 先于本函数级 autouse fixture 实例化，它们的窗口天然落在
    快照里不会被误回收；函数级 fixture 的 teardown 按逆序先于本 fixture
    收尾，也不会撞上已删的 C++ 对象。各模块末尾的批量关闭保留作兜底，
    泄漏变少后它本身不再产生可观耗时。
    """
    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        yield
        return
    before = {id(widget) for widget in QApplication.topLevelWidgets()}
    yield
    strays = [
        widget
        for widget in QApplication.topLevelWidgets()
        if id(widget) not in before
    ]
    if not strays:
        return
    _stop_media_outputs(strays)
    for widget in strays:
        try:
            widget.close()
            widget.deleteLater()
        except RuntimeError:
            # C++ 对象已随其它路径销毁（fixture 自行 deleteLater 等）。
            continue
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


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
