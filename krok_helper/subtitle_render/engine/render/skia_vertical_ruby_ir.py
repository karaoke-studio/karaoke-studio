"""Final vertical ruby positions and clips over the shared upper-reading clocks."""
import hashlib
import math

from ..layout.line.skia_vertical import upright_vertical_text, vertical_cells, vertical_line_box
from ..ruby.skia_geometry import upper_ruby_targets
from ..ruby.skia_metrics import measure_timed_readings
from ..ruby.skia_timing import prepare_ruby_target_timing, wipe_points_to_ir


def _reading_units(reading, clock, ruby_column, reading_top, span_height, run_offset):
    ascent = float(reading.baseline_metrics['ascent'])
    units = []
    for index, (layout, metrics, run, interval) in enumerate(zip(
            reading.unit_layouts, reading.cells, reading.run_indices, clock.intervals, strict=True)):
        name, cell_width, glyph_offset, *_ = layout
        left, ink_top, right, ink_bottom = metrics['ink']
        if right <= left or ink_bottom <= ink_top:
            raise ValueError('Skia 竖排注音需要有墨迹的矢量字形')
        x = ruby_column + glyph_offset - cell_width / 2
        baseline = reading_top + index * span_height / len(reading.cells) + ascent
        units.append({'text': name, 'metrics': metrics, 'run_index': run_offset + run,
                      'origin_x': x, 'baseline_y': baseline, 'wipe_left': x + left, 'wipe_right': x + right,
                      'wipe_top': baseline + ink_top, 'wipe_bottom': baseline + ink_bottom,
                      'wipe_end_position': 1.0, 'interval_ms': list(interval)})
    return units


def vertical_ruby_layout(prepared, *, width, height, dpr, resolve_font, measure):
    geometry, measured = prepared.geometry, prepared.measured
    track = geometry.track
    annotation_indices = {id(ruby): i for i, ruby in enumerate(track.rubies)}
    entries, readings = [], []
    version = 1
    for index, (line, style) in enumerate(zip(track.lines, geometry.styles, strict=True)):
        intervals = measured.snapshot.resolve_intervals(line, geometry.style)
        targets = sorted(upper_ruby_targets(track, index, line, style, intervals, annotation_indices),
                         key=lambda target: min(target.indices))
        timing = prepare_ruby_target_timing(line, targets, style.ruby_main_progress_mode)
        groups, previous_end = [], 0
        for target, clock in zip(targets, timing.readings, strict=True):
            if min(target.indices) < previous_end:
                raise ValueError('Skia 竖排同列注音的正文范围不能重叠')
            previous_end = max(target.indices) + 1
            if (not clock.units or clock.units != clock.glyph_units
                    or any(not upright_vertical_text(unit) for unit in clock.units)):
                raise ValueError('Skia 竖排注音单元必须是单个直立字符')
            if len(clock.units) > 1:
                version = 2
            groups.append((target, clock, len(readings)))
            readings.append((target.effective, target.style))
        entries.append((timing.main, groups))

    ruby = measure_timed_readings(readings, resolve_font=resolve_font, measure=measure)
    fonts = dict(measured.fonts)
    for key, font in ruby.fonts.items():
        if key in fonts and fonts[key] != font:
            raise ValueError('Skia 注音字体资源身份冲突')
        fonts[key] = font

    lines = []
    for item in prepared.plan.lines:
        main = measured.lines[item.track_index]
        local = geometry.locals[item.track_index]
        main_clocks, groups = entries[item.track_index]
        style = item.layout_style
        # The allowance is rounded on the device grid, before the ruby gap.
        line_height = max((float(ruby.readings[i].baseline_metrics['ascent'])
                           + float(ruby.readings[i].baseline_metrics['descent']) for _, _, i in groups), default=0.0)
        allowance = max(math.floor(line_height * dpr + 0.5) / dpr + style.ruby_gap_px, 0.0) if groups else 0.0
        cells = vertical_cells(local, main, item, width=width, height=height, ruby_allowance=allowance)
        for cell, curve in zip(cells, main_clocks, strict=True):
            if curve is not None:
                cell['wipe_points'] = wipe_points_to_ir(curve)
        column, top, height_per_cell, _ = vertical_line_box(item, main, len(cells), width, height, allowance)
        ruby_groups, previous_bottom = [], -math.inf
        for target, clock, reading_index in groups:
            first, end = min(target.indices), max(target.indices) + 1
            ruby_column = column + style.font_size_px / 2 + style.ruby_gap_px + target.style.ruby_font_size_px / 2
            units = _reading_units(ruby.readings[reading_index], clock, ruby_column, top + first * height_per_cell,
                                   (end - first) * height_per_cell, len(measured.runs))
            if min(unit['wipe_top'] for unit in units) < previous_bottom:
                raise ValueError('Skia 竖排同列注音的墨迹范围不能重叠')
            previous_bottom = max(unit['wipe_bottom'] for unit in units)
            group = {'style_cell': first, 'units': units}
            if len(groups) > 1:
                group['span_end_cell'] = end
            ruby_groups.append(group)
        lines.append({'cells': cells, 'text_flow': 'vertical', 'ruby_groups': ruby_groups})

    return {'lines': lines, 'fonts': fonts, 'runs': [*measured.runs, *ruby.runs],
            'metrics_identity': hashlib.sha256(f'{measured.identity}:{ruby.identity}'.encode()).hexdigest(),
            'ruby_schema': 1, 'vertical_ruby_schema': version}
