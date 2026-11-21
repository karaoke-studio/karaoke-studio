"""Declare the outline layers needed by the final Skia glyphs."""

from ..style.style_semantics import style_for_role_in_layout


def declare_outline_layers(ir, plan, *, title):
    styles = [item.layout_style for item in plan.lines]
    role_styles = [
        style_for_role_in_layout(item.layout_style, label)
        for item in plan.lines
        for label in {cell.role_label for cell in item.render_line.chars
                      if cell.role_label in item.layout_style.custom_style_schemes}
    ]
    styles.extend(role_styles)
    secondary = any(style.stroke2_enabled and style.stroke2_width_px > 0 for style in styles)
    if secondary or any(style.stroke_width_px > 0 for style in styles):
        ir['required_features'].append('p06_primary_solid_outline')
        if role_styles:
            ir['required_features'].append('p08_title_role_outlines' if title else 'p06_role_solid_outlines')
    if secondary:
        ir['required_features'].append('p06_secondary_solid_outline')
    return secondary
