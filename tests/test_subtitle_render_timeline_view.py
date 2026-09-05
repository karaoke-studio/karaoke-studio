from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend.editor.timeline_view import (  # noqa: E402
    TrackTimelineView,
    _format_precise_ms,
    _line_block_tooltip,
    build_lanes,
)
from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    LineAnimationOverride,
    RubyAnnotation,
    Style,
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


def test_timeline_uses_visual_interval_projection_boundary() -> None:
    source_path = Path(
        "krok_helper/subtitle_render/frontend/editor/timeline_view.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert (
        "krok_helper.subtitle_render.engine.render.adapters.timeline_projection"
        in imported_modules
    )
    assert "krok_helper.subtitle_render.engine.painter" not in imported_modules


def test_build_lanes_multiple_sources() -> None:
    track = _make_track()
    lanes = build_lanes([("主字幕", track), ("コーラス", track)])
    assert [lane.name for lane in lanes] == ["主字幕", "コーラス"]


def test_timeline_accepts_focus_for_host_shortcuts(qapp) -> None:
    widget = TrackTimelineView()
    assert widget.focusPolicy() == Qt.FocusPolicy.StrongFocus


def test_build_lanes_utopia_shared_ruby_uses_visual_cell_window(qapp) -> None:
    line = TimingLine(
        chars=[
            TimingChar(
                "\u4e8c",
                1000,
                source_span_start_ms=1000,
                source_span_end_ms=3000,
                source_span_index=0,
                source_span_count=2,
            ),
            TimingChar(
                "\u4eba",
                2000,
                source_span_start_ms=1000,
                source_span_end_ms=3000,
                source_span_index=1,
                source_span_count=2,
            ),
        ],
        end_ms=3000,
    )
    ruby = RubyAnnotation(
        kanji="\u4e8c\u4eba",
        reading="\u3075\u305f\u308a",
        pos_start_ms=1000,
        pos_end_ms=3000,
        reading_part_ms=[1200, 1600],
    )
    track = TimingTrack(lines=[line], rubies=[ruby])

    raw_cells = build_lanes([("主字幕", track)])[0].blocks[0].cells
    visual_cells = build_lanes(
        [("主字幕", track)],
        Style(font_size_px=48, entry_anim="utopia"),
    )[0].blocks[0].cells

    assert raw_cells[1].start_ms == 2000
    assert visual_cells[1].start_ms > raw_cells[1].start_ms


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


def _double_click(widget: TrackTimelineView, x: float, y: float) -> None:
    widget.mouseDoubleClickEvent(
        _mouse_event(widget, QMouseEvent.Type.MouseButtonDblClick, x, y)
    )


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


def test_click_block_emits_line_selected(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 160)
    widget.set_tracks([("主字幕", _make_track()), ("コーラス", _make_track())])
    widget.set_duration(10_000)

    received: list[tuple[int, int]] = []
    widget.lineSelected.connect(lambda lane, line: received.append((lane, line)))

    # 第二轨的第二句：track line 2（中间隔一个空行）
    _lane, rect = widget._lane_geometry()[1]
    _click(widget, widget._x_for_ms(4400), rect.center().y())
    assert received == [(1, 2)]

    # 空白处点击只 seek，不产生行选中
    _lane0, rect0 = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(8000), rect0.center().y())
    assert received == [(1, 2)]


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


def test_short_handle_paints_at_exact_time_but_keeps_wide_hit_target(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(10_000)
    widget.set_display_windows([{0: (990, 2_610)}])

    _lane, lane_rect = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(1_650), lane_rect.center().y())
    left_rect, right_rect, _lane_index, _block = widget._handle_rects()

    assert left_rect.left() == pytest.approx(widget._x_for_ms(990), abs=0.01)
    assert left_rect.right() == pytest.approx(widget._x_for_ms(1_000), abs=0.01)
    assert right_rect.left() == pytest.approx(widget._x_for_ms(2_600), abs=0.01)
    assert right_rect.right() == pytest.approx(widget._x_for_ms(2_610), abs=0.01)
    assert left_rect.width() < 12
    assert right_rect.width() < 12
    assert widget._handle_hit_rect(left_rect, entry=True).width() >= 18
    assert widget._handle_hit_rect(right_rect, entry=False).width() >= 18


