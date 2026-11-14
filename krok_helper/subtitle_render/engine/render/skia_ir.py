"""Canonical metrics -> existing semantic plan -> final horizontal Skia IR.

Font discovery and process IPC are injected configuration capabilities. No Qt
measurement or rendering is used here, and frame draws never call this module.
"""
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.layout.display.section_edges import section_edge_context
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.layout.line.skia_geometry import positioned_cells
from krok_helper.subtitle_render.engine.layout.display.skia_schedule import display_lines
from krok_helper.subtitle_render.engine.layout.display.skia_collision import SkiaCollisionGeometry
from krok_helper.subtitle_render.engine.layout.plan.semantic import LayoutPlanResolvers, build_track_layout_plan
from krok_helper.subtitle_render.engine.layout.plan.page_offsets import PageOffsetResolvers, resolve_page_offset_windows
from krok_helper.subtitle_render.engine.text.skia_metrics import measure_track
from .render_ir import build_render_ir


def _validate_style(style):
    if (style.vertical or style.right_to_left or style.layout_semantics not in {'legacy', 'n3_1074'}
            or style.lower_ruby_enabled):
        raise ValueError('Skia 当前规范布局仅支持横排，不支持注音或导唱符')
    if style.dual_line_layout:
        if style.line_horizontal_layout == 'asymmetric' and style.smart_horizontal != 'none':
            raise ValueError('Skia 多行布局暂不支持智能水平避让')
        if len(style.line_alignments) > 32:
            raise ValueError('Skia 当前最多支持 32 行网格')


def build_skia_render_ir(track, style, *, measure, resolve_font, **kwargs):
    if kwargs.get('extra_tracks'):
        raise ValueError('Skia 当前规范布局尚不支持附加轨道')
    prepared = []

    def build_plan(source, source_style, **plan_kwargs):
        _validate_style(source_style)
        if source.rubies or any(c.vector_glyph for l in source.lines for c in l.chars):
            raise ValueError('Skia 当前规范布局尚不支持注音或导唱符')
        with layout_pass():
            section_edge_context(source, source_style)
            styles = []
            for line in source.lines:
                resolved = style_for_line(source_style, line)
                _validate_style(resolved)
                styles.append(resolved)
            measured = measure_track(source, source_style, styles, measure=measure, resolve_font=resolve_font)
            geometry = SkiaCollisionGeometry(source, source_style, styles, measured)
            def display(track, style, **size):
                return display_lines(track, style, geometry, **size)
            def offsets(w, h, track, style):
                return resolve_page_offset_windows(w, h, track, style,
                    PageOffsetResolvers(display, geometry.page_lines), metrics_identity=geometry.identity)
            ports = LayoutPlanResolvers(display, offsets, measured.snapshot.resolve_intervals, measured.identity)
            plan = build_track_layout_plan(source, source_style, ports, **plan_kwargs)
            prepared.append((plan, measured, geometry))
            return plan

    ir = build_render_ir(track, style, layout_plan_builder=build_plan, **kwargs)
    plan, measured, geometry = prepared[0]
    lines = []
    for index, item in enumerate(plan.lines):
        position = geometry.position(item, kwargs['width'], kwargs['height'])
        lines.append({'cells': positioned_cells(geometry.locals[index], position)})
    ir['required_features'] = ['p05_resolved_horizontal', 'p05_single_line_placement', 'p05_inline_roles',
                               'p05_fixed_row_grid', 'p05_page_collisions']
    ir['skia_layout'] = {'version': 2, 'metrics_identity': measured.identity,
                         'fonts': measured.fonts, 'runs': measured.runs, 'lines': lines}
    return ir
