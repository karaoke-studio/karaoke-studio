"""Tests for subtitle render line animation interpolation."""

from __future__ import annotations

import pytest

from krok_helper.subtitle_render.engine.animator import line_animation_state
from krok_helper.subtitle_render.models import (
    LineAnimationOverride,
    Style,
    TimingLine,
    effective_karaoke_animation,
    style_with_line_animation,
)


def test_line_animation_override_replaces_only_animation_fields():
    style = Style(
        font_size_px=100,
        entry_anim="fade",
        entry_lead_ms=1200,
        exit_anim="slide_out",
        exit_fade_ms=900,
    )
    line = TimingLine(
        animation_override=LineAnimationOverride(
            entry_anim="slide_in",
            entry_duration_ms=400,
            exit_anim="none",
            exit_duration_ms=0,
        )
    )

    effective = style_with_line_animation(style, line)

    assert effective.entry_anim == "slide_in"
    assert effective.entry_lead_ms == 400
    assert effective.exit_anim == "none"
    assert effective.exit_fade_ms == 0
    assert effective.font_size_px == 100


def test_karaoke_animation_keeps_legacy_utopia_and_allows_explicit_override():
    assert effective_karaoke_animation(Style(entry_anim="utopia")) == "utopia"
    assert effective_karaoke_animation(Style(exit_anim="utopia")) == "utopia"
    assert (
        effective_karaoke_animation(
            Style(entry_anim="utopia", karaoke_anim="none")
        )
        == "none"
    )
    assert effective_karaoke_animation(Style(karaoke_anim="utopia")) == "utopia"


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


def test_rise_entry_fades_from_transparent_to_opaque():
    style = Style(font_size_px=100, entry_anim="rise", entry_lead_ms=1000)

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
    assert start.dy > 0.0
    assert mid.opacity == pytest.approx(0.75)
    assert mid.dy > 0.0
    assert done.opacity == pytest.approx(1.0)
    assert done.dy == pytest.approx(0.0)


def test_rise_exit_fades_from_opaque_to_transparent():
    style = Style(font_size_px=100, exit_anim="rise", exit_fade_ms=1000)

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
    assert before.dy == pytest.approx(0.0)
    assert mid.opacity == pytest.approx(0.25)
    assert mid.dy < 0.0
    assert end.opacity == pytest.approx(0.0)
    assert end.dy < mid.dy


def test_max_line_animation_excursion_none_for_shear_effects():
    # char_drip / spin_flip 的剪切包络随首帧 tan 发散、依赖行内字形宽度，
    # 无可靠上界：调用方（条带/多带预扫）必须据此禁用裁剪优化
    from krok_helper.subtitle_render.engine.animator import max_line_animation_excursion

    drip = Style(font_size_px=48, entry_anim="char_drip", entry_lead_ms=250)
    assert max_line_animation_excursion(drip, 1080) is None
    flip = Style(font_size_px=48, exit_anim="spin_flip", exit_fade_ms=250)
    assert max_line_animation_excursion(flip, 1080) is None


def test_max_line_animation_excursion_bounded_for_travel_effects():
    from krok_helper.subtitle_render.engine.animator import max_line_animation_excursion

    rise = Style(font_size_px=48, entry_anim="rise", entry_lead_ms=300)
    assert max_line_animation_excursion(rise, 1080) == pytest.approx(
        max(48 * 0.35, 18.0)
    )
    utopia = Style(font_size_px=48, exit_anim="utopia", exit_fade_ms=750)
    assert max_line_animation_excursion(utopia, 2160) == pytest.approx(
        2160 / 15.0 + 48 * 1.5
    )
    assert max_line_animation_excursion(Style(), 1080) == 0.0
