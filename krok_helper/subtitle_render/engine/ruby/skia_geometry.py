"""Prepare upper/lower ruby targets and local canonical geometry.

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
from .selection import active_rubies_for_line, ruby_target_indices, effective_ruby_for_target, ruby_target_x_range
from .skia_metrics import MeasuredRubyReadings, measure_timed_readings
from .style_semantics import ruby_style_for_target_indices, ruby_script_stroke_style, ruby_stroke_width
from .skia_lower import select_lower_targets
from .skia_reference import measure_references
from .skia_spacing import reading_origins, resolve_line_spacing


@dataclass(frozen=True)
class RubyTarget:
    annotation_index: int
    indices: tuple[int, ...]
    effective: RubyAnnotation
    style: Style
    lower: bool = False


@dataclass(frozen=True)
class RubyGeometry:
    target: RubyTarget
    span: tuple[float, float]
    baseline_offset: float
    cells: tuple
    glyph_units: tuple[str, ...] = ()
    zoom_center_offset: float | None = None


@dataclass(frozen=True)
class RubyLineGeometry:
    intervals: tuple
    gaps: tuple[int, ...]
    overflow: tuple[int, int]
    main: LocalLineGeometry
    rubies: tuple[RubyGeometry, ...]
    anchor: tuple[float, float]
    placement_ink: tuple | None = None


@dataclass(frozen=True)
class RubyTrackGeometry:
    measured: MeasuredRubyReadings
    lines: tuple[RubyLineGeometry, ...]
    identity: str
    main_identity: str


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


def upper_ruby_targets(track, index, line, appearance, clock, annotation_indices):
    # Keep loader ownership even for hand-built tracks lacking a source index.
    owner = line if line.track_line_index is not None else replace(line, track_line_index=index)
    targets = []
    for ruby in active_rubies_for_line(track.rubies, owner, intervals=clock):
        indices = ruby_target_indices(ruby, line, clock)
        if not indices:
            continue
        effective = effective_ruby_for_target(ruby, indices, clock)
        brush = ruby_style_for_target_indices(appearance, line, indices)
        style = ruby_script_stroke_style(brush, effective.reading)
        targets.append(RubyTarget(annotation_indices[id(ruby)], tuple(indices), effective, style))
    return targets


def prepare_ruby_geometry(track, style, line_styles, measured, *, resolve_font, measure):
    """Use canonical intervals for ownership, repeated-text targets and clamping.

    The resulting cells use a separate ruby font-instance table; their y values
    are offsets from the final main baseline. The final IR must explicitly map
    that table and supply validated segmented clocks before enabling drawing.
    """
    annotation_indices = {id(r): i for i, r in enumerate(track.rubies)}
    lower = select_lower_targets(track, style, line_styles, measured)
    targets, intervals, readings, reference_pairs = [], [], [], []
    for index, (line, appearance) in enumerate(zip(track.lines, line_styles, strict=True)):
        # Reverse singing is not text direction. The shared selection below
        # returns no annotation for a reverse line, exactly as the GPU does.
        if appearance.vertical or appearance.right_to_left:
            raise ValueError('Skia 当前注音几何准备仅支持正向横排上注音')
        clock = measured.snapshot.resolve_intervals(line, style)
        intervals.append(clock)
        line_targets = upper_ruby_targets(track, index, line, appearance, clock, annotation_indices)
        readings.extend((target.effective, target.style) for target in line_targets)
        for generated in lower[index]:
            target = RubyTarget(-1-generated.clock.annotation_index, generated.indices,
                                generated.effective, generated.style, True)
            line_targets.append(target)
            readings.append((target.effective, target.style))
            reference_pairs.append((ruby_style_for_target_indices(appearance, line, list(target.indices)), target.style))
        targets.append(line_targets)
    ruby_metrics = measure_timed_readings(readings, resolve_font=resolve_font, measure=measure)
    references = measure_references(reference_pairs, resolve_font=resolve_font, measure=measure)
    lower_targets = [t for group in targets for t in group if t.lower]
    lower_baselines = {target.annotation_index: round(reference.lower_baseline(target.style.ruby_gap_px))
                       for target, reference in zip(lower_targets, references, strict=True)}
    offset, lines = 0, []
    for line, appearance, main, clock, line_targets in zip(
            track.lines, line_styles, measured.lines, intervals, targets, strict=True):
        reading_metrics = ruby_metrics.readings[offset:offset + len(line_targets)]
        offset += len(line_targets)
        gaps, left_overflow, right_overflow = resolve_line_spacing(main, appearance, line_targets, reading_metrics)
        local = (local_line_geometry(main, appearance, char_gaps=gaps) if line_targets
                 else local_line_geometry(main, appearance))
        lefts = char_left_positions(main.widths, 0, False, appearance.letter_spacing_px, char_gaps=gaps,
                                    n3_no_backtracking=appearance.layout_semantics == 'n3_1074')
        ranges = [(x, x + w) for x, w in zip(lefts, main.widths, strict=True)]
        initial_lefts = char_left_positions(main.widths, 0, False, appearance.letter_spacing_px,
                                           n3_no_backtracking=appearance.layout_semantics == 'n3_1074')
        initial_ranges = [(x, x+w) for x, w in zip(initial_lefts, main.widths, strict=True)]
        ascent = _main_box_ascent(line, appearance, main, line_targets)
        has_lower = any(t.lower for t in line_targets)
        rubies = []
        for target, metrics in zip(line_targets, reading_metrics, strict=True):
            original = target.effective if target.lower else track.rubies[target.annotation_index]
            span = ruby_target_x_range(original, line, clock, ranges)
            if span is None:
                continue
            if has_lower:
                # The GPU prepares annotation unit rounding before inserting
                # slots, then translates each whole group with its first main
                # cell. Re-rounding after a negative origin crosses zero can
                # move an internal wipe boundary by an extra pixel.
                initial = ruby_target_x_range(original, line, clock, initial_ranges)
                if initial is None:
                    raise ValueError('Skia 注音缺少初始正文几何')
                shift = lefts[min(target.indices)]-initial_lefts[min(target.indices)]
                origins = [(text, x+shift, width, pen) for text, x, width, pen in
                           reading_origins(target, metrics, initial[0], max(initial[1]-initial[0], 1))]
            else:
                origins = reading_origins(target, metrics, span[0], max(span[1]-span[0], 1))
            descents = [_box_height(cell, ruby_metrics.runs[run]['size'], ruby_stroke_width(target.style), 'descent')
                        for cell, run in zip(metrics.cells, metrics.run_indices, strict=True)]
            baseline = (lower_baselines[target.annotation_index] if target.lower
                        else -ascent - appearance.ruby_gap_px - max(descents, default=0))
            cells = []
            for (_, origin, width, pen), cell, run in zip(origins, metrics.cells, metrics.run_indices, strict=True):
                ink = cell['ink']
                visible = ink[2] > ink[0] and ink[3] > ink[1]
                x = origin + pen
                cells.append({'metrics': cell, 'run_index': run, 'origin_x': x, 'baseline_y': baseline,
                              'layout_left': origin, 'layout_right': origin + width,
                              'wipe_left': x + ink[0] if visible else origin,
                              'wipe_right': x + ink[2] if visible else origin + width})
            rubies.append(RubyGeometry(target, tuple(span), baseline, tuple(cells),
                                       tuple(unit[0] for unit in metrics.unit_layouts), metrics.zoom_center_offset))
        # The collision envelope takes visible lower ink.
        boxes = [local.ink] if local.ink else []
        for group in rubies:
            if group.target.lower:
                for cell in group.cells:
                    a, b, c, d = cell['metrics']['ink']
                    if c > a and d > b:
                        boxes.append((a+cell['origin_x'], b+cell['baseline_y'],
                                      c+cell['origin_x'], d+cell['baseline_y']))
        placement_ink = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                         max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes else None
        if appearance.layout_semantics == 'n3_1074':
            # N3 aligns the union of the main grid and ruby UNIT BOXES. Ink
            # bearings are irrelevant here; overflow on either side shifts
            # every path together and must later enter page-width statistics.
            anchor_boxes = [(0, max(local.layout_width, 1))]
            anchor_boxes.extend((c['layout_left'], c['layout_right']) for r in rubies for c in r.cells)
        else:
            # Legacy alignment unions main ink with the lower readings' unit boxes.
            anchor_boxes = [(local.ink[0], local.ink[2])] if local.ink else [(0, max(local.layout_width, 1))]
            anchor_boxes.extend((c['layout_left'], c['layout_right']) for r in rubies if r.target.lower
                                for c in r.cells)
        anchor = (min(box[0] for box in anchor_boxes), max(box[1] for box in anchor_boxes))
        lines.append(RubyLineGeometry(tuple(clock), tuple(gaps), (left_overflow, right_overflow),
                                      local, tuple(rubies), anchor, placement_ink))
    signature = (measured.identity, ruby_metrics.identity, value_signature(track.rubies), value_signature(line_styles),
                 tuple(reference.identity for reference in references))
    identity = hashlib.sha256(repr(signature).encode('utf-8')).hexdigest()
    return RubyTrackGeometry(ruby_metrics, tuple(lines), identity, measured.identity)


def ruby_line_position(item, main_metrics, prepared, width, height, *, inline_width=0):
    """Place an entire prepared line using its semantic anchor, including ruby.

    Page/smart policies consume this same anchor width; applying a ruby-only
    shift would detach the main text and its wipe clips.
    """
    left, right = prepared.anchor
    if inline_width:
        left, right = min(left, -inline_width), max(right, 0)
    position = place_horizontal_line(item, main_metrics.baseline_metrics, width=width, height=height,
                                     ink_left=left, ink_right=right, layout_width=right-left,
                                     row_frame=main_metrics.row_frame)
    return (replace(position, offset_x=position.offset_x-left)
            if item.layout_style.layout_semantics == 'n3_1074' else position)
