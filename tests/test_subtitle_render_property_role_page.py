"""Focused composition contracts for the subtitle role-property page."""

from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from krok_helper.subtitle_render.frontend.property_role_page import (
    RolePropertyPageBuilder,
)


class _Container(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.parts = None

    def set_widgets(self, *widgets) -> None:
        self.parts = widgets


class _Host:
    def _make_scheme_navigation(self, parent):
        return QWidget(parent)

    def _make_color_section(self, parent=None, *, inline=False):
        assert inline
        return QWidget(parent)

    def _make_font_section(self, parent=None, *, inline=False):
        assert inline
        return QWidget(parent)


def _plain_card():
    card = QWidget()
    return card, QVBoxLayout(card)


def test_role_builder_preserves_composition_order_and_contracts(qapp) -> None:
    host = _Host()
    section = RolePropertyPageBuilder(
        host,
        plain_card_factory=_plain_card,
        role_header_factory=_Container,
        font_preview_factory=QWidget,
        property_pair_factory=_Container,
    ).make_font_color_section()

    assert section is host._scheme_section
    assert host._font_preview_requested is True
    assert host._role_header.parts[1] is host._font_preview_widget
    assert host._font_color_row.parts[0] is host._color_section
    assert host._font_color_row.parts[2] is host._font_section
    divider = host._font_color_row.parts[1]
    assert divider.objectName() == "SubtitlePropertyInnerDivider"
    assert divider.width() == 1
