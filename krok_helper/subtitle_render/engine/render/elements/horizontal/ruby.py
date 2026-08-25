"""Frame-independent geometry and wipe policy for horizontal ruby text."""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QFont, QFontMetrics, QPainterPath

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingLine
from krok_helper.subtitle_render.engine.render.effects import (
    glow_extent,
    ruby_decoration_kind,
    ruby_glow_radius,
    ruby_shadow_dx,
    ruby_shadow_dy,
    ruby_vertical_extra,
    visual_stroke_extent,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (
    RubyLayout,
    RubyWipeSegment,
)
from krok_helper.subtitle_render.engine.ruby import (
    build_ruby_font_for_text,
    effective_ruby_for_target,
    ruby_font_size,
    ruby_layout_left_offset,
    ruby_layout_units,
    ruby_script_stroke_style,
    ruby_stroke2_width,
    ruby_stroke_width,
    ruby_style_for_target_indices,
    ruby_target_indices,
    ruby_visual_units_and_intervals,
)
from krok_helper.subtitle_render.engine.ruby.timing import (
    _ruby_progress_ratio as ruby_progress_ratio,
)


def ruby_wipe_geometry(
    ruby: RubyAnnotation,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int,
    baseline_y: int,
    target_width: int,
    style: Style,
    *,
    rtl: bool,
) -> tuple[tuple[RubyWipeSegment, ...], float, float, tuple]:
    """Build N3-style timed glyph geometry independently from its layout box."""
    logical_units = ruby_visual_units_and_intervals(ruby)
    if not logical_units:
        return (), float(x), float(x), ()
    visual_units = list(reversed(logical_units)) if rtl else logical_units
    unit_layouts = ruby_layout_units(
        [unit for unit, _interval in visual_units],
        ruby_metrics,
        x,
        target_width,
        style=style,
        base_text=ruby.kanji,
    )
    segments: list[RubyWipeSegment] = []
    signature: list[tuple] = []
    bounds: list[tuple[float, float]] = []
    edge_half = float(max(int(ruby_stroke_width(style)), 0) // 2)
    for (unit, interval), (_draw_unit, unit_x, unit_width) in zip(
        visual_units,
        unit_layouts,
    ):
        path = QPainterPath()
        path.addText(float(unit_x), float(baseline_y), ruby_font, unit)
        ink = path.boundingRect()
        if ink.isEmpty():
            ink_left = float(unit_x)
            ink_right = float(unit_x + max(unit_width, 0.0))
        else:
            ink_left = float(ink.left())
            ink_right = float(ink.right())
        if ink_right < ink_left:
            ink_left, ink_right = ink_right, ink_left
        draw_left = ink_left - edge_half
        draw_right = ink_right + edge_half
        start_ms, end_ms = interval
        segments.append(
            RubyWipeSegment(
                int(start_ms),
                max(int(start_ms), int(end_ms)),
                draw_right if rtl else draw_left,
                draw_left if rtl else draw_right,
            )
        )
        bounds.append((draw_left, draw_right))
        signature.append(
            (
                unit,
                round(float(unit_x) - float(x), 3),
                round(float(unit_width), 3),
                round(ink_left - float(x), 3),
                round(ink_right - float(x), 3),
            )
        )
    if not bounds:
        return (), float(x), float(x), tuple(signature)
    segments.sort(key=lambda segment: (segment.start_ms, segment.end_ms))
    adjusted = list(segments)
    for index in range(len(adjusted) - 1):
        current = adjusted[index]
        following = adjusted[index + 1]
        overlaps = (
            current.axis_end <= following.axis_start
            if rtl
            else current.axis_end >= following.axis_start
        )
        if overlaps:
            adjusted[index] = replace(current, axis_end=following.axis_start)
    return (
        tuple(adjusted),
        min(left for left, _right in bounds),
        max(right for _left, right in bounds),
        tuple(signature),
    )


def role_ruby_vertical_extra(
    line: TimingLine,
    rubies: list[RubyAnnotation],
    intervals: list[tuple[int, int]],
    style: Style,
) -> int:
    """Reserve enough vertical space for the largest role-specific ruby."""
    extra = 0
    for ruby in rubies:
        indices = ruby_target_indices(ruby, line, intervals)
        if not indices:
            continue
        paint_ruby = effective_ruby_for_target(ruby, indices, intervals)
        ruby_style = ruby_script_stroke_style(
            ruby_style_for_target_indices(style, line, indices),
            paint_ruby.reading,
        )
        font = build_ruby_font_for_text(ruby_style, paint_ruby.reading)
        metrics = QFontMetrics(font)
        extra = max(
            extra,
            ruby_vertical_extra(
                ruby_style,
                metrics,
                font_size_px=max(font.pixelSize(), 1),
            ),
        )
    return extra


def n3_ruby_fill_rect(
    left: int,
    width: int,
    baseline_y: int,
    ruby_metrics: QFontMetrics,
    style: Style,
    *,
    brush_style: Style | None = None,
    font_size_px: int | None = None,
) -> QRectF:
    """Return the ruby ``DrawLineInfo`` gradient area used by N3."""
    font_size = (
        ruby_font_size(style)
        if font_size_px is None
        else max(int(font_size_px), 1)
    )
    metric_total = max(ruby_metrics.ascent() + ruby_metrics.descent(), 1)
    descent = font_size * max(ruby_metrics.descent(), 0) // metric_total
    draw_edge = ruby_stroke_width(style)
    anchor_style = brush_style or style
    anchor_edge = ruby_stroke_width(anchor_style)
    anchor_edge2 = ruby_stroke2_width(anchor_style)
    draw_bottom = float(baseline_y + descent + draw_edge // 2)
    draw_top = draw_bottom - float(font_size + draw_edge)
    inset = float((anchor_edge + anchor_edge2) // 2)
    top = draw_top + inset
    bottom = draw_bottom - inset
    return QRectF(
        float(left),
        top,
        float(max(width, 1)),
        float(max(bottom - top, 1.0)),
    )


def ruby_text_rect(layout: RubyLayout, ruby_metrics: QFontMetrics) -> QRectF:
    left_offset = ruby_layout_left_offset(
        layout.ruby.reading,
        ruby_metrics,
        layout.target_width,
        layout.style,
        layout.ruby.kanji,
    )
    return QRectF(
        float(layout.x + left_offset),
        float(layout.baseline_y - ruby_metrics.ascent()),
        float(layout.reading_width),
        float(ruby_metrics.height()),
    )


def _ruby_clip_padding(style: Style, *, after: bool) -> int:
    stroke_width = ruby_stroke_width(style)
    stroke2_width = ruby_stroke2_width(style)
    stroke_extent = visual_stroke_extent(stroke_width, stroke2_width)
    if not after:
        return max(
            stroke_extent,
            glow_extent(
                stroke_width,
                stroke2_width,
                ruby_glow_radius(style, after=False),
            ),
            2,
        )
    return max(
        stroke_extent,
        (
            glow_extent(
                stroke_width,
                stroke2_width,
                ruby_glow_radius(style, after=True),
            )
            if ruby_decoration_kind(style) == "glow"
            else 0
        ),
        stroke_extent + abs(ruby_shadow_dx(style)),
        stroke_extent + abs(ruby_shadow_dy(style)),
        2,
    )


def ruby_after_clip_rect(
    layout: RubyLayout,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    ratio: float,
) -> QRectF:
    rect = ruby_text_rect(layout, ruby_metrics)
    pad = _ruby_clip_padding(style, after=True)
    ratio_c = min(ratio, 1.0)
    clip_left = (
        rect.left()
        + (rect.width() * (1.0 - ratio_c) if rtl else 0.0)
        - pad
    )
    return QRectF(
        clip_left,
        rect.top() - pad,
        rect.width() * ratio_c + pad,
        rect.height() + pad * 2,
    )


def ruby_wipe_state(
    layout: RubyLayout,
    t_ms: int,
) -> tuple[bool, bool, float]:
    """Return ``(visible, complete, front)`` for the ruby glyph wipe."""
    segments = layout.wipe_segments
    if not segments:
        ratio = ruby_progress_ratio(layout.ruby, t_ms)
        front = layout.wipe_left + (layout.wipe_right - layout.wipe_left) * ratio
        return ratio > 0.0, ratio >= 1.0, front
    return ruby_segment_wipe_state(segments, layout.ruby.pos_end_ms, t_ms)


def ruby_segment_wipe_state(
    segments: tuple[RubyWipeSegment, ...],
    pos_end_ms: int,
    t_ms: int,
) -> tuple[bool, bool, float]:
    """Evaluate timed glyph-axis segments, including empty-part pauses."""
    first = segments[0]
    if t_ms <= first.start_ms:
        return False, False, first.axis_start
    previous_front = first.axis_start
    for segment in segments:
        if t_ms < segment.start_ms:
            return True, False, previous_front
        if t_ms < segment.end_ms:
            duration = segment.end_ms - segment.start_ms
            local = (
                (t_ms - segment.start_ms) / duration
                if duration > 0
                else 1.0
            )
            front = (
                segment.axis_start
                + (segment.axis_end - segment.axis_start) * local
            )
            return True, False, front
        previous_front = segment.axis_end
    complete = t_ms >= max(int(pos_end_ms), segments[-1].end_ms)
    return True, complete, previous_front


def ruby_after_clip_rect_at_time(
    layout: RubyLayout,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    t_ms: int,
) -> QRectF:
    """Clip the after layer at the current glyph front."""
    _visible, _complete, front = ruby_wipe_state(layout, t_ms)
    rect = ruby_text_rect(layout, ruby_metrics)
    pad = _ruby_clip_padding(style, after=True)
    wipe_left = layout.wipe_left if layout.wipe_segments else rect.left()
    wipe_right = layout.wipe_right if layout.wipe_segments else rect.right()
    if rtl:
        left = min(max(front, wipe_left), wipe_right)
        return QRectF(
            left - pad,
            rect.top() - pad,
            max(wipe_right - left, 0.0) + pad,
            rect.height() + pad * 2,
        )
    right = min(max(front, wipe_left), wipe_right)
    return QRectF(
        wipe_left - pad,
        rect.top() - pad,
        max(right - wipe_left, 0.0) + pad,
        rect.height() + pad * 2,
    )


def ruby_before_clip_rect_at_time(
    layout: RubyLayout,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    t_ms: int,
) -> QRectF:
    """Keep the before ruby glow on the unsung side of the glyph front."""
    _visible, _complete, front = ruby_wipe_state(layout, t_ms)
    rect = ruby_text_rect(layout, ruby_metrics)
    pad = _ruby_clip_padding(style, after=False)
    wipe_left = layout.wipe_left if layout.wipe_segments else rect.left()
    wipe_right = layout.wipe_right if layout.wipe_segments else rect.right()
    front = min(max(front, wipe_left), wipe_right)
    if rtl:
        return QRectF(
            wipe_left - pad,
            rect.top() - pad,
            max(front - wipe_left, 0.0) + pad,
            rect.height() + pad * 2,
        )
    return QRectF(
        front,
        rect.top() - pad,
        max(wipe_right - front, 0.0) + pad,
        rect.height() + pad * 2,
    )
