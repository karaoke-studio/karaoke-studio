"""Final vertical ruby positions and clips over the shared upper-reading clocks."""
import hashlib
import math

from ..layout.line.skia_vertical import (
    vertical_cells, vertical_glyph_rotates, vertical_glyph_shift, vertical_line_box,
)
from ..ruby.skia_timing import wipe_points_to_ir
from .skia_ruby_paint_ir import ruby_decoration_kinds, solid_ruby_outlines


def _reading_units(reading, clock, ruby_column, reading_top, span_height, run_offset, font_size, wipe_pad,
                   *, singing_scale):
    ascent = float(reading.baseline_metrics['ascent'])
    font_height = ascent + float(reading.baseline_metrics['descent'])
    units = []
    cell_height = span_height / len(reading.cells)
    for index, (layout, metrics, run, interval) in enumerate(zip(
            reading.unit_layouts, reading.cells, reading.run_indices, clock.intervals, strict=True)):
        name, cell_width, glyph_offset, *_ = layout
        left, ink_top, right, ink_bottom = metrics['ink']
        if right <= left or ink_bottom <= ink_top:
            raise ValueError('Skia 竖排注音需要有墨迹的矢量字形')
        shift = vertical_glyph_shift(name)
        x = ruby_column + glyph_offset - cell_width / 2 + font_size * shift
        cell_top = reading_top + index * span_height / len(reading.cells)
        baseline = cell_top + ascent - font_height * shift
        unit = {'text': name, 'metrics': metrics, 'run_index': run_offset + run,
                'origin_x': x, 'baseline_y': baseline, 'wipe_left': x + left - wipe_pad,
                'wipe_right': x + right + wipe_pad,
                'wipe_top': baseline + ink_top, 'wipe_bottom': baseline + ink_bottom,
                'wipe_end_position': 1.0, 'interval_ms': list(interval)}
        if vertical_glyph_rotates(name):
            pivot_y = baseline - ascent + font_height / 2
            unit['vertical_rotation_pivot'] = [ruby_column, pivot_y]
            unit['wipe_top'] = pivot_y + x + left - ruby_column
            unit['wipe_bottom'] = pivot_y + x + right - ruby_column
        if singing_scale:
            unit['vertical_singing_pivot'] = [ruby_column, cell_top + cell_height / 2]
        units.append(unit)
    return units


def vertical_ruby_layout(prepared, *, width, height, dpr, singing_tracks):
    geometry, measured = prepared.geometry, prepared.measured
    readings = prepared.vertical_readings
    ruby = readings.measured
    outlined = False
    decorations = set()
    fonts = dict(measured.fonts)
    for key, font in ruby.fonts.items():
        if key in fonts and fonts[key] != font:
            raise ValueError('Skia 注音字体资源身份冲突')
        fonts[key] = font

    lines = []
    for item in prepared.plan.lines:
        main = measured.lines[item.track_index]
        local = geometry.locals[item.track_index]
        main_clocks, groups = readings.entries[item.track_index]
        style = item.layout_style
        singing_scale = item.track_index in singing_tracks
        allowance = (geometry.vertical_reading_allowance if style.dual_line_layout
                     else readings.allowances[item.track_index])
        cells = vertical_cells(local, main, item, width=width, height=height, ruby_allowance=allowance,
                               singing_scale=singing_scale)
        for cell, curve in zip(cells, main_clocks, strict=True):
            if curve is not None:
                cell['wipe_points'] = wipe_points_to_ir(curve)
        column, top, height_per_cell, _ = vertical_line_box(item, main, len(cells), width, height, allowance)
        ruby_groups, previous_bottom = [], -math.inf
        for target, clock, reading_index in groups:
            first, end = min(target.indices), max(target.indices) + 1
            ruby_column = column + style.font_size_px / 2 + style.ruby_gap_px + target.style.ruby_font_size_px / 2
            outlines = solid_ruby_outlines(target.style)
            decoration = set(ruby_decoration_kinds(target.style)) & {'shadow', 'glow'}
            if singing_scale and 'shadow' in decoration:
                raise ValueError('Skia 竖排 Utopia 唱字暂不支持注音阴影')
            decorations.update(decoration)
            wipe_pad = (outlines.get('stroke', {}).get('width', 0) // 2) / dpr
            units = _reading_units(ruby.readings[reading_index], clock, ruby_column, top + first * height_per_cell,
                                   (end - first) * height_per_cell, len(measured.runs),
                                   target.style.ruby_font_size_px, wipe_pad, singing_scale=singing_scale)
            if min(unit['wipe_top'] for unit in units) < previous_bottom:
                raise ValueError('Skia 竖排同列注音的墨迹范围不能重叠')
            previous_bottom = max(unit['wipe_bottom'] for unit in units)
            group = {'style_cell': first, 'units': units}
            if outlines:
                group['outlines'] = outlines
                outlined = True
            if len(groups) > 1:
                group['span_end_cell'] = end
            ruby_groups.append(group)
        final = {'cells': cells, 'text_flow': 'vertical', 'ruby_groups': ruby_groups}
        if style.dual_line_layout:
            final['vertical_lane'] = item.lane
        lines.append(final)

    result = {'lines': lines, 'fonts': fonts, 'runs': [*measured.runs, *ruby.runs],
              'metrics_identity': hashlib.sha256(f'{measured.identity}:{ruby.identity}'.encode()).hexdigest(),
              'ruby_schema': 1, 'vertical_ruby_schema': readings.version}
    if readings.shifted:
        result['vertical_ruby_shift_schema'] = 1
    if readings.rotated:
        result['vertical_ruby_rotation_schema'] = 1
    if outlined:
        result['ruby_solid_outline_schema'] = 1
    result.update({f'ruby_{kind}_schema': 1 for kind in decorations})
    return result
