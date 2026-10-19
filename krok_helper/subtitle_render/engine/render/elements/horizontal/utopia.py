"""Utopia transition scope identities and dynamic horizontal bounds."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Hashable

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import (
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QTransform,
)

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.paint import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
)
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingLine
from krok_helper.subtitle_render.engine.layout.line.style import (
    line_end_ms,
    line_start_ms,
)
from krok_helper.subtitle_render.engine.render.core.layers import (
    BakedLayer,
    LayerAnimation,
    LayerCache,
    LayerContext,
    SCOPE_GROUP,
)
from krok_helper.subtitle_render.engine.render.effects import (
    brush_for_fill,
    decoration_kind_for_state,
    fill_brush_rect,
    fill_signature,
    glow_concentration_level,
    glow_extent,
    glow_radius,
    main_stroke2_width,
    paint_glow_path,
    paint_shadow_silhouette,
    paint_split_glow_path,
    paint_text_layer_stack,
    ruby_visual_padding,
    style_for_state,
    text_visual_padding,
    visual_text_padding,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (
    FillSegment,
    LineCharTransition,
    LineLayout,
    RubyLayout,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.layers import (
    GlyphLayerPorts,
    after_glow_source_clip_rect,
    before_glow_source_clip_rect,
    build_glyph_run_glow_layer,
    glyph_run_layer_key,
    inflate_rect,
    karaoke_glow_states_differ,
    paint_glyph_run_after_glow_direct,
    relative_fill_rect_signature,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.layout import (
    glyph_run_path,
    glyph_run_rect,
    role_glyphs_by_index,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.ruby import (
    ruby_text_path_and_rect,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
    character_scale_origin,
    character_transform,
    transition_char_state,
    utopia_following_done_time,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.wipe import (
    fill_clip_band,
    fill_clip_band_for_glyphs,
    n3_following_wipe_band,
    run_fill_complete,
)
from krok_helper.subtitle_render.engine.ruby import ruby_layout_units
from krok_helper.subtitle_render.engine.ruby.timing import (
    _ruby_utopia_reading_units_and_intervals as ruby_utopia_reading_units_and_intervals,
    _ruby_utopia_visual_units as ruby_utopia_visual_units,
    resolve_char_ruby_groups,
    utopia_main_group_for_index,
    utopia_wipe_window_for_index,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.engine.text import GlyphLayout
from krok_helper.subtitle_render.engine.text.font_weight import embolden_glyph_path


UTOPIA_RUN_GLOW_CACHE = LayerCache(max_items=128)


def utopia_glow_cache_enabled() -> bool:
    """Return whether transformed Utopia glow bitmaps may be reused."""

    return os.environ.get("KROK_SUBTITLE_GLOW_CACHE", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def clear_utopia_glow_cache() -> None:
    """Drop all cached Utopia main-text glow layers and masks."""

    UTOPIA_RUN_GLOW_CACHE.clear()


@dataclass(frozen=True)
class ScopeBoundsLayer:
    """Bounds-only layer for effects that remain dynamically painted."""

    rect: QRectF
    scope_id: Hashable
    z_index: int = 0
    scope: str = SCOPE_GROUP

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "ScopeBoundsLayer":
        return self

    def static_key(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> Hashable | None:
        return None

    def bake(
        self,
        ctx: LayerContext,
        layout: object,
        key: Hashable,
    ) -> BakedLayer:
        raise AssertionError("bounds-only layers are never baked")

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation(clip_rect=self.rect)

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
        return (
            int(math.floor(self.rect.top())),
            int(math.ceil(self.rect.bottom())),
        )


def utopia_main_scope_layers(
    layout: LineLayout,
    line: TimingLine,
    style: Style,
    t_ms: int,
    transition: LineCharTransition,
    frame_height: int,
) -> list[ScopeBoundsLayer]:
    """Resolve the dynamic Utopia bounds for main-text character groups."""

    glyphs_by_index = role_glyphs_by_index(line, layout.text_layout)
    count = max(len(line.chars), 1)
    layers: list[ScopeBoundsLayer] = []
    handled_indices: set[int] = set()
    ruby_groups = resolve_char_ruby_groups(
        layout.active_rubies,
        line,
        layout.intervals,
    )
    for index in range(len(line.chars)):
        if index in handled_indices:
            continue
        if index >= len(layout.intervals) or index >= len(layout.char_x_ranges):
            continue
        if index >= len(glyphs_by_index) or glyphs_by_index[index] is None:
            continue
        group = utopia_main_group_for_index(
            layout.active_rubies,
            line,
            layout.intervals,
            index,
            groups=ruby_groups,
        )
        group_ruby: RubyAnnotation | None = None
        group_scope_indices: list[int] | None = None
        group_done_ms: int | None = None
        if group is not None:
            group_scope_indices, group_ruby = group
            group_done_ms = utopia_following_done_time(
                line,
                layout.intervals,
                group_scope_indices[-1],
                style,
            )
            group_exiting = t_ms > group_done_ms
            if group_exiting and index != group_scope_indices[0]:
                continue
            if group_exiting:
                indices = [
                    candidate
                    for candidate in group_scope_indices
                    if candidate < len(layout.intervals)
                    and candidate < len(layout.char_x_ranges)
                    and candidate < len(glyphs_by_index)
                    and glyphs_by_index[candidate] is not None
                ]
                handled_indices.update(indices[1:])
            else:
                indices = [index]
        else:
            indices = [index]
            group_scope_indices = indices

        if not indices:
            continue
        first_index = indices[0]
        last_index = indices[-1]
        following_done_ms = (
            group_done_ms
            if group_done_ms is not None
            else utopia_following_done_time(
                line,
                layout.intervals,
                last_index,
                style,
            )
        )
        char_start, char_end = utopia_wipe_window_for_index(
            line,
            layout.intervals,
            layout.char_x_ranges,
            ruby_groups,
            first_index,
            style,
            fallback_start=layout.intervals[first_index][0],
            fallback_end=layout.intervals[last_index][1],
        )
        opacity, dx, dy, rotation, scale_x, scale_y, skew_y = (
            transition_char_state(
                style,
                transition,
                first_index,
                count,
                char_start_ms=char_start,
                char_end_ms=char_end,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        )
        if opacity <= 0.0:
            continue
        group_glyphs = [
            glyphs_by_index[candidate]
            for candidate in indices
            if glyphs_by_index[candidate] is not None
        ]
        if not group_glyphs:
            continue
        left = min(layout.char_x_ranges[candidate][0] for candidate in indices)
        right = max(layout.char_x_ranges[candidate][1] for candidate in indices)
        width = max(right - left, 1)
        group_rect = glyph_run_rect(group_glyphs, layout.baseline_y)
        scale_origin_x, scale_origin_y = character_scale_origin(
            style, left, layout.baseline_y
        )
        transform = character_transform(
            center_x=left + width / 2,
            center_y=group_rect.top() + group_rect.height() / 2,
            dx=dx,
            dy=dy,
            rotation=rotation,
            scale_x=scale_x,
            scale_y=scale_y,
            skew_y=skew_y,
            scale_origin_x=scale_origin_x,
            scale_origin_y=scale_origin_y,
        )
        rect = transform.map(
            glyph_run_path(group_glyphs, layout.baseline_y)
        ).boundingRect()
        pad = max(
            text_visual_padding(glyph.style, after=False)
            for glyph in group_glyphs
        )
        pad = max(
            pad,
            max(
                text_visual_padding(glyph.style, after=True)
                for glyph in group_glyphs
            ),
        )
        layers.append(
            ScopeBoundsLayer(
                inflate_rect(rect, pad),
                utopia_scope_id(
                    line,
                    group_scope_indices,
                    group_ruby,
                    "main",
                ),
                z_index=index,
            )
        )
    return layers


def utopia_ruby_scope_layers(
    layout: LineLayout,
    line: TimingLine,
    style: Style,
    t_ms: int,
    transition: LineCharTransition,
    frame_height: int,
) -> list[ScopeBoundsLayer]:
    """Resolve the dynamic Utopia bounds for horizontal ruby annotations."""

    if layout.ruby_metrics is None:
        return []
    layers: list[ScopeBoundsLayer] = []
    for index, ruby_layout in enumerate(layout.ruby_layouts):
        if not ruby_layout.indices:
            continue
        target_ruby_font = ruby_layout.font or layout.ruby_font
        target_ruby_metrics = ruby_layout.metrics or layout.ruby_metrics
        rect = utopia_ruby_scope_rect(
            ruby_layout,
            target_ruby_font,
            target_ruby_metrics,
            line,
            layout.intervals,
            layout.rtl,
            ruby_layout.style,
            t_ms,
            transition,
            frame_height,
        )
        if rect is None:
            continue
        pad = max(
            ruby_visual_padding(ruby_layout.style, after=False),
            ruby_visual_padding(ruby_layout.style, after=True),
        )
        layers.append(
            ScopeBoundsLayer(
                inflate_rect(rect, pad),
                utopia_scope_id(
                    line,
                    ruby_layout.indices,
                    ruby_layout.ruby,
                    "ruby",
                ),
                z_index=10_000 + index,
            )
        )
    return layers


def utopia_ruby_scope_rect(
    layout: RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    rtl: bool,
    style: Style,
    t_ms: int,
    transition: LineCharTransition,
    frame_height: int,
) -> QRectF | None:
    """Resolve one ruby annotation's transformed Utopia bounds."""

    first_index = min(layout.indices)
    last_index = max(layout.indices)
    if first_index >= len(intervals) or last_index >= len(intervals):
        return None
    following_done_ms = utopia_following_done_time(
        line,
        intervals,
        last_index,
        style,
    )
    ruby_groups = resolve_char_ruby_groups([layout.ruby], line, intervals)
    char_x_ranges = [
        (layout.x, layout.x + layout.target_width) for _index in line.chars
    ]
    char_start, char_end = utopia_wipe_window_for_index(
        line,
        intervals,
        char_x_ranges,
        ruby_groups,
        first_index,
        style,
        fallback_start=intervals[first_index][0],
        fallback_end=intervals[last_index][1],
    )
    opacity, dx, dy, rotation, scale_x, scale_y, skew_y = transition_char_state(
        style,
        transition,
        first_index,
        max(len(line.chars), 1),
        char_start_ms=char_start,
        char_end_ms=char_end,
        t_ms=t_ms,
        frame_height=frame_height,
        following_done_ms=following_done_ms,
    )
    if opacity <= 0.0:
        return None
    group_exiting = len(layout.indices) > 1 and t_ms > following_done_ms
    if group_exiting:
        reading = (
            "".join(reversed(ruby_utopia_visual_units(layout.ruby.reading)))
            if rtl
            else layout.ruby.reading
        )
        path, _ = ruby_text_path_and_rect(
            reading,
            ruby_font,
            ruby_metrics,
            layout.x,
            layout.baseline_y,
            layout.target_width,
            style,
            base_text=layout.ruby.kanji,
        )
        scale_origin_x, scale_origin_y = character_scale_origin(
            style, layout.x, layout.baseline_y
        )
        transform = character_transform(
            center_x=layout.x + layout.reading_width / 2,
            center_y=(
                layout.baseline_y
                - ruby_metrics.ascent()
                + ruby_metrics.height() / 2
            ),
            dx=dx,
            dy=dy,
            rotation=rotation,
            scale_x=scale_x,
            scale_y=scale_y,
            skew_y=skew_y,
            scale_origin_x=scale_origin_x,
            scale_origin_y=scale_origin_y,
        )
        return transform.map(path).boundingRect()

    visual_units = ruby_utopia_reading_units_and_intervals(layout.ruby)
    if rtl:
        visual_units = list(reversed(visual_units))
    units = [unit for unit, _interval in visual_units]
    unit_intervals = [interval for _unit, interval in visual_units]
    if not units or len(units) != len(unit_intervals):
        path, _ = ruby_text_path_and_rect(
            layout.ruby.reading,
            ruby_font,
            ruby_metrics,
            layout.x,
            layout.baseline_y,
            layout.target_width,
            style,
            base_text=layout.ruby.kanji,
        )
        return path.boundingRect()

    rect: QRectF | None = None
    for (unit, unit_x, unit_width), (start_ms, end_ms) in zip(
        ruby_layout_units(
            units,
            ruby_metrics,
            layout.x,
            layout.target_width,
            style=style,
            base_text=layout.ruby.kanji,
        ),
        unit_intervals,
    ):
        opacity, dx, dy, rotation, scale_x, scale_y, skew_y = (
            transition_char_state(
                style,
                transition,
                first_index,
                max(len(line.chars), 1),
                char_start_ms=start_ms,
                char_end_ms=end_ms,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        )
        if opacity <= 0.0:
            continue
        path = QPainterPath()
        path.addText(float(unit_x), float(layout.baseline_y), ruby_font, unit)
        path = embolden_glyph_path(path, ruby_font)
        scale_origin_x, scale_origin_y = character_scale_origin(
            style, unit_x, layout.baseline_y
        )
        transform = character_transform(
            center_x=unit_x + unit_width / 2,
            center_y=(
                layout.baseline_y
                - ruby_metrics.ascent()
                + ruby_metrics.height() / 2
            ),
            dx=dx,
            dy=dy,
            rotation=rotation,
            scale_x=scale_x,
            scale_y=scale_y,
            skew_y=skew_y,
            scale_origin_x=scale_origin_x,
            scale_origin_y=scale_origin_y,
        )
        unit_rect = transform.map(path).boundingRect()
        rect = unit_rect if rect is None else rect.united(unit_rect)
    return rect


def utopia_scope_id(
    line: TimingLine,
    indices: list[int],
    ruby: RubyAnnotation | None,
    kind: str,
) -> tuple:
    """Build a stable cache scope identity for one Utopia group."""

    return (
        "utopia",
        kind,
        line_start_ms(line),
        line_end_ms(line),
        tuple(indices),
        ruby.kanji if ruby is not None else "",
        ruby.reading if ruby is not None else "",
    )


def get_or_build_run_glow(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> BakedLayer:
    """A3：按上正 glyph 身份缓存 glow 烘焙位图（before/after 各一条）。"""
    key = (
        glyph_run_layer_key(glyphs, role_style, colors, after=after),
        "glow",
        after,
        relative_fill_rect_signature(
            glyphs,
            baseline_y,
            fill_rect,
            global_anchor=(colors.after if after else colors.before).shadow.mode
            == "image",
        ),
    )
    return UTOPIA_RUN_GLOW_CACHE.get_or_build(
        key,
        lambda: _baked_run_glow(
            glyphs,
            role_style,
            colors,
            after=after,
            fill_rect=fill_rect,
            baseline_y=baseline_y,
        ),
    )


def _baked_run_glow(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> BakedLayer:
    image, dx, dy = build_glyph_run_glow_layer(
        glyphs,
        role_style,
        colors,
        after=after,
        fill_rect=fill_rect,
        baseline_y=baseline_y,
    )
    return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))


def get_or_build_run_glow_mask(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    *,
    after: bool,
) -> BakedLayer:
    """Cache an opaque-white glow alpha mask for spatial brush fills."""
    mask_fill = PaintFill(mode="solid", color="#FFFFFF")
    mask_state = KaraokeColorState(shadow=mask_fill)
    mask_colors = KaraokeColors(before=mask_state, after=mask_state)
    key = (
        glyph_run_layer_key(
            glyphs, role_style, mask_colors, after=after
        ),
        "glow-mask",
        after,
    )
    return UTOPIA_RUN_GLOW_CACHE.get_or_build(
        key,
        lambda: _baked_run_glow(
            glyphs,
            role_style,
            mask_colors,
            after=after,
        ),
    )


def blit_tinted_run_glow_mask(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    role_style: Style,
    fill: PaintFill,
    *,
    after: bool,
    transform: QTransform | None,
    fill_rect: QRectF,
) -> None:
    """Transform a cached glow mask, then colour it in fixed line space."""
    baked = get_or_build_run_glow_mask(
        glyphs, role_style, after=after
    )
    if baked.image.isNull():
        return
    run_left = min(glyph.left for glyph in glyphs)
    anchor = QPointF(
        float(run_left) + baked.offset.x(),
        float(baseline_y) + baked.offset.y(),
    )
    source_rect = QRectF(
        anchor.x(),
        anchor.y(),
        float(baked.image.width()),
        float(baked.image.height()),
    )
    effective_transform = transform or QTransform()
    mapped = effective_transform.mapRect(source_rect)
    left = math.floor(mapped.left())
    top = math.floor(mapped.top())
    right = math.ceil(mapped.right())
    bottom = math.ceil(mapped.bottom())
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    tinted = QImage(
        width, height, QImage.Format.Format_ARGB32_Premultiplied
    )
    tinted.fill(0)

    mask_painter = QPainter(tinted)
    try:
        mask_painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, True
        )
        local_transform = QTransform(effective_transform)
        local_transform *= QTransform.fromTranslate(-float(left), -float(top))
        mask_painter.setTransform(local_transform)
        mask_painter.drawImage(anchor, baked.image)
        mask_painter.resetTransform()
        mask_painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceIn
        )
        local_fill_rect = fill_rect.translated(-float(left), -float(top))
        mask_painter.fillRect(
            QRectF(0.0, 0.0, float(width), float(height)),
            brush_for_fill(fill, local_fill_rect),
        )
    finally:
        mask_painter.end()
    painter.drawImage(QPointF(float(left), float(top)), tinted)


