"""Project the line-wide coordinates needed by a Skia body gradient."""

from ..style.style_semantics import effective_karaoke_colors


_GRADIENTS = {'gradient_horizontal', 'gradient_vertical', 'split_vertical'}


def write_body_gradient_bounds(track, ir, plan):
    enabled = False
    for final, item in zip(ir['skia_layout']['lines'], plan.lines, strict=True):
        look = item.layout_style
        colors = effective_karaoke_colors(look)
        modes = {colors.before.text.mode, colors.after.text.mode}
        if not modes & _GRADIENTS:
            continue
        source = ir['track']['lines'][item.track_index]
        if (not modes <= (_GRADIENTS | {'solid'})
                or look.vertical or look.right_to_left or item.line.wipe_reverse
                or look.stroke_width_px > 0 or (look.stroke2_enabled and look.stroke2_width_px > 0)
                or source.get('entry_anim') != 'none' or source.get('exit_anim') != 'none'
                or source.get('scanline') or source.get('fx_bursts') or source.get('stroke_flash')
                or source.get('karaoke_anim') != 'none' or track.rubies or look.lower_ruby_enabled
                or any(c.role_label is not None or c.vector_glyph is not None for c in item.render_line.chars)):
            raise ValueError('Skia 正文渐变当前仅支持正向横排基础正文')
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
    return enabled
