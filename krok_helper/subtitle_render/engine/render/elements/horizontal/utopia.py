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
    glow_radius,
    ruby_visual_padding,
    text_visual_padding,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (
    LineCharTransition,
    LineLayout,
    RubyLayout,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.layers import (
    build_glyph_run_glow_layer,
    glyph_run_layer_key,
    inflate_rect,
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
    character_transform,
    transition_char_state,
    utopia_following_done_time,
)
from krok_helper.subtitle_render.engine.ruby import ruby_layout_units
from krok_helper.subtitle_render.engine.ruby.timing import (
    _ruby_utopia_reading_units_and_intervals as ruby_utopia_reading_units_and_intervals,
    _ruby_utopia_visual_units as ruby_utopia_visual_units,
    resolve_char_ruby_groups,
    utopia_main_group_for_index,
    utopia_wipe_window_for_index,
)


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
        transform = character_transform(
            center_x=left + width / 2,
            center_y=group_rect.top() + group_rect.height() / 2,
            dx=dx,
            dy=dy,
            rotation=rotation,
            scale_x=scale_x,
            scale_y=scale_y,
            skew_y=skew_y,
            scale_origin_x=left,
            scale_origin_y=layout.baseline_y,
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
            scale_origin_x=layout.x,
            scale_origin_y=layout.baseline_y,
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
            scale_origin_x=unit_x,
            scale_origin_y=layout.baseline_y,
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