def blit_cached_run_glow(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    transform: QTransform | None,
    fill_rect: QRectF | None = None,
) -> None:
    """A3：在 ``transform`` 下贴出缓存的上正 glow 位图（替代逐帧 ``_paint_glow_path``）。

    glow 在上正坐标烘焙、自然 anchor ``(run_left+dx, baseline_y+dy)`` 贴出；``transform``
    把它送到与逐帧矢量 body 相同的变换位置。调用方在贴前已设好设备空间 clip（扫光带），
    本函数 ``setTransform(combine=True)`` 不影响该 clip（Qt clip 存于设备坐标）。
    ``fill_rect`` is the shared line brush area used by N3 gradients and
    MilleFeuille fills.
    """
    if glow_radius(role_style, after=after) == 0:
        return
    state = colors.after if after else colors.before
    if (
        state.shadow.mode != "solid"
        and fill_rect is not None
        and transform is not None
        and not transform.isIdentity()
    ):
        blit_tinted_run_glow_mask(
            painter,
            glyphs,
            baseline_y,
            role_style,
            state.shadow,
            after=after,
            transform=transform,
            fill_rect=fill_rect,
        )
        return
    baked = get_or_build_run_glow(
        glyphs, role_style, colors, after=after,
        fill_rect=fill_rect, baseline_y=baseline_y,
    )
    if baked.image.isNull():
        return
    run_left = min(glyph.left for glyph in glyphs)
    anchor = QPointF(float(run_left) + baked.offset.x(), float(baseline_y) + baked.offset.y())
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if transform is not None:
            painter.setTransform(transform, combine=True)
        painter.drawImage(anchor, baked.image)
    finally:
        painter.restore()


