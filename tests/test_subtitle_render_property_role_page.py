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
    def __init__(self) -> None:
        self.updates = []
        self.font_script_changes = []
        self.font_page_syncs = 0

    def _make_scheme_navigation(self, parent):
        return QWidget(parent)

    def _make_color_section(self, parent=None, *, inline=False):
        assert inline
        return QWidget(parent)

    def _make_font_section(self, parent=None, *, inline=False):
        assert inline
        return QWidget(parent)

    def _make_font_settings_page(self, subject, script, parent):
        page = QWidget(parent)
        page.setObjectName(f"{subject}:{script}")
        return page

    def _on_font_script_changed(self, script):
        self.font_script_changes.append(script)

    def _sync_font_settings_page(self):
        self.font_page_syncs += 1

    def _update_style(self, **changes):
        self.updates.append(changes)


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


def test_role_font_builder_preserves_page_order_and_flags(qapp) -> None:
    host = _Host()
    builder = RolePropertyPageBuilder(
        host,
        plain_card_factory=_plain_card,
        role_header_factory=_Container,
        font_preview_factory=QWidget,
        property_pair_factory=_Container,
    )
    section = builder.make_font_section()

    assert section.header.text() == "字体"
    assert host._font_tab_stack.count() == 4
    assert [host._font_tab_stack.widget(i).objectName() for i in range(4)] == [
        "main:japanese",
        "main:latin",
        "ruby:japanese",
        "ruby:latin",
    ]
    assert host._font_stroke_controls == {}
    assert host._font_controls == {}
    assert host._font_size_follow_checks == {}
    assert host._ruby_anchor_check.toolTip().startswith("关闭后")


def test_role_font_builder_routes_role_flags(qapp) -> None:
    host = _Host()
    builder = RolePropertyPageBuilder(
        host,
        plain_card_factory=_plain_card,
        role_header_factory=_Container,
        font_preview_factory=QWidget,
        property_pair_factory=_Container,
    )
    builder.make_font_section()

    host._italic_check.setChecked(True)
    host._ruby_anchor_check.setChecked(True)

    assert host.updates == [
        {"italic": True},
        {"affects_ruby_anchor": True},
    ]
