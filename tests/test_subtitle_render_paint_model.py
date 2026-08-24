from __future__ import annotations

from krok_helper.subtitle_render.paint import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
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
