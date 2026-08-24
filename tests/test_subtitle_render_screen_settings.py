from __future__ import annotations

from krok_helper.subtitle_render.screen_settings import (
    ScreenSettings,
    match_screen_preset_key,
    screen_settings_from_dict,
    screen_settings_to_dict,
)


def test_screen_settings_round_trip_preserves_supported_values() -> None:
    settings = ScreenSettings(
        preset_key="custom",
        par="1:1",
        width=2560,
        height=1440,
        fps=120,
    )

    assert screen_settings_from_dict(screen_settings_to_dict(settings)) == settings


def test_screen_settings_normalizes_invalid_persisted_values() -> None:
    settings = screen_settings_from_dict(
        {
            "preset_key": "missing",
            "par": "invalid",
            "width": 1,
            "height": 100_000,
            "fps": 30,
        }
    )

    assert settings == ScreenSettings(
        preset_key="custom",
        par="1:1",
        width=160,
        height=4320,
        fps=60,
    )


def test_screen_settings_resolves_matching_preset() -> None:
    assert match_screen_preset_key(1920, 1080, "1:1") == "hdtv_1080"
    assert match_screen_preset_key(1920, 1080, "4:3") == "custom"


def test_property_panel_keeps_screen_settings_compatibility_exports() -> None:
    from krok_helper.subtitle_render.frontend import property_panel

    assert property_panel.ScreenSettings is ScreenSettings
    assert property_panel.screen_settings_from_dict is screen_settings_from_dict
