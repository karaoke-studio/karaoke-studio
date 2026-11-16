"""Serialize final upper/lower ruby geometry and clocks from the canonical plan.

Source ownership, page placement and timing are resolved before this boundary.
"""
from strange_uta_game.backend.domain.graphemes import split_graphemes
from krok_helper.subtitle_render.engine.layout.line.skia_geometry import positioned_cells
from krok_helper.subtitle_render.engine.ruby.skia_timing import prepare_ruby_wipe_timing, wipe_points_to_ir
from krok_helper.subtitle_render.engine.ruby.timing import is_utopia_group_marker
from krok_helper.subtitle_render.engine.ruby.skia_overlap import prepare_overlap_wipes
from .skia_lower_ir import lower_solid_paint
from krok_helper.subtitle_render.engine.ruby.skia_standalone_marks import starts_with_mark


def prepared_ruby_layout(prepared, *, width, height, dpr=1, lower_source_indices=None):
    geometry, measured = prepared.geometry, prepared.measured
    ruby = geometry.ruby
    if ruby is None:
        raise ValueError('Skia 最终注音布局缺少准备几何')
    if lower_source_indices is None and any(group.target.lower for line in ruby.lines for group in line.rubies):
        raise ValueError('Skia 下方注音最终绘制需要完整来源映射')
    markers = any(is_utopia_group_marker(r) for r in geometry.track.rubies)
    timings = [prepare_ruby_wipe_timing(item.line, local, item.layout_style.ruby_main_progress_mode)
               for item, local in zip(prepared.plan.lines, ruby.lines, strict=True)]
    standalone_marks = any(starts_with_mark(unit) for timing in timings for c in timing.readings for unit in c.glyph_units)
    unit_projection = standalone_marks or any(c.units != tuple(split_graphemes(g.target.effective.reading)) or c.glyph_units != c.units
        for local, timing in zip(ruby.lines, timings, strict=True)
        for g, c in zip(local.rubies, timing.readings, strict=True))
    fonts = dict(measured.fonts)
    for key, resource in ruby.measured.fonts.items():
        if key in fonts and fonts[key] != resource:
            raise ValueError('Skia 注音字体资源身份冲突')
        fonts[key] = resource
    run_offset = len(measured.runs)
    lines = []
    for item, local, timing in zip(prepared.plan.lines, ruby.lines, timings, strict=True):
        position = geometry.position(item, width, height)
        reverse = item.line.wipe_reverse
        if reverse and (local.rubies or any(curve is not None for curve in timing.main)):
            raise ValueError('Skia 反向走字行不能携带注音组或注音分段时钟（现有 GPU 省略该行注音）')
        # The GPU adjusts every main/annotation endpoint (skia_overlap).
        overlap = prepare_overlap_wipes(local, measured.lines[item.track_index], timing, dpr=dpr, reverse=reverse)
        cells = positioned_cells(local.main, position)
        for index, (cell, curve) in enumerate(zip(cells, timing.main, strict=True)):
            cell['wipe_end_position'] = overlap[0][index]
            if curve is not None:
                cell['wipe_points'] = wipe_points_to_ir(curve)
        groups = []
        for group_index, (group, clock) in enumerate(zip(local.rubies, timing.readings, strict=True)):
            if clock.glyph_units != group.glyph_units:
                raise ValueError('Skia 注音计时单元与已测量字形不一致')
            if len(clock.units) != len(group.cells):
                raise ValueError('Skia 注音最终时钟与已测量字形数量不一致')
            if not clock.units:
                continue
            indices = group.target.indices
            style_cell = next((i for i in indices if item.line.chars[i].role_label), indices[0])
            units = []
            for unit_index, (cell, text, interval) in enumerate(zip(group.cells, clock.units, clock.intervals, strict=True)):
                units.append({**cell, 'run_index': cell['run_index']+run_offset, 'text': text,
                    'interval_ms': list(interval), 'baseline_y': cell['baseline_y']+position.baseline_y,
                    **{key: cell[key]+position.offset_x for key in ('origin_x', 'wipe_left', 'wipe_right')}})
                units[-1]['wipe_end_position'] = overlap[1][group_index][unit_index]
                if unit_projection:
                    units[-1]['glyph_text'] = clock.glyph_units[unit_index]
            serialized = {'style_cell': style_cell, 'units': units}
            if group.target.lower:
                serialized.update(lower=True, source_index=lower_source_indices[group.target.annotation_index],
                                  paint=lower_solid_paint(group.target.style))
            groups.append(serialized)
        lines.append({'cells': cells, 'ruby_groups': groups})
    return {'version': 2, 'wipe_schema': 1, 'ruby_schema': 1, 'metrics_identity': ruby.identity,
            **({'ruby_marker_schema': 1} if markers else {}),
            **({'ruby_unit_schema': 1} if unit_projection else {}),
            **({'standalone_mark_schema': 1} if standalone_marks else {}),
            'overlap_schema': 1,
            **({'lower_ruby_schema': 1} if lower_source_indices is not None else {}),
            'fonts': fonts, 'runs': [*measured.runs, *ruby.measured.runs], 'lines': lines}