def paint_glyph_run_before_glow_direct(
    painter: QPainter,
    glyphs: list[GlyphLayout],
    baseline_y: int,
    band: tuple[int, int] | None,
    *,
    rtl: bool,
    complete: bool,
    fill_rect: QRectF | None = None,
) -> None:
    """Paint N3's before-glow by clipping the stroke source before blur."""
    if complete:
        return
    role_style = glyphs[0].style
    colors = effective_karaoke_colors(role_style)
    path = glyph_run_path(glyphs, baseline_y)
    rect = glyph_run_rect(glyphs, baseline_y)
    pad = glow_extent(
        role_style.stroke_width_px,
        role_style.stroke2_width_px,
        glow_radius(role_style, after=False),
    )
    if band is not None and utopia_glow_cache_enabled():
        front = float(band[0] if rtl else band[1])
        paint_cached_run_glow_source_wipe(
            painter,
            path,
            rect,
            glyphs,
            baseline_y,
            role_style,
            colors,
            after=False,
            front=front,
            rtl=rtl,
            transform=None,
            fill_rect=fill_rect,
        )
        return
    paint_glow_path(
        painter,
        path,
        colors.before.shadow,
        fill_rect if fill_rect is not None else rect,
        glow_radius(role_style, after=False),
        role_style.stroke_width_px,
        role_style.stroke2_width_px,
        source_clip=(
            before_glow_source_clip_rect(band, rect, pad, rtl)
            if band is not None
            else None
        ),
        concentration_level=glow_concentration_level(role_style, after=False),
    )


