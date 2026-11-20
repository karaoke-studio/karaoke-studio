"""Qt-free canonical geometry of vector guide glyphs (vector_guide_schema=1).

The existing GPU draws a guide from its M/L/C/Q/Z commands scaled by
``max(size, 1) / units_per_em`` (y-down, baseline 0, winding fill), from the
cell's left edge, and advances by the *unrounded* ``size * advance / em``
(at least 1). Commands are first normalized exactly like the IR glyph table
(two decimals), so the native renderer re-derives the same advance and ink.
"""
from __future__ import annotations

import math


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
    """vector_guide_schema=1 subset: solid horizontal LTR main text only."""
    from .semantics import guide_symbol_is_bitmap, render_line_with_guide_symbols
    from ..layout.line.style import style_for_line
    for line in track.lines:
        cells = [c for c in render_line_with_guide_symbols(line).chars if c.vector_glyph is not None]
        if not cells:
            continue
        appearance = style_for_line(style, line)
        if any(guide_symbol_is_bitmap(c.vector_glyph) or not c.vector_glyph.path_commands for c in cells):
            raise ValueError('Skia 导唱符当前仅支持矢量轮廓')
        if (appearance.vertical or appearance.right_to_left or appearance.lower_ruby_enabled
                or any(c.role_label is not None for c in cells)):
            raise ValueError('Skia 导唱符仅支持已验证的横排实色正文子集')


# Verified with a line-level prefix guide: Utopia singing, upper ruby, solid
# double outlines and glow. Any other capability keeps the plain subset only.
VECTOR_GUIDE_COMPOSITION_TRIGGERS = frozenset({
    'p05_resolved_ruby', 'p06_primary_solid_outline', 'p06_static_glow', 'p07_singing_scale'})
VECTOR_GUIDE_COMPOSITION_FEATURES = frozenset({
    'p05_fixed_row_grid', 'p05_inline_roles', 'p05_overlap_wipe', 'p05_page_collisions',
    'p05_resolved_horizontal', 'p05_resolved_ruby', 'p05_segmented_wipe', 'p05_single_line_placement',
    'p05_smart_horizontal', 'p05_upper_ruby', 'p05_vector_guides', 'p05_vector_guide_composition',
    'p06_outline_ruby', 'p06_primary_solid_outline', 'p06_ruby_glow', 'p06_ruby_solid_outline',
    'p06_secondary_solid_outline', 'p06_static_glow', 'p07_ruby_singing_scale', 'p07_singing_glow',
    'p07_singing_scale'})


def compose_vector_guides(track, features):
    """Append p05_vector_guide_composition, or reject an unverified combination."""
    from .semantics import render_line_with_guide_symbols
    if 'p05_vector_guides' not in features or not VECTOR_GUIDE_COMPOSITION_TRIGGERS.intersection(features):
        return
    for line in track.lines:
        if not any(c.vector_glyph is not None for c in render_line_with_guide_symbols(line).chars):
            continue
        if line.inline_guide_symbols or line.guide_symbol is None or line.guide_symbol.replacement_prefix:
            raise ValueError('Skia 导唱符组合仅支持整行前置导唱符')
    if not set(features) <= VECTOR_GUIDE_COMPOSITION_FEATURES - {'p05_vector_guide_composition'}:
        raise ValueError('Skia 导唱符组合仅支持已验证的 Utopia、上注音、实色描边与光晕')
    features.append('p05_vector_guide_composition')


def vector_guide_metrics(glyph, size, font_metrics, glyph_id):
    """Cell metrics payload: glyphless, keyed by the deduplicated glyph table id."""
    ink = vector_guide_ink(glyph, size)
    return {'advance': vector_guide_advance(glyph, size), 'ascent': float(font_metrics['ascent']),
            'descent': float(font_metrics['descent']), 'leading': float(font_metrics.get('leading', 0.0)),
            'ink': list(ink) if ink else [0.0, 0.0, 0.0, 0.0], 'glyphs': [], 'outline_available': True,
            'design_advance': 0.0, 'design_left_bearing': 0.0, 'design_right_bearing': 0.0,
            'font_identity': f'vector:{glyph_id}', 'vector_glyph_id': glyph_id, 'selected_font': 0}
