"""歌词检索页 / Hi-Res 混流页：从 UI 点下去，通路要通。

和 :mod:`tests.test_alignment_click_paths` 同一个理由 —— 这两页也是"页面对象化"
改造过的，页面里每一句 ``self._host.xxx(...)`` 都是手写的转调，写错了方法级
单测照样全绿，只有真点下按钮才会炸。对齐页就是这么漏出去的。

对齐页那份单独成文，因为它要喂素材和波形才点得动；这两页素材面简单得多。
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from krok_helper.background import BackgroundTask
from krok_helper.hires import page as hires_page
from krok_helper.hires.page import HiResPage
from krok_helper.lyrics_search import page as lyrics_page
from krok_helper.lyrics_search.page import LyricsSearchPage
from tests.page_fakes import hires_host, lyrics_host
from tests.ui_sweep import block_modals, clickable, crash_collector, fake_popen


@pytest.fixture(autouse=True)
def _no_real_work(monkeypatch):
    """后台任务只建不跑、子进程不起、模态不转 —— 只留"点击 → 槽函数"这一段。"""
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(BackgroundTask, "start", lambda self, *a, **k: None)
    monkeypatch.setattr(BackgroundTask, "isRunning", lambda self: False)
    block_modals(monkeypatch)
    for module in (hires_page, lyrics_page):
        if hasattr(module, "subprocess"):
            monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
        if hasattr(module, "open_in_explorer"):
            monkeypatch.setattr(module, "open_in_explorer", lambda *a, **k: None)
        for name in ("show_fluent_error", "show_fluent_info", "show_fluent_warning"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, lambda *a, **k: None)


@pytest.fixture
def hires():
    calls: list = []
    page = HiResPage(host=hires_host(calls))
    yield page, calls
    page.deleteLater()


@pytest.fixture
def lyrics():
    calls: list = []
    page = LyricsSearchPage(host=lyrics_host(calls))
    yield page, calls
    page.deleteLater()


def _sweep(page) -> list[str]:
    clicked: list[str] = []
    with crash_collector() as crashes:
        for name, widget in clickable(page):
            if not widget.isEnabled():
                continue
            widget.click()
            assert not crashes, f"点「{name}」炸了：{crashes[0]}"
            clicked.append(name)
    return clicked


def test_clicking_through_the_hires_page(hires) -> None:
    page, _ = hires

    clicked = _sweep(page)

    # 空页面（没导入素材）状态下实测能点到 10 个；钉个下限免得哪天悄悄退化成空转。
    assert len(clicked) >= 8, f"只点到 {len(clicked)} 个按钮，扫描八成漏了：{clicked}"


def test_clicking_through_the_lyrics_page(lyrics) -> None:
    page, _ = lyrics

    clicked = _sweep(page)

    assert len(clicked) >= 12, f"只点到 {len(clicked)} 个按钮，扫描八成漏了：{clicked}"
    assert "lyrics_search_button" in clicked


def test_searching_lyrics_creates_a_task(lyrics) -> None:
    """通路的终点：填了关键词点搜索，要真的建出后台任务。"""
    page, calls = lyrics

    page.lyrics_keyword_edit.setText("GO GHOST")
    with crash_collector() as crashes:
        page.lyrics_search_button.click()

    assert not crashes, f"点「搜索」炸了：{crashes[0]}"
    assert any(kind == "task" for kind, _ in calls), "搜索没建出后台任务"
