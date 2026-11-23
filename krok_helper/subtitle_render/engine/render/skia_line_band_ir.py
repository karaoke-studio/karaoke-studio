"""Project line-band rectangles from measured text and display placement."""

import math

from ...domain.band_presets import band_preset, preset_id_for_image
from ..layout.line.skia_placement import legacy_ruby_allowance
from ..ruby.style_semantics import ruby_stroke2_width, ruby_stroke_width
from ..style.style_semantics import style_for_role_in_layout

_BAND_TRANSITIONS = {'char_fade', 'flame', 'lightning'}


def _reading_ink_top(group, ink_top):
    edge = int(ruby_stroke_width(group.target.style))
    edge2 = int(ruby_stroke2_width(group.target.style))
    # The final horizontal fill bounds use the placed ink and both pens.
    return group.baseline_offset + ink_top - (edge + edge2 + 1) // 2


def _text_bounds(item, local, measured):
    # Avatars advance the row but do not contribute visual ink or font height.
    boxes, runs = [], []
    pads = {}
    for char, cell, run in zip(item.render_line.chars, local.cells, measured.slot_metrics, strict=True):
        if cell['metrics'].get('bitmap_guide'):
            continue
        runs.append(run)
        if char.role_label not in pads:
            look = style_for_role_in_layout(item.layout_style, char.role_label)
            primary = max(look.stroke_width_px, 0)
            secondary = max(look.stroke2_width_px, 0) if look.stroke2_enabled else 0
            pads[char.role_label] = math.ceil((primary + secondary) / 2)
        left, top, right, bottom = cell['metrics']['ink']
        if left < right and top < bottom:
            x = cell['origin_x']
            boxes.append((x + left, top, x + right, bottom))
    reference = runs or (measured.baseline_metrics,)
    top = -max(float(run['ascent']) for run in reference)
    bottom = max(float(run['descent']) for run in reference)
    ink = (min(b[0] for b in boxes), min(b[1] for b in boxes),
           max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes else None
    return top, bottom, ink, max(pads.values(), default=0)


def write_line_band(ir, style, prepared, *, width, height):
    if style.vertical or not style.line_band_enabled or not style.line_band_image:
        return
    preset_id = preset_id_for_image(style.line_band_image)
    preset = band_preset(preset_id) if preset_id is not None else None
    staff = preset_id == 'staff_scroll'
    material = ir['style'].get('line_band_image')
    if preset_id is None and not material:
        return
    sequence = bool(material and material.lower().endswith(('.webp', '.gif')))
    stroke = style.line_band_stroke_enabled and style.line_band_stroke_size_px > 0
    shadow = style.line_band_shadow_enabled and style.line_band_shadow_size_px > 0
    glow = style.line_band_glow_enabled and style.line_band_glow_size_px > 0
    geometry = prepared.geometry
    ruby = geometry.ruby
    if ((not preset and (preset_id is not None or not (sequence or material.lower().endswith('.png'))))
            or style.line_band_fill_mode not in {'stretch', 'repeat', 'cover', 'three_slice'}
            or style.right_to_left
            or style.line_band_edge_kind != 'none'
            or (stroke and style.line_band_stroke_position not in {'outside', 'inside', 'center'})):
        raise ValueError('Skia 行背景带当前支持横排 PNG、GIF、WebP 和内置预设')
    if preset and not material:
        raise ValueError('Skia 行背景带无法读取预设图片')
    if staff and any(not ir['style'].get(key) for key in
                     ('line_band_image', 'line_band_lines_image', 'line_band_clef_image')):
        raise ValueError('Skia 五线谱背景带缺少预设图层')
    transitions = _BAND_TRANSITIONS
    if 'p07_ruby_petal_scanline_image' in ir['required_features']:
        transitions = transitions | {'petal', 'note'}
    if any(source.get('entry_anim') not in ({'none', 'fade', 'slide_in', 'rise'} | transitions)
           or source.get('exit_anim') not in ({'none', 'fade', 'slide_out', 'rise'} | transitions)
           for source in ir['track']['lines']):
        raise ValueError('Skia 行背景带缺少对应行出入场能力')
    if any({source.get('entry_anim'), source.get('exit_anim')} & transitions
           for source in ir['track']['lines']):
        ir['required_features'].append('p08_line_band_transition')
    if any(source.get('scanline') or source.get('fx_bursts') for source in ir['track']['lines']):
        ir['required_features'].append('p08_line_band_with_effects')
    for item, final in zip(prepared.plan.lines, ir['skia_layout']['lines'], strict=True):
        at = geometry.position(item, width, height)
        local = geometry.locals[item.track_index]
        measured = prepared.measured.lines[item.track_index]
        top, bottom, ink, pad = _text_bounds(item, local, measured)
        left, right = 0, max(local.layout_width, 1)
        look = item.layout_style
        volume_width = geometry.volume_widths[item.track_index]
        if volume_width:
            volume = geometry.volume_styles[item.track_index]
            volume_stroke = volume.volume_stroke_width if volume.volume_enabled else volume.lit_stroke_width
            left = min(left, volume.volume_offset_x - volume_width)
            right = max(right, volume.volume_offset_x)
            center = final['volume_signal_anchor'][1] - at.baseline_y + volume.volume_offset_y
            half_height = max(volume.volume_size, 1) / 2 + max(volume_stroke, 0)
            top, bottom = min(top, center - half_height), max(bottom, center + half_height)
        elif ink and ink[3] - ink[1] + 2 * pad >= (bottom - top) / 2:
            left, top, right, bottom = ink[0] - pad, ink[1] - pad, ink[2] + pad, ink[3] + pad
        readings = ruby.lines[item.track_index].rubies if ruby is not None else ()
        if style.line_band_include_ruby:
            for group in readings:
                boxes = [cell['metrics']['ink'] for cell in group.cells]
                ink_top = min((top for left, top, right, bottom in boxes if right > left and bottom > top), default=None)
                if ink_top is not None:
                    top = min(top, _reading_ink_top(group, ink_top))
        y = at.baseline_y + style.line_band_offset_y_px
        if (look.layout_semantics == 'legacy' and look.line_y_position == 'center'
                and not look.dual_line_layout and measured.row_frame is None
                and not any(group.target.lower for group in readings)):
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
        if 'p05_lower_ruby' in ir['required_features']:
            ir['required_features'].append('p08_lower_ruby_line_band')
    if staff:
        ir['required_features'].append('p08_staff_line_band')
        if sequence:
            ir['required_features'].append('p08_animated_staff_line_band')
    elif sequence and preset:
        ir['required_features'].append('p08_animated_line_band')
    elif sequence:
        ir['required_features'].append('p08_line_band_image_sequence')
    if style.line_band_tint:
        ir['required_features'].append('p08_tinted_line_band')
    if stroke:
        ir['required_features'].append('p08_line_band_outer_stroke' if style.line_band_stroke_position == 'outside'
                                       else 'p08_line_band_stroke_position')
    if shadow or glow:
        ir['required_features'].append('p08_line_band_blur')
    if sequence and (stroke or shadow or glow):
        ir['required_features'].append('p08_line_band_animated_decoration')
    if fitted:
        ir['required_features'].append('p08_line_band_fit')
    if sliced:
        ir['required_features'].append('p08_line_band_slices')
