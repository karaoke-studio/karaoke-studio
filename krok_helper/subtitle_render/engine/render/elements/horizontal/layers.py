"""Geometry and cache policy for horizontal render layers."""

from __future__ import annotations

from PyQt6.QtCore import QRectF

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.paint import (
    KaraokeColors,
    KaraokeColorState,
)
from krok_helper.subtitle_render.engine.render.effects import (
    fill_signature,
    glow_concentration_level,
    glow_radius,
    karaoke_state_signature,
    main_stroke2_width,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.engine.text import GlyphLayout
from krok_helper.subtitle_render.engine.value_signature import value_signature


def horizontal_after_clip_rect(band: tuple[int, int], rtl: bool) -> QRectF:
    band_left, band_right = band
    if rtl:
        return QRectF(
            float(band_left),
            -1_000_000.0,
            1_000_000.0,
            2_000_000.0,
        )
    return QRectF(
        -1_000_000.0,
        -1_000_000.0,
        float(band_right) + 1_000_000.0,
        2_000_000.0,
    )


def horizontal_before_clip_rect(band: tuple[int, int], rtl: bool) -> QRectF:
    """Keep the before layer only on the unsung side of the wipe front."""
    band_left, band_right = band
    if rtl:
        return QRectF(
            -1_000_000.0,
            -1_000_000.0,
            float(band_left) + 1_000_000.0,
            2_000_000.0,
        )
    return QRectF(
        float(band_right),
        -1_000_000.0,
        1_000_000.0,
        2_000_000.0,
    )


def after_glow_loose_clip_rect(
    band: tuple[int, int],
    rect: QRectF,
    glow_pad: int,
    rtl: bool,
    complete: bool,
) -> QRectF:
    """Return the already-blurred after-glow bitmap clip."""
    band_left, band_right = band
    glow_pad_f = float(glow_pad)
    left = float(band_left) - (0.0 if rtl and not complete else glow_pad_f)
    right = float(band_right) + (glow_pad_f if rtl or complete else 0.0)
    return QRectF(
        left,
        rect.top() - glow_pad,
        right - left,
        rect.height() + glow_pad * 2,
    )


def after_glow_source_clip_rect(
    band: tuple[int, int],
    rect: QRectF,
    glow_pad: int,
    rtl: bool,
    complete: bool,
) -> QRectF | None:
    """Return the sung-side source clip applied before Gaussian blur."""
    if complete:
        return None
    band_left, band_right = band
    top = rect.top() - glow_pad
    height = rect.height() + glow_pad * 2
    if rtl:
        return QRectF(float(band_left), top, 1_000_000.0, height)
    return QRectF(
        -1_000_000.0,
        top,
        float(band_right) + 1_000_000.0,
        height,
    )


def before_glow_source_clip_rect(
    band: tuple[int, int],
    rect: QRectF,
    glow_pad: int,
    rtl: bool,
) -> QRectF:
    """Return the unsung-side source clip applied before Gaussian blur."""
    band_left, band_right = band
    top = rect.top() - glow_pad
    height = rect.height() + glow_pad * 2
    if rtl:
        return QRectF(
            -1_000_000.0,
            top,
            float(band_left) + 1_000_000.0,
            height,
        )
    return QRectF(float(band_right), top, 1_000_000.0, height)


def inflate_rect(rect: QRectF, pad: int | float) -> QRectF:
    pad_f = float(max(pad, 0))
    return rect.adjusted(-pad_f, -pad_f, pad_f, pad_f)


def glyph_run_layer_key(
    glyphs: list[GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
) -> tuple:
    """Return the position-independent cache key for one glyph-run layer."""
    run_left = min(glyph.left for glyph in glyphs)
    glyph_sig = tuple(
        (
            glyph.text,
            glyph.font.family(),
            glyph.font.pixelSize(),
            int(glyph.font.weight()),
            glyph.font.italic(),
            glyph.left - run_left,
            round(float(glyph.path_offset_x), 3),
            glyph.width,
            value_signature(glyph.vector_glyph),
        )
        for glyph in glyphs
    )
    state = colors.after if after else colors.before
    return (
        glyph_sig,
        karaoke_state_signature(state),
        role_style.shadow_offset_x,
        role_style.shadow_offset_y,
        role_style.stroke_width_px,
        main_stroke2_width(role_style),
        role_style.decoration_kind,
        glow_radius(role_style, after=False),
        glow_concentration_level(role_style),
        after,
    )


def relative_fill_rect_signature(
    glyphs: list[GlyphLayout],
    baseline_y: int,
    fill_rect: QRectF | None,
    *,
    global_anchor: bool = False,
) -> tuple[float, float, float, float] | None:
    """Return brush coordinates that affect a cached glyph run."""
    run_left = min(glyph.left for glyph in glyphs)
    if global_anchor:
        if fill_rect is None:
            return (
                round(float(run_left), 3),
                round(float(baseline_y), 3),
                0.0,
                0.0,
            )
        return (
            round(float(fill_rect.left()), 3),
            round(float(fill_rect.top()), 3),
            round(float(fill_rect.width()), 3),
            round(float(fill_rect.height()), 3),
        )
    if fill_rect is None:
        return None
    return (
        round(float(fill_rect.left()) - run_left, 3),
        round(float(fill_rect.top()) - baseline_y, 3),
        round(float(fill_rect.width()), 3),
        round(float(fill_rect.height()), 3),
    )


def karaoke_state_uses_image(state: KaraokeColorState) -> bool:
    return any(
        fill.mode == "image"
        for fill in (state.text, state.stroke, state.stroke2, state.shadow)
    )


def glyph_run_after_glow_key(
    glyphs: list[GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
) -> tuple:
    run_left = min(glyph.left for glyph in glyphs)
    glyph_sig = tuple(
        (
            glyph.text,
            glyph.font.family(),
            glyph.font.pixelSize(),
            int(glyph.font.weight()),
            glyph.font.italic(),
            glyph.left - run_left,
            round(float(glyph.path_offset_x), 3),
            glyph.width,
            value_signature(glyph.vector_glyph),
        )
        for glyph in glyphs
    )
    return (
        "after_glow",
        glyph_sig,
        fill_signature(colors.after.shadow),
        role_style.stroke_width_px,
        main_stroke2_width(role_style),
        glow_radius(role_style, after=True),
        glow_concentration_level(role_style),
        role_style.decoration_kind,
    )


def karaoke_glow_states_differ(style: Style, colors: KaraokeColors) -> bool:
    """Return whether before/after glow sources need split processing."""
    if style.decoration_kind != "glow":
        return False
    return (
        fill_signature(colors.before.shadow)
        != fill_signature(colors.after.shadow)
        or glow_radius(style, after=False) != glow_radius(style, after=True)
    )


def glyph_run_needs_after_glow(glyphs: list[GlyphLayout]) -> bool:
    if not glyphs:
        return False
    role_style = glyphs[0].style
    if glow_radius(role_style, after=True) == 0:
        return False
    return karaoke_glow_states_differ(
        role_style,
        effective_karaoke_colors(role_style),
    )


def glyph_run_needs_before_glow_split(glyphs: list[GlyphLayout]) -> bool:
    if not glyphs:
        return False
    role_style = glyphs[0].style
    if glow_radius(role_style, after=False) == 0:
        return False
    return karaoke_glow_states_differ(
        role_style,
        effective_karaoke_colors(role_style),
    )
