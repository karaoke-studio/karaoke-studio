"""Declare the outline layers needed by the final Skia glyphs."""

from ..style.style_semantics import style_for_role_in_layout
from ..text.font_slots import is_n3_latin_text, main_script_stroke_style


def declare_outline_layers(ir, plan, *, title):
    styles = [item.layout_style for item in plan.lines]
    has_roles = latin_override = latin_outline = latin_outer_override = latin_outer = False
    for item in plan.lines:
        base = item.layout_style
        labels = {cell.role_label for cell in item.render_line.chars
                  if cell.role_label in base.custom_style_schemes}
        roles = {label: style_for_role_in_layout(base, label) for label in labels}
        styles.extend(roles.values())
        has_roles |= bool(roles)
        for cell in item.render_line.chars:
            if cell.vector_glyph is not None or not is_n3_latin_text(cell.text):
                continue
            look = roles.get(cell.role_label, base)
            latin = main_script_stroke_style(look, cell.text)
            latin_outline |= latin.stroke_width_px > 0
            latin_override |= latin.stroke_width_px != look.stroke_width_px
            outer = latin.stroke2_width_px if latin.stroke2_enabled else 0
            latin_outer |= outer > 0
            latin_outer_override |= outer != (look.stroke2_width_px if look.stroke2_enabled else 0)
    secondary = latin_outer or any(style.stroke2_enabled and style.stroke2_width_px > 0 for style in styles)
    if secondary or latin_outline or any(style.stroke_width_px > 0 for style in styles):
        ir['required_features'].append('p06_primary_solid_outline')
        if has_roles:
            ir['required_features'].append('p08_title_role_outlines' if title else 'p06_role_solid_outlines')
    if latin_override:
        ir['required_features'].append('p06_latin_primary_outline')
    if secondary:
        ir['required_features'].append('p06_secondary_solid_outline')
    if latin_outer_override:
        ir['required_features'].append('p06_latin_secondary_outline')
    return secondary
