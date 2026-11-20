"""Project the static line-band rectangle from measured text and display placement."""

import math

from ..layout.line.skia_placement import legacy_ruby_allowance


def write_static_line_band(ir, style, prepared, *, width, height):
    if not style.line_band_enabled or not style.line_band_image:
        return
    if not style.line_band_image.startswith('preset:') and not ir['style'].get('line_band_image'):
        return
    if (not style.line_band_image.lower().endswith('.png') or style.line_band_image.startswith('preset:')
            or style.line_band_fill_mode != 'stretch' or style.vertical or style.right_to_left
            or style.line_band_tint or style.line_band_edge_kind != 'none'
            or style.line_band_shadow_enabled or style.line_band_stroke_enabled or style.line_band_glow_enabled
            or style.volume_enabled or style.lit_enabled or prepared.geometry.ruby is not None
            or any(char.role_label or char.vector_glyph is not None
                   for item in prepared.plan.lines for char in item.render_line.chars)):
        raise ValueError('Skia 行背景带当前支持静态横排 PNG 拉伸')
    if any(source.get('entry_anim') not in {'none', 'fade', 'slide_in', 'rise'}
           or source.get('exit_anim') not in {'none', 'fade', 'slide_out', 'rise'}
           or source.get('scanline') or source.get('fx_bursts')
           for source in ir['track']['lines']):
        raise ValueError('Skia 行背景带缺少对应行出入场能力')
    geometry = prepared.geometry
    for item, final in zip(prepared.plan.lines, ir['skia_layout']['lines'], strict=True):
        at = geometry.position(item, width, height)
        local = geometry.locals[item.track_index]
        measured = prepared.measured.lines[item.track_index]
        reference = measured.slot_metrics or (measured.baseline_metrics,)
        top = -max(float(run['ascent']) for run in reference)
        bottom = max(float(run['descent']) for run in reference)
        left, right = 0, max(local.layout_width, 1)
        look = item.layout_style
        pad = math.ceil((max(look.stroke_width_px, 0)
                         + (max(look.stroke2_width_px, 0) if look.stroke2_enabled else 0)) / 2)
        if local.ink and local.ink[3] - local.ink[1] + 2 * pad >= (bottom - top) / 2:
            left, top, right, bottom = (local.ink[0] - pad, local.ink[1] - pad,
                                       local.ink[2] + pad, local.ink[3] + pad)
        y = at.baseline_y + style.line_band_offset_y_px
        if (look.layout_semantics == 'legacy' and look.line_y_position == 'center'
                and not look.dual_line_layout and measured.row_frame is None):
            # Direct2D centers the single-row band on the font box. The subtitle
            # itself includes the style's upper reading allowance when centered.
            y -= legacy_ruby_allowance(look) / 2
        final['line_band_rect'] = [at.offset_x + left - style.line_band_pad_left_px,
                                   y + top - style.line_band_pad_top_px,
                                   at.offset_x + right + style.line_band_pad_right_px,
                                   y + bottom + style.line_band_pad_bottom_px]
    ir['skia_layout']['line_band_schema'] = 1
    ir['required_features'].append('p08_static_line_band')
