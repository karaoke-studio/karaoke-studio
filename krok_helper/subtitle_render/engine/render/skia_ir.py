"""Canonical metrics -> existing semantic plan -> final horizontal Skia IR.

Font discovery and process IPC are injected configuration capabilities. No Qt
measurement or rendering is used here, and frame draws never call this module.
"""
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.layout.display.section_edges import section_edge_context
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.layout.line.skia_placement import place_horizontal_line
from krok_helper.subtitle_render.engine.layout.display.skia_schedule import display_lines
from krok_helper.subtitle_render.engine.layout.plan.semantic import LayoutPlanResolvers, build_track_layout_plan
from krok_helper.subtitle_render.engine.layout.plan.page_offsets import PageOffsetResolvers, resolve_page_offset_windows
from krok_helper.subtitle_render.engine.text.skia_metrics import measure_track
from .render_ir import build_render_ir


def _unsupported(*args, **kwargs):
    raise ValueError('Skia 当前规范布局尚不支持多行分页或避让')


def _page_offsets(w, h, track, style):
    # The shared policy's single-line/allowed-overlap early returns apply.
    return resolve_page_offset_windows(w, h, track, style, PageOffsetResolvers(
        display_lines=_unsupported, measure_lines=_unsupported))


def _validate_style(style):
    if (style.vertical or style.right_to_left or style.layout_semantics not in {'legacy', 'n3_1074'}
            or style.lower_ruby_enabled):
        raise ValueError('Skia 当前规范布局仅支持横排，不支持注音或导唱符')
    if style.dual_line_layout:
        if not style.allow_inter_page_line_overlap or style.auto_fill_section_time:
            raise ValueError('Skia 多行布局暂不支持跨页避让或段内填充，请启用允许跨页重叠并关闭段内填充')
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
            ports = LayoutPlanResolvers(display_lines, _page_offsets, measured.snapshot.resolve_intervals, measured.identity)
            plan = build_track_layout_plan(source, source_style, ports, **plan_kwargs)
            prepared.append((plan, measured))
            return plan

    ir = build_render_ir(track, style, layout_plan_builder=build_plan, **kwargs)
    plan, measured = prepared[0]
    lines = []
    for index, item in enumerate(plan.lines):
        resolved = item.layout_style
        line_metrics = measured.lines[index]
        metrics = line_metrics.cells
        cursor = 0
        cells = []
        ink_ranges = []
        for cell_index, (m, width, offset) in enumerate(zip(metrics, line_metrics.widths, line_metrics.offsets, strict=True)):
            x = cursor + offset
            ink = m['ink']
            visible = ink[2] > ink[0] and ink[3] > ink[1]
            if visible:
                ink_ranges.append((x + ink[0], x + ink[2]))
            cells.append({'metrics': m, 'run_index': line_metrics.run_indices[cell_index], 'origin_x': x,
                          'wipe_left': x + ink[0] if visible else cursor,
                          'wipe_right': x + ink[2] if visible else cursor + width})
            if width > 0:
                cursor += max(width + resolved.letter_spacing_px, 0) if cell_index + 1 < len(metrics) else width
        left = min((r[0] for r in ink_ranges), default=0)
        right = max((r[1] for r in ink_ranges), default=max(cursor, 1))
        position = place_horizontal_line(item, line_metrics.baseline_metrics, width=kwargs['width'], height=kwargs['height'],
                                     ink_left=left, ink_right=right, layout_width=cursor)
        for cell in cells:
            cell['baseline_y'] = position.baseline_y
            for key in ('origin_x', 'wipe_left', 'wipe_right'):
                cell[key] += position.offset_x
        lines.append({'cells': cells})
    ir['required_features'] = ['p05_resolved_horizontal', 'p05_single_line_placement', 'p05_inline_roles', 'p05_fixed_row_grid']
    ir['skia_layout'] = {'version': 2, 'metrics_identity': measured.identity,
                         'fonts': measured.fonts, 'runs': measured.runs, 'lines': lines}
    return ir
