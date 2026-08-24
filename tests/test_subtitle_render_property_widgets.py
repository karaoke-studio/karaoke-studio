"""Focused contracts for reusable subtitle property widgets."""

from __future__ import annotations

from PyQt6.QtCore import Qt

from krok_helper.subtitle_render.frontend.property_widgets import (
    CollapsibleSection,
    ToggleSwitch,
)


def test_property_toggle_switch_preserves_compact_interaction_contract(qapp) -> None:
    switch = ToggleSwitch()

    assert switch.isCheckable() is True
    assert switch.sizeHint().width() == 38
    assert switch.sizeHint().height() == 22
    assert switch.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_collapsible_property_section_preserves_summary_and_switch_contract(qapp) -> None:
    section = CollapsibleSection("标题", switch=True)
    section.set_collapsed_summary("已启用")

    assert section.header.text() == "标题"
    assert isinstance(section.header_switch, ToggleSwitch)
    assert section.header.arrowType() == Qt.ArrowType.DownArrow
    assert section._summary_label.isHidden() is True

    section.set_expanded(False)

    assert section.header.arrowType() == Qt.ArrowType.RightArrow
    assert section._summary_label.text() == "已启用"
    assert section._summary_label.isHidden() is False
