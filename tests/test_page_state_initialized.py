"""页面读到的每一个自有属性，构造完就该在。

搬迁时最容易漏的不是方法而是**状态**：``_suppress_preview_seek_restart`` 的初始化
留在了外壳的 ``__init__`` 里，读写它的代码全在对齐页 —— 而它在页面里也有赋值
（只是在另一条分支上），所以边界测试算下来"既读又写"，一路绿；直到自动对齐真的
跑完、走到那条先读后写的路径，才 AttributeError。

这里换个问法：把页面照常造出来，然后逐个 ``hasattr``。没有"哪条分支先跑"的余地。

``getattr(self, "x", None)`` / ``hasattr(self, "x")`` 这类**明写了兜底**的不算 ——
那是作者声明过"这东西可能还没有"，不是漏。
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
from PyQt6.QtWidgets import QApplication

from krok_helper.alignment.page import AlignmentPage
from krok_helper.hires.page import HiResPage
from krok_helper.lyrics_search.page import LyricsSearchPage
from tests.page_fakes import alignment_host, hires_host, lyrics_host

ROOT = pathlib.Path(__file__).resolve().parents[1] / "krok_helper"

CASES = [
    (AlignmentPage, ROOT / "alignment" / "page.py", alignment_host),
    (LyricsSearchPage, ROOT / "lyrics_search" / "page.py", lyrics_host),
    (HiResPage, ROOT / "hires" / "page.py", hires_host),
]


def _attributes_read(source: str, class_name: str) -> set[str]:
    tree = ast.parse(source)
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == class_name)

    own = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    read: set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            # 方法体里还嵌着小控件类（AlignmentExportProxyButton 之类），它们的
            # ``self`` 是自己不是页面 —— 一起算进来会误报。
            if isinstance(child, ast.ClassDef):
                continue
            if (
                isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == "self"
                and isinstance(child.ctx, ast.Load)
            ):
                read.add(child.attr)
            visit(child)

    visit(cls)

    # 作者明写了兜底的名字放过：getattr(self, "x", ...) / hasattr(self, "x")
    guarded = set(re.findall(r'(?:getattr|hasattr)\(\s*self\s*,\s*["\'](\w+)["\']', source))
    return read - own - guarded


@pytest.mark.parametrize("cls,path,host_factory", CASES, ids=lambda v: getattr(v, "__name__", None) or str(v)[-30:])
def test_every_attribute_the_page_reads_exists_after_construction(cls, path, host_factory) -> None:
    QApplication.instance() or QApplication([])
    page = cls(host=host_factory())
    try:
        missing = sorted(
            name
            for name in _attributes_read(path.read_text(encoding="utf-8"), cls.__name__)
            if not hasattr(page, name)
        )
    finally:
        page.deleteLater()

    assert not missing, (
        f"{cls.__name__} 会读这些属性，但构造完并不存在（八成是初始化落在外壳没跟着搬）："
        + "、".join(missing)
    )


#: 外壳里赋了值、自己不读，但确实有用的：Qt 侧要有人持着引用，不然被 GC 掉。
KEEP_ALIVE = {"_update_launch_worker", "_update_progress_win"}


def test_the_shell_keeps_no_leftover_page_state() -> None:
    """反向的同一件事：外壳不该还留着只有页面在用的状态。

    ``_suppress_preview_seek_restart`` 就是从这一侧漏的 —— 初始化留在外壳，
    读写都在对齐页。另有五个 ``_hires_*`` 是同类遗留，只是恰好没被读到过。
    """
    shell = ROOT / "gui_qt.py"
    source = shell.read_text(encoding="utf-8")
    tree = ast.parse(source)

    assigned: set[str] = set()
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            (assigned if isinstance(node.ctx, ast.Store) else read).add(node.attr)

    page_sources = {path: path.read_text(encoding="utf-8") for _, path, _ in CASES}
    leftovers: list[str] = []
    for name in sorted(assigned - read - KEEP_ALIVE):
        if name.startswith("__") or re.search(rf'["\']{re.escape(name)}["\']', source):
            continue  # 以字符串形式被引用（getattr / setattr）也算外壳自己在用
        owners = [p.parent.name for p, text in page_sources.items() if re.search(rf"self\.{re.escape(name)}\b", text)]
        if owners:
            leftovers.append(f"{name}（{'、'.join(owners)} 在用）")

    assert not leftovers, "外壳还留着页面的状态，初始化和读写分了家：" + "；".join(leftovers)
