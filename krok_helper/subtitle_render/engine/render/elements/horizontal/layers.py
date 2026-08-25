"""Geometry and cache policy for horizontal render layers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Hashable

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QImage, QPainter

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingLine
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
from krok_helper.subtitle_render.engine.guide import (
    bitmap_guide_content_size,
    bitmap_guide_image,
    guide_symbol_is_bitmap,
)
from krok_helper.subtitle_render.engine.render.core.layers import (
    BakedLayer,
    LayerAnimation,
    LayerContext,
    SCOPE_LINE,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (
    FillSegment,
    LineCharTransition,
    LineLayout,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.layout import (
    bitmap_guide_anchor_descent,
    bitmap_guide_glyphs,
    glyph_is_bitmap_guide,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
    transition_char_state,
    utopia_following_done_time,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.engine.text import GlyphLayout
from krok_helper.subtitle_render.engine.value_signature import value_signature


@dataclass(frozen=True)
class BitmapGuidePorts:
    """Painter-owned sweep-band capabilities used by bitmap guide layers."""

    fill_clip_band: Callable[..., tuple[int, int] | None]
    fill_clip_band_for_glyphs: Callable[..., tuple[int, int] | None]
    n3_following_wipe_band: Callable[..., tuple[int, int] | None]


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


def bitmap_guide_target_rect(
    glyph: GlyphLayout,
    baseline_y: int,
) -> QRectF | None:
    symbol = glyph.vector_glyph
    if not guide_symbol_is_bitmap(symbol):
        return None
    width, height = bitmap_guide_content_size(symbol, glyph.style)
    left = float(glyph.left + int(symbol.bitmap_margin_left_px))
    bottom = (
        baseline_y
        + bitmap_guide_anchor_descent(glyph)
        - int(symbol.bitmap_margin_bottom_px)
    )
    top = float(bottom - height)
    return QRectF(left, top, float(max(width, 1)), float(max(height, 1)))


def paint_bitmap_guide_glyph(
    painter: QPainter,
    glyph: GlyphLayout,
    baseline_y: int,
    *,
    after: bool,
    band: tuple[int, int] | None,
    rtl: bool,
) -> None:
    symbol = glyph.vector_glyph
    if not guide_symbol_is_bitmap(symbol):
        return
    image_path = (
        symbol.bitmap_after_path
        if after and symbol.bitmap_after_path
        else symbol.bitmap_before_path
    )
    image = bitmap_guide_image(image_path)
    rect = bitmap_guide_target_rect(glyph, baseline_y)
    if image is None or rect is None or image.isNull():
        return
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if after and band is not None:
            painter.setClipRect(horizontal_after_clip_rect(band, rtl))
        painter.drawImage(rect, image)
    finally:
        painter.restore()


def bitmap_guide_band_for_segments(
    fill_segments: list[FillSegment],
    glyph: GlyphLayout,
    t_ms: int,
    rtl: bool,
    ports: BitmapGuidePorts,
) -> tuple[int, int] | None:
    band = ports.fill_clip_band_for_glyphs(
        fill_segments,
        [glyph],
        t_ms,
        rtl,
    )
    if band is None:
        band = ports.fill_clip_band(fill_segments, t_ms, rtl)
    following_band = ports.n3_following_wipe_band(
        fill_segments,
        {glyph.index},
        t_ms,
        rtl,
    )
    if following_band is not None:
        return following_band
    return band


def bitmap_guide_band_for_glyph(
    layout: LineLayout,
    glyph: GlyphLayout,
    t_ms: int,
    ports: BitmapGuidePorts,
) -> tuple[int, int] | None:
    return bitmap_guide_band_for_segments(
        layout.fill_segments,
        glyph,
        t_ms,
        layout.rtl,
        ports,
    )


def paint_bitmap_guide_glyphs(
    painter: QPainter,
    layout: LineLayout,
    t_ms: int,
    ports: BitmapGuidePorts,
    *,
    after: bool,
) -> None:
    for glyph in bitmap_guide_glyphs(layout.text_layout):
        band = bitmap_guide_band_for_glyph(layout, glyph, t_ms, ports)
        if after and band is None:
            continue
        paint_bitmap_guide_glyph(
            painter,
            glyph,
            layout.baseline_y,
            after=after,
            band=band,
            rtl=layout.rtl,
        )


def paint_bitmap_guide_transition_glyph(
    painter: QPainter,
    glyph: GlyphLayout,
    fill_segments: list[FillSegment],
    baseline_y: int,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
    t_ms: int,
    transition: LineCharTransition,
    style: Style,
    ports: BitmapGuidePorts,
    *,
    rtl: bool,
) -> None:
    if not glyph_is_bitmap_guide(glyph):
        return
    following_done_ms = (
        utopia_following_done_time(line, intervals, index, style)
        if transition.effect == "utopia"
        else None
    )
    char_start_ms = intervals[index][0] if index < len(intervals) else glyph.index
    char_end_ms = (
        intervals[index][1]
        if index < len(intervals)
        else char_start_ms
    )
    opacity = transition_char_state(
        style,
        transition,
        index,
        max(len(line.chars), 1),
        char_start_ms=char_start_ms,
        char_end_ms=char_end_ms,
        t_ms=t_ms,
        frame_height=painter.device().height(),
        following_done_ms=following_done_ms,
    )[0]
    if opacity <= 0.0:
        return
    band = bitmap_guide_band_for_segments(
        fill_segments,
        glyph,
        t_ms,
        rtl,
        ports,
    )
    painter.save()
    try:
        painter.setOpacity(painter.opacity() * opacity)
        paint_bitmap_guide_glyph(
            painter,
            glyph,
            baseline_y,
            after=False,
            band=band,
            rtl=rtl,
        )
        if band is not None:
            paint_bitmap_guide_glyph(
                painter,
                glyph,
                baseline_y,
                after=True,
                band=band,
                rtl=rtl,
            )
    finally:
        painter.restore()


@dataclass(frozen=True)
class BitmapGuideLayer:
    glyph: GlyphLayout
    baseline_y: int
    fill_segments: list
    t_ms: int
    rtl: bool
    after: bool
    ports: BitmapGuidePorts
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "BitmapGuideLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> None:
        return None

    def bake(
        self,
        ctx: LayerContext,
        layout: object,
        key: Hashable,
    ) -> BakedLayer:
        return BakedLayer(image=QImage(), offset=QPointF())

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation()

    def paint_dynamic(
        self,
        painter: QPainter,
        ctx: LayerContext,
        layout: object,
    ) -> None:
        band = bitmap_guide_band_for_segments(
            self.fill_segments,
            self.glyph,
            self.t_ms,
            self.rtl,
            self.ports,
        )
        if self.after and band is None:
            return
        paint_bitmap_guide_glyph(
            painter,
            self.glyph,
            self.baseline_y,
            after=self.after,
            band=band,
            rtl=self.rtl,
        )

    def vertical_bounds(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple[int, int] | None:
        rect = bitmap_guide_target_rect(self.glyph, self.baseline_y)
        if rect is None:
            return None
        return int(math.floor(rect.top())), int(math.ceil(rect.bottom()))