def paint_full_glow_source_wipe(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    role_style: Style,
    colors: KaraokeColors,
    *,
    front: float,
    rtl: bool,
    fill_rect: QRectF | None,
) -> None:
    """Paint an entire source-clipped glow after geometry transforms."""
    before_kind = decoration_kind_for_state(role_style, after=False)
    after_kind = decoration_kind_for_state(role_style, after=True)
    before_radius = glow_radius(role_style, after=False)
    after_radius = glow_radius(role_style, after=True)
    before_concentration = glow_concentration_level(role_style, after=False)
    after_concentration = glow_concentration_level(role_style, after=True)
    stroke2_width = main_stroke2_width(role_style)
    if (
        before_kind == "glow"
        and after_kind == "glow"
        and before_radius > 0
        and before_radius == after_radius
        and before_concentration == after_concentration
    ):
        pad = glow_extent(
            role_style.stroke_width_px, stroke2_width, before_radius
        )
        top = rect.top() - pad
        height = rect.height() + pad * 2
        if rtl:
            before_source_clip = QRectF(
                -1_000_000.0, top, front + 1_000_000.0, height
            )
            after_source_clip = QRectF(front, top, 1_000_000.0, height)
        else:
            before_source_clip = QRectF(front, top, 1_000_000.0, height)
            after_source_clip = QRectF(
                -1_000_000.0, top, front + 1_000_000.0, height
            )
        paint_split_glow_path(
            painter,
            path,
            colors.before.shadow,
            colors.after.shadow,
            fill_rect if fill_rect is not None else rect,
            before_radius,
            role_style.stroke_width_px,
            stroke2_width,
            before_source_clip=before_source_clip,
            after_source_clip=after_source_clip,
            concentration_level=before_concentration,
        )
        return

    for after in (False, True):
        kind = after_kind if after else before_kind
        if kind != "glow":
            continue
        radius = after_radius if after else before_radius
        if radius <= 0:
            continue
        pad = glow_extent(
            role_style.stroke_width_px, stroke2_width, radius
        )
        source_is_right = rtl == after
        source_clip = (
            QRectF(
                front,
                rect.top() - pad,
                1_000_000.0,
                rect.height() + pad * 2,
            )
            if source_is_right
            else QRectF(
                -1_000_000.0,
                rect.top() - pad,
                front + 1_000_000.0,
                rect.height() + pad * 2,
            )
        )
        state = colors.after if after else colors.before
        paint_glow_path(
            painter,
            path,
            state.shadow,
            fill_rect if fill_rect is not None else rect,
            radius,
            role_style.stroke_width_px,
            stroke2_width,
            source_clip=source_clip,
            concentration_level=(
                after_concentration if after else before_concentration
            ),
        )


