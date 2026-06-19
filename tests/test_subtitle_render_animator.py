"""Tests for subtitle render line animation interpolation."""

from __future__ import annotations

import pytest

from krok_helper.subtitle_render.engine.animator import line_animation_state
from krok_helper.subtitle_render.models import Style


def test_slide_in_fades_from_transparent_to_opaque():
    style = Style(font_size_px=100, entry_anim="slide_in", entry_lead_ms=1000)

    start = line_animation_state(
        style,
        t_ms=0,
        display_start_ms=0,
        display_end_ms=3000,
        lane=0,
    )
    mid = line_animation_state(
        style,
        t_ms=500,
        display_start_ms=0,
        display_end_ms=3000,
        lane=0,
    )
    done = line_animation_state(
        style,
        t_ms=1000,
        display_start_ms=0,
        display_end_ms=3000,
        lane=0,
    )

    assert start.opacity == pytest.approx(0.0)
    assert start.dx < 0.0
    assert mid.opacity == pytest.approx(0.75)
    assert mid.dx < 0.0
    assert done.opacity == pytest.approx(1.0)
    assert done.dx == pytest.approx(0.0)


def test_slide_out_fades_from_opaque_to_transparent():
    style = Style(font_size_px=100, exit_anim="slide_out", exit_fade_ms=1000)

    before = line_animation_state(
        style,
        t_ms=2000,
        display_start_ms=0,
        display_end_ms=3000,
        lane=0,
    )
    mid = line_animation_state(
        style,
        t_ms=2500,
        display_start_ms=0,
        display_end_ms=3000,
        lane=0,
    )
    end = line_animation_state(
        style,
        t_ms=3000,
        display_start_ms=0,
        display_end_ms=3000,
        lane=0,
    )

    assert before.opacity == pytest.approx(1.0)
    assert before.dx == pytest.approx(0.0)
    assert mid.opacity == pytest.approx(0.25)
    assert mid.dx < 0.0
    assert end.opacity == pytest.approx(0.0)
    assert end.dx < mid.dx
