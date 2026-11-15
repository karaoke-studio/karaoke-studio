"""Canonical metrics -> existing semantic plan -> final horizontal Skia IR.

Font discovery and process IPC are injected configuration capabilities. No Qt
measurement or rendering is used here, and frame draws never call this module.
"""
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.layout.display.section_edges import section_edge_context
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.layout.line.skia_geometry import positioned_cells
from krok_helper.subtitle_render.engine.layout.plan.skia_plan import prepare_skia_layout
from .render_ir import build_render_ir
from .skia_ruby_ir import prepared_ruby_layout
from .skia_lower_ir import lower_sources
from krok_helper.subtitle_render.engine.ruby.timing import is_utopia_group_marker


def _validate_style(style):
    if style.vertical or style.right_to_left or style.layout_semantics not in {'legacy', 'n3_1074'}:
        raise ValueError('Skia 当前规范布局仅支持正向横排')
    if style.dual_line_layout:
        if style.line_horizontal_layout == 'asymmetric' and style.smart_horizontal not in {'none','center_position','equal_margins'}:
            raise ValueError('Skia 智能水平布局模式无效')
        if len(style.line_alignments) > 32:
            raise ValueError('Skia 当前最多支持 32 行网格')


def build_skia_render_ir(track, style, *, measure, resolve_font, **kwargs):
    if kwargs.get('extra_tracks'):
        raise ValueError('Skia 当前规范布局尚不支持附加轨道')
    prepared = []

    def build_plan(source, source_style, **plan_kwargs):
        _validate_style(source_style)
        if any(c.vector_glyph for l in source.lines for c in l.chars):
            raise ValueError('Skia 当前规范布局尚不支持导唱符')
        if any(is_utopia_group_marker(ruby) for ruby in source.rubies):
            raise ValueError('Skia 最终注音组尚未验收空读音或短语标记')
        with layout_pass():
            section_edge_context(source, source_style)
            for line in source.lines:
                _validate_style(style_for_line(source_style, line))
            result = prepare_skia_layout(source, source_style, measure=measure, resolve_font=resolve_font, **plan_kwargs)
        prepared.append(result)
        return result.plan

    ir = build_render_ir(track, style, layout_plan_builder=build_plan,
                         serialize_source_baselines=False, **kwargs)
    plan, measured, geometry = prepared[0].plan, prepared[0].measured, prepared[0].geometry
    geometry.set_lanes(plan.lines)
    lines = []
    for index, item in enumerate(plan.lines):
        position = geometry.position(item, kwargs['width'], kwargs['height'])
        lines.append({'cells': positioned_cells(geometry.locals[index], position)})
    ir['required_features'] = ['p05_resolved_horizontal', 'p05_single_line_placement', 'p05_inline_roles',
                               'p05_fixed_row_grid', 'p05_page_collisions', 'p05_smart_horizontal', 'p05_segmented_wipe']
    ir['skia_layout'] = {'version': 2, 'wipe_schema': 1, 'metrics_identity': measured.identity,
                         'fonts': measured.fonts, 'runs': measured.runs, 'lines': lines}
    if geometry.ruby is not None:
        lower_enabled = any(item.layout_style.lower_ruby_enabled for item in plan.lines)
        sources, indices = lower_sources(prepared[0])
        ir['skia_layout'] = prepared_ruby_layout(prepared[0], width=kwargs['width'], height=kwargs['height'],
            dpr=kwargs.get('dpr', 1), lower_source_indices=indices if lower_enabled else None)
        ir['track']['rubies'].extend(sources)
        ir['required_features'] += ['p05_resolved_ruby', 'p05_upper_ruby']
        if lower_enabled:
            ir['required_features'] += ['p05_lower_ruby', 'p05_overlap_wipe']
    return ir
