"""Focused contracts for reusable subtitle property widgets."""

from __future__ import annotations

from PyQt6.QtCore import Qt

from krok_helper.subtitle_render.frontend.properties.property_widgets import (
    ClickableRow,
    CollapsibleSection,
    FolderTabPanel,
    PillSelector,
    SubGroup,
    ToggleSwitch,
    subgroup_label,
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


def test_property_pill_selector_changes_only_for_a_new_choice(qapp) -> None:
    selector = PillSelector((("before", "走字前"), ("after", "走字后")))
    changes: list[str] = []
    selector.changed.connect(changes.append)

    selector._buttons["after"].click()
    selector._buttons["after"].click()

    assert selector.current() == "after"
    assert changes == ["after"]
    assert selector._buttons["after"].isChecked() is True


def test_property_folder_tabs_keep_left_and_right_selection_independent(qapp) -> None:
    panel = FolderTabPanel(
        (("main", "正文"), ("ruby", "注音")),
        (("jp", "日文"), ("latin", "英数")),
    )
    left_changes: list[str] = []
    right_changes: list[str] = []
    panel.leftChanged.connect(left_changes.append)
    panel.rightChanged.connect(right_changes.append)

    panel._buttons[("left", "ruby")].click()
    panel._buttons[("right", "latin")].click()

    assert panel.current_left() == "ruby"
    assert panel.current_right() == "latin"
    assert left_changes == ["ruby"]
    assert right_changes == ["latin"]


def test_property_subgroup_preserves_heading_grid_and_collapse_contract(qapp) -> None:
    subgroup = SubGroup("字符排版", collapsed=True)

    assert isinstance(subgroup._header, ClickableRow)
    assert subgroup.is_collapsed() is True
    assert subgroup._chevron.text() == "▸"
    assert subgroup.grid.columnStretch(0) == 1
    assert subgroup.grid.columnStretch(1) == 1

    subgroup.show()
    qapp.processEvents()
    subgroup.set_collapsed(False)

    assert subgroup.is_collapsed() is False
    assert subgroup._chevron.text() == "▾"


def test_property_subgroup_label_preserves_accessible_style_hook(qapp) -> None:
    label = subgroup_label("行布局")

    assert label.text() == "行布局"
    assert label.objectName() == "SubtitlePropertySubheading"
