"""Hi-Res 混流页与宿主之间的边界。

同前两页：mixin 还混在 ``KrokHelperQtApp`` 上，清单挡住"顺手再摸一个宿主
成员"，且只该变短。
"""

from __future__ import annotations

import ast
import pathlib

PAGE = pathlib.Path(__file__).resolve().parents[1] / "krok_helper" / "hires" / "page.py"

#: 页面调用的宿主服务 —— 变成独立对象时，这些要么跟着搬，要么进宿主接口。
HOST_SERVICES = {
    "_track_background_task",  # 混流跑在后台线程
    "_resolve_ffmpeg_dir",  # 全局设置：ffmpeg 位置
    "_resolve_output_name_mode",  # 全局设置：输出命名模式
    "_resolve_output_name_templates",  # 全局设置：命名模板
    "_notify_handoff",  # 接收伴奏时的右下角提示
    "_open_settings_window",  # 打开全局设置的对应分页
}

#: 由外壳 ``__init__`` 建、页面读写的任务槽位 —— 应当跟着页面一起搬。
HOST_OWNED_STATE = {"hires_task"}


def _foreign_members() -> set[str]:
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HiResPageMixin")
    own = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assigned: set[str] = set()
    read: set[str] = set()
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            (assigned if isinstance(node.ctx, ast.Store) else read).add(node.attr)
    return read - own - assigned


def test_the_page_only_reaches_for_the_declared_host_surface() -> None:
    unexpected = _foreign_members() - HOST_SERVICES - HOST_OWNED_STATE

    assert not unexpected, (
        "Hi-Res 页多摸了宿主成员：" + "、".join(sorted(unexpected)) + "。"
        "先判断它该跟着页面搬、还是该走显式宿主接口，再决定要不要加进清单。"
    )


def test_the_declared_surface_has_not_gone_stale() -> None:
    stale = (HOST_SERVICES | HOST_OWNED_STATE) - _foreign_members()

    assert not stale, "清单里这些已经不再被引用，可以删了：" + "、".join(sorted(stale))


def test_the_page_does_not_import_the_shell() -> None:
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}

    assert not any(m == "krok_helper.gui_qt" or m.startswith("krok_helper.gui_qt.") for m in imported)


def test_the_handoff_entry_points_live_on_this_page() -> None:
    """其他页转交产物的落点就在本页，宿主契约靠它满足。"""
    from krok_helper.gui_qt import KrokHelperQtApp
    from krok_helper.hires.page import HiResPageMixin
    from krok_helper.workflow_host import WorkflowHost

    assert issubclass(KrokHelperQtApp, WorkflowHost)
    for entry in ("accept_separated_accompaniment", "set_video_path", "set_on_vocal_path"):
        assert getattr(KrokHelperQtApp, entry).__qualname__.startswith(HiResPageMixin.__name__)
