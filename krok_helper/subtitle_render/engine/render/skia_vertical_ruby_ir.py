"""Final vertical ruby positions and clips over the shared upper-reading clocks."""
import hashlib
import math

from ..layout.line.skia_vertical import upright_vertical_text, vertical_cells, vertical_line_box
from ..ruby.skia_geometry import upper_ruby_targets
from ..ruby.skia_metrics import measure_timed_readings
from ..ruby.skia_timing import prepare_ruby_target_timing, wipe_points_to_ir


def vertical_ruby_layout(prepared, *, width, height, dpr, resolve_font, measure):
    geometry, measured = prepared.geometry, prepared.measured
    track = geometry.track
    annotation_indices = {id(ruby): i for i, ruby in enumerate(track.rubies)}
    entries, readings = [], []
    version = 1
    for index, (line, style) in enumerate(zip(track.lines, geometry.styles, strict=True)):
        intervals = measured.snapshot.resolve_intervals(line, geometry.style)
        targets = upper_ruby_targets(track, index, line, style, intervals, annotation_indices)
        if len(targets) > 1:
            raise ValueError('Skia 竖排每行仅支持一组直立注音')
        timing = prepare_ruby_target_timing(line, targets, style.ruby_main_progress_mode)
        if targets:
            target = targets[0]
            clock = timing.readings[0]
            if (not clock.units or clock.units != clock.glyph_units
                    or any(not upright_vertical_text(unit) for unit in clock.units)):
                raise ValueError('Skia 竖排注音单元必须是单个直立字符')
            if len(clock.units) > 1:
                version = 2
            entries.append((target, timing, len(readings)))
            readings.append((target.effective, target.style))
        else:
            entries.append(None)

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
        entry = entries[item.track_index]
        if entry is None:
            lines.append({'cells': vertical_cells(local, main, item, width=width, height=height),
                          'text_flow': 'vertical', 'ruby_groups': []})
            continue
        target, timing, reading_index = entry
        reading = ruby.readings[reading_index]
        style = item.layout_style
        ascent = float(reading.baseline_metrics['ascent'])
        descent = float(reading.baseline_metrics['descent'])
        # The allowance is rounded on the device grid, before the ruby gap.
        allowance = max(math.floor((ascent + descent) * dpr + 0.5) / dpr + style.ruby_gap_px, 0.0)
        cells = vertical_cells(local, main, item, width=width, height=height, ruby_allowance=allowance)
        for cell, curve in zip(cells, timing.main, strict=True):
            if curve is not None:
                cell['wipe_points'] = wipe_points_to_ir(curve)
        column, top, height_per_cell, _ = vertical_line_box(item, main, len(cells), width, height, allowance)
        ruby_column = column + style.font_size_px / 2 + style.ruby_gap_px + target.style.ruby_font_size_px / 2
        first = min(target.indices)
        span_height = (max(target.indices) - first + 1) * height_per_cell
        reading_top = top + first * height_per_cell
        units = []
        for index, (layout, metrics, run, interval) in enumerate(zip(
                reading.unit_layouts, reading.cells, reading.run_indices, timing.readings[0].intervals, strict=True)):
            name, cell_width, glyph_offset, *_ = layout
            left, ink_top, right, ink_bottom = metrics['ink']
            if right <= left or ink_bottom <= ink_top:
                raise ValueError('Skia 竖排注音需要有墨迹的矢量字形')
            x = ruby_column + glyph_offset - cell_width / 2
            baseline = reading_top + index * span_height / len(reading.cells) + ascent
            units.append({'text': name, 'metrics': metrics, 'run_index': len(measured.runs) + run,
                          'origin_x': x, 'baseline_y': baseline, 'wipe_left': x + left, 'wipe_right': x + right,
                          'wipe_top': baseline + ink_top, 'wipe_bottom': baseline + ink_bottom,
                          'wipe_end_position': 1.0, 'interval_ms': list(interval)})
        lines.append({'cells': cells, 'text_flow': 'vertical',
                      'ruby_groups': [{'style_cell': first, 'units': units}]})

    return {'lines': lines, 'fonts': fonts, 'runs': [*measured.runs, *ruby.runs],
            'metrics_identity': hashlib.sha256(f'{measured.identity}:{ruby.identity}'.encode()).hexdigest(),
            'ruby_schema': 1, 'vertical_ruby_schema': version}
