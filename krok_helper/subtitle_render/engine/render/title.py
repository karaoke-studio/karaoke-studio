"""Title-overlay font selection and immutable layout contracts."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Hashable

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QFont, QFontMetrics, QImage, QPainter, QPainterPath

from krok_helper.subtitle_render.engine.render.layers import (
    BakedLayer,
    LayerAnimation,
    LayerCompositor,
    LayerContext,
    SCOPE_LINE,
)
from krok_helper.subtitle_render.engine.style.title_semantics import (
    resolve_title_overlay,
    resolve_title_role_overlay,
    resolve_title_text,
)
from krok_helper.subtitle_render.engine.text import (
    clamp_weight,
    n3_char_box_ascent,
    n3_char_box_descent,
)
from krok_helper.subtitle_render.models import (
    Style,
    TitleOverlay,
    normalize_title_char_role_labels,
)
from krok_helper.subtitle_render.n3.font_catalog import resolve_qt_font_family
from krok_helper.subtitle_render.paint import PaintFill
from krok_helper.subtitle_render.timing import TimingTrack


@dataclass(frozen=True)
class TitleGlyphLayout:
    text: str
    x: float
    advance: float
    font: QFont
    metrics: QFontMetrics
    title: TitleOverlay


@dataclass(frozen=True)
class TitleOverlayLayout:
    """Time-independent geometry for one title overlay."""

    lines: list[str]
    widths: list[float]
    block_w: float
    block_h: float
    line_h: float
    gap: int
    x0: float
    y_top: float
    font: QFont
    metrics: QFontMetrics
    latin_font: QFont
    latin_metrics: QFontMetrics
    font_for: Callable[[str], QFont] | None
    glyph_rows: list[list[TitleGlyphLayout]]
    line_heights: list[float]
    line_ascents: list[float]


@dataclass(frozen=True)
class TitleRenderPorts:
    """Painter-backend services consumed by title-layer rasterization."""

    fill_signature: Callable[[PaintFill], tuple]
    make_raster_image: Callable[[int, int, float], QImage]
    paint_text_stack: Callable[[QPainter, QPainterPath, QRectF, TitleOverlay], None]
    raster_scale_key: Callable[[float], int]
    visual_padding: Callable[[TitleOverlay], int]


def build_title_font(title: TitleOverlay) -> QFont:
    font = QFont(
        resolve_qt_font_family(title.font_family),
        max(title.font_size_px, 1),
    )
    font.setPixelSize(max(title.font_size_px, 1))
    font.setWeight(clamp_weight(title.font_weight))
    font.setItalic(title.italic)
    return font


def build_title_latin_font(title: TitleOverlay) -> QFont:
    family = title.font_family_latin or title.font_family
    font = QFont(resolve_qt_font_family(family), max(title.font_size_px, 1))
    font.setPixelSize(max(title.font_size_px, 1))
    font.setWeight(clamp_weight(title.font_weight))
    font.setItalic(title.italic)
    return font


def make_title_font_for(
    title: TitleOverlay,
    jp_font: QFont,
    latin_font: QFont,
) -> Callable[[str], QFont] | None:
    if not title.font_family_latin or latin_font.family() == jp_font.family():
        return None

    def font_for(text: str) -> QFont:
        return latin_font if (text and text.isascii()) else jp_font

    return font_for


def title_block_origin(
    img_w: int,
    img_h: int,
    block_w: float,
    block_h: float,
    title: TitleOverlay,
    *,
    edge_px: float = 0.0,
) -> tuple[float, float]:
    """Place a title block on its nine-grid anchor."""
    anchor = title.anchor
    half_edge = max(float(edge_px), 0.0) / 2.0
    if anchor.endswith("left"):
        x0 = title.offset_x + half_edge
    elif anchor.endswith("right"):
        x0 = img_w - block_w - title.offset_x - half_edge
    else:
        x0 = (img_w - block_w) / 2.0 + title.offset_x
    if anchor.startswith("top"):
        y_top = float(title.offset_y)
    elif anchor.startswith("bottom"):
        y_top = img_h - block_h - title.offset_y
    else:
        y_top = (img_h - block_h) / 2.0 + title.offset_y
    return x0, y_top


def layout_title_overlay(
    img_w: int,
    img_h: int,
    track: TimingTrack,
    title: TitleOverlay,
    *,
    style: Style | None = None,
) -> TitleOverlayLayout | None:
    text = resolve_title_text(title, track)
    lines = text.split("\n")
    if not any(line.strip() for line in lines):
        return None
    font = build_title_font(title)
    metrics = QFontMetrics(font)
    latin_font = build_title_latin_font(title)
    font_for = make_title_font_for(title, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    labels = normalize_title_char_role_labels(text, title.char_role_labels)
    title_space_percent = max(
        10,
        min(
            int(style.space_width_percent if style is not None else Style.space_width_percent),
            100,
        ),
    )
    glyph_rows: list[list[TitleGlyphLayout]] = []
    widths: list[float] = []
    line_heights: list[float] = []
    line_ascents: list[float] = []
    max_edge = 0.0
    fallback_ascent = n3_char_box_ascent(
        metrics,
        title.font_size_px,
        title.stroke_width_px,
    )
    fallback_descent = n3_char_box_descent(
        metrics,
        title.font_size_px,
        title.stroke_width_px,
    )
    for row_index, text_line in enumerate(lines):
        glyphs: list[TitleGlyphLayout] = []
        cursor = 0.0
        max_ascent = 0.0
        max_descent = 0.0
        for char_index, char in enumerate(text_line):
            glyph_title = (
                resolve_title_role_overlay(
                    style,
                    title,
                    labels[row_index][char_index],
                )
                if style is not None
                else title
            )
            glyph_jp_font = build_title_font(glyph_title)
            glyph_latin_font = build_title_latin_font(glyph_title)
            glyph_font_for = make_title_font_for(
                glyph_title,
                glyph_jp_font,
                glyph_latin_font,
            )
            glyph_font = (
                glyph_font_for(char) if glyph_font_for is not None else glyph_jp_font
            )
            glyph_metrics = QFontMetrics(glyph_font)
            if char == " ":
                space_unit = glyph_font.pixelSize()
                if space_unit <= 0:
                    space_unit = max(int(glyph_title.font_size_px), 1)
                advance = float(space_unit * title_space_percent // 100)
            else:
                advance = float(glyph_metrics.horizontalAdvance(char))
            glyphs.append(
                TitleGlyphLayout(
                    text=char,
                    x=cursor,
                    advance=advance,
                    font=glyph_font,
                    metrics=glyph_metrics,
                    title=glyph_title,
                )
            )
            cursor += advance
            if char_index + 1 < len(text_line):
                cursor += int(glyph_title.letter_spacing_px)
            max_ascent = max(
                max_ascent,
                n3_char_box_ascent(
                    glyph_metrics,
                    glyph_title.font_size_px,
                    glyph_title.stroke_width_px,
                ),
            )
            max_descent = max(
                max_descent,
                n3_char_box_descent(
                    glyph_metrics,
                    glyph_title.font_size_px,
                    glyph_title.stroke_width_px,
                ),
            )
            max_edge = max(max_edge, float(max(glyph_title.stroke_width_px, 0)))
        if not glyphs:
            max_ascent = fallback_ascent
            max_descent = fallback_descent
        glyph_rows.append(glyphs)
        widths.append(cursor)
        line_ascents.append(max_ascent)
        line_heights.append(max_ascent + max_descent)
    block_w = max(widths) if widths else 0.0
    line_h = max(line_heights, default=metrics.height())
    gap = max(int(title.line_gap_px), 0)
    block_h = sum(line_heights) + gap * max(len(lines) - 1, 0)
    if block_w <= 0 or block_h <= 0:
        return None

    x0, y_top = title_block_origin(
        img_w,
        img_h,
        block_w,
        block_h,
        title,
        edge_px=max_edge,
    )
    return TitleOverlayLayout(
        lines=lines,
        widths=widths,
        block_w=block_w,
        block_h=float(block_h),
        line_h=line_h,
        gap=gap,
        x0=x0,
        y_top=y_top,
        font=font,
        metrics=metrics,
        latin_font=latin_font,
        latin_metrics=latin_metrics,
        font_for=font_for,
        glyph_rows=glyph_rows,
        line_heights=line_heights,
        line_ascents=line_ascents,
    )


def title_overlay_layer_key(
    layout: TitleOverlayLayout,
    title: TitleOverlay,
    *,
    fill_signature: Callable[[PaintFill], tuple],
) -> tuple:
    return (
        tuple(layout.lines),
        tuple(round(width, 3) for width in layout.widths),
        round(layout.block_w, 3),
        round(layout.block_h, 3),
        round(layout.line_h, 3),
        layout.gap,
        title.align,
        layout.font.family(),
        layout.font.pixelSize(),
        int(layout.font.weight()),
        layout.font.italic(),
        layout.latin_font.family(),
        layout.latin_font.pixelSize(),
        int(layout.latin_font.weight()),
        layout.latin_font.italic(),
        title.letter_spacing_px,
        fill_signature(title.fill),
        fill_signature(title.stroke),
        title.stroke_width_px,
        fill_signature(title.stroke2),
        title.stroke2_width_px,
        title.decoration_kind,
        title.glow_radius_px,
        title.glow_concentration_level,
        fill_signature(title.shadow),
        title.shadow_offset_x,
        title.shadow_offset_y,
        tuple(
            (
                glyph.text,
                round(glyph.x, 3),
                round(glyph.advance, 3),
                glyph.font.family(),
                glyph.font.pixelSize(),
                int(glyph.font.weight()),
                glyph.font.italic(),
                fill_signature(glyph.title.fill),
                fill_signature(glyph.title.stroke),
                glyph.title.stroke_width_px,
                fill_signature(glyph.title.stroke2),
                glyph.title.stroke2_width_px,
                glyph.title.decoration_kind,
                glyph.title.glow_radius_px,
                glyph.title.glow_concentration_level,
                fill_signature(glyph.title.shadow),
                glyph.title.shadow_offset_x,
                glyph.title.shadow_offset_y,
            )
            for row in layout.glyph_rows
            for glyph in row
        ),
    )


def build_title_overlay_layer(
    layout: TitleOverlayLayout,
    title: TitleOverlay,
    *,
    ports: TitleRenderPorts,
    device_pixel_ratio: float = 1.0,
) -> tuple[QImage, int, int]:
    glyph_titles = [glyph.title for row in layout.glyph_rows for glyph in row] or [
        title
    ]
    extent = max(ports.visual_padding(item) for item in glyph_titles) + 4
    pad_left = max(max(0, -item.shadow_offset_x) for item in glyph_titles) + extent
    pad_right = max(max(0, item.shadow_offset_x) for item in glyph_titles) + extent
    pad_top = max(max(0, -item.shadow_offset_y) for item in glyph_titles) + extent
    pad_bottom = max(max(0, item.shadow_offset_y) for item in glyph_titles) + extent
    img_w = max(int(math.ceil(pad_left + layout.block_w + pad_right)), 1)
    img_h = max(int(math.ceil(pad_top + layout.block_h + pad_bottom)), 1)
    image = ports.make_raster_image(img_w, img_h, device_pixel_ratio)
    image.fill(0)

    painter = QPainter(image)
    try:
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        line_top = float(pad_top)
        for glyphs, width, line_height, line_ascent in zip(
            layout.glyph_rows,
            layout.widths,
            layout.line_heights,
            layout.line_ascents,
        ):
            if glyphs:
                if title.align == "center":
                    line_x = pad_left + (layout.block_w - width) / 2.0
                elif title.align == "right":
                    line_x = pad_left + (layout.block_w - width)
                else:
                    line_x = float(pad_left)
                baseline = line_top + line_ascent
                run_start = 0
                while run_start < len(glyphs):
                    run_end = run_start + 1
                    run_title = glyphs[run_start].title
                    while run_end < len(glyphs) and glyphs[run_end].title == run_title:
                        run_end += 1
                    run = glyphs[run_start:run_end]
                    path = QPainterPath()
                    for glyph in run:
                        path.addText(
                            float(line_x + glyph.x),
                            baseline,
                            glyph.font,
                            glyph.text,
                        )
                    left = float(line_x + run[0].x)
                    right = float(line_x + run[-1].x + run[-1].advance)
                    ascent = max(glyph.metrics.ascent() for glyph in run)
                    descent = max(glyph.metrics.descent() for glyph in run)
                    rect = QRectF(
                        left,
                        float(baseline - ascent),
                        max(right - left, 1.0),
                        float(ascent + descent),
                    )
                    ports.paint_text_stack(painter, path, rect, run_title)
                    run_start = run_end
            line_top += line_height + layout.gap
    finally:
        painter.end()
    return image, -pad_left, -pad_top


@dataclass(frozen=True)
class TitleOverlayLayer:
    """Layer-compositor adapter for one static title overlay block."""

    title_layout: TitleOverlayLayout
    title: TitleOverlay
    opacity: float
    ports: TitleRenderPorts = field(repr=False, compare=False)
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> TitleOverlayLayer:
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple:
        return (
            *title_overlay_layer_key(
                self.title_layout,
                self.title,
                fill_signature=self.ports.fill_signature,
            ),
            self.ports.raster_scale_key(ctx.device_pixel_ratio),
        )

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        image, dx, dy = build_title_overlay_layer(
            self.title_layout,
            self.title,
            ports=self.ports,
            device_pixel_ratio=ctx.device_pixel_ratio,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation(
            top_left=QPointF(float(self.title_layout.x0), float(self.title_layout.y_top)),
            opacity=max(0.0, min(1.0, self.opacity)),
        )

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        return

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int]:
        pad = max(
            (
                self.ports.visual_padding(glyph.title)
                for row in self.title_layout.glyph_rows
                for glyph in row
            ),
            default=self.ports.visual_padding(self.title),
        )
        return (
            int(math.floor(self.title_layout.y_top - pad)),
            int(math.ceil(self.title_layout.y_top + self.title_layout.block_h + pad)),
        )


def make_title_overlay_layer(
    layout: TitleOverlayLayout,
    title: TitleOverlay,
    opacity: float,
    *,
    ports: TitleRenderPorts,
) -> TitleOverlayLayer:
    return TitleOverlayLayer(layout, title, opacity, ports)


def paint_title_overlay(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    style: Style,
    opacity: float,
    *,
    compositor: LayerCompositor,
    ports: TitleRenderPorts,
) -> None:
    title = resolve_title_overlay(style)
    if title is None:
        return
    layout = layout_title_overlay(img_w, img_h, track, title, style=style)
    if layout is None:
        return
    compositor.paint_ordered(
        painter,
        LayerContext(t_ms=0, logical_w=img_w, logical_h=img_h),
        [make_title_overlay_layer(layout, title, opacity, ports=ports)],
    )


__all__ = [
    "TitleGlyphLayout",
    "TitleOverlayLayout",
    "TitleOverlayLayer",
    "TitleRenderPorts",
    "build_title_overlay_layer",
    "build_title_font",
    "build_title_latin_font",
    "layout_title_overlay",
    "make_title_font_for",
    "make_title_overlay_layer",
    "paint_title_overlay",
    "title_block_origin",
    "title_overlay_layer_key",
]