def test_handle_hover_shows_effective_animation_name_and_duration(
    qapp, monkeypatch
) -> None:
    track = _make_track()
    track.lines[0].animation_override = LineAnimationOverride(
        entry_anim="slide_in",
        entry_duration_ms=650,
        exit_anim="char_fade",
        exit_duration_ms=900,
    )
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([('主字幕', track)])
    widget.set_style(Style(entry_anim="fade", exit_anim="slide_out"))
    widget.set_duration(10_000)
    widget.set_display_windows([{0: (500, 3200)}])
    shown: list[str] = []
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.editor.timeline_view.show_fluent_tooltip",
        lambda text, **_kwargs: shown.append(text),
    )

    _lane, lane_rect = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(1650), lane_rect.center().y())
    left_rect, right_rect, _lane_index, _block = widget._handle_rects()

    _move(widget, left_rect.center().x(), left_rect.center().y())
    _move(widget, right_rect.center().x(), right_rect.center().y())

    # 括号里的第一个时长是虚线框本身（上屏 500 → 走字 1000、2600 → 消失 3200）
    assert shown == [
        "入场覆盖：00:00.500 → 00:01.000（500 ms）：滑入（500 ms）",
        "退场覆盖：00:02.600 → 00:03.200（600 ms）：逐字淡出（600 ms）",
    ]
    assert all("本句" not in text and "全局" not in text for text in shown)


def test_block_hover_shows_start_and_end_with_milliseconds(qapp, monkeypatch) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(10_000)
    shown: list[str] = []
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.editor.timeline_view.show_fluent_tooltip",
        lambda text, **_kwargs: shown.append(text),
    )

    _lane, lane_rect = widget._lane_geometry()[0]
    _move(widget, widget._x_for_ms(1650), lane_rect.center().y())

    assert shown == ["开始：00:01.000\n结束：00:02.600\n主唱：あいう"]


def test_handle_hover_uses_global_animation_when_line_has_no_override(
    qapp, monkeypatch
) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([('主字幕', _make_track())])
    widget.set_style(
        Style(
            entry_anim="fade",
            entry_lead_ms=300,
            exit_anim="slide_out",
            exit_fade_ms=500,
        )
    )
    widget.set_duration(10_000)
    widget.set_display_windows([{0: (500, 3200)}])
    shown: list[str] = []
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.editor.timeline_view.show_fluent_tooltip",
        lambda text, **_kwargs: shown.append(text),
    )

    _lane, lane_rect = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(1650), lane_rect.center().y())
    left_rect, right_rect, _lane_index, _block = widget._handle_rects()

    _move(widget, left_rect.center().x(), left_rect.center().y())
    _move(widget, right_rect.center().x(), right_rect.center().y())

    assert shown == [
        "入场覆盖：00:00.500 → 00:01.000（500 ms）：淡入（300 ms）",
        "退场覆盖：00:02.600 → 00:03.200（600 ms）：滑出（500 ms）",
    ]


