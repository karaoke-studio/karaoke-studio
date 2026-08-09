"""歌词检索页与外壳之间的边界。

这一页**已经是独立对象**：搜索服务、两个后台任务、结果与选中态都挂在自己身上，
能碰到的外部东西只有构造时注入的 ``_host``。查两件事：静态上不绕过 ``_host``，
动态上能脱离主窗口构造。
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from krok_helper.lyrics_search.page import LyricsSearchHost, LyricsSearchPage
from krok_helper.settings import AppSettings

PAGE = pathlib.Path(__file__).resolve().parents[1] / "krok_helper" / "lyrics_search" / "page.py"


def _fake_host(calls: list) -> SimpleNamespace:
    return SimpleNamespace(
        settings=AppSettings(),
        track_background_task=lambda task: task,
        install_single_click_combo_behavior=lambda combo: calls.append("combo"),
        import_current_lyrics_to_timing=lambda: calls.append("import"),
    )


@pytest.fixture
def page():
    QApplication.instance() or QApplication([])
    calls: list = []
    widget = LyricsSearchPage(host=_fake_host(calls))
    yield widget, calls
    widget.deleteLater()


def test_the_page_reaches_outside_only_through_the_host() -> None:
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LyricsSearchPage")
    own = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assigned: set[str] = set()
    read: set[str] = set()
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            (assigned if isinstance(node.ctx, ast.Store) else read).add(node.attr)

    inherited = {name for name in read if hasattr(LyricsSearchPage.__mro__[1], name)}
    outside = read - own - assigned - inherited

    assert not outside, "页面绕过 _host 摸了外部成员：" + "、".join(sorted(outside))


def test_a_fake_host_satisfies_the_contract() -> None:
    assert isinstance(_fake_host([]), LyricsSearchHost)


def test_the_page_builds_without_the_main_window(page) -> None:
    widget, calls = page

    assert widget.lyrics_keyword_edit is not None
    assert widget.lyrics_results_table is not None
    assert widget.running_tasks() == []
    assert calls.count("combo") == 3, "三个下拉框都该装上单击即选"


def test_preferences_round_trip(page) -> None:
    widget, _ = page

    widget.persist_preferences()
    widget.restore_preferences()

    assert widget._current_lyrics_source_ids()
    assert widget._current_lyrics_preview_mode()
    assert widget._current_lyrics_language()


def test_restoring_preferences_suppresses_write_back(page) -> None:
    """灌设置期间控件会变，那时回写会把偏好覆盖成中间态。

    页面自己管这面旗（以前是问外壳要 ``_loading_settings_into_ui``）。
    """
    widget, _ = page
    seen: list[bool] = []
    original = widget._persist_lyrics_preferences
    widget._persist_lyrics_preferences = lambda: seen.append(widget._restoring_preferences)

    try:
        widget.restore_preferences()
    finally:
        widget._persist_lyrics_preferences = original

    assert all(seen), "恢复过程中触发的回写都该看到旗是举起来的"
    assert not widget._restoring_preferences, "恢复结束后旗要放下"


def test_the_page_does_not_import_the_shell() -> None:
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}

    assert not any(m == "krok_helper.gui_qt" or m.startswith("krok_helper.gui_qt.") for m in imported)
