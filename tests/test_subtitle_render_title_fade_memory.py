"""标题的淡入淡出时长要记在应用级设置里。

改一次就该一直沿用 —— 每开一个新项目重设一遍 300 → 250 很烦。和「标题是否开启」
「标题布局」是同一类"用户习惯"，所以存在同一处（``new_project_defaults``）。

尾段那两项是 ``Optional``：``None`` 表示"跟随开头"，是合法取值，不能在存取过程里
被当成缺省丢掉。
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend.main_window import (  # noqa: E402
    SubtitleRenderWindow,
)
from krok_helper.subtitle_render.models import TitleOverlay  # noqa: E402


class _Recorder:
    """一份留在内存里的 subtitle_render 设置命名空间。"""

    def __init__(self) -> None:
        self.data: dict = {}

    def load(self) -> dict:
        return dict(self.data)

    def save(self, data: dict) -> None:
        self.data = dict(data)


@pytest.fixture
def settings() -> _Recorder:
    return _Recorder()


@pytest.fixture
def make_window(settings):
    app = QApplication.instance() or QApplication([])
    built: list[SubtitleRenderWindow] = []

    def factory() -> SubtitleRenderWindow:
        widget = SubtitleRenderWindow.for_embedding(settings_provider=settings)
        built.append(widget)
        return widget

    yield factory
    for widget in built:
        widget.close()
        widget.deleteLater()
    app.processEvents()


def _edit_title(window: SubtitleRenderWindow, **changes) -> None:
    title = window._style.title_overlay or TitleOverlay()
    window._property_panel.set_style(
        replace(window._style, title_overlay=replace(title, enabled=True, **changes)),
        emit=True,
    )
    QApplication.instance().processEvents()


def test_editing_the_fade_updates_the_app_default(make_window) -> None:
    window = make_window()

    _edit_title(window, fade_in_ms=250, fade_out_ms=180)

    app_title = window._app_default_style.title_overlay
    assert app_title.fade_in_ms == 250
    assert app_title.fade_out_ms == 180


def test_the_fade_is_written_next_to_the_other_title_habits(make_window, settings) -> None:
    window = make_window()
    _edit_title(window, fade_in_ms=250)

    window._save_persisted_state()

    defaults = settings.data["new_project_defaults"]
    assert "title_enabled" in defaults and "title_layout_name" in defaults
    assert defaults["title_fades"]["fade_in_ms"] == 250


def test_a_new_instance_starts_from_the_remembered_fade(make_window) -> None:
    """真正要的效果：下次打开还是 250。"""
    first = make_window()
    _edit_title(first, fade_in_ms=250, fade_out_ms=180)
    first._save_persisted_state()

    second = make_window()

    assert second._app_default_style.title_overlay.fade_in_ms == 250
    assert second._style.title_overlay.fade_in_ms == 250
    assert second._style.title_overlay.fade_out_ms == 180


def test_the_tail_none_means_follow_the_head_and_round_trips(make_window, settings) -> None:
    """尾段的 ``None`` 是"跟随开头"，不是"没设置"。"""
    window = make_window()
    _edit_title(window, fade_in_ms=250)
    window._save_persisted_state()

    assert settings.data["new_project_defaults"]["title_fades"]["tail_fade_in_ms"] is None

    reopened = make_window()

    assert reopened._app_default_style.title_overlay.tail_fade_in_ms is None


def test_an_explicit_tail_fade_is_remembered_too(make_window) -> None:
    first = make_window()
    _edit_title(first, tail_fade_in_ms=120, tail_fade_out_ms=90)
    first._save_persisted_state()

    second = make_window()

    title = second._app_default_style.title_overlay
    assert title.tail_fade_in_ms == 120
    assert title.tail_fade_out_ms == 90


def test_the_title_text_stays_per_song(make_window) -> None:
    """标题文字是逐曲的，绝不能跟着记进应用级默认。"""
    first = make_window()
    _edit_title(first, text_template="某首歌 / 某歌手", fade_in_ms=250)
    first._save_persisted_state()

    second = make_window()

    assert second._app_default_style.title_overlay.fade_in_ms == 250
    assert second._app_default_style.title_overlay.text_template == TitleOverlay().text_template


def test_garbage_in_the_settings_falls_back_to_the_defaults(make_window, settings) -> None:
    """手改坏了设置文件也不该把标题时长搞成负数或字符串。"""
    settings.data = {
        "new_project_defaults": {
            "title_fades": {"fade_in_ms": "很快", "fade_out_ms": -50}
        }
    }

    window = make_window()

    title = window._app_default_style.title_overlay
    assert title.fade_in_ms == TitleOverlay().fade_in_ms
    assert title.fade_out_ms == 0