def test_drag_left_handle_writes_show_override(qapp) -> None:
    track = _make_track()
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", track)])
    widget.set_duration(10_000)
    widget.set_display_windows([{0: (800, 3000)}])

    edits: list[tuple] = []
    widget.displayWindowEdited.connect(lambda *args: edits.append(args))

    _lane, rect = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(1650), rect.center().y())  # 选中第一句

    left_rect, _right, _lane_idx, _block = widget._handle_rects()
    _click(widget, left_rect.center().x(), left_rect.center().y())
    assert widget._drag is not None and widget._drag[0] == "lead"

    _move(widget, widget._x_for_ms(300), left_rect.center().y())
    assert track.lines[0].display_start_override_ms == pytest.approx(300, abs=30)
    assert widget._windows[0][0][0] == track.lines[0].display_start_override_ms
    current = track.lines[0].display_start_override_ms
    assert widget._drag_badge_content() == (
        f"Δ −{800 - current} ms",
        f"→ {_format_precise_ms(current)}",
        current,
    )
    assert edits == []  # 拖动中不通知

    _release(widget, widget._x_for_ms(300), left_rect.center().y())
    assert len(edits) == 1
    track_index, line_index, old_values, new_values = edits[0]
    assert (track_index, line_index) == (0, 0)
    assert old_values == (None, None)
    assert new_values == (track.lines[0].display_start_override_ms, None)
    assert widget._drag is None


def test_handle_press_release_without_move_emits_nothing(qapp) -> None:
    track = _make_track()
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", track)])
    widget.set_duration(10_000)
    widget.set_display_windows([{0: (800, 3000)}])

    edits: list[tuple] = []
    widget.displayWindowEdited.connect(lambda *args: edits.append(args))

    _lane, rect = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(1650), rect.center().y())
    left_rect, _right, _lane_idx, _block = widget._handle_rects()
    _click(widget, left_rect.center().x(), left_rect.center().y())
    _release(widget, left_rect.center().x(), left_rect.center().y())
    assert edits == []  # 没有实际变化不产生可撤销编辑


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


def test_double_click_left_handle_edits_entry_margin(qapp) -> None:
    track = _make_track()
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", track)])
    widget.set_duration(10_000)
    widget.set_display_windows([{0: (800, 3000)}])

    edits: list[tuple] = []
    widget.displayWindowEdited.connect(lambda *args: edits.append(args))

    _lane, lane_rect = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(1650), lane_rect.center().y())
    left_rect, _right, _lane_idx, _block = widget._handle_rects()

    _double_click(widget, left_rect.center().x(), left_rect.center().y())
    assert widget._margin_editor is not None
    assert not widget._margin_editor.isHidden()
    editor_y = widget._margin_editor.geometry().center().y()
    assert lane_rect.top() <= editor_y <= lane_rect.bottom()
    assert widget._margin_edit is not None
    assert widget._margin_edit.text() == "200"

    widget._margin_edit.setText("600")
    widget._commit_margin_editor()

    assert track.lines[0].display_start_override_ms == 400
    assert widget._windows[0][0] == (400, 3000)
    assert edits == [(0, 0, (None, None), (400, None))]


def test_double_click_right_handle_edits_exit_margin(qapp) -> None:
    track = _make_track()
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", track)])
    widget.set_duration(10_000)
    widget.set_display_windows([{0: (800, 3000)}])

    edits: list[tuple] = []
    widget.displayWindowEdited.connect(lambda *args: edits.append(args))

    _lane, lane_rect = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(1650), lane_rect.center().y())
    _left, right_rect, _lane_idx, _block = widget._handle_rects()

    _double_click(widget, right_rect.center().x(), right_rect.center().y())
    assert widget._margin_edit is not None
    assert widget._margin_edit.text() == "400"

    widget._margin_edit.setText("900")
    widget._commit_margin_editor()

    assert track.lines[0].display_end_override_ms == 3500
    assert widget._windows[0][0] == (800, 3500)
    assert edits == [(0, 0, (None, None), (None, 3500))]


def test_overlapped_blocks_select_topmost_line_and_edit_its_margin(qapp) -> None:
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("前", 1000)], end_ms=3000),
            TimingLine(chars=[TimingChar("後", 2500)], end_ms=4200),
        ]
    )
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", track)])
    widget.set_duration(8_000)
    widget.set_display_windows([{0: (800, 3200), 1: (2300, 4500)}])

    _lane, lane_rect = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(2600), lane_rect.center().y())

    assert widget._selected == (0, 1)
    _left, right_rect, _lane_idx, block = widget._handle_rects()
    assert block.line_index == 1

    _double_click(widget, right_rect.center().x(), right_rect.center().y())
    assert widget._margin_edit is not None
    widget._margin_edit.setText("900")
    widget._commit_margin_editor()

    assert track.lines[0].display_end_override_ms is None
    assert track.lines[1].display_end_override_ms == 5100


