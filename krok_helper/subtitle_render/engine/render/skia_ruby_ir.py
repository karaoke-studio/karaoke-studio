"""Serialize prepared upper-ruby geometry and clocks for the native group probe.

This does not open the product ruby gate. Source ownership, page placement and
timing have already been resolved by the canonical preparation boundary.
"""
from strange_uta_game.backend.domain.graphemes import split_graphemes
from krok_helper.subtitle_render.engine.layout.line.skia_geometry import positioned_cells
from krok_helper.subtitle_render.engine.ruby.skia_timing import prepare_ruby_wipe_timing, wipe_points_to_ir
from krok_helper.subtitle_render.engine.ruby.timing import is_utopia_group_marker


def prepared_ruby_layout(prepared, *, width, height):
    geometry, measured = prepared.geometry, prepared.measured
    ruby = geometry.ruby
    if ruby is None:
        raise ValueError('Skia 最终注音布局缺少准备几何')
    if any(is_utopia_group_marker(annotation) for annotation in geometry.track.rubies):
        raise ValueError('Skia 最终注音组尚未验收空读音或短语标记')
    fonts = dict(measured.fonts)
    for key, resource in ruby.measured.fonts.items():
        if key in fonts and fonts[key] != resource:
            raise ValueError('Skia 注音字体资源身份冲突')
        fonts[key] = resource
    run_offset = len(measured.runs)
    lines = []
    for item, local in zip(prepared.plan.lines, ruby.lines, strict=True):
        position = geometry.position(item, width, height)
        timing = prepare_ruby_wipe_timing(item.line, local, item.layout_style.ruby_main_progress_mode)
        cells = positioned_cells(local.main, position)
        for cell, curve in zip(cells, timing.main, strict=True):
            if curve is not None:
                cell['wipe_points'] = wipe_points_to_ir(curve)
        groups = []
        for group, clock in zip(local.rubies, timing.readings, strict=True):
            if is_utopia_group_marker(group.target.effective):
                raise ValueError('Skia 最终注音组尚未验收空读音或短语标记')
            if clock.units != tuple(split_graphemes(group.target.effective.reading)):
                raise ValueError('Skia 注音 parts 跨越塑形单元边界，尚不支持此组合')
            if not clock.units:
                continue
            indices = group.target.indices
            style_cell = next((i for i in indices if item.line.chars[i].role_label), indices[0])
            units = []
            for cell, text, interval in zip(group.cells, clock.units, clock.intervals, strict=True):
                units.append({**cell, 'run_index': cell['run_index']+run_offset, 'text': text,
                    'interval_ms': list(interval), 'baseline_y': cell['baseline_y']+position.baseline_y,
                    **{key: cell[key]+position.offset_x for key in ('origin_x', 'wipe_left', 'wipe_right')}})
            groups.append({'style_cell': style_cell, 'units': units})
        lines.append({'cells': cells, 'ruby_groups': groups})
    return {'version': 2, 'wipe_schema': 1, 'ruby_schema': 1, 'metrics_identity': ruby.identity,
            'fonts': fonts, 'runs': [*measured.runs, *ruby.measured.runs], 'lines': lines}
