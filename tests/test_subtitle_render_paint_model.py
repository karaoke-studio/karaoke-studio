from __future__ import annotations

from krok_helper.subtitle_render.paint import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
)
from krok_helper.subtitle_render.serialization.paint import (
    paint_fill_from_dict,
    paint_fill_to_dict,
)


def test_paint_model_preserves_nicokara_defaults() -> None:
    colors = KaraokeColors()

    assert colors.before.text.color == "#FFFFFF"
    assert colors.before.stroke.color == "#222222"
    assert colors.after.text.color == "#FF5A6F"
    assert colors.after.shadow.color == "#000000"


def test_paint_model_default_factories_do_not_share_mutable_fills() -> None:
    first = KaraokeColorState()
    second = KaraokeColorState()

    first.text.gradient_stops.append((50, "#123456"))

    assert second.text.gradient_stops == [(0, "#FFFFFF"), (100, "#FFFFFF")]


def test_models_keeps_paint_compatibility_exports() -> None:
    from krok_helper.subtitle_render import models

    assert models.PaintFill is PaintFill
    assert models.KaraokeColorState is KaraokeColorState
    assert models.KaraokeColors is KaraokeColors
    assert models.paint_fill_to_dict is paint_fill_to_dict
    assert models.paint_fill_from_dict is paint_fill_from_dict


def test_paint_codec_round_trips_fractional_stops_and_image_scale() -> None:
    fill = paint_fill_from_dict(
        {
            "mode": "gradient_vertical",
            "color": "#123456",
            "gradient_stops": [[12.5, "#111111"], [80, "#EEEEEE"]],
            "image_scale_pct": 125,
        }
    )

    assert fill.gradient_stops == [
        (0, "#123456"),
        (12.5, "#111111"),
        (80, "#EEEEEE"),
        (100, "#123456"),
    ]
    assert paint_fill_to_dict(fill)["image_scale_pct"] == 125