def test_selection_paint_smoke(qapp) -> None:
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", _make_track())])
    widget.set_duration(10_000)
    widget.set_display_windows([{0: (500, 3200)}])
    _lane, rect = widget._lane_geometry()[0]
    _click(widget, widget._x_for_ms(1650), rect.center().y())
    widget.grab()  # 选中态 + 把手绘制


# ---------------------------------------------------------------------------
# 反向走字标记：块快照携带标记、绘制差异、右键菜单切换
# ---------------------------------------------------------------------------


def test_build_lanes_carries_wipe_reverse_flag() -> None:
    track = _make_track()
    track.lines[0].wipe_reverse = True
    lanes = build_lanes([("主字幕", track)])
    assert lanes[0].blocks[0].wipe_reverse is True
    assert lanes[0].blocks[1].wipe_reverse is False


def test_toggle_wipe_reverse_writes_override_and_emits(qapp) -> None:
    track = _make_track()
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", track)])
    widget.set_duration(10_000)

    edits: list[tuple] = []
    widget.wipeReverseEdited.connect(lambda *args: edits.append(args))

    widget._toggle_wipe_reverse(0, 0)

    line = track.lines[0]
    assert line.wipe_reverse is True
    assert line.wipe_reverse_override is True
    assert edits == [(0, 0, (None, False), (True, True))]
    # 块快照随标记原地重建（保留视口与选中态）
    assert widget._lanes[0].blocks[0].wipe_reverse is True
    assert "反向走字" in _line_block_tooltip(widget._lanes[0].blocks[0])

    widget._toggle_wipe_reverse(0, 0)
    assert line.wipe_reverse is False
    assert line.wipe_reverse_override is False
    assert edits[-1] == (0, 0, (True, True), (False, False))
    assert widget._lanes[0].blocks[0].wipe_reverse is False


def test_wipe_reverse_marker_changes_static_pixmap(qapp) -> None:
    track = _make_track()
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", track)])
    widget.set_duration(10_000)

    before = widget._render_static_pixmap().toImage()
    track.lines[0].wipe_reverse = True
    widget.refresh_tracks()
    after = widget._render_static_pixmap().toImage()

    assert before.size() == after.size()
    assert before != after


def test_context_menu_on_block_toggles_wipe_reverse(qapp, monkeypatch) -> None:
    from PyQt6.QtGui import QContextMenuEvent

    from krok_helper.subtitle_render.frontend.editor import timeline_view

    track = _make_track()
    widget = TrackTimelineView()
    widget.resize(800, 180)
    widget.set_tracks([("主字幕", track)])
    widget.set_duration(10_000)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        timeline_view.RoundMenu,
        "exec",
        lambda menu, *_args, **_kwargs: captured.setdefault("menu", menu),
    )

    edits: list[tuple] = []
    widget.wipeReverseEdited.connect(lambda *args: edits.append(args))

    _lane, lane_rect = widget._lane_geometry()[0]
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse,
        QPoint(round(widget._x_for_ms(1500)), round(lane_rect.center().y())),
    )
    widget.contextMenuEvent(event)

    menu = captured["menu"]
    action = {item.text(): item for item in menu.actions()}["反向走字"]
    assert action.isCheckable()
    assert not action.isChecked()

    action.trigger()
    assert track.lines[0].wipe_reverse is True
    assert track.lines[0].wipe_reverse_override is True
    assert edits == [(0, 0, (None, False), (True, True))]

    # 右键空白轨道区不弹菜单
    captured.clear()
    empty = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse,
        QPoint(round(widget._x_for_ms(8000)), round(lane_rect.center().y())),
    )
    widget.contextMenuEvent(empty)
    assert "menu" not in captured
