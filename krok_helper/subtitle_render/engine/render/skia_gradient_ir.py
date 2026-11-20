"""Project the line-wide coordinates needed by Skia body and outline gradients and image paints."""

from ..style.style_semantics import effective_karaoke_colors


_GRADIENTS = {'gradient_horizontal', 'gradient_vertical', 'split_vertical'}


def _layer_modes(look):
    """Paint modes of every drawn layer: body, then each present outline."""
    colors = effective_karaoke_colors(look)
    modes = {colors.before.text.mode, colors.after.text.mode}
    outlined = look.stroke_width_px > 0 or (look.stroke2_enabled and look.stroke2_width_px > 0)
    if look.stroke_width_px > 0:
        modes |= {colors.before.stroke.mode, colors.after.stroke.mode}
    if look.stroke2_enabled and look.stroke2_width_px > 0:
        modes |= {colors.before.stroke2.mode, colors.after.stroke2.mode}
    return modes, outlined


def write_body_gradient_bounds(ir, plan):
    """Write the glyph ink union and declare the corresponding paint capabilities.

    The existing GPU maps every gradient layer of a line onto this ink union: a
    horizontal paint spans the glyph ink, a vertical paint spans the ink padded
    by the line's visual outline pad (applied natively at the reference scale).
    Image paints need no line domain: the existing GPU tiles them on the canvas.
    """
    enabled = outlined_gradient = images = ruby_paint = singing_paint = False
    for final, item in zip(ir['skia_layout']['lines'], plan.lines, strict=True):
        look = item.layout_style
        modes, outlined = _layer_modes(look)
        if not modes & (_GRADIENTS | {'image'}):
            continue
        source = ir['track']['lines'][item.track_index]
        annotated = bool(final.get('ruby_groups'))
        singing = source.get('karaoke_anim') == 'utopia' and annotated and not source.get('zoom_pulse')
        if (not modes <= (_GRADIENTS | {'solid', 'image'})
                or look.vertical or look.right_to_left or item.line.wipe_reverse
                or source.get('entry_anim') != 'none' or source.get('exit_anim') != 'none'
                or source.get('scanline') or source.get('fx_bursts') or source.get('stroke_flash')
                or (source.get('karaoke_anim') != 'none' and not singing) or look.lower_ruby_enabled
                or any(c.role_label is not None or c.vector_glyph is not None for c in item.render_line.chars)):
            raise ValueError('Skia 正文渐变当前仅支持正向横排基础正文')
        ruby_paint = ruby_paint or annotated
        singing_paint = singing_paint or singing
        images = images or 'image' in modes
        if not modes & _GRADIENTS:
            continue
        boxes = []
        for cell in final['cells']:
            left, top, right, bottom = cell['metrics']['ink']
            if left < right and top < bottom:
                x, y = cell['origin_x'], cell['baseline_y']
                boxes.append((x + left, y + top, x + right, y + bottom))
        if boxes:
            left, top, right, bottom = zip(*boxes, strict=True)
            final['body_gradient_bounds'] = [min(left), min(top), max(right), max(bottom)]
        enabled = True
        outlined_gradient = outlined_gradient or outlined
    for enabled_feature, feature in (
        (enabled, 'p06_body_gradient'), (outlined_gradient, 'p06_outline_gradient'),
        (images, 'p06_image_fill'), (ruby_paint, 'p06_ruby_main_paint'),
        (singing_paint, 'p06_utopia_main_paint'),
    ):
        if enabled_feature:
            ir['required_features'].append(feature)
