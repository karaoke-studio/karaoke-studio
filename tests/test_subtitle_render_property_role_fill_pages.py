"""Focused contracts for role fill-editor pages."""

from PyQt6.QtCore import QEvent, QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QPushButton

from krok_helper.subtitle_render.frontend.properties.roles.fills import (
    RoleFillPagesBuilder,
)


def _mouse_event(event_type, point, buttons, modifiers=Qt.KeyboardModifier.NoModifier):
    pressed = (
        Qt.MouseButton.LeftButton
        if event_type is QEvent.Type.MouseButtonPress
        else Qt.MouseButton.NoButton
    )
    return QMouseEvent(event_type, point, buttons, pressed, modifiers)


class _Host:
    def __init__(self) -> None:
        self.requests = []
        self.style_updates = []
        self.arrangements = []

    def _paint_color_button(self, field: str, color: str):
        self.requests.append((field, color))
        return QPushButton(color)

    def _update_gradient_stops(self, _stops):
        pass

    def _edit_gradient_path(self):
        self.requests.append("gradient_path")

    def _update_split_stops(self, _stops):
        pass

    def _sync_gradient_stop_controls(self):
        pass

    def _sync_split_stop_controls(self):
        pass

    def _wire_color_edit_session(self, _button):
        pass

    # 4.2.8.10 起渐变/拼色复制粘贴在宿主上弹成功/失败提示，构建器会取
    # 这四个方法；真实宿主实现在 PropertyPanel，夹具按 no-op 桩齐签名。
    def _show_gradient_copy_success(self):
        pass

    def _show_split_copy_success(self):
        pass

    def _show_color_info_paste_success(self, _kind):
        pass

    def _show_color_info_paste_invalid(self, _kind, _reason):
        pass

    def _choose_gradient_stop_color(self, *args, **kwargs):
        pass

    def _choose_split_stop_color(self, *args, **kwargs):
        pass

    def _set_gradient_stop_position(self, _value):
        pass

    def _set_split_stop_position(self, _value):
        pass

    def _update_style(self, **changes):
        self.style_updates.append(changes)

    def _update_current_fill(self, **changes):
        self.fill_updates = getattr(self, "fill_updates", [])
        self.fill_updates.append(changes)

    def _choose_paint_image(self):
        pass

    def _arrange_stop_editor(self, *args, **kwargs):
        self.arrangements.append((args, kwargs))


def test_solid_fill_page_preserves_color_button_contract(qapp) -> None:
    host = _Host()
    page = RoleFillPagesBuilder(host).make_solid_page()

    assert host.requests == [("color", "#FFFFFF")]
    assert host._paint_solid_btn.text() == "#FFFFFF"
    assert page.layout().contentsMargins().left() == 0


def test_gradient_fill_page_preserves_editor_and_control_contracts(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        ColorButton,
        GradientStopsEditor,
        _double_spin,
    )

    host = _Host()
    builder = RoleFillPagesBuilder(
        host,
        gradient_editor_factory=GradientStopsEditor,
        color_button_factory=ColorButton,
        double_spin_factory=_double_spin,
    )
    page = builder.make_gradient_page()

    assert host.requests == [
        ("start_color", "#FFFFFF"),
        ("end_color", "#FF5A6F"),
    ]
    assert host._paint_gradient_start_btn.isHidden()
    assert host._paint_gradient_end_btn.isHidden()
    assert host._gradient_stop_position_spin.minimum() == 0
    assert host._gradient_stop_position_spin.maximum() == 100
    assert host._gradient_stop_position_spin.decimals() == 3
    assert host._gradient_stop_delete_btn.toolTip() == "删除关键点"
    assert host._ruby_horizontal_gradient_with_main_check.isChecked()
    assert page.layout() is host._gradient_editor_layout
    host._gradient_path_btn.click()
    assert host.requests[-1] == "gradient_path"
    assert host.arrangements[0][1] == {
        "vertical": False,
        "footer": host._ruby_horizontal_gradient_with_main_check,
        "actions": host._gradient_actions_row,
    }


