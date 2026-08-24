"""Vertical subtitle glyph geometry and time-independent line layout."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Hashable, Protocol

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import (
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QTransform,
)

from krok_helper.subtitle_render.engine.guide import guide_symbol_is_bitmap
from krok_helper.subtitle_render.engine.layout.line_style import lane_count
from krok_helper.subtitle_render.engine.render.layers import (
    BakedLayer,
    LayerAnimation,
    LayerCompositor,
    LayerContext,
    SCOPE_LINE,
)
from krok_helper.subtitle_render.engine.ruby import (
    active_rubies_for_line,
    build_ruby_font,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.engine.text import (
    build_font,
    build_latin_font,
    is_emoji_text,
    latin_font_size,
    latin_font_weight,
    make_font_for,
)
from krok_helper.subtitle_render.engine.timing.timeline import (
    DisplayLine,
    char_fill_ratio,
    compute_char_intervals,
)
from krok_helper.subtitle_render.engine.value_signature import value_signature
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.paint import KaraokeColors, KaraokeColorState
from krok_helper.subtitle_render.sources.guide_symbols import scaled_guide_symbol_path
from krok_helper.subtitle_render.timing import RubyAnnotation, TimingLine, TimingTrack


VERTICAL_REFERENCE_CHAR = "永"
VERTICAL_ROTATE_CHARS = set(
    "ーｰ"
    "—―‐‑‒–"
    "〜～"
    "→←"
    "（）()"
    "「」『』"
    "【】〔〕"
    "［］｛｝"
    "〈〉《》"
    "[]{}<>"
)
VERTICAL_CORNER_PUNCT = set("、。，．")
VERTICAL_SMALL_KANA = set(
    "ぁぃぅぇぉっゃゅょゎ"
    "ァィゥェォッャュョヮ"
    "ヵヶ"
)


@dataclass(frozen=True)
class VerticalLineLayout:
    """Time-independent geometry for one vertical subtitle line."""

    font: QFont
    metrics: QFontMetrics
    cell_w: int
    cell_h: int
    ascent: int
    column_x: int
    y_top: int
    block_h: int
    intervals: list[tuple[int, int]]
    cells: list[tuple[int, int]]
    line_rect: QRectF
    text_path: QPainterPath
    colors: KaraokeColors
    active_rubies: list[RubyAnnotation]


class ResolveCharRubyGroups(Protocol):
    def __call__(
        self,
        rubies: list[RubyAnnotation],
        line: TimingLine,
        intervals: list[tuple[int, int]],
    ) -> object: ...


class CharacterFillRatio(Protocol):
    def __call__(
        self,
        line: TimingLine,
        intervals: list[tuple[int, int]],
        fill_ranges: list[tuple[int, int]],
        active_rubies: list[RubyAnnotation],
        index: int,
        t_ms: int,
        *,
        groups: object,
        ruby_main_progress_mode: str,
    ) -> float: ...


@dataclass(frozen=True)
class VerticalProgressPorts:
    resolve_char_ruby_groups: ResolveCharRubyGroups
    character_fill_ratio: CharacterFillRatio


@dataclass(frozen=True)
class VerticalRasterPorts:
    paint_text_layer_stack: Callable[..., None]
    visual_stroke_extent: Callable[[int, int], int]
    glow_extent: Callable[[int, int, int], int]


@dataclass(frozen=True)
class VerticalCachePorts:
    karaoke_state_signature: Callable[[KaraokeColorState], tuple]


class GlowRadiusResolver(Protocol):
    def __call__(self, style: Style, *, after: bool) -> int: ...


@dataclass(frozen=True)
class VerticalLayerPorts:
    progress: VerticalProgressPorts
    raster: VerticalRasterPorts
    cache: VerticalCachePorts
    main_stroke2_width: Callable[[Style], int]
    glow_radius: GlowRadiusResolver
    ruby_layers: Callable[[VerticalLineLayout, TimingLine, int, Style], list]


def vertical_orientation(char: str) -> str:
    """Return ``R`` for simplified UTR#50 rotation, otherwise ``U``."""
    return "R" if char in VERTICAL_ROTATE_CHARS else "U"


