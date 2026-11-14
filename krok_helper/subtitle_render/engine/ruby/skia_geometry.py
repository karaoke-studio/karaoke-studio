"""Prepare upper-ruby targets and local geometry from canonical measurements.

This is a configuration-time value projection. It neither enables a rendering
profile nor allocates frame-time wipe progress. Main intervals remain owned by
the metric snapshot; interference only inserts spatial gaps.
"""
from dataclasses import dataclass, replace
import hashlib

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import RubyAnnotation
from krok_helper.subtitle_render.engine.layout.line.skia_geometry import LocalLineGeometry, local_line_geometry
from krok_helper.subtitle_render.engine.layout.line.skia_placement import place_horizontal_line
from krok_helper.subtitle_render.engine.text.cell_positions import char_left_positions
from krok_helper.subtitle_render.engine.style.style_semantics import style_for_role_in_layout
from krok_helper.subtitle_render.engine.text.font_slots import font_style_for_cell
from krok_helper.subtitle_render.engine.value_signature import value_signature
from .interference import RubySpacingSpan, resolve_ruby_spacing
from .selection import active_rubies_for_line, ruby_target_indices, effective_ruby_for_target, ruby_target_x_range
from .skia_metrics import MeasuredRubyReadings, measure_readings
from .style_semantics import ruby_style_for_target_indices, ruby_script_stroke_style, ruby_stroke_width
from .unit_layout import measured_ruby_origins


@dataclass(frozen=True)
class RubyTarget:
    annotation_index: int
    indices: tuple[int, ...]
    effective: RubyAnnotation
    style: Style


@dataclass(frozen=True)
class RubyGeometry:
    target: RubyTarget
    span: tuple[float, float]
    baseline_offset: float
    cells: tuple


@dataclass(frozen=True)
class RubyLineGeometry:
    intervals: tuple
    gaps: tuple[int, ...]
    overflow: tuple[int, int]
    main: LocalLineGeometry
    rubies: tuple[RubyGeometry, ...]
    anchor: tuple[float, float]


@dataclass(frozen=True)
class RubyTrackGeometry:
    measured: MeasuredRubyReadings
    lines: tuple[RubyLineGeometry, ...]
    identity: str


def _box_height(metrics, size, stroke, side):
    ascent, descent = max(metrics['ascent'], 0), max(metrics['descent'], 0)
    return max(size, 1) * (ascent if side == 'ascent' else descent) / max(ascent + descent, 1) + max(stroke, 0) / 2


def _main_box_ascent(line, style, measured, targets):
    slots = [font_style_for_cell(style_for_role_in_layout(style, c.role_label), c.text)[0] for c in line.chars]
    indices = [i for i, c in enumerate(line.chars) if c.text.strip() and slots[i].affects_ruby_anchor]
    if not indices:
        targeted = {i for t in targets for i in t.indices}
        indices = [i for i, c in enumerate(line.chars) if c.text.strip() and i in targeted]
    if not indices:
        return _box_height(measured.baseline_metrics, style.font_size_px, style.stroke_width_px, 'ascent')
    return max(_box_height(measured.cells[i], slots[i].font_size_px, slots[i].stroke_width_px, 'ascent')
               for i in indices)


