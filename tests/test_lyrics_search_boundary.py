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

from krok_helper.lyrics import LyricsSearchBatch, LyricsSearchCandidate
from krok_helper.lyrics_search import page as lyrics_page
from krok_helper.lyrics_search.page import (
    LYRICS_SOURCE_OPTIONS,
    LyricsSearchHost,
    LyricsSearchPage,
)
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


def _loaded_candidate() -> LyricsSearchCandidate:
    return LyricsSearchCandidate(
        provider_id="utaten",
        provider_name="UtaTen",
        track_id="1",
        title="結び",
        artist="Ayumu Imazu",
        album="",
        duration_seconds=None,
        plain_lyrics="[tool:utaten-ruby]\n{結び||むすび}",
        lyrics_loaded=True,
    )


def test_search_failure_reports_through_info_bar(page, monkeypatch) -> None:
    widget, _ = page
    bars: list[dict] = []
    monkeypatch.setattr(lyrics_page.InfoBar, "warning", lambda **kwargs: bars.append(kwargs))

    widget._finish_lyrics_search_failure("网络错误：请求超时")

    assert bars, "失败反馈应当走 InfoBar"
    assert "网络错误：请求超时" in bars[0]["content"]
    assert "失败" in widget.lyrics_status_label.text()


def test_partial_search_errors_keep_results_without_info_bar(page, monkeypatch) -> None:
    widget, _ = page
    bars: list[dict] = []
    monkeypatch.setattr(lyrics_page.InfoBar, "warning", lambda **kwargs: bars.append(kwargs))
    candidate = _loaded_candidate()
    batch = LyricsSearchBatch(
        results=[candidate],
        overflow_results=[],
        next_provider_pages={},
        has_more=False,
        errors=("网易云音乐：HTTP 503",),
    )

    widget._finish_lyrics_search_success((False, batch))

    assert widget.lyrics_search_results == [candidate], "部分来源失败不能清空其它来源的结果"
    assert "已加载 1 条候选结果" in widget.lyrics_status_label.text()
    assert not bars, "已有结果时不该再弹来源错误"


def test_failure_with_existing_results_does_not_report_bar(page, monkeypatch) -> None:
    widget, _ = page
    bars: list[dict] = []
    monkeypatch.setattr(lyrics_page.InfoBar, "warning", lambda **kwargs: bars.append(kwargs))
    candidate = _loaded_candidate()
    widget.lyrics_search_results = [candidate]
    widget.lyrics_selected_candidate = candidate

    widget._finish_lyrics_search_failure("网络错误：请求超时")

    assert widget.lyrics_search_results == [candidate]
    assert "已保留 1 条已有结果" in widget.lyrics_status_label.text()
    assert not bars, "已有结果时不该再弹错误"


def _select_lyrics_source(widget, label: str) -> None:
    index = next(
        i for i, (option_label, _ids) in enumerate(LYRICS_SOURCE_OPTIONS)
        if option_label == label
    )
    widget.lyrics_source_combo.setCurrentIndex(index)


def test_artist_field_is_visible_only_for_utaten(page) -> None:
    widget, _ = page

    assert widget.lyrics_artist_edit.isHidden(), "聚合模式不该出现歌手输入框"

    _select_lyrics_source(widget, "UtaTen")
    assert not widget.lyrics_artist_edit.isHidden()
    assert "完整歌名" in widget.lyrics_keyword_edit.placeholderText()

    _select_lyrics_source(widget, "聚合")
    assert widget.lyrics_artist_edit.isHidden()


def test_artist_field_has_same_size_as_keyword(page) -> None:
    widget, _ = page
    widget.resize(1200, 760)
    widget.show()
    QApplication.processEvents()
    _select_lyrics_source(widget, "UtaTen")
    QApplication.processEvents()

    assert widget.lyrics_artist_edit.size() == widget.lyrics_keyword_edit.size()
    assert {
        widget.lyrics_source_combo.height(),
        widget.lyrics_keyword_edit.height(),
        widget.lyrics_artist_edit.height(),
        widget.lyrics_search_button.height(),
    } == {42}
    assert widget.lyrics_artist_edit.geometry().top() == widget.lyrics_keyword_edit.geometry().top()
    assert widget.lyrics_artist_edit.geometry().left() > widget.lyrics_keyword_edit.geometry().left()
    # UtaTen 横向排列时来源下拉和搜索按钮仍与输入行顶对齐。
    assert widget.lyrics_source_combo.mapTo(widget, widget.lyrics_source_combo.rect().topLeft()).y() == (
        widget.lyrics_search_fields.mapTo(widget, widget.lyrics_search_fields.rect().topLeft()).y()
    )
    assert widget.lyrics_search_button.mapTo(widget, widget.lyrics_search_button.rect().topLeft()).y() == (
        widget.lyrics_search_fields.mapTo(widget, widget.lyrics_search_fields.rect().topLeft()).y()
    )
    widget.close()


