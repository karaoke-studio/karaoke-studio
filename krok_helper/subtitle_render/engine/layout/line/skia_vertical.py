"""Simple vertical solid main text: the existing GPU's fixed-cell column.

Each authored cell is one fixed box ``font_size``
wide and ``ascent + descent`` tall, stacked from the column top; the glyph keeps
its horizontal cell-box offset around the column centre and sits on
``cell top + ascent``. The column centre and block top follow the GPU's
margin-only placement. Japanese punctuation and small kana may have a separate
glyph transform. The vertical wipe always follows the original cell box.
"""
from krok_helper.subtitle_render.engine.text.font_slots import is_n3_latin_text

_SMALL_KANA = frozenset('ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ')
_CORNER_PUNCTUATION = frozenset('、。')
_ROTATED_PUNCTUATION = frozenset('「」『』ー〜')
_TRANSFORMED_GLYPHS = _SMALL_KANA | _CORNER_PUNCTUATION | _ROTATED_PUNCTUATION
_BLANK_CELLS = frozenset(('', ' ', '\u3000'))


def _extended_text(text):
    return text in _BLANK_CELLS or (len(text) == 1 and is_n3_latin_text(text))


def _supported_text(text):
    if _extended_text(text):
        return True
    return upright_vertical_text(text) or text in _TRANSFORMED_GLYPHS


def upright_vertical_text(text):
    if len(text) != 1:
        return False
    code = ord(text)
    return (0x4e00 <= code <= 0x9fff or 0xac00 <= code <= 0xd7a3
            or ((0x3041 <= code <= 0x3094 or 0x30a1 <= code <= 0x30fa) and text not in _SMALL_KANA))


def vertical_japanese_glyphs(track):
    return any(char.text in _TRANSFORMED_GLYPHS for line in track.lines for char in line.chars)


def vertical_solid_schema(track):
    return 2 if any(_extended_text(char.text) for line in track.lines for char in line.chars) else 1


def validate_vertical_style(style):
    if (style.right_to_left or style.dual_line_layout or any(style.line_offsets_x)
            or style.lower_ruby_enabled):
        raise ValueError('Skia 竖排仅支持已验证的单列实色正文')


def validate_vertical_source(track):
    for line in track.lines:
        if track.rubies and line.wipe_reverse:
            raise ValueError('Skia 竖排注音当前仅支持正向走字')
        for char in line.chars:
            if not _supported_text(char.text) or char.role_label is not None:
                raise ValueError('Skia 竖排单元包含未验证字符、多码点或行内角色')


def vertical_line_box(item, measured, count, width, height, ruby_allowance=0):
    style = item.layout_style
    if item.lane:
        raise ValueError('Skia 竖排仅支持单列')
    size = float(style.font_size_px)
    ascent = float(measured.baseline_metrics['ascent'])
    descent = float(measured.baseline_metrics['descent'])
    cell_height = max(ascent + descent, 1.0)
    block = cell_height * count
    margin = float(style.line_y_margin_px)
    column = width - margin - ruby_allowance - size / 2
    top = {'top': margin, 'center': max((height - block) / 2, 0.0),
           'bottom': height - margin - block}[style.line_y_position]
    return column, top, cell_height, ascent


def vertical_cells(local, measured, item, *, width, height, ruby_allowance=0):
    """Final cells of one vertical column in logical canvas pixels."""
    size = float(item.layout_style.font_size_px)
    column, top, cell_height, ascent = vertical_line_box(
        item, measured, len(local.cells), width, height, ruby_allowance)
    cells = []
    for index, cell in enumerate(local.cells):
        # The glyph keeps its offset from its own horizontal cell-box centre.
        text = item.line.chars[index].text
        offset = 0.28 if text in _CORNER_PUNCTUATION else 0.10 if text in _SMALL_KANA else 0.0
        x = column + measured.offsets[index] - measured.widths[index] / 2 + size * offset
        ink = cell['metrics']['ink']
        cell_top = top + index * cell_height
        positioned = {**cell, 'origin_x': x, 'baseline_y': cell_top + ascent - cell_height * offset,
                      'wipe_left': x + ink[0], 'wipe_right': x + ink[2],
                      'wipe_top': cell_top, 'wipe_bottom': cell_top + cell_height,
                      'wipe_end_position': 1.0}
        if text in _ROTATED_PUNCTUATION:
            positioned['rotation_pivot_x'] = column
        cells.append(positioned)
    return cells
