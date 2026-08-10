"""外壳碰到的每一个自有成员，类上或 ``__init__`` 里都得真有。

页面对象化留下的最阴险的一类残骸：**调用点没跟着搬**。外壳里还写着
``self._stop_alignment_preview()`` / ``self._start_aligned_export()``，方法早就
跟着对齐页走了。有的外面裹着 ``if getattr(self, "align_preview_process", None)``
（条件恒为假，切页后预览一直响）、有的裹着 ``try/except: pass``（强退更新时
静默失败）、有的干脆裸奔（在对齐页按 Ctrl+S 直接 AttributeError）。

页面那侧由 ``tests/test_page_state_initialized.py`` 盯着，这是外壳那侧的同一条。
明写了 ``getattr(self, "x", ...)`` / ``hasattr`` 兜底的放过 —— 那是作者声明过
"这东西可能还没有"（测试里塞假外壳时用得上）。
"""

from __future__ import annotations

import ast
import pathlib
import re

from krok_helper.gui_qt import KrokHelperQtApp

SHELL = pathlib.Path(__file__).resolve().parents[1] / "krok_helper" / "gui_qt.py"


def test_the_shell_touches_nothing_that_moved_away() -> None:
    source = SHELL.read_text(encoding="utf-8")
    cls = next(
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.ClassDef) and n.name == "KrokHelperQtApp"
    )

    assigned: set[str] = set()
    read: set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            # 方法体里嵌的小控件类，它们的 self 不是外壳。
            if isinstance(child, ast.ClassDef):
                continue
            if (
                isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == "self"
            ):
                (assigned if isinstance(child.ctx, ast.Store) else read).add(child.attr)
            visit(child)

    visit(cls)
    guarded = set(re.findall(r'(?:getattr|hasattr)\(\s*self\s*,\s*["\'](\w+)["\']', source))
    missing = sorted(n for n in read - assigned - guarded if not hasattr(KrokHelperQtApp, n))

    assert not missing, (
        "外壳会碰这些成员，但类上没有、自己也没赋过值（多半是搬走之后调用点没跟着改）："
        + "、".join(missing)
    )
