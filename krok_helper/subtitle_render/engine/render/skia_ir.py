"""Canonical metrics -> existing semantic plan -> final single-line Skia IR.

Font discovery and process IPC are injected configuration capabilities. No Qt
measurement or rendering is used here, and frame draws never call this module.
"""
import hashlib
import json
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.layout.display.section_edges import section_edge_context
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.layout.plan.semantic import LayoutPlanResolvers, build_track_layout_plan
from krok_helper.subtitle_render.engine.layout.plan.page_offsets import PageOffsetResolvers, resolve_page_offset_windows
from krok_helper.subtitle_render.engine.text.metric_snapshot import CellMetric, LineMetric, TextMetricSnapshot
from krok_helper.subtitle_render.engine.text.skia_width import cell_geometry
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
    fonts, prepared = {}, []

    def build_plan(source, source_style, **plan_kwargs):
        _validate_style(source_style)
        if source.rubies or any(c.role_label or c.vector_glyph for l in source.lines for c in l.chars):
            raise ValueError('Skia 当前规范布局尚不支持角色、注音或导唱符')
        runs, styles = [], []
        with layout_pass():
            section_edge_context(source, source_style)
            for line in source.lines:
                resolved = style_for_line(source_style, line)
                _validate_style(resolved)
                resource, options = resolve_font(resolved)
                key = hashlib.sha256(json.dumps(resource, sort_keys=True).encode()).hexdigest()
                fonts[key] = resource
                runs.append({'font_ids': [key], 'size': resolved.font_size_px,
                             'cells': [c.text for c in line.chars], **options})
                styles.append(resolved)
            measured = measure(fonts, runs) if runs else []
            snapshots, widths, offsets = [], [], []
            for line, resolved, result in zip(source.lines, styles, measured, strict=True):
                cells = result['cells']
                if len(cells) != len(line.chars): raise ValueError('Skia 批量度量单元数失配')
                geometry = [cell_geometry(c.text, m, resolved) for c, m in zip(line.chars, cells, strict=True)]
                widths.append([g[0] for g in geometry]); offsets.append([g[1] for g in geometry])
                snapshots.append(LineMetric.capture(line, source_style,
                    [CellMetric.from_payload(c.text, m) for c, m in zip(line.chars, cells, strict=True)],
                    layout_widths=widths[-1]))
            snapshot = TextMetricSnapshot(tuple(snapshots))
            ports = LayoutPlanResolvers(_unsupported, _page_offsets, snapshot.resolve_intervals, snapshot.identity)
            plan = build_track_layout_plan(source, source_style, ports, **plan_kwargs)
            prepared.append((plan, runs, measured, widths, offsets, snapshot.identity))
            return plan

    ir = build_render_ir(track, style, layout_plan_builder=build_plan, **kwargs)
    plan, runs, measured, widths, offsets, identity = prepared[0]
    lines = []
    for index, item in enumerate(plan.lines):
        resolved = item.layout_style
        metrics = measured[index]['cells']
        center = kwargs['width'] / 2 + (resolved.line_offsets_x[0] if resolved.line_offsets_x else 0)
        cursor = 0
        baseline = kwargs['height'] - resolved.line_y_margin_px - max((m['descent'] for m in metrics), default=0)
        cells = []
        ink_ranges = []
        for cell_index, (m, width, offset) in enumerate(zip(metrics, widths[index], offsets[index], strict=True)):
            x = cursor + offset
            ink = m['ink']
            visible = ink[2] > ink[0] and ink[3] > ink[1]
            if visible:
                ink_ranges.append((x + ink[0], x + ink[2]))
            cells.append({'metrics': m, 'origin_x': x, 'baseline_y': baseline,
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
        lines.append({'run': {k: v for k, v in runs[index].items() if k != 'cells'}, 'cells': cells})
    ir['required_features'] = ['p05_resolved_horizontal']
    ir['skia_layout'] = {'version': 1, 'metrics_identity': identity, 'fonts': fonts, 'lines': lines}
    return ir
