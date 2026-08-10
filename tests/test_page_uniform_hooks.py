"""外壳跟各页打交道的三条统一钩子。

这三件事外壳都要**逐页**做一遍：主题切换后重绘、写盘前收设置、关窗前查后台任务。
以前是按方法名 ``getattr(self, "_refresh_alignment_material_inputs", None)`` 去自己
身上找 —— 页面搬走之后那串名字全成了 ``None``，配着 ``if fn is None: continue``
一声不响地空转，换主题后对齐页的卡片和导出面板就一直停在旧配色，谁都不知道。

所以钉两头：每个页面类都得有这三条；外壳的主题刷新真的会挨个调到。
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from krok_helper.alignment.page import AlignmentPage
from krok_helper.hires.page import HiResPage
from krok_helper.lyrics_search.page import LyricsSearchPage

PAGES = [AlignmentPage, LyricsSearchPage, HiResPage]
HOOKS = ["rerender_after_theme_change", "collect_settings", "running_tasks"]


@pytest.mark.parametrize("page_class", PAGES, ids=lambda c: c.__name__)
@pytest.mark.parametrize("hook", HOOKS)
def test_every_page_answers_the_shell(page_class, hook: str) -> None:
    assert callable(getattr(page_class, hook, None)), f"{page_class.__name__} 少了 {hook}"


def test_the_theme_refresh_reaches_every_page(monkeypatch) -> None:
    """外壳换主题时，清单上的每一页都得被叫到。"""
    from krok_helper import gui_qt

    QApplication.instance() or QApplication([])

    class _Page:
        def __init__(self, name: str) -> None:
            self.name = name
            self.rerendered = False

        def rerender_after_theme_change(self) -> None:
            self.rerendered = True

    pages = [_Page("align"), _Page("lyrics"), _Page("hires")]
    shell = gui_qt.KrokHelperQtApp.__new__(gui_qt.KrokHelperQtApp)
    monkeypatch.setattr(gui_qt.KrokHelperQtApp, "_workflow_pages", lambda _self: pages)
    monkeypatch.setattr(gui_qt.KrokHelperQtApp, "_apply_styles", lambda _self: None)
    monkeypatch.setattr(gui_qt, "setThemeColor", lambda *a, **k: None)

    gui_qt.KrokHelperQtApp._apply_theme_refresh(shell)

    assert all(page.rerendered for page in pages), (
        "换主题没叫到：" + "、".join(p.name for p in pages if not p.rerendered)
    )


def test_one_page_blowing_up_does_not_stop_the_others() -> None:
    """一页重绘炸了，剩下的还得刷 —— 主题是全局的，不能刷一半。"""
    from krok_helper import gui_qt

    QApplication.instance() or QApplication([])

    class _Boom:
        def rerender_after_theme_change(self) -> None:
            raise RuntimeError("炸给你看")

    class _Fine:
        def __init__(self) -> None:
            self.rerendered = False

        def rerender_after_theme_change(self) -> None:
            self.rerendered = True

    fine = _Fine()
    shell = gui_qt.KrokHelperQtApp.__new__(gui_qt.KrokHelperQtApp)
    original_pages = gui_qt.KrokHelperQtApp._workflow_pages
    original_styles = gui_qt.KrokHelperQtApp._apply_styles
    original_color = gui_qt.setThemeColor
    gui_qt.KrokHelperQtApp._workflow_pages = lambda _self: [_Boom(), fine]
    gui_qt.KrokHelperQtApp._apply_styles = lambda _self: None
    gui_qt.setThemeColor = lambda *a, **k: None
    try:
        gui_qt.KrokHelperQtApp._apply_theme_refresh(shell)
    finally:
        gui_qt.KrokHelperQtApp._workflow_pages = original_pages
        gui_qt.KrokHelperQtApp._apply_styles = original_styles
        gui_qt.setThemeColor = original_color

    assert fine.rerendered