def test_split_fill_page_preserves_hard_stop_and_vertical_contracts(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        ColorButton,
        GradientStopsEditor,
        _double_spin,
    )

    host = _Host()
    builder = RoleFillPagesBuilder(
        host,
        gradient_editor_factory=GradientStopsEditor,
        color_button_factory=ColorButton,
        double_spin_factory=_double_spin,
    )
    page = builder.make_split_page()

    assert host._split_editor._orientation == "vertical"
    assert host._split_editor._hard_edges is True
    assert host._split_stop_position_spin.decimals() == 3
    assert host._split_stop_delete_btn.toolTip() == "删除分段点"
    assert host.arrangements[0][1] == {
        "vertical": True,
        "actions": host._split_actions_row,
    }
    assert page.layout() is host.arrangements[0][0][0]


def test_image_fill_page_preserves_path_and_scale_contracts(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import _spin

    host = _Host()
    builder = RoleFillPagesBuilder(host, spin_factory=_spin)
    page = builder.make_image_page()

    assert host._paint_image_browse_btn.text() == "浏览..."
    assert host._paint_image_browse_btn.minimumHeight() == 32
    assert host._paint_image_scale_spin.minimum() == 1
    assert host._paint_image_scale_spin.maximum() == 1000
    host._paint_image_path_edit.setText("C:/image.png")
    host._paint_image_path_edit.editingFinished.emit()
    host._paint_image_scale_spin.setValue(125)
    assert host.fill_updates == [
        {"image_path": "C:/image.png"},
        {"image_scale_pct": 125},
    ]
    assert page.layout().columnStretch(0) == 1


def test_moving_stop_through_another_keeps_both_and_keeps_drag(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        GradientStopsEditor,
    )

    editor = GradientStopsEditor()
    editor.set_orientation("gradient_vertical")
    editor.set_stops(
        [(0, "#111111"), (40, "#222222"), (80, "#333333"), (100, "#444444")]
    )

    # Dragging the #333333 stop exactly onto the 40 stop must not delete it:
    # equal positions are legal while the drag merely passes over them.
    editor._selected = 2
    editor._begin_drag()
    editor._apply_drag_position(40)
    assert editor._stops == [
        (0, "#111111"),
        (40, "#222222"),
        (40, "#333333"),
        (100, "#444444"),
    ]

    # The dragged marker (not the equal-position neighbour) keeps following.
    editor._apply_drag_position(30)
    assert editor._stops == [
        (0, "#111111"),
        (30, "#333333"),
        (40, "#222222"),
        (100, "#444444"),
    ]
    assert editor._stops[editor._selected] == (30, "#333333")

    # Releasing at 30 hits nothing: no merge, all four stops survive.
    editor._end_drag(merge=True)
    assert len(editor._stops) == 4


def test_moving_endpoint_anchor_spawns_stop_that_merges_back(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        GradientStopsEditor,
    )

    editor = GradientStopsEditor()
    editor.set_orientation("gradient_vertical")
    editor.set_stops([(0, "#111111"), (40, "#222222"), (100, "#444444")])

    editor._selected = 2  # the 100 endpoint anchor
    editor._begin_drag()
    editor._apply_drag_position(70)
    assert editor._stops == [
        (0, "#111111"),
        (40, "#222222"),
        (70, "#444444"),
        (100, "#444444"),
    ]
    assert editor._stops[editor._selected] == (70, "#444444")

    # Dragging the spawned marker back onto the endpoint simply restores the
    # frozen baseline: the anchor never moved, so nothing needs merging.
    editor._apply_drag_position(100)
    assert editor._stops == [
        (0, "#111111"),
        (40, "#222222"),
        (100, "#444444"),
    ]
    editor._end_drag(merge=True)
    assert editor._stops == [
        (0, "#111111"),
        (40, "#222222"),
        (100, "#444444"),
    ]


def test_dropping_stop_onto_interior_stop_merges(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        GradientStopsEditor,
    )

    editor = GradientStopsEditor()
    editor.set_orientation("gradient_vertical")
    editor.set_stops(
        [(0, "#111111"), (40, "#222222"), (80, "#333333"), (100, "#444444")]
    )

    editor._selected = 2
    editor.set_selected_position(40)  # dropped exactly onto the 40 stop
    assert editor._stops == [
        (0, "#111111"),
        (40, "#333333"),
        (100, "#444444"),
    ]
    assert editor._stops[editor._selected] == (40, "#333333")


def test_dragging_through_endpoint_does_not_pin_visitor(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        GradientStopsEditor,
    )

    editor = GradientStopsEditor()
    editor.set_orientation("gradient_vertical")
    editor.set_stops([(0, "#111111"), (40, "#222222"), (100, "#444444")])

    # Touching the 0 edge mid-drag neither merges nor converts the dragged
    # stop into the anchor: it must keep following the pointer afterwards.
    editor._selected = 1
    editor._begin_drag()
    editor._apply_drag_position(0)
    assert editor._stops == [(0, "#111111"), (0, "#222222"), (100, "#444444")]
    editor._apply_drag_position(25)
    assert editor._stops == [
        (0, "#111111"),
        (25, "#222222"),
        (100, "#444444"),
    ]
    assert editor._stops[editor._selected] == (25, "#222222")

    # Releasing exactly at the endpoint edge is a deliberate drop: it merges
    # into the endpoint anchor and the moved marker wins.
    editor._apply_drag_position(0)
    editor._end_drag(merge=True)
    assert editor._stops == [(0, "#222222"), (100, "#444444")]


def test_release_without_movement_keeps_colocated_stops(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        GradientStopsEditor,
    )

    stops = [(0, "#111111"), (40, "#222222"), (40, "#333333"), (100, "#444444")]
    editor = GradientStopsEditor()
    editor.set_stops(stops)
    editor._selected = 2
    editor._begin_drag()

    editor.mouseReleaseEvent(None)  # click without movement must not merge
    assert editor._stops == stops


def test_mouse_drag_merges_only_on_release_drop(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        GradientStopsEditor,
    )

    editor = GradientStopsEditor()
    editor.set_orientation("gradient_horizontal")
    editor.resize(240, editor.sizeHint().height())
    editor.set_stops(
        [(0, "#111111"), (30, "#222222"), (40, "#333333"), (100, "#444444")]
    )

    bar = editor._bar_rect()

    def drag_to(ratio):
        editor.mouseMoveEvent(
            _mouse_event(
                QEvent.Type.MouseMove,
                QPointF(bar.left() + bar.width() * ratio, bar.center().y()),
                Qt.MouseButton.LeftButton,
            )
        )

    # Press the 30 marker on its pointer body, sweep to exactly the 40 stop's
    # pixel: passing over it mid-drag must not merge anything.
    editor.mousePressEvent(
        _mouse_event(
            QEvent.Type.MouseButtonPress,
            editor._marker_center(30),
            Qt.MouseButton.LeftButton,
        )
    )
    drag_to(0.35)
    drag_to(0.4)
    assert editor._stops == [
        (0, "#111111"),
        (40, "#333333"),
        (40, "#222222"),
        (100, "#444444"),
    ]

    # Releasing on that exact position merges — same position-equality check
    # the old mid-drag code used, now only evaluated when the drag settles.
    editor.mouseReleaseEvent(
        _mouse_event(
            QEvent.Type.MouseButtonRelease,
            QPointF(bar.left() + bar.width() * 0.4, bar.center().y()),
            Qt.MouseButton.LeftButton,
        )
    )
    assert editor._stops == [
        (0, "#111111"),
        (40, "#222222"),
        (100, "#444444"),
    ]


def test_release_within_one_percent_merges_onto_interior_marker(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        GradientStopsEditor,
    )

    editor = GradientStopsEditor()
    editor.set_orientation("gradient_vertical")
    editor.set_stops(
        [(0, "#111111"), (40, "#333333"), (80, "#222222"), (100, "#444444")]
    )

    editor._selected = 2
    editor._begin_drag()
    editor._apply_drag_position(40.6)  # 0.6 off the 40 marker: no merge yet
    assert len(editor._stops) == 4
    editor._end_drag(merge=True)  # inside the 1% drop window: snaps onto 40
    assert editor._stops == [
        (0, "#111111"),
        (40, "#222222"),
        (100, "#444444"),
    ]
    assert editor._stops[editor._selected] == (40, "#222222")


def test_release_beyond_one_percent_keeps_both(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        GradientStopsEditor,
    )

    editor = GradientStopsEditor()
    editor.set_orientation("gradient_vertical")
    editor.set_stops(
        [(0, "#111111"), (40, "#333333"), (80, "#222222"), (100, "#444444")]
    )

    editor._selected = 2
    editor._begin_drag()
    editor._apply_drag_position(41.2)  # 1.2 off the 40 marker: outside window
    editor._end_drag(merge=True)
    assert editor._stops == [
        (0, "#111111"),
        (40, "#333333"),
        (41.2, "#222222"),
        (100, "#444444"),
    ]


def test_release_near_bar_end_snaps_flush_onto_endpoint(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        GradientStopsEditor,
    )

    editor = GradientStopsEditor()
    editor.set_orientation("gradient_vertical")
    editor.set_stops(
        [(0, "#111111"), (40, "#222222"), (60, "#333333"), (100, "#444444")]
    )

    # 99.7 is inside the 0.5% end window: snap flush onto 100 so the dragged
    # color attaches exactly to the bar edge.
    editor._selected = 2
    editor._begin_drag()
    editor._apply_drag_position(99.7)
    editor._end_drag(merge=True)
    assert editor._stops == [
        (0, "#111111"),
        (40, "#222222"),
        (100, "#333333"),
    ]
    assert editor._stops[editor._selected] == (100, "#333333")

    # 99.4 is outside the 0.5% end window (and endpoint stops are excluded
    # from the 1% interior window): the marker stays where it was released.
    editor.set_stops(
        [(0, "#111111"), (40, "#222222"), (60, "#333333"), (100, "#444444")]
    )
    editor._selected = 2
    editor._begin_drag()
    editor._apply_drag_position(99.4)
    editor._end_drag(merge=True)
    assert editor._stops == [
        (0, "#111111"),
        (40, "#222222"),
        (99.4, "#333333"),
        (100, "#444444"),
    ]


def test_set_stops_keeps_selection_among_equal_position_stops(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        GradientStopsEditor,
    )

    stops = [(0, "#111111"), (40, "#222222"), (40, "#333333"), (100, "#444444")]
    editor = GradientStopsEditor()
    editor.set_stops(stops)
    editor._selected = 2

    # The panel round-trips the same stops back on every drag frame; the
    # equal-position neighbour must not steal the selection.
    editor.set_stops(stops)
    assert editor._stops[editor._selected][1] == "#333333"


def test_vertical_bar_shrinks_before_pointer_lane_is_clipped(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        GradientStopsEditor,
    )

    editor = GradientStopsEditor()
    editor.set_orientation("gradient_vertical")

    # Full width: preferred geometry unchanged (12 left + 48 bar + tag lane).
    editor.resize(editor.sizeHint())
    assert editor.width() == 91
    assert editor._bar_rect() == QRectF(12, 15, 48, editor.height() - 30)

    # Squeezed widths: the bar absorbs the shortage so the right-side tag
    # lane plus its 4px margin always stays inside the widget. 44 is the last
    # width where the bar still shrinks (1px); the floor matches the 1px
    # floors of the horizontal bar's width and the vertical bar's height.
    for width in (90, 75, 60, 51, 44):
        editor.resize(width, editor.sizeHint().height())
        bar = editor._bar_rect()
        lane = editor._pointer_lane_rect()
        assert bar.left() == 12
        assert bar.width() == width - 43
        assert lane.right() <= width - editor._BAR_RIGHT_MARGIN

    # Extreme squeeze: the bar bottoms out at the 1px floor instead of vanishing.
    assert editor._BAR_MIN_WIDTH == 1
    editor.resize(20, editor.sizeHint().height())
    assert editor._bar_rect().width() == 1


def test_horizontal_bar_geometry_is_unaffected_by_vertical_adaptation(qapp) -> None:
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        GradientStopsEditor,
    )

    editor = GradientStopsEditor()
    editor.resize(120, editor.sizeHint().height())
    assert editor._bar_rect() == QRectF(15, 8, 90, 22)
    assert editor.sizeHint() == QSize(220, 52)
