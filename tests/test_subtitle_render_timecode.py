from __future__ import annotations

from krok_helper.subtitle_render.timecode import format_timecode_ms, parse_timecode_ms


def test_timecode_contract_round_trips_supported_values() -> None:
    for value in (0, 300, 90_000, 3_723_000, 5_999_990):
        assert parse_timecode_ms(format_timecode_ms(value)) == value


def test_property_panel_keeps_timecode_compatibility_exports() -> None:
    from krok_helper.subtitle_render.frontend.properties import property_panel

    assert property_panel.parse_timecode_ms is parse_timecode_ms
    assert property_panel.format_timecode_ms is format_timecode_ms