def test_artist_text_is_ignored_outside_utaten(page) -> None:
    widget, _ = page
    widget.lyrics_artist_edit.setText("Ayumu Imazu")

    _select_lyrics_source(widget, "聚合")
    assert widget._current_lyrics_artist() == ""

    _select_lyrics_source(widget, "UtaTen")
    assert widget._current_lyrics_artist() == "Ayumu Imazu"


def test_empty_keyword_message_stays_original_for_non_utaten(page, monkeypatch) -> None:
    widget, _ = page
    bars: list[dict] = []
    monkeypatch.setattr(lyrics_page.InfoBar, "info", lambda **kwargs: bars.append(kwargs))

    _select_lyrics_source(widget, "聚合")
    widget._start_lyrics_search()
    assert "请输入搜索关键词。" in bars[-1]["content"]

    _select_lyrics_source(widget, "UtaTen")
    widget._start_lyrics_search()
    assert "请输入歌名或歌手。" in bars[-1]["content"]


def test_start_search_passes_artist_only_for_utaten(page, monkeypatch) -> None:
    widget, _ = page
    captured: dict = {}

    def fake_search_batch(keyword, **kwargs):
        captured["keyword"] = keyword
        captured.update(kwargs)
        return LyricsSearchBatch(
            results=[], overflow_results=[], next_provider_pages={}, has_more=False
        )

    monkeypatch.setattr(widget._search_service, "search_batch", fake_search_batch)
    started: list = []
    monkeypatch.setattr(
        lyrics_page.BackgroundTask,
        "start",
        lambda self, *args, **kwargs: started.append(self),
    )

    _select_lyrics_source(widget, "UtaTen")
    widget.lyrics_keyword_edit.clear()
    widget.lyrics_artist_edit.setText("Ayumu Imazu")
    widget._start_lyrics_search()
    started[-1]._runner(lambda _message: None)
    assert captured["keyword"] == ""
    assert captured["artist"] == "Ayumu Imazu"
    assert captured["provider_ids"] == ("utaten",)

    # 非 UtaTen：即使歌手框里还留着文字，也不能进入请求。
    _select_lyrics_source(widget, "聚合")
    widget.lyrics_keyword_edit.setText("結び")
    widget._start_lyrics_search()
    started[-1]._runner(lambda _message: None)
    assert captured["artist"] == ""
    assert captured["keyword"] == "結び"


def test_utaten_artist_only_search_does_not_require_keyword(page, monkeypatch) -> None:
    widget, _ = page
    started: list[tuple] = []
    monkeypatch.setattr(
        lyrics_page.BackgroundTask,
        "start",
        lambda self, *args, **kwargs: started.append((self, args, kwargs)),
    )
    _select_lyrics_source(widget, "UtaTen")
    widget.lyrics_keyword_edit.clear()
    widget.lyrics_artist_edit.setText("Ayumu Imazu")

    widget._start_lyrics_search()

    assert started, "只填歌手也应该能发起搜索"
    assert widget.lyrics_search_artist == "Ayumu Imazu"
    assert widget.lyrics_search_keyword == ""


def test_search_with_no_results_and_errors_is_consistent(page, monkeypatch) -> None:
    widget, _ = page
    bars: list[dict] = []
    monkeypatch.setattr(lyrics_page.InfoBar, "warning", lambda **kwargs: bars.append(kwargs))
    batch = LyricsSearchBatch(
        results=[],
        overflow_results=[],
        next_provider_pages={},
        has_more=False,
        errors=("UtaTen 搜索失败：UtaTen 返回空白搜索页面，请稍后重试。",),
    )

    widget._finish_lyrics_search_success((False, batch))

    assert widget.lyrics_search_results == []
    assert widget.lyrics_status_label.text() == "没有找到匹配的歌词结果。"
    assert widget.lyrics_results_table.rowCount() == 0
    assert bars and "UtaTen 返回空白搜索页面" in bars[0]["content"]