def paint_cached_run_glow_source_wipe(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    glyphs: list[GlyphLayout],
    baseline_y: int,
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    front: float,
    rtl: bool,
    transform: QTransform | None,
    fill_rect: QRectF | None,
) -> None:
    """Blur only the moving front strip and reuse cached full glow elsewhere.

    Source-clipped blur differs from the full cached halo only within one
    Gaussian support radius of ``front``.  N3's source-before-blur result is
    therefore reproduced by rasterising that narrow strip dynamically and
    clipping the cached full halo to the unaffected far side.
    """
    radius = glow_radius(role_style, after=after)
    if radius <= 0:
        return
    state = colors.after if after else colors.before
    stroke2_width = main_stroke2_width(role_style)
    pad = glow_extent(role_style.stroke_width_px, stroke2_width, radius)
    top = rect.top() - pad
    height = rect.height() + pad * 2
    source_is_right = rtl == after
    if source_is_right:
        source_clip = QRectF(front, top, 1_000_000.0, height)
        baked_clip = QRectF(
            front + pad,
            -1_000_000.0,
            1_000_000.0,
            2_000_000.0,
        )
    else:
        source_clip = QRectF(
            -1_000_000.0,
            top,
            front + 1_000_000.0,
            height,
        )
        baked_clip = QRectF(
            -1_000_000.0,
            -1_000_000.0,
            front - pad + 1_000_000.0,
            2_000_000.0,
        )
    strip_clip = QRectF(
        front - pad,
        -1_000_000.0,
        float(pad * 2),
        2_000_000.0,
    )
    paint_glow_path(
        painter,
        path,
        state.shadow,
        fill_rect if fill_rect is not None else rect,
        radius,
        role_style.stroke_width_px,
        stroke2_width,
        source_clip=source_clip,
        concentration_level=glow_concentration_level(role_style, after=after),
        target_clip=strip_clip,
    )
    painter.save()
    try:
        painter.setClipRect(baked_clip)
        blit_cached_run_glow(
            painter,
            glyphs,
            baseline_y,
            role_style,
            colors,
            after=after,
            transform=transform,
            fill_rect=fill_rect,
        )
    finally:
        painter.restore()


def paint_cached_run_split_glow_source_wipe(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    glyphs: list[GlyphLayout],
    baseline_y: int,
    role_style: Style,
    colors: KaraokeColors,
    *,
    front: float,
    rtl: bool,
    transform: QTransform | None,
    fill_rect: QRectF | None,
) -> bool:
    """N3 fast path: one dynamic blur for both colours plus cached far halos."""
    before_radius = glow_radius(role_style, after=False)
    after_radius = glow_radius(role_style, after=True)
    if before_radius <= 0 or before_radius != after_radius:
        return False
    stroke2_width = main_stroke2_width(role_style)
    pad = glow_extent(
        role_style.stroke_width_px, stroke2_width, before_radius
    )
    top = rect.top() - pad
    height = rect.height() + pad * 2
    if rtl:
        before_source_clip = QRectF(
            -1_000_000.0, top, front + 1_000_000.0, height
        )
        after_source_clip = QRectF(front, top, 1_000_000.0, height)
        before_baked_clip = QRectF(
            -1_000_000.0,
            -1_000_000.0,
            front - pad + 1_000_000.0,
            2_000_000.0,
        )
        after_baked_clip = QRectF(
            front + pad,
            -1_000_000.0,
            1_000_000.0,
            2_000_000.0,
        )
    else:
        before_source_clip = QRectF(front, top, 1_000_000.0, height)
        after_source_clip = QRectF(
            -1_000_000.0, top, front + 1_000_000.0, height
        )
        before_baked_clip = QRectF(
            front + pad,
            -1_000_000.0,
            1_000_000.0,
            2_000_000.0,
        )
        after_baked_clip = QRectF(
            -1_000_000.0,
            -1_000_000.0,
            front - pad + 1_000_000.0,
            2_000_000.0,
        )
    strip_clip = QRectF(
        front - pad,
        -1_000_000.0,
        float(pad * 2),
        2_000_000.0,
    )
    paint_split_glow_path(
        painter,
        path,
        colors.before.shadow,
        colors.after.shadow,
        fill_rect if fill_rect is not None else rect,
        before_radius,
        role_style.stroke_width_px,
        stroke2_width,
        before_source_clip=before_source_clip,
        after_source_clip=after_source_clip,
        concentration_level=glow_concentration_level(role_style, after=False),
        target_clip=strip_clip,
    )
    for after, clip in (
        (False, before_baked_clip),
        (True, after_baked_clip),
    ):
        painter.save()
        try:
            painter.setClipRect(clip)
            blit_cached_run_glow(
                painter,
                glyphs,
                baseline_y,
                role_style,
                colors,
                after=after,
                transform=transform,
                fill_rect=fill_rect,
            )
        finally:
            painter.restore()
    return True


