"""标题「显示时段」几个时刻字段的取值上限。

原来写死 600000 ms（10 分钟），那个数没有来源。N3 里 ``TitleShowTime`` 的
HeadOffset / HeadEnd / TailOffset 都直接落在时间标签这条轴上，上限是
``Nkm3Constants.TIME_TAG_TIME_MAX = 5999990``（``[99:59:99]``，约 100 分钟），
没有更窄的限制 —— 我们跟它对齐。

淡入淡出不在这次范围内：那是动画时长，不是时间轴上的时刻。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.engine.show_time import MAX_SHOW_TIME_MS  # noqa: E402
from krok_helper.subtitle_render.frontend import property_panel as pp  # noqa: E402
from krok_helper.subtitle_render.frontend.property_panel import (  # noqa: E402
    TITLE_TIME_MAX_MS,
    PropertyPanel,
)
from krok_helper.subtitle_render.models import Style, TitleOverlay  # noqa: E402


@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication([])
    widget = PropertyPanel()
    widget.set_style(Style(title_overlay=TitleOverlay(enabled=True)))
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_the_cap_matches_the_n3_time_tag_limit() -> None:
    """和渲染层用的哨兵是同一个数，别各写各的。"""
    assert TITLE_TIME_MAX_MS == MAX_SHOW_TIME_MS == 5_999_990


@pytest.mark.parametrize(
    "name",
    [
        "_title_duration_edit",
        "_title_head_edit",
        "_title_tail_edit",
        "_title_tail_duration_edit",
    ],
)
def test_every_title_time_field_accepts_the_full_range(panel, name: str) -> None:
    edit = getattr(panel, name)

    assert edit._maximum == TITLE_TIME_MAX_MS
    assert edit.maximum() == TITLE_TIME_MAX_MS
    # 上限值本身要能规范化显示并解析回来。
    assert edit.submit_text(pp.format_timecode_ms(TITLE_TIME_MAX_MS))
    assert edit.value() == TITLE_TIME_MAX_MS


def test_the_duration_reaches_the_model_unclamped(panel) -> None:
    panel._title_duration_edit.setValue(TITLE_TIME_MAX_MS)

    assert panel._title_duration_edit.value() == TITLE_TIME_MAX_MS
    assert panel.subtitle_style.title_overlay.duration_ms == TITLE_TIME_MAX_MS


def test_a_value_past_the_old_cap_survives(panel) -> None:
    """旧上限是 600000；这一档以前根本填不进去。"""
    panel._title_duration_edit.setValue(1_200_000)

    assert panel.subtitle_style.title_overlay.duration_ms == 1_200_000


@pytest.mark.parametrize("name", ["_title_fade_in_edit", "_title_fade_out_edit"])
def test_the_fades_keep_their_own_range(panel, name: str) -> None:
    """淡入淡出是动画时长，不跟着放宽。"""
    assert getattr(panel, name)._maximum == 10_000
