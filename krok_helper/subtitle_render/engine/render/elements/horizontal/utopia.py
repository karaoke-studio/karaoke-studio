"""Utopia transition scope identities and dynamic horizontal bounds."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Hashable

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QFont, QFontMetrics, QPainter, QPainterPath

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingLine
from krok_helper.subtitle_render.engine.layout.line.style import (
    line_end_ms,
    line_start_ms,
)
from krok_helper.subtitle_render.engine.render.core.layers import (
    BakedLayer,
    LayerAnimation,
    LayerContext,
    SCOPE_GROUP,
)
from krok_helper.subtitle_render.engine.render.effects import (
    ruby_visual_padding,
    text_visual_padding,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (
    LineCharTransition,
    LineLayout,
    RubyLayout,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.layers import (
    inflate_rect,
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

