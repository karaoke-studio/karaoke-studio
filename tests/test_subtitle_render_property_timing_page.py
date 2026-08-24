"""Focused construction contracts for the subtitle timing-property page."""

from __future__ import annotations

from krok_helper.subtitle_render.frontend.property_timing_page import (
    TimingPropertyPageBuilder,
)


class _Host:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self.sync_refreshes = 0

    def _update_style(self, **changes) -> None:
        self.updates.append(changes)

    def _sync_sync_each_page_enabled(self) -> None:
        self.sync_refreshes += 1


def test_timing_property_builder_preserves_section_and_control_contracts(qapp) -> None:
    host = _Host()
    tooltip_calls: list[tuple[object, int]] = []
    builder = TimingPropertyPageBuilder(
        host,
        tooltip_installer=lambda widget, *, show_delay: tooltip_calls.append(
            (widget, show_delay)
        ),
    )

    section = builder.make_section()

    assert section.header.text() == "时间"
    assert host._line_lead_spin.minimum() == 0
    assert host._line_lead_spin.maximum() == 10_000
    assert host._line_offset_spin.minimum() == -10_000
    assert host._section_gap_spin.maximum() == 60_000
    assert host._section_gap_spin.isHidden()
    assert host._section_ending_combo.count() == 2
    assert host._section_ending_combo.itemData(0) == "hold"
    assert host._section_ending_combo.itemData(1) == "clear"
    assert host._lane_gap_spin.maximum() == 5_000
    assert not host._sync_each_page_check.isEnabled()
    assert len(tooltip_calls) == 6
    assert {delay for _widget, delay in tooltip_calls} == {300}


def test_timing_property_builder_routes_controls_to_style_fields(qapp) -> None:
    host = _Host()
    builder = TimingPropertyPageBuilder(
        host,
        tooltip_installer=lambda *_args, **_kwargs: None,
    )
    builder.make_section()

    host._line_lead_spin.setValue(250)
    host._line_offset_spin.setValue(-120)
    host._section_ending_combo.setCurrentIndex(1)
    host._sync_entry_check.setChecked(True)
    host._ruby_main_reading_units_check.setChecked(True)
    host._allow_animation_overlap_check.setChecked(True)
    host._auto_fill_section_time_check.setChecked(True)

    assert host.updates == [
        {"line_lead_in_ms": 250},
        {"timing_offset_ms": -120},
        {"section_ending_mode": "clear"},
        {"sync_entry": True},
        {"ruby_main_progress_mode": "reading_units"},
        {"allow_entry_exit_animation_overlap": True},
        {"auto_fill_section_time": True},
    ]
    assert host.sync_refreshes == 1