def paint_glyph_run_combined_glow(
    painter: QPainter,
    glyphs: list[GlyphLayout],
    baseline_y: int,
    fill_segments: list[FillSegment],
    t_ms: int,
    rtl: bool,
    *,
    fill_rect: QRectF | None,
) -> None:
    """Paint one static run's N3 before/after decoration with one blur."""
    style = glyphs[0].style
    colors = effective_karaoke_colors(style)
    indices = {glyph.index for glyph in glyphs}
    band = fill_clip_band_for_glyphs(fill_segments, glyphs, t_ms, rtl)
    complete = run_fill_complete(fill_segments, indices, t_ms)
    if band is None:
        blit_cached_run_glow(
            painter,
            glyphs,
            baseline_y,
            style,
            colors,
            after=False,
            transform=None,
            fill_rect=fill_rect,
        )
        return
    if complete:
        blit_cached_run_glow(
            painter,
            glyphs,
            baseline_y,
            style,
            colors,
            after=True,
            transform=None,
            fill_rect=fill_rect,
        )
        return
    front = float(band[0] if rtl else band[1])
    path = glyph_run_path(glyphs, baseline_y)
    rect = glyph_run_rect(glyphs, baseline_y)
    paint_cached_run_split_glow_source_wipe(
        painter,
        path,
        rect,
        glyphs,
        baseline_y,
        style,
        colors,
        front=front,
        rtl=rtl,
        transform=None,
        fill_rect=fill_rect,
    )


