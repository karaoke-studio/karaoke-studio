"""Canonical metrics -> existing semantic plan -> final single-line Skia IR.

Font discovery and process IPC are injected configuration capabilities. No Qt
measurement or rendering is used here, and frame draws never call this module.
"""
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.layout.display.section_edges import section_edge_context
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.layout.plan.semantic import LayoutPlanResolvers, build_track_layout_plan
from krok_helper.subtitle_render.engine.layout.plan.page_offsets import PageOffsetResolvers, resolve_page_offset_windows
from krok_helper.subtitle_render.engine.text.skia_metrics import measure_track
from .render_ir import build_render_ir


def _unsupported(*args, **kwargs):
    raise ValueError('Skia 当前规范布局尚不支持多行分页或避让')


def _page_offsets(w, h, track, style):
    # Use the authoritative policy's single-line early return, not a second pager.
    return resolve_page_offset_windows(w, h, track, style, PageOffsetResolvers(
        display_lines=_unsupported, measure_lines=_unsupported))


def _validate_style(style):
    if (style.dual_line_layout or style.vertical or style.right_to_left or style.layout_semantics != 'legacy'
            or style.lower_ruby_enabled):
        raise ValueError('Skia 当前规范布局仅支持 legacy 单行横排，不支持角色、注音或导唱符')


def build_skia_render_ir(track, style, *, measure, resolve_font, **kwargs):
    if kwargs.get('extra_tracks'):
        raise ValueError('Skia 当前规范布局尚不支持附加轨道')
    prepared = []

    def build_plan(source, source_style, **plan_kwargs):
        _validate_style(source_style)
        if source.rubies or any(c.role_label or c.vector_glyph for l in source.lines for c in l.chars):
            raise ValueError('Skia 当前规范布局尚不支持角色、注音或导唱符')
        with layout_pass():
            section_edge_context(source, source_style)
            styles = []
            for line in source.lines:
                resolved = style_for_line(source_style, line)
                _validate_style(resolved)
                styles.append(resolved)
            measured = measure_track(source, source_style, styles, measure=measure, resolve_font=resolve_font)
            ports = LayoutPlanResolvers(_unsupported, _page_offsets, measured.snapshot.resolve_intervals, measured.identity)
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
        center = kwargs['width'] / 2 + (resolved.line_offsets_x[0] if resolved.line_offsets_x else 0)
        cursor = 0
        baseline = kwargs['height'] - resolved.line_y_margin_px - line_metrics.baseline_metrics['descent']
        cells = []
        ink_ranges = []
        for cell_index, (m, width, offset) in enumerate(zip(metrics, line_metrics.widths, line_metrics.offsets, strict=True)):
            x = cursor + offset
            ink = m['ink']
            visible = ink[2] > ink[0] and ink[3] > ink[1]
            if visible:
                ink_ranges.append((x + ink[0], x + ink[2]))
            cells.append({'metrics': m, 'run_index': line_metrics.run_indices[cell_index], 'origin_x': x, 'baseline_y': baseline,
                          'wipe_left': x + ink[0] if visible else cursor,
                          'wipe_right': x + ink[2] if visible else cursor + width})
            cursor += max(width + resolved.letter_spacing_px, 0) if cell_index + 1 < len(metrics) else width
        # Legacy anchors the actual ink union. N3's cell-grid anchoring is a
        # different semantic and remains gated until its own geometry is ready.
        left = min((r[0] for r in ink_ranges), default=0)
        right = max((r[1] for r in ink_ranges), default=max(cursor, 1))
        dx = center - (left + right) / 2
        for cell in cells:
            for key in ('origin_x', 'wipe_left', 'wipe_right'):
                cell[key] += dx
        lines.append({'cells': cells})
    ir['required_features'] = ['p05_resolved_horizontal']
    ir['skia_layout'] = {'version': 2, 'metrics_identity': measured.identity,
                         'fonts': measured.fonts, 'runs': measured.runs, 'lines': lines}
    return ir
