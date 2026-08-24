"""Focused contracts for role fill-editor pages."""

from PyQt6.QtWidgets import QPushButton

from krok_helper.subtitle_render.frontend.property_role_fill_pages import (
    RoleFillPagesBuilder,
)


class _Host:
    def __init__(self) -> None:
        self.requests = []

    def _paint_color_button(self, field: str, color: str):
        self.requests.append((field, color))
        return QPushButton(color)


def test_solid_fill_page_preserves_color_button_contract(qapp) -> None:
    host = _Host()
    page = RoleFillPagesBuilder(host).make_solid_page()

    assert host.requests == [("color", "#FFFFFF")]
    assert host._paint_solid_btn.text() == "#FFFFFF"
    assert page.layout().contentsMargins().left() == 0
