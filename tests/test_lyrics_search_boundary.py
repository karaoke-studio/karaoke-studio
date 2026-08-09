"""歌词检索页与宿主之间的边界。

同 ``test_alignment_page_boundary``：mixin 还混在 ``KrokHelperQtApp`` 上，
``self`` 是同一个对象，所以要有一份写死的清单挡住"顺手再摸一个宿主成员"。
清单只该变短。
"""

from __future__ import annotations

import ast
import pathlib

PAGE = pathlib.Path(__file__).resolve().parents[1] / "krok_helper" / "lyrics_search" / "page.py"

#: 页面调用的宿主服务 —— 变成独立对象时，这些要么跟着搬，要么进宿主接口。
HOST_SERVICES = {
    "settings",  # 配置读写
    "_track_background_task",  # 搜索/抓取跑在后台线程
    "_install_single_click_combo_behavior",  # 下拉框单击即选
    "_import_current_lyrics_to_timing",  # 把歌词交给第 4 步打轴
    "width",  # QWidget 自己的
}

#: 由外壳 ``__init__`` 建、页面读写的任务槽位 —— 应当跟着页面一起搬，
#: 现在留在外壳只是因为它们和其他模块的任务槽位并排声明。
HOST_OWNED_STATE = {
    "lyrics_search_task",
    "lyrics_fetch_task",
    "lyrics_search_service",
}


def _foreign_members() -> set[str]:
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LyricsSearchPageMixin")
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
        "歌词检索页多摸了宿主成员：" + "、".join(sorted(unexpected)) + "。"
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