def vertical_glyph_offset(
    char: str,
    cell_w: int,
    cell_h: int,
) -> tuple[float, float]:
    if char in VERTICAL_CORNER_PUNCT:
        return cell_w * 0.28, -cell_h * 0.28
    if char in VERTICAL_SMALL_KANA:
        return cell_w * 0.10, -cell_h * 0.10
    return 0.0, 0.0


def vertical_glyph_path(
    text: str,
    font: QFont,
    metrics: QFontMetrics,
    column_x: int,
    cell_top: int,
    cell_w: int,
    cell_h: int,
    ascent: int,
    *,
    vector_glyph=None,
) -> QPainterPath:
    """Build one centered vertical glyph path, including guide symbols."""
    if vector_glyph is not None:
        if guide_symbol_is_bitmap(vector_glyph):
            return QPainterPath()
        path = scaled_guide_symbol_path(
            vector_glyph,
            pixel_size=max(int(font.pixelSize()), 1),
            left=float(column_x - max(int(font.pixelSize()), 1) / 2),
            baseline_y=float(cell_top + ascent),
        )
        bounds = path.boundingRect()
        return QTransform.fromTranslate(
            float(column_x) - bounds.center().x(),
            float(cell_top + cell_h / 2) - bounds.center().y(),
        ).map(path)
    advance = metrics.horizontalAdvance(text)
    baseline = cell_top + ascent
    glyph_x = column_x - advance / 2
    path = QPainterPath()
    if vertical_orientation(text) == "R":
        path.addText(float(glyph_x), float(baseline), font, text)
        center_x = float(column_x)
        center_y = float(cell_top + cell_h / 2)
        transform = QTransform()
        transform.translate(center_x, center_y)
        transform.rotate(90)
        transform.translate(-center_x, -center_y)
        return transform.map(path)
    dx, dy = vertical_glyph_offset(text, cell_w, cell_h)
    path.addText(float(glyph_x + dx), float(baseline + dy), font, text)
    return path


def vertical_cell_width(metrics: QFontMetrics) -> int:
    width = metrics.horizontalAdvance(VERTICAL_REFERENCE_CHAR)
    if width <= 0:
        width = metrics.height()
    return max(width, 1)


def vertical_ruby_allowance(track: TimingTrack, style: Style) -> int:
    if not track.rubies:
        return 0
    ruby_metrics = QFontMetrics(build_ruby_font(style))
    return max(ruby_metrics.height() + int(style.ruby_gap_px), 0)


def resolve_vertical_columns(
    img_w: int,
    track: TimingTrack,
    display_lines: list[DisplayLine],
    style: Style,
) -> dict[int, int]:
    metrics = QFontMetrics(build_font(style))
    cell_w = vertical_cell_width(metrics)
    margin = style.line_y_margin_px
    gap = max(style.line_gap_px, 0)
    ruby_w = vertical_ruby_allowance(track, style)
    right_center = img_w - margin - ruby_w - cell_w / 2
    max_lane = max((item.lane for item in display_lines), default=0)
    return {
        lane: int(round(right_center - lane * (cell_w + ruby_w + gap)))
        for lane in range(max(lane_count(style), max_lane + 1))
    }