def afterglow_strip_enabled() -> bool:
    """走字中 after-glow 只逐帧模糊扫光前沿窄带（默认开）。

    ``KROK_SUBTITLE_AFTERGLOW_STRIP=0`` 退回整行逐帧
    ``_paint_glyph_run_after_glow_direct``（A/B 像素 oracle / 紧急回退用）。
    """
    return os.environ.get("KROK_SUBTITLE_AFTERGLOW_STRIP", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def paint_glyph_run_after_glow_wipe(
    painter: QPainter,
    glyphs: list[GlyphLayout],
    baseline_y: int,
    band: tuple[int, int],
    *,
    rtl: bool,
    complete: bool,
    fill_rect: QRectF | None = None,
) -> None:
    """走字中的已唱发光：前沿窄带逐帧模糊 + 其余贴整段烘焙位图。

    N3 语义要求「先按扫光线裁源、再模糊」让前沿保持柔和，因此该层无法整层烘焙。
    但 blur(裁切源) 与 blur(完整源) 只在扫光线 ±支撑半径（``glow_extent``，≥3×radius）
    内不同：seam（前沿 - pad）之前两者逐像素一致 → 直接贴 ``UTOPIA_RUN_GLOW_CACHE`` 里
    整段 after-glow 烘焙；seam 之后仅对 2×pad 宽的窄带做逐帧 stroke+blur。模糊成本
    随画布面积线性，长行收益一个数量级。"""
    role_style = glyphs[0].style
    if complete or not afterglow_strip_enabled() or not utopia_glow_cache_enabled():
        _paint_glyph_run_after_glow_direct(
            painter,
            glyphs,
            baseline_y,
            band,
            rtl=rtl,
            complete=complete,
            fill_rect=fill_rect,
        )
        return
    colors = effective_karaoke_colors(role_style)
    path = glyph_run_path(glyphs, baseline_y)
    rect = glyph_run_rect(glyphs, baseline_y)
    radius = glow_radius(role_style, after=True)
    pad = glow_extent(role_style.stroke_width_px, role_style.stroke2_width_px, radius)
    band_left, band_right = band
    front = float(band_left) if rtl else float(band_right)
    # 前沿窄带 [front-pad, front+pad]；seam 在其已唱侧边缘。
    if rtl:
        seam = front + pad
        strip_clip = QRectF(front - pad, -1_000_000.0, 2.0 * pad, 2_000_000.0)
        baked_clip = QRectF(seam, -1_000_000.0, 1_000_000.0, 2_000_000.0)
    else:
        seam = front - pad
        strip_clip = QRectF(seam, -1_000_000.0, 2.0 * pad, 2_000_000.0)
        baked_clip = QRectF(-1_000_000.0, -1_000_000.0, seam + 1_000_000.0, 2_000_000.0)
    painter.save()
    try:
        painter.setClipRect(strip_clip)
        paint_glow_path(
            painter,
            path,
            colors.after.shadow,
            fill_rect if fill_rect is not None else rect,
            radius,
            role_style.stroke_width_px,
            role_style.stroke2_width_px,
            source_clip=after_glow_source_clip_rect(band, rect, pad, rtl, complete),
            concentration_level=glow_concentration_level(role_style, after=True),
            target_clip=strip_clip,
        )
    finally:
        painter.restore()
    baked = get_or_build_run_glow(
        glyphs,
        role_style,
        colors,
        after=True,
        fill_rect=fill_rect,
        baseline_y=baseline_y,
    )
    if baked.image.isNull():
        return
    run_left = min(glyph.left for glyph in glyphs)
    anchor = QPointF(float(run_left) + baked.offset.x(), float(baseline_y) + baked.offset.y())
    painter.save()
    try:
        painter.setClipRect(baked_clip)
        painter.drawImage(anchor, baked.image)
    finally:
        painter.restore()


HORIZONTAL_GLYPH_LAYER_PORTS = GlyphLayerPorts(
    fill_clip_band=lambda *args, **kwargs: fill_clip_band(*args, **kwargs),
    fill_clip_band_for_glyphs=lambda *args, **kwargs: (
        fill_clip_band_for_glyphs(*args, **kwargs)
    ),
    n3_following_wipe_band=lambda *args, **kwargs: (
        n3_following_wipe_band(*args, **kwargs)
    ),
    paint_glyph_run_after_glow_wipe=lambda *args, **kwargs: (
        paint_glyph_run_after_glow_wipe(*args, **kwargs)
    ),
    paint_glyph_run_before_glow_direct=lambda *args, **kwargs: (
        paint_glyph_run_before_glow_direct(*args, **kwargs)
    ),
    paint_glyph_run_combined_glow=lambda *args, **kwargs: (
        paint_glyph_run_combined_glow(*args, **kwargs)
    ),
    run_fill_complete=lambda *args, **kwargs: run_fill_complete(*args, **kwargs),
)


def paint_char_karaoke_stack(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    *,
    char_x: int,
    char_width: int,
    baseline_y: int,
    metrics: QFontMetrics,
    colors: KaraokeColors,
    style: Style,
    ratio: float,
    rtl: bool = False,
    clip_rect: QRectF | None = None,
    glow_run: list[GlyphLayout] | None = None,
    glow_transform: QTransform | None = None,
    geometry_transform: QTransform | None = None,
    fill_rect: QRectF | None = None,
) -> None:
    # A3（§9.7）：``glow_run`` 给定（utopia 路径）时，glow 走上正烘焙缓存 + 变换 blit，
    # 不再每帧 _paint_glow_path 重算高斯；body 仍逐帧矢量（锐利）。``glow_run`` 为 None
    # 时退回原逐帧 glow 路径（保留旧行为，可回退）。
    def _use_cached_glow(after: bool) -> bool:
        # N3 transforms the glyph geometry before drawing the shared
        # decoration source and applying Gaussian blur.  Reusing an already
        # blurred upright bitmap would also scale/rotate the blur kernel,
        # producing a visibly different Utopia outline during entry, wipe and
        # outro.  Keep the cache for identity frames; transformed frames must
        # blur the transformed source.
        return (
            glow_run is not None
            and decoration_kind_for_state(style, after=after) == "glow"
            and (
                geometry_transform is None
                or geometry_transform.isIdentity()
            )
        )
    stroke2_width = main_stroke2_width(style)
    before_style = style_for_state(style, after=False)
    after_style = style_for_state(style, after=True)

    def _blit_glow(after: bool) -> None:
        blit_cached_run_glow(
            painter, glow_run, baseline_y,
            after_style if after else before_style,
            colors,
            after=after, transform=glow_transform, fill_rect=fill_rect,
        )

    if ratio <= 0.0:
        use_cached_glow = _use_cached_glow(after=False)
        if use_cached_glow:
            _blit_glow(after=False)
        paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.before,
            before_style,
            stroke_width=before_style.stroke_width_px,
            stroke2_width=stroke2_width,
            shadow_dx=before_style.shadow_offset_x,
            shadow_dy=before_style.shadow_offset_y,
            glow_radius=glow_radius(before_style, after=False),
            draw_glow=not use_cached_glow,
            fill_rect=fill_rect,
            after=False,
        )
        return

    if ratio < 1.0:
        clip_bounds = clip_rect if clip_rect is not None else QRectF(
            float(char_x),
            float(baseline_y - metrics.ascent()),
            float(char_width),
            float(metrics.height()),
        )
        glow_states_differ = karaoke_glow_states_differ(style, colors)
        use_cached_before_glow = (
            _use_cached_glow(after=False) and not glow_states_differ
        )
        front = char_x + char_width * (1.0 - ratio if rtl else ratio)
        if glow_states_differ:
            transformed_geometry = (
                geometry_transform is not None
                and not geometry_transform.isIdentity()
            )
            if transformed_geometry:
                paint_full_glow_source_wipe(
                    painter,
                    path,
                    clip_bounds,
                    style,
                    colors,
                    front=front,
                    rtl=rtl,
                    fill_rect=fill_rect,
                )
            elif glow_run is not None and utopia_glow_cache_enabled():
                combined = paint_cached_run_split_glow_source_wipe(
                    painter,
                    path,
                    clip_bounds,
                    glow_run,
                    baseline_y,
                    style,
                    colors,
                    front=front,
                    rtl=rtl,
                    transform=glow_transform,
                    fill_rect=fill_rect,
                )
                if not combined:
                    for after in (False, True):
                        paint_cached_run_glow_source_wipe(
                            painter,
                            path,
                            clip_bounds,
                            glow_run,
                            baseline_y,
                            style,
                            colors,
                            after=after,
                            front=front,
                            rtl=rtl,
                            transform=glow_transform,
                            fill_rect=fill_rect,
                        )
            else:
                paint_full_glow_source_wipe(
                    painter,
                    path,
                    clip_bounds,
                    style,
                    colors,
                    front=front,
                    rtl=rtl,
                    fill_rect=fill_rect,
                )
        elif use_cached_before_glow:
            _blit_glow(after=False)
        elif (
            glow_radius(before_style, after=False) > 0
            and before_style.decoration_kind == "glow"
        ):
            # 等态发光、缓存路径不可用：光晕整幅画一次（GPU 逐侧独占裁剪
            # 在前后光同色同半径时与整幅一次等价），下面的 before 主体层
            # 不再携带光晕——否则主体按锋面裁剪会把已唱侧光晕一并切掉。
            paint_glow_path(
                painter,
                path,
                colors.before.shadow,
                fill_brush_rect(
                    colors.before.shadow,
                    fill_rect if fill_rect is not None else rect,
                    None,
                ),
                glow_radius(before_style, after=False),
                before_style.stroke_width_px,
                stroke2_width,
                concentration_level=glow_concentration_level(
                    before_style, after=False
                ),
            )
        utopia_shadow_split = (
            geometry_transform is not None
            and (
                (
                    before_style.decoration_kind == "shadow"
                    and bool(
                        before_style.shadow_offset_x
                        or before_style.shadow_offset_y
                    )
                )
                or (
                    after_style.decoration_kind == "shadow"
                    and bool(
                        after_style.shadow_offset_x
                        or after_style.shadow_offset_y
                    )
                )
            )
        )
        if utopia_shadow_split:
            shadow_states_differ = (
                fill_signature(colors.before.shadow)
                != fill_signature(colors.after.shadow)
                or before_style.shadow_offset_x
                != after_style.shadow_offset_x
                or before_style.shadow_offset_y
                != after_style.shadow_offset_y
                or before_style.decoration_kind
                != after_style.decoration_kind
            )
            if not shadow_states_differ:
                paint_shadow_silhouette(
                    painter,
                    path,
                    colors.before.shadow,
                    fill_rect if fill_rect is not None else rect,
                    before_style.shadow_offset_x,
                    before_style.shadow_offset_y,
                    before_style.stroke_width_px,
                    stroke2_width,
                )
            else:
                for after in (False, True):
                    state_style = after_style if after else before_style
                    if state_style.decoration_kind != "shadow":
                        continue
                    shadow_front = front + state_style.shadow_offset_x
                    source_is_right = rtl == after
                    output_clip = (
                        QRectF(
                            shadow_front,
                            -1_000_000.0,
                            1_000_000.0,
                            2_000_000.0,
                        )
                        if source_is_right
                        else QRectF(
                            -1_000_000.0,
                            -1_000_000.0,
                            shadow_front + 1_000_000.0,
                            2_000_000.0,
                        )
                    )
                    painter.save()
                    try:
                        painter.setClipRect(output_clip)
                        state = colors.after if after else colors.before
                        paint_shadow_silhouette(
                            painter,
                            path,
                            state.shadow,
                            fill_rect if fill_rect is not None else rect,
                            state_style.shadow_offset_x,
                            state_style.shadow_offset_y,
                            state_style.stroke_width_px,
                            stroke2_width,
                        )
                    finally:
                        painter.restore()
        # before 栈只画未唱侧（锋面右/左半），已唱半边由下方 after 栈独占
        # ——与上方 shadow 分割和 GPU 的双侧互斥裁剪同口径。此前 before 全
        # 量垫底，半透明配色在已唱半边呈 α² 双层（2026-10 用户实测唱字
        # 特效过程透明度不生效的根因）。
        painter.save()
        try:
            painter.setClipRect(
                QRectF(
                    front,
                    -1_000_000.0,
                    1_000_000.0,
                    2_000_000.0,
                )
                if not rtl
                else QRectF(
                    -1_000_000.0,
                    -1_000_000.0,
                    front + 1_000_000.0,
                    2_000_000.0,
                )
            )
            paint_text_layer_stack(
                painter,
                path,
                rect,
                colors.before,
                before_style,
                stroke_width=before_style.stroke_width_px,
                stroke2_width=stroke2_width,
                shadow_dx=before_style.shadow_offset_x,
                shadow_dy=before_style.shadow_offset_y,
                glow_radius=glow_radius(before_style, after=False),
                # 光晕已在上方单独整幅画过（等态共享 / 分态逐侧 / 缓存
                # blit 三种来源），主体层不再携带，避免随锋面裁剪被切断。
                draw_glow=False,
                fill_rect=fill_rect,
                draw_shadow=not utopia_shadow_split,
                after=False,
            )
        finally:
            painter.restore()
        stroke_pad = visual_text_padding(style)
        # RTL：单字内扫光从右向左，已唱区贴字符右缘。
        clip_x = char_x + (char_width * (1.0 - ratio) if rtl else 0.0)
        # 已唱描边 + 填充：保持卡拉ok 走字的硬边（按字框紧裁），发光已单独画过。
        painter.save()
        try:
            painter.setClipRect(
                QRectF(
                    float(clip_x - stroke_pad),
                    float(clip_bounds.top() - stroke_pad),
                    float(char_width * ratio + stroke_pad),
                    float(clip_bounds.height() + stroke_pad * 2),
                )
            )
            paint_text_layer_stack(
                painter,
                path,
                rect,
                colors.after,
                after_style,
                stroke_width=after_style.stroke_width_px,
                stroke2_width=stroke2_width,
                shadow_dx=after_style.shadow_offset_x,
                shadow_dy=after_style.shadow_offset_y,
                glow_radius=glow_radius(after_style, after=True),
                draw_glow=False,
                fill_rect=fill_rect,
                draw_shadow=not utopia_shadow_split,
                after=True,
            )
        finally:
            painter.restore()
        return

    use_cached_glow = _use_cached_glow(after=True)
    if use_cached_glow:
        _blit_glow(after=True)
    paint_text_layer_stack(
        painter,
        path,
        rect,
        colors.after,
        after_style,
        stroke_width=after_style.stroke_width_px,
        stroke2_width=stroke2_width,
        shadow_dx=after_style.shadow_offset_x,
        shadow_dy=after_style.shadow_offset_y,
        glow_radius=glow_radius(after_style, after=True),
        draw_glow=not use_cached_glow,
        fill_rect=fill_rect,
        after=True,
    )
