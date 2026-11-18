"""Simple vertical solid main text: the existing GPU's fixed-cell column.

vertical_solid_schema=1. Each authored cell is one fixed box ``font_size``
wide and ``ascent + descent`` tall, stacked from the column top; the glyph keeps
its horizontal cell-box offset around the column centre and sits on
``cell top + ascent``. The column centre and block top follow the GPU's
margin-only placement. Only upright single CJK ideographs, Hangul syllables and
full-size kana are validated; rotated, offset or multi-codepoint cells, ruby,
RTL, multi-row grids and lane offsets stay rejected.
"""

_SMALL_KANA = frozenset('ぁぃぅぇぉっゃゅょゎゕゖ'
                        'ァィゥェォッャュョヮヵヶ')


def vertical_simple_text(text):
    if len(text) != 1:
        return False
    code = ord(text)
    return (0x4e00 <= code <= 0x9fff or 0xac00 <= code <= 0xd7a3
            or ((0x3041 <= code <= 0x3096 or 0x30a1 <= code <= 0x30fa) and text not in _SMALL_KANA))


def validate_vertical_style(style):
    if (style.right_to_left or style.dual_line_layout or any(style.line_offsets_x)
            or style.lower_ruby_enabled):
        raise ValueError('Skia 竖排仅支持已验证的单列实色正文')


def validate_vertical_source(track):
    if track.rubies:
        raise ValueError('Skia 竖排暂不支持注音')
    for line in track.lines:
        for char in line.chars:
            if not vertical_simple_text(char.text) or char.role_label is not None:
                raise ValueError('Skia 竖排单元包含未验证字符、多码点或行内角色')


def vertical_cells(local, measured, item, *, width, height):
    """Final cells of one vertical column in logical canvas pixels."""
    style = item.layout_style
    if item.lane:
        raise ValueError('Skia 竖排仅支持单列')
    size = float(style.font_size_px)
    ascent = float(measured.baseline_metrics['ascent'])
    descent = float(measured.baseline_metrics['descent'])
    cell_height = max(ascent + descent, 1.0)
    block = cell_height * len(local.cells)
    margin = float(style.line_y_margin_px)
    column = width - margin - size / 2
    top = {'top': margin, 'center': max((height - block) / 2, 0.0),
           'bottom': height - margin - block}[style.line_y_position]
    cells = []
    for index, cell in enumerate(local.cells):
        # The glyph keeps its offset from its own horizontal cell-box centre.
        x = column + measured.offsets[index] - measured.widths[index] / 2
        ink = cell['metrics']['ink']
        cell_top = top + index * cell_height
        cells.append({**cell, 'origin_x': x, 'baseline_y': cell_top + ascent,
                      'wipe_left': x + ink[0], 'wipe_right': x + ink[2],
                      'wipe_top': cell_top, 'wipe_bottom': cell_top + cell_height,
                      'wipe_end_position': 1.0})
    return cells
