from __future__ import annotations

from krok_helper.subtitle_render.timing import (
    GuideSymbol,
    TimingChar,
    TimingLine,
    TimingTrack,
    guide_symbol_replacement_count,
    line_visible_chars,
    timing_line_start_ms,
)
from krok_helper.subtitle_render.serialization.timing import (
    guide_symbol_from_dict,
    guide_symbol_to_dict,
    line_animation_override_from_dict,
    line_animation_override_to_dict,
    subtitle_loading_settings_from_dict,
    subtitle_loading_settings_to_dict,
    track_page_plan_from_dict,
    track_page_plan_to_dict,
)


def test_timing_model_preserves_guide_prefix_semantics() -> None:
    symbol = GuideSymbol(
        count=2,
        duration_ms=100,
        replacement_prefix=("●", "●"),
    )
    line = TimingLine(
        chars=[
            TimingChar("●", 1_000),
            TimingChar("●", 1_100),
            TimingChar("歌", 1_200),
        ],
        guide_symbol=symbol,
    )

    assert guide_symbol_replacement_count(line) == 2
    assert [char.text for char in line_visible_chars(line)] == ["歌"]
    assert timing_line_start_ms(line) == 1_000


def test_timing_track_options_preserve_first_seen_order() -> None:
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("a", 0, role_label="主唱")],
                singer_id=2,
                singer_label="甲",
            ),
            TimingLine(
                chars=[TimingChar("b", 1, role_label="和声")],
                singer_id=1,
                singer_label="乙",
            ),
            TimingLine(
                chars=[TimingChar("c", 2, role_label="主唱")],
                singer_id=2,
                singer_label="甲",
            ),
        ]
    )

    assert track.singer_options == [(2, "甲"), (1, "乙")]
    assert track.role_options == ["主唱", "和声"]


def test_models_keeps_timing_compatibility_exports() -> None:
    from krok_helper.subtitle_render import models

    assert models.GuideSymbol is GuideSymbol
    assert models.TimingChar is TimingChar
    assert models.TimingLine is TimingLine
    assert models.TimingTrack is TimingTrack
    assert models.line_visible_chars is line_visible_chars
    assert models.timing_line_start_ms is timing_line_start_ms


def test_models_keeps_timing_codec_compatibility_exports() -> None:
    from krok_helper.subtitle_render import models

    assert models.guide_symbol_from_dict is guide_symbol_from_dict
    assert models.guide_symbol_to_dict is guide_symbol_to_dict
    assert models.line_animation_override_from_dict is line_animation_override_from_dict
    assert models.line_animation_override_to_dict is line_animation_override_to_dict
    assert models.subtitle_loading_settings_from_dict is subtitle_loading_settings_from_dict
    assert models.subtitle_loading_settings_to_dict is subtitle_loading_settings_to_dict
    assert models.track_page_plan_from_dict is track_page_plan_from_dict
    assert models.track_page_plan_to_dict is track_page_plan_to_dict
