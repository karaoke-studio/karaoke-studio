"""Project line-band rectangles from measured text and display placement."""

import math

from ...domain.band_presets import band_preset, preset_id_for_image
from ..layout.line.skia_placement import legacy_ruby_allowance
from ..ruby.style_semantics import ruby_stroke2_width, ruby_stroke_width


def _upper_reading_ink_top(group, ink_top):
    edge = int(ruby_stroke_width(group.target.style))
    edge2 = int(ruby_stroke2_width(group.target.style))
    # The final horizontal fill bounds use the placed ink and both pens.
    return group.baseline_offset + ink_top - (edge + edge2 + 1) // 2


def write_line_band(ir, style, prepared, *, width, height):
    if not style.line_band_enabled or not style.line_band_image:
        return
    preset_id = preset_id_for_image(style.line_band_image)
    preset = band_preset(preset_id) if preset_id is not None else None
    staff = preset_id == 'staff_scroll'
    material = ir['style'].get('line_band_image')
    if preset_id is None and not material:
        return
    animated = bool(preset and material and material.lower().endswith('.webp'))
    geometry = prepared.geometry
    ruby = geometry.ruby
    if ((not preset and (preset_id is not None or not style.line_band_image.lower().endswith('.png')))
            or style.line_band_fill_mode not in {'stretch', 'repeat', 'cover', 'three_slice'}
            or style.vertical or style.right_to_left
            or (animated and style.line_band_tint) or style.line_band_edge_kind != 'none'
            or style.line_band_shadow_enabled or style.line_band_stroke_enabled or style.line_band_glow_enabled
            or style.volume_enabled or style.lit_enabled or style.lower_ruby_enabled
            or any(char.role_label or char.vector_glyph is not None
                   for item in prepared.plan.lines for char in item.render_line.chars)):
        raise ValueError('Skia 行背景带当前支持横排静态 PNG 和内置预设')
    if preset and not material:
        raise ValueError('Skia 行背景带无法读取预设图片')
    if staff and any(not ir['style'].get(key) for key in
                     ('line_band_image', 'line_band_lines_image', 'line_band_clef_image')):
        raise ValueError('Skia 五线谱背景带缺少预设图层')
    if any(source.get('entry_anim') not in {'none', 'fade', 'slide_in', 'rise'}
           or source.get('exit_anim') not in {'none', 'fade', 'slide_out', 'rise'}
           for source in ir['track']['lines']):
        raise ValueError('Skia 行背景带缺少对应行出入场能力')
    if any(source.get('scanline') or source.get('fx_bursts') for source in ir['track']['lines']):
        ir['required_features'].append('p08_line_band_with_effects')
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
        if ruby is not None and style.line_band_include_ruby:
            for group in ruby.lines[item.track_index].rubies:
                if group.target.lower:
                    raise ValueError('Skia 行背景带暂不支持下方注音')
                boxes = [cell['metrics']['ink'] for cell in group.cells]
                ink_top = min((top for left, top, right, bottom in boxes if right > left and bottom > top), default=None)
                if ink_top is not None:
                    top = min(top, _upper_reading_ink_top(group, ink_top))
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
    fitted = style.line_band_fill_mode in {'cover', 'repeat'}
    sliced = style.line_band_fill_mode == 'three_slice'
    ir['skia_layout']['line_band_schema'] = 3 if sliced else 2 if fitted else 1
    ir['required_features'].append('p08_static_line_band')
    if ruby is not None:
        ir['required_features'].append('p08_ruby_line_band')
    if staff:
        ir['required_features'].append('p08_staff_line_band')
        if animated:
            ir['required_features'].append('p08_animated_staff_line_band')
    elif animated:
        ir['required_features'].append('p08_animated_line_band')
    if style.line_band_tint:
        ir['required_features'].append('p08_tinted_line_band')
    if fitted:
        ir['required_features'].append('p08_line_band_fit')
    if sliced:
        ir['required_features'].append('p08_line_band_slices')
