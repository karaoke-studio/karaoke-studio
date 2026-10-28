from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace

import pytest

from krok_helper.subtitle_render.domain.frame_rate import frame_rate_text, frame_timestamp, frame_count

from krok_helper.subtitle_render.settings.screen import (
    ScreenSettings,
    match_screen_preset_key,
    screen_settings_from_dict,
    screen_settings_to_dict,
    resolve_source_screen,
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
            "fps": float("nan"),
        }
    )

    assert settings == ScreenSettings(
        preset_key="custom",
        par="1:1",
        width=160,
        height=4320,
        fps=60,
        resolution_mode="custom",
        fps_mode="custom",
    )


def test_screen_settings_resolves_matching_preset() -> None:
    assert match_screen_preset_key(1920, 1080, "1:1") == "hdtv_1080"
    assert match_screen_preset_key(1920, 1080, "4:3") == "custom"


def test_property_panel_keeps_screen_settings_compatibility_exports() -> None:
    from krok_helper.subtitle_render.frontend.properties import property_panel

    assert property_panel.ScreenSettings is ScreenSettings
    assert property_panel.screen_settings_from_dict is screen_settings_from_dict


def test_legacy_screen_values_stay_custom_after_source_changes():
    settings = screen_settings_from_dict({"width": 2560, "height": 1440, "fps": 120})
    assert (settings.resolution_mode, settings.fps_mode) == ("custom", "custom")
    info = SimpleNamespace(video_width=1920, video_height=1080, video_fps=24)
    assert resolve_source_screen(settings, info) == settings


@pytest.mark.parametrize("resolution_mode,size", [("source", (1920, 1080)), ("source_2x", (3840, 2160))])
@pytest.mark.parametrize("fps_mode,multiplier", [("source", 1), ("source_2x", 2)])
def test_source_modes_resolve_independently_and_round_trip(resolution_mode, size, fps_mode, multiplier):
    info = SimpleNamespace(video_width=1920, video_height=1080, video_fps=30000 / 1001)
    settings = ScreenSettings(resolution_mode=resolution_mode, fps_mode=fps_mode)
    resolved = resolve_source_screen(settings, info)
    assert (resolved.width, resolved.height) == size
    assert resolved.fps == 30000 / 1001 * multiplier
    assert screen_settings_from_dict(screen_settings_to_dict(resolved)) == resolved
    assert resolve_source_screen(resolved, info) == resolved  # 2x never compounds.


def test_source_modes_keep_original_size_limits():
    settings = ScreenSettings(resolution_mode="source_2x", fps_mode="source_2x")
    info = SimpleNamespace(video_width=3840, video_height=2160, video_fps=120)
    resolved = resolve_source_screen(settings, info)
    assert (resolved.width, resolved.height, resolved.fps) == (7680, 4320, 240)
    assert screen_settings_from_dict(screen_settings_to_dict(resolved)) == resolved
    with pytest.raises(ValueError, match="超出支持范围"):
        resolve_source_screen(settings, SimpleNamespace(video_width=7680, video_height=4320, video_fps=60))
    with pytest.raises(ValueError, match="分辨率"):
        resolve_source_screen(settings, None)
    with pytest.raises(ValueError, match="帧率"):
        resolve_source_screen(replace(settings, resolution_mode="custom"), None)


def test_source_frame_rate_is_not_capped_at_custom_editor_default():
    info = SimpleNamespace(video_width=1920, video_height=1080, video_fps=600)
    resolved = resolve_source_screen(ScreenSettings(fps_mode="source_2x"), info)
    assert resolved.fps == 1200
    assert screen_settings_from_dict(screen_settings_to_dict(resolved)) == resolved


@pytest.mark.parametrize("fps", [24, 48, 50, 240, 24000 / 1001, 30000 / 1001, 60000 / 1001])
def test_fractional_frame_cadence_does_not_drift_over_long_exports(fps):
    rate = Fraction(fps).limit_denominator(1_000_000)
    assert frame_rate_text(fps) == str(rate)
    index = 1_000_000
    assert frame_timestamp(index, fps) == round(index * 1000 / rate)
    expected = 3_600_000 * rate / 1000
    assert frame_count(3_600_000, fps) == -(-expected.numerator // expected.denominator)
