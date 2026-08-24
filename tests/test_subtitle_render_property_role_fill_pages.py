"""Focused contracts for role fill-editor pages."""

from PyQt6.QtWidgets import QPushButton

from krok_helper.subtitle_render.frontend.property_role_fill_pages import (
    RoleFillPagesBuilder,
)


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

    def _update_split_stops(self, _stops):
        pass

    def _sync_gradient_stop_controls(self):
        pass

    def _sync_split_stop_controls(self):
        pass

    def _wire_color_edit_session(self, _button):
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
    from krok_helper.subtitle_render.frontend.property_panel import (
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
    assert host.arrangements[0][1] == {
        "vertical": False,
        "footer": host._ruby_horizontal_gradient_with_main_check,
    }


def test_split_fill_page_preserves_hard_stop_and_vertical_contracts(qapp) -> None:
    from krok_helper.subtitle_render.frontend.property_panel import (
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
    assert host.arrangements[0][1] == {"vertical": True}
    assert page.layout() is host.arrangements[0][0][0]


def test_image_fill_page_preserves_path_and_scale_contracts(qapp) -> None:
    from krok_helper.subtitle_render.frontend.property_panel import _spin

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