def resolve_vertical_top(img_h: int, block_h: int, style: Style) -> int:
    margin = style.line_y_margin_px
    if style.line_y_position == "top":
        return margin
    if style.line_y_position == "center":
        return max((img_h - block_h) // 2, 0)
    return img_h - margin - block_h


def vertical_after_clip_rect(
    column_x: int,
    cell_w: int,
    y0: int,
    y_scan: int,
    pad: int,
) -> QRectF:
    return QRectF(
        float(column_x - cell_w / 2 - pad),
        float(y0 - pad),
        float(cell_w + pad * 2),
        float((y_scan - y0) + pad),
    )


def vertical_before_clip_rect(
    column_x: float,
    cell_w: float,
    y_scan: float,
    pad: int,
) -> QRectF:
    """Return the complementary unsung layer below the vertical wipe."""
    return QRectF(
        float(column_x - cell_w / 2 - pad),
        float(y_scan),
        float(cell_w + pad * 2),
        1_000_000.0,
    )


def vertical_fill_band(
    cells: list[tuple[int, int]],
    intervals: list[tuple[int, int]],
    t_ms: int,
    *,
    ports: VerticalProgressPorts,
    line: TimingLine | None = None,
    active_rubies: list[RubyAnnotation] | None = None,
    ruby_main_progress_mode: str = "checkpoint_segments",
) -> tuple[int, int] | None:
    """Return the sung vertical band ``(y_top, y_scan)``."""
    if not cells:
        return None
    y_top = cells[0][0]
    scan = float(y_top)
    ruby_groups = (
        ports.resolve_char_ruby_groups(active_rubies, line, intervals)
        if ruby_main_progress_mode == "reading_units"
        and line is not None
        and active_rubies
        else None
    )
    for index, ((cell_top, cell_bottom), (start, end)) in enumerate(
        zip(cells, intervals)
    ):
        ratio = (
            ports.character_fill_ratio(
                line,
                intervals,
                cells,
                active_rubies or [],
                index,
                t_ms,
                groups=ruby_groups,
                ruby_main_progress_mode=ruby_main_progress_mode,
            )
            if ruby_groups is not None and line is not None
            else char_fill_ratio(start, end, t_ms)
        )
        if ratio <= 0.0:
            break
        if ratio >= 1.0:
            scan = cell_bottom
            continue
        scan = cell_top + (cell_bottom - cell_top) * ratio
        break
    if scan <= y_top:
        return None
    return y_top, int(round(scan))


def build_baked_path_stack(
    path: QPainterPath,
    rect: QRectF,
    state: KaraokeColorState,
    style: Style,
    *,
    ports: VerticalRasterPorts,
    stroke_width: int,
    stroke2_width: int,
    shadow_dx: int,
    shadow_dy: int,
    glow_radius: int,
) -> tuple[QImage, int, int] | None:
    is_glow = style.decoration_kind == "glow"
    stroke_extent = ports.visual_stroke_extent(stroke_width, stroke2_width)
    glow_extra = (
        ports.glow_extent(stroke_width, stroke2_width, glow_radius)
        if is_glow
        else 0
    )
    extent = max(stroke_extent, glow_extra, 0) + 4
    pad_left = max(0, -shadow_dx) + extent
    pad_right = max(0, shadow_dx) + extent
    pad_top = max(0, -shadow_dy) + extent
    pad_bottom = max(0, shadow_dy) + extent
    path_bounds = path.boundingRect()
    if path_bounds.isEmpty():
        return None
    left = math.floor(path_bounds.left())
    top = math.floor(path_bounds.top())
    right = math.ceil(path_bounds.right())
    bottom = math.ceil(path_bounds.bottom())
    img_w = max((right - left) + pad_left + pad_right, 1)
    img_h = max((bottom - top) + pad_top + pad_bottom, 1)
    offset_x = left - pad_left
    offset_y = top - pad_top
    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        painter.translate(-offset_x, -offset_y)
        ports.paint_text_layer_stack(
            painter,
            path,
            rect,
            state,
            style,
            stroke_width=stroke_width,
            stroke2_width=stroke2_width,
            shadow_dx=shadow_dx,
            shadow_dy=shadow_dy,
            glow_radius=glow_radius,
        )
    finally:
        painter.end()
    return image, offset_x, offset_y


@dataclass(frozen=True)
class BakedPathStackLayer:
    path: QPainterPath
    rect: QRectF
    state: KaraokeColorState
    style: Style
    cache_key: tuple
    stroke_width: int
    stroke2_width: int
    shadow_dx: int
    shadow_dy: int
    glow_radius: int
    ports: VerticalRasterPorts = field(repr=False, compare=False)
    clip_rect: QRectF | None = None
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> BakedPathStackLayer:
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple:
        return self.cache_key

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        built = build_baked_path_stack(
            self.path,
            self.rect,
            self.state,
            self.style,
            ports=self.ports,
            stroke_width=self.stroke_width,
            stroke2_width=self.stroke2_width,
            shadow_dx=self.shadow_dx,
            shadow_dy=self.shadow_dy,
            glow_radius=self.glow_radius,
        )
        if built is None:
            return BakedLayer(image=QImage(), offset=QPointF())
        image, offset_x, offset_y = built
        return BakedLayer(
            image=image,
            offset=QPointF(float(offset_x), float(offset_y)),
        )

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation(top_left=QPointF(0.0, 0.0), clip_rect=self.clip_rect)

    def paint_dynamic(
        self,
        painter: QPainter,
        ctx: LayerContext,
        layout: object,
    ) -> None:
        return

    def vertical_bounds(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple[int, int] | None:
        path_bounds = self.path.boundingRect()
        if path_bounds.isEmpty():
            return None
        is_glow = self.style.decoration_kind == "glow"
        extent = max(
            self.ports.visual_stroke_extent(
                self.stroke_width,
                self.stroke2_width,
            ),
            self.ports.glow_extent(
                self.stroke_width,
                self.stroke2_width,
                self.glow_radius,
            )
            if is_glow
            else 0,
            abs(self.shadow_dy),
            0,
        ) + 4
        top = int(math.floor(path_bounds.top())) - extent
        bottom = int(math.ceil(path_bounds.bottom())) + extent
        if self.clip_rect is not None:
            top = max(top, int(math.floor(self.clip_rect.top())))
            bottom = min(bottom, int(math.ceil(self.clip_rect.bottom())))
        if bottom < top:
            return None
        return top, bottom


def vertical_main_path_signature(
    line: TimingLine,
    style: Style,
    layout: VerticalLineLayout,
) -> tuple:
    return (
        "vmain",
        tuple(char.text for char in line.chars),
        tuple(value_signature(char.vector_glyph) for char in line.chars),
        style.font_family,
        style.font_family_latin,
        style.font_size_px,
        latin_font_size(style),
        int(style.font_weight),
        latin_font_weight(style),
        style.italic,
        layout.column_x,
        layout.y_top,
        layout.cell_w,
        layout.cell_h,
        layout.ascent,
    )


def baked_stack_key(
    path_signature: tuple,
    rect: QRectF,
    state: KaraokeColorState,
    style: Style,
    *,
    ports: VerticalCachePorts,
    stroke_width: int,
    stroke2_width: int,
    shadow_dx: int,
    shadow_dy: int,
    glow_radius: int,
    after: bool,
) -> tuple:
    return (
        path_signature,
        int(round(rect.left())),
        int(round(rect.top())),
        int(round(rect.width())),
        int(round(rect.height())),
        ports.karaoke_state_signature(state),
        style.decoration_kind,
        stroke_width,
        stroke2_width,
        shadow_dx,
        shadow_dy,
        glow_radius,
        after,
    )


def vertical_after_clip_pad(style: Style, *, ports: VerticalLayerPorts) -> int:
    stroke2_width = ports.main_stroke2_width(style)
    stroke_extent = ports.raster.visual_stroke_extent(
        style.stroke_width_px,
        stroke2_width,
    )
    return max(
        stroke_extent,
        ports.raster.glow_extent(
            style.stroke_width_px,
            stroke2_width,
            ports.glow_radius(style, after=True),
        )
        if style.decoration_kind == "glow"
        else 0,
        stroke_extent + abs(style.shadow_offset_x),
        stroke_extent + abs(style.shadow_offset_y),
        2,
    )


def vertical_before_clip_pad(
    stroke_width: int,
    stroke2_width: int,
    before_glow_radius: int,
    shadow_dx: int,
    shadow_dy: int,
    *,
    ports: VerticalLayerPorts,
) -> int:
    stroke_extent = ports.raster.visual_stroke_extent(
        stroke_width,
        stroke2_width,
    )
    return max(
        stroke_extent,
        ports.raster.glow_extent(
            stroke_width,
            stroke2_width,
            before_glow_radius,
        ),
        stroke_extent + abs(shadow_dx),
        stroke_extent + abs(shadow_dy),
        2,
    )


def vertical_layer_stack(
    layout: VerticalLineLayout,
    line: TimingLine,
    t_ms: int,
    style: Style,
    *,
    ports: VerticalLayerPorts,
) -> list:
    layers: list = []
    stroke2_width = ports.main_stroke2_width(style)
    main_signature = vertical_main_path_signature(line, style, layout)
    band = vertical_fill_band(
        layout.cells,
        layout.intervals,
        t_ms,
        ports=ports.progress,
        line=line,
        active_rubies=layout.active_rubies,
        ruby_main_progress_mode=style.ruby_main_progress_mode,
    )
    before_glow_radius = ports.glow_radius(style, after=False)
    before_clip = None
    if (
        band is not None
        and style.decoration_kind == "glow"
        and before_glow_radius > 0
    ):
        before_clip = vertical_before_clip_rect(
            layout.column_x,
            layout.cell_w,
            band[1],
            vertical_before_clip_pad(
                style.stroke_width_px,
                stroke2_width,
                before_glow_radius,
                style.shadow_offset_x,
                style.shadow_offset_y,
                ports=ports,
            ),
        )
    layers.append(
        BakedPathStackLayer(
            path=layout.text_path,
            rect=layout.line_rect,
            state=layout.colors.before,
            style=style,
            cache_key=baked_stack_key(
                main_signature,
                layout.line_rect,
                layout.colors.before,
                style,
                ports=ports.cache,
                stroke_width=style.stroke_width_px,
                stroke2_width=stroke2_width,
                shadow_dx=style.shadow_offset_x,
                shadow_dy=style.shadow_offset_y,
                glow_radius=before_glow_radius,
                after=False,
            ),
            stroke_width=style.stroke_width_px,
            stroke2_width=stroke2_width,
            shadow_dx=style.shadow_offset_x,
            shadow_dy=style.shadow_offset_y,
            glow_radius=before_glow_radius,
            ports=ports.raster,
            clip_rect=before_clip,
            z_index=0,
        )
    )
    if band is not None:
        y0, y_scan = band
        after_glow_radius = ports.glow_radius(style, after=True)
        layers.append(
            BakedPathStackLayer(
                path=layout.text_path,
                rect=layout.line_rect,
                state=layout.colors.after,
                style=style,
                cache_key=baked_stack_key(
                    main_signature,
                    layout.line_rect,
                    layout.colors.after,
                    style,
                    ports=ports.cache,
                    stroke_width=style.stroke_width_px,
                    stroke2_width=stroke2_width,
                    shadow_dx=style.shadow_offset_x,
                    shadow_dy=style.shadow_offset_y,
                    glow_radius=after_glow_radius,
                    after=True,
                ),
                stroke_width=style.stroke_width_px,
                stroke2_width=stroke2_width,
                shadow_dx=style.shadow_offset_x,
                shadow_dy=style.shadow_offset_y,
                glow_radius=after_glow_radius,
                ports=ports.raster,
                clip_rect=vertical_after_clip_rect(
                    layout.column_x,
                    layout.cell_w,
                    y0,
                    y_scan,
                    vertical_after_clip_pad(style, ports=ports),
                ),
                z_index=1,
            )
        )
    if layout.active_rubies:
        layers.extend(ports.ruby_layers(layout, line, t_ms, style))
    return layers


def paint_line_vertical_layers(
    painter: QPainter,
    layout: VerticalLineLayout,
    line: TimingLine,
    t_ms: int,
    style: Style,
    *,
    compositor: LayerCompositor,
    ports: VerticalLayerPorts,
) -> None:
    layers = vertical_layer_stack(layout, line, t_ms, style, ports=ports)
    if not layers:
        return
    compositor.paint_ordered(
        painter,
        LayerContext(t_ms=t_ms, logical_w=0, logical_h=0),
        layers,
    )


def layout_vertical_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    *,
    column_x: int | None,
    source_line: TimingLine | None = None,
    resolved_intervals: tuple[tuple[int, int], ...] | None = None,
) -> VerticalLineLayout | None:
    chars = line.chars
    if not chars:
        return None
    font = build_font(style)
    metrics = QFontMetrics(font)
    latin_font = build_latin_font(style)
    font_for = make_font_for(style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    cell_w = vertical_cell_width(metrics)
    cell_h = metrics.height()
    ascent = metrics.ascent()
    resolved_column_x = (
        column_x
        if column_x is not None
        else int(round(img_w - style.line_y_margin_px - cell_w / 2))
    )
    block_h = cell_h * len(chars)
    y_top = resolve_vertical_top(img_h, block_h, style)
    intervals = (
        list(resolved_intervals)
        if resolved_intervals is not None
        else compute_char_intervals(line)
    )
    text_path = QPainterPath()
    cells: list[tuple[int, int]] = []
    for index, char in enumerate(chars):
        cell_top = y_top + index * cell_h
        cells.append((cell_top, cell_top + cell_h))
        glyph_font = font_for(char.text) if font_for is not None else font
        glyph_metrics = (
            QFontMetrics(glyph_font)
            if is_emoji_text(char.text)
            else latin_metrics
            if (font_for is not None and char.text and char.text.isascii())
            else metrics
        )
        text_path.addPath(
            vertical_glyph_path(
                char.text,
                glyph_font,
                glyph_metrics,
                resolved_column_x,
                cell_top,
                cell_w,
                cell_h,
                ascent,
                vector_glyph=char.vector_glyph,
            )
        )
    line_rect = QRectF(
        float(resolved_column_x - cell_w / 2),
        float(y_top),
        float(cell_w),
        float(block_h),
    )
    return VerticalLineLayout(
        font=font,
        metrics=metrics,
        cell_w=cell_w,
        cell_h=cell_h,
        ascent=ascent,
        column_x=resolved_column_x,
        y_top=y_top,
        block_h=block_h,
        intervals=intervals,
        cells=cells,
        line_rect=line_rect,
        text_path=text_path,
        colors=effective_karaoke_colors(style),
        active_rubies=active_rubies_for_line(track.rubies, source_line or line),
    )


__all__ = [
    "BakedPathStackLayer",
    "VerticalCachePorts",
    "VerticalLayerPorts",
    "VerticalLineLayout",
    "VerticalProgressPorts",
    "VerticalRasterPorts",
    "build_baked_path_stack",
    "baked_stack_key",
    "layout_vertical_line",
    "paint_line_vertical_layers",
    "resolve_vertical_columns",
    "resolve_vertical_top",
    "vertical_after_clip_rect",
    "vertical_after_clip_pad",
    "vertical_before_clip_rect",
    "vertical_before_clip_pad",
    "vertical_cell_width",
    "vertical_glyph_offset",
    "vertical_glyph_path",
    "vertical_fill_band",
    "vertical_main_path_signature",
    "vertical_layer_stack",
    "vertical_orientation",
    "vertical_ruby_allowance",
]
