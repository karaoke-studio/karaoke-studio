"""Qt-free canonical geometry of vector guide glyphs (vector_guide_schema=1).

The existing GPU draws a guide from its M/L/C/Q/Z commands scaled by
``max(size, 1) / units_per_em`` (y-down, baseline 0, winding fill), from the
cell's left edge, and advances by the *unrounded* ``size * advance / em``
(at least 1). Commands are first normalized exactly like the IR glyph table
(two decimals), so the native renderer re-derives the same advance and ink.
"""
from __future__ import annotations

import math

from ...domain.models import effective_karaoke_animation
from ..render.skia_line_animation_ir import ordinary_line_animation
from ..style.style_semantics import uses_authored_decoration


def vector_guide_advance(glyph, size) -> float:
    return max(float(size) * glyph['advance_width'] / glyph['units_per_em'], 1.0)


def _extrema(points, degree):
    """Axis extrema parameters in (0, 1) of a quadratic or cubic Bezier."""
    out = []
    for axis in (0, 1):
        p = [pt[axis] for pt in points]
        if degree == 2:
            denominator = p[0] - 2 * p[1] + p[2]
            if denominator:
                out.append((p[0] - p[1]) / denominator)
            continue
        a = -p[0] + 3 * p[1] - 3 * p[2] + p[3]
        b = 2 * (p[0] - 2 * p[1] + p[2])
        c = p[1] - p[0]
        if abs(a) < 1e-12:
            if b:
                out.append(-c / b)
            continue
        disc = b * b - 4 * a * c
        if disc >= 0:
            root = math.sqrt(disc)
            out += [(-b + root) / (2 * a), (-b - root) / (2 * a)]
    return [t for t in out if 0 < t < 1]


def _bezier(points, t):
    if len(points) == 3:
        u = 1 - t
        return tuple(u * u * points[0][i] + 2 * u * t * points[1][i] + t * t * points[2][i] for i in (0, 1))
    u = 1 - t
    return tuple(u ** 3 * points[0][i] + 3 * u * u * t * points[1][i] + 3 * u * t * t * points[2][i]
                 + t ** 3 * points[3][i] for i in (0, 1))


def vector_guide_ink(glyph, size):
    """Tight ink bounds (left, top, right, bottom) in logical pixels, or None."""
    scale = max(float(size), 1.0) / glyph['units_per_em']
    points, current, open_figure = [], None, False
    for command in glyph['path_commands']:
        kind, values = command[0], [v * scale for v in command[1:]]
        if not all(math.isfinite(value) for value in values):
            raise ValueError('Skia 导唱符轮廓包含非有限坐标')
        pairs = [(values[i], values[i + 1]) for i in range(0, len(values) - 1, 2)]
        if kind == 'M' and len(values) == 2:
            current, open_figure = pairs[0], True
            points.append(current)
        elif kind == 'L' and len(values) == 2 and open_figure:
            current = pairs[0]
            points.append(current)
        elif kind in ('C', 'Q') and len(values) == (6 if kind == 'C' else 4) and open_figure:
            curve = [current, *pairs]
            points.append(curve[-1])
            points += [_bezier(curve, t) for t in _extrema(curve, 3 if kind == 'C' else 2)]
            current = curve[-1]
        elif kind == 'Z' and open_figure:
            open_figure = False
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    if not all(math.isfinite(v) for v in (*xs, *ys)):
        raise ValueError('Skia 导唱符轮廓包含非有限坐标')
    return min(xs), min(ys), max(xs), max(ys)


def validate_vector_guides(track, style):
    """Validate the geometry shared by the horizontal vector-guide profiles."""
    from .semantics import guide_symbol_is_bitmap, render_line_with_guide_symbols
    from ..layout.line.style import style_for_line
    role_guides = False
    for line in track.lines:
        # Bitmap guides follow the separate bitmap_guide_schema subset.
        chars = render_line_with_guide_symbols(line).chars
        cells = [c for c in chars
                 if c.vector_glyph is not None and not guide_symbol_is_bitmap(c.vector_glyph)]
        if not cells:
            continue
        appearance = style_for_line(style, line)
        if any(guide_symbol_is_bitmap(c.vector_glyph) or not c.vector_glyph.path_commands for c in cells):
            raise ValueError('Skia 导唱符当前仅支持矢量轮廓')
        if any(c.role_label in appearance.custom_style_schemes for c in cells):
            if appearance.vertical:
                pass  # Composition gate lifted: layers compose as in Direct2D.
            role_guides |= effective_karaoke_animation(appearance) == 'utopia'
    return role_guides


def project_vector_guide_roles(ir, plan_lines):
    """Use the line's paint and font slot for an unresolved guide label."""
    from .semantics import guide_symbol_is_bitmap

    for item in plan_lines:
        styles = item.layout_style.custom_style_schemes
        source = ir['track']['lines'][item.track_index]
        for cell, payload in zip(item.render_line.chars, source['chars'], strict=True):
            if (cell.vector_glyph is not None and not guide_symbol_is_bitmap(cell.vector_glyph)
                    and cell.role_label not in styles):
                payload.pop('role_label', None)


