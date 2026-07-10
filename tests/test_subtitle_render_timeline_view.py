from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend.timeline_view import (  # noqa: E402
    TrackTimelineView,
    build_lanes,
)
from krok_helper.subtitle_render.models import (  # noqa: E402
    TimingChar,
    TimingLine,
    TimingTrack,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_track() -> TimingTrack:
    line1 = TimingLine(
        chars=[
            TimingChar("あ", 1000),
            TimingChar("い", 1500, pause_release_ms=1800),
            TimingChar("う", 2200),
        ],
        end_ms=2600,
        singer_label="主唱",
        singer_id=0,
    )
    blank = TimingLine(is_blank=True)
    line2 = TimingLine(
        chars=[TimingChar("か", 4000)],
        end_ms=None,
        singer_label="和声",
        singer_id=1,
    )
    return TimingTrack(lines=[line1, blank, line2])


def test_build_lanes_char_cells_and_blanks() -> None:
    lanes = build_lanes([("主字幕", _make_track())])

    assert len(lanes) == 1
    lane = lanes[0]
    assert lane.name == "主字幕"
    # 空行不产生块
    assert len(lane.blocks) == 2

    block = lane.blocks[0]
    assert block.line_index == 0
    assert block.text == "あいう"
    assert block.singer_id == 0
    assert (block.start_ms, block.end_ms) == (1000, 2600)
    # 字符单元：あ 到下一字符起点；い 被 pause_release 截断；う 到行末
    assert [(c.start_ms, c.end_ms) for c in block.cells] == [
        (1000, 1500),
        (1500, 1800),
        (2200, 2600),
    ]

    # 缺 end_ms 的行用兜底时长
    tail = lane.blocks[1]
    assert (tail.start_ms, tail.end_ms) == (4000, 5000)
    assert tail.singer_id == 1


def test_build_lanes_multiple_sources() -> None:
    track = _make_track()
    lanes = build_lanes([("主字幕", track), ("コーラス", track)])
    assert [lane.name for lane in lanes] == ["主字幕", "コーラス"]


def _mouse_event(widget, kind, x: float, y: float) -> QMouseEvent:
    return QMouseEvent(
        kind,
        QPointF(x, y),
        widget.mapToGlobal(QPoint(int(x), int(y))).toPointF(),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _click(widget: TrackTimelineView, x: float, y: float) -> None:
    widget.mousePressEvent(_mouse_event(widget, QMouseEvent.Type.MouseButtonPress, x, y))


def _move(widget: TrackTimelineView, x: float, y: float) -> None:
    widget.mouseMoveEvent(_mouse_event(widget, QMouseEvent.Type.MouseMove, x, y))


def _release(widget: TrackTimelineView, x: float, y: float) -> None:
    widget.mouseReleaseEvent(
        _mouse_event(widget, QMouseEvent.Type.MouseButtonRelease, x, y)
    )


def test_click_block_snaps_to_char_start(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 160)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(10_000)

    received: list[int] = []
    widget.seekRequested.connect(received.append)

    lane, rect = widget._lane_geometry()[0]
    assert lane.blocks
    # 点进第二个字符（1500–1800ms 区间中点）
    x = widget._x_for_ms(1650)
    _click(widget, x, rect.center().y())
    assert received == [1500]


def test_click_empty_area_seeks_to_time(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 160)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(10_000)

    received: list[int] = []
    widget.seekRequested.connect(received.append)

    _lane, rect = widget._lane_geometry()[0]
    # 8000ms 处没有任何块 → 直接跳到该时刻
    x = widget._x_for_ms(8000)
    _click(widget, x, rect.center().y())
    assert len(received) == 1
    assert abs(received[0] - 8000) <= 20  # 像素→毫秒往返允许量化误差


def test_click_in_label_gutter_is_ignored(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 160)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(10_000)

    received: list[int] = []
    widget.seekRequested.connect(received.append)

    _lane, rect = widget._lane_geometry()[0]
    _click(widget, 10, rect.center().y())
    assert received == []


def test_paint_smoke_empty_and_populated(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 160)
    widget.grab()  # 空态绘制

    widget.set_tracks([("主字幕", _make_track()), ("コーラス", _make_track())])
    widget.set_duration(10_000)
    widget.set_time(1650)
    widget.grab()  # 双轨 + 播放头绘制

    widget.set_tracks([])
    widget.grab()  # 回空态


def test_set_time_without_tracks_is_safe(qapp) -> None:
    widget = TrackTimelineView()
    widget.set_time(5000)
    widget.set_duration(0)
    widget.grab()


def test_default_viewport_is_15_seconds(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 160)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(60_000)
    assert widget.view_span_ms == 15_000
    assert widget.view_start_ms == 0


def test_short_song_viewport_clamps_to_duration(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 160)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(10_000)
    assert widget.view_span_ms == 10_000


def test_zoom_about_anchor_keeps_anchor_fixed(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 160)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(60_000)

    anchor = 7500.0
    x_before = widget._x_for_ms(anchor)
    widget._zoom_about(anchor, 0.5)
    assert widget.view_span_ms == 7500
    assert abs(widget._x_for_ms(anchor) - x_before) < 0.5

    # 缩放下限 2 秒
    widget._zoom_about(anchor, 0.0001)
    assert widget.view_span_ms == 2000


def test_pan_clamps_to_timeline(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 160)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(60_000)

    widget._pan_by(-5000)
    assert widget.view_start_ms == 0
    widget._pan_by(999_999)
    assert widget.view_start_ms == 45_000  # 60s - 15s 视口


def test_playhead_follow_pages_viewport(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 160)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(60_000)

    widget.set_time(5_000)  # 视口内不动
    assert widget.view_start_ms == 0
    widget.set_time(20_000)  # 越界翻页：播放头落在新页 10% 处
    assert widget.view_start_ms == pytest.approx(18_500)


def test_ruler_click_seeks(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 160)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(60_000)

    received: list[int] = []
    widget.seekRequested.connect(received.append)

    x = widget._x_for_ms(6000)
    _click(widget, x, widget._ruler_rect().center().y())
    assert len(received) == 1
    assert abs(received[0] - 6000) <= 30


def test_zoombar_right_handle_drag_zooms(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 160)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(60_000)

    bar_y = widget._zoombar_rect().center().y()
    _x0, x1 = widget._zoombar_thumb_span()
    _click(widget, x1, bar_y)  # 按住右把手
    assert widget._drag == ("zoom_right",)

    target_x = widget._zoombar_x_for_ms(10_000)
    _move(widget, target_x, bar_y)
    assert widget.view_span_ms == pytest.approx(10_000, abs=200)

    _release(widget, target_x, bar_y)
    assert widget._drag is None


def test_zoombar_thumb_drag_pans(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 160)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(60_000)

    bar_y = widget._zoombar_rect().center().y()
    x0, x1 = widget._zoombar_thumb_span()
    mid = (x0 + x1) / 2
    _click(widget, mid, bar_y)
    assert widget._drag is not None and widget._drag[0] == "pan"

    _move(widget, mid + (x1 - x0), bar_y)  # 向右拖一个视口宽
    assert widget.view_start_ms == pytest.approx(15_000, abs=500)
    _release(widget, mid, bar_y)


def test_click_block_selects_and_click_empty_deselects(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(10_000)
    widget.set_display_windows([{0: (500, 3200), 2: (3500, 6000)}])

    _lane, rect = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(1650), rect.center().y())
    assert widget._selected == (0, 0)

    handles = widget._handle_rects()
    assert handles is not None
    left_rect, right_rect, lane_index, block = handles
    assert lane_index == 0
    # 左框 = [上屏 500ms, 首字符 1000ms]；右框 = [行末 2600ms, 消失 3200ms]
    assert left_rect.left() == pytest.approx(widget._x_for_ms(500), abs=1)
    assert left_rect.right() == pytest.approx(widget._x_for_ms(1000), abs=1)
    assert right_rect.left() == pytest.approx(widget._x_for_ms(2600), abs=1)
    assert right_rect.right() == pytest.approx(widget._x_for_ms(3200), abs=1)

    _click(widget, widget._x_for_ms(8000), rect.center().y())  # 空白处
    assert widget._selected is None
    assert widget._handle_rects() is None


def test_drag_left_handle_writes_show_override(qapp) -> None:
    track = _make_track()
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", track)])
    widget.set_duration(10_000)
    widget.set_display_windows([{0: (800, 3000)}])

    changed: list[int] = []
    widget.displayWindowChanged.connect(changed.append)

    _lane, rect = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(1650), rect.center().y())  # 选中第一句

    left_rect, _right, _lane_idx, _block = widget._handle_rects()
    _click(widget, left_rect.center().x(), left_rect.center().y())
    assert widget._drag is not None and widget._drag[0] == "lead"

    _move(widget, widget._x_for_ms(300), left_rect.center().y())
    assert track.lines[0].display_start_override_ms == pytest.approx(300, abs=30)
    assert widget._windows[0][0][0] == track.lines[0].display_start_override_ms
    assert changed == []  # 拖动中不通知

    _release(widget, widget._x_for_ms(300), left_rect.center().y())
    assert changed == [0]
    assert widget._drag is None


def test_drag_right_handle_clamps_to_sing_end(qapp) -> None:
    track = _make_track()
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", track)])
    widget.set_duration(10_000)
    widget.set_display_windows([{0: (800, 3000)}])

    _lane, rect = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(1650), rect.center().y())

    _left, right_rect, _lane_idx, _block = widget._handle_rects()
    _click(widget, right_rect.center().x(), right_rect.center().y())
    assert widget._drag is not None and widget._drag[0] == "tail"

    _move(widget, widget._x_for_ms(5000), right_rect.center().y())
    assert track.lines[0].display_end_override_ms == pytest.approx(5000, abs=30)

    # 往回拖不早于走字结束（2600ms）
    _move(widget, widget._x_for_ms(1200), right_rect.center().y())
    assert track.lines[0].display_end_override_ms == 2600
    _release(widget, widget._x_for_ms(1200), right_rect.center().y())


def test_selection_paint_smoke(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(10_000)
    widget.set_display_windows([{0: (500, 3200)}])
    _lane, rect = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(1650), rect.center().y())
    widget.grab()  # 选中态 + 把手绘制