def prepare_ruby_geometry(track, style, line_styles, measured, *, resolve_font, measure):
    """Use canonical intervals for ownership, repeated-text targets and clamping.

    The resulting cells use a separate ruby font-instance table; their y values
    are offsets from the final main baseline. The final IR must explicitly map
    that table and supply validated segmented clocks before enabling drawing.
    """
    annotation_indices = {id(r): i for i, r in enumerate(track.rubies)}
    targets, intervals, readings = [], [], []
    for index, (line, appearance) in enumerate(zip(track.lines, line_styles, strict=True)):
        if appearance.vertical or appearance.right_to_left or appearance.lower_ruby_enabled or line.wipe_reverse:
            raise ValueError('Skia 当前注音几何准备仅支持正向横排上注音')
        if any(style_for_role_in_layout(appearance, c.role_label).stroke_width_px > 0 for c in line.chars):
            raise ValueError('Skia 当前注音几何准备尚不支持带描边的主字')
        clock = measured.snapshot.resolve_intervals(line, style)
        intervals.append(clock)
        # Loader ownership is authoritative; hand-created tracks still have a
        # concrete source index and must not leak annotations into peer lines.
        owner = line if line.track_line_index is not None else replace(line, track_line_index=index)
        line_targets = []
        for ruby in active_rubies_for_line(track.rubies, owner, intervals=clock):
            indices = ruby_target_indices(ruby, line, clock)
            if not indices:
                continue
            effective = effective_ruby_for_target(ruby, indices, clock)
            brush = ruby_style_for_target_indices(appearance, line, indices)
            ruby_style = ruby_script_stroke_style(brush, effective.reading)
            target = RubyTarget(annotation_indices[id(ruby)], tuple(indices), effective, ruby_style)
            line_targets.append(target)
            readings.append((effective.reading, ruby_style))
        targets.append(line_targets)
    ruby_metrics = measure_readings(readings, resolve_font=resolve_font, measure=measure)
    offset, lines = 0, []
    for line, appearance, main, clock, line_targets in zip(
            track.lines, line_styles, measured.lines, intervals, targets, strict=True):
        reading_metrics = ruby_metrics.readings[offset:offset + len(line_targets)]
        offset += len(line_targets)
        spans = [RubySpacingSpan(min(t.indices), max(t.indices), False, i) for i, t in enumerate(line_targets)]

        def draw_bounds(span, left, width):
            target, metrics = line_targets[span.key], reading_metrics[span.key]
            origins = measured_ruby_origins(metrics.unit_layouts, left, width,
                                           style=target.style, base_text=target.effective.kanji)
            if not origins:
                return left, left
            return min(c[1] for c in origins), max(c[1] + c[2] for c in origins)

        gaps, left_overflow, right_overflow = resolve_ruby_spacing(main.widths, appearance, spans, draw_bounds=draw_bounds)
        local = local_line_geometry(main, appearance, char_gaps=gaps) if spans else local_line_geometry(main, appearance)
        lefts = char_left_positions(main.widths, 0, False, appearance.letter_spacing_px, char_gaps=gaps,
                                    n3_no_backtracking=appearance.layout_semantics == 'n3_1074')
        ranges = [(x, x + w) for x, w in zip(lefts, main.widths, strict=True)]
        ascent = _main_box_ascent(line, appearance, main, line_targets)
        rubies = []
        for target, metrics in zip(line_targets, reading_metrics, strict=True):
            original = track.rubies[target.annotation_index]
            span = ruby_target_x_range(original, line, clock, ranges)
            if span is None:
                continue
            origins = measured_ruby_origins(metrics.unit_layouts, span[0], max(span[1] - span[0], 1),
                                           style=target.style, base_text=target.effective.kanji)
            descents = [_box_height(cell, ruby_metrics.runs[run]['size'], ruby_stroke_width(target.style), 'descent')
                        for cell, run in zip(metrics.cells, metrics.run_indices, strict=True)]
            baseline = -ascent - appearance.ruby_gap_px - max(descents, default=0)
            cells = []
            for (_, origin, width, pen), cell, run in zip(origins, metrics.cells, metrics.run_indices, strict=True):
                ink = cell['ink']
                visible = ink[2] > ink[0] and ink[3] > ink[1]
                x = origin + pen
                cells.append({'metrics': cell, 'run_index': run, 'origin_x': x, 'baseline_y': baseline,
                              'layout_left': origin, 'layout_right': origin + width,
                              'wipe_left': x + ink[0] if visible else origin,
                              'wipe_right': x + ink[2] if visible else origin + width})
            rubies.append(RubyGeometry(target, tuple(span), baseline, tuple(cells)))
        anchor = ((local.ink[0], local.ink[2]) if local.ink else (0, max(local.layout_width, 1)))
        if appearance.layout_semantics == 'n3_1074':
            # N3 aligns the union of the main grid and ruby UNIT BOXES. Ink
            # bearings are irrelevant here; overflow on either side shifts
            # every path together and must later enter page-width statistics.
            boxes = [(c['layout_left'], c['layout_right']) for r in rubies for c in r.cells]
            anchor = (min([0, *(b[0] for b in boxes)]),
                      max([max(local.layout_width, 1), *(b[1] for b in boxes)]))
        lines.append(RubyLineGeometry(tuple(clock), tuple(gaps), (left_overflow, right_overflow),
                                      local, tuple(rubies), anchor))
    signature = (measured.identity, ruby_metrics.identity, value_signature(track.rubies), value_signature(line_styles))
    identity = hashlib.sha256(repr(signature).encode('utf-8')).hexdigest()
    return RubyTrackGeometry(ruby_metrics, tuple(lines), identity)


def ruby_line_position(item, main_metrics, prepared, width, height):
    """Place an entire prepared line using its semantic anchor, including ruby.

    Page/smart policies must consume this same anchor width when integrated;
    applying a ruby-only shift would detach the main text and its wipe clips.
    """
    left, right = prepared.anchor
    position = place_horizontal_line(item, main_metrics.baseline_metrics, width=width, height=height,
                                     ink_left=left, ink_right=right, layout_width=right-left)
    return (replace(position, offset_x=position.offset_x-left)
            if item.layout_style.layout_semantics == 'n3_1074' else position)