def vector_guides_form_prefix(chars):
    from .semantics import guide_symbol_is_bitmap
    guides = [c.vector_glyph is not None and not guide_symbol_is_bitmap(c.vector_glyph) for c in chars]
    count = sum(guides)
    return count > 0 and all(guides[:count])


def compose_vector_guides(plan_lines, ir):
    """Declare guide capabilities from the cells and effects that share a line."""
    from .semantics import guide_symbol_is_bitmap

    features = ir['required_features']
    if 'p05_vector_guides' not in features:
        return

    # These capabilities describe the IR version. The check below uses the
    # line's actual paint, geometry and timing to decide whether it needs them.
    required = set()
    if 'p05_lower_ruby' in features:
        required.update(('p05_vector_guide_composition', 'p05_vector_guide_lower_ruby'))
    has_outline_profile = 'p06_primary_solid_outline' in features
    for item, final in zip(plan_lines, ir['skia_layout']['lines'], strict=True):
        chars = item.render_line.chars
        if not any(c.vector_glyph is not None and not guide_symbol_is_bitmap(c.vector_glyph) for c in chars):
            continue

        look = item.layout_style
        line = item.line
        source = ir['track']['lines'][item.track_index]
        readings = final.get('ruby_groups', ())
        upper_reading = any(not group.get('lower') for group in readings)
        lower_reading = any(group.get('lower') for group in readings)
        has_shadow = uses_authored_decoration(look, 'shadow')
        has_glow = uses_authored_decoration(look, 'glow')
        singing = source.get('karaoke_anim') == 'utopia'
        plain_effects = not any(source.get(flag) for flag in ('scanline', 'zoom_pulse', 'stroke_flash', 'fx_bursts'))
        ordinary = source.get('karaoke_anim') == 'none' and plain_effects
        whole_line = ordinary and ordinary_line_animation(source)
        stationary = ordinary and source.get('entry_anim') == source.get('exit_anim') == 'none'
        forward = not (look.vertical or look.right_to_left or line.wipe_reverse)
        cell_transform = (ordinary and forward and 'p07_character_transform_cells' in features
                          and {source.get('entry_anim'), source.get('exit_anim')} <= {'char_drip', 'spin_flip', 'none'})
        needs_composition = has_outline_profile or upper_reading or lower_reading or has_shadow or has_glow or singing
        if not needs_composition:
            continue

        required.add('p05_vector_guide_composition')
        if look.vertical and (has_shadow or has_glow):
            required.add('p06_vertical_vector_guide_decoration')
        if lower_reading:
            allowed_modes = {'none', 'utopia', 'no_wipe'} if forward else {'none'}
            if (look.vertical or line.wipe_reverse or not plain_effects or not ordinary_line_animation(source)
                    or source.get('karaoke_anim') not in allowed_modes):
                pass  # Composition gate lifted: layers compose as in Direct2D.
        if has_shadow and not (look.vertical or line.wipe_reverse or upper_reading or source.get('scanline')
                               or 'p05_role_vector_guides' in features):
            if not (forward and whole_line) or any(c.role_label is not None for c in chars):
                pass  # Composition gate lifted: layers compose as in Direct2D.
            required.add('p06_vector_guide_shadow')
            if not stationary:
                required.add('p07_horizontal_shadow_line_animation')
            continue
        if vector_guides_form_prefix(chars):
            continue
        # Retain legacy declarations for clients that share an older sidecar.
        # Drawing itself uses the line's reading, outline and effect layers.
        if (upper_reading or lower_reading) and forward and whole_line:
            required.add('p05_inline_vector_guide_ruby')
        elif (has_outline_profile and (whole_line or cell_transform) and not look.vertical
              and not (upper_reading or lower_reading or has_glow or has_shadow)):
            required.add('p06_inline_vector_guide_outline')

    features.extend(sorted(required))


def vector_guide_metrics(glyph, size, font_metrics, glyph_id):
    """Cell metrics payload: glyphless, keyed by the deduplicated glyph table id."""
    ink = vector_guide_ink(glyph, size)
    return {'advance': vector_guide_advance(glyph, size), 'ascent': float(font_metrics['ascent']),
            'descent': float(font_metrics['descent']), 'leading': float(font_metrics.get('leading', 0.0)),
            'ink': list(ink) if ink else [0.0, 0.0, 0.0, 0.0], 'glyphs': [], 'outline_available': True,
            'design_advance': 0.0, 'design_left_bearing': 0.0, 'design_right_bearing': 0.0,
            'font_identity': f'vector:{glyph_id}', 'vector_glyph_id': glyph_id, 'selected_font': 0}
