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


def vertical_glyph_shift(text):
    if text in _CORNER_PUNCTUATION:
        return 0.28
    return 0.10 if text in _SMALL_KANA else 0.0


def vertical_glyph_rotates(text):
    return text in _ROTATED_PUNCTUATION


def vertical_solid_schema(track):
    return 2 if any(_extended_text(char.text) for line in track.lines for char in line.chars) else 1


def validate_vertical_style(style):
    if style.right_to_left or any(style.line_offsets_x) or style.lower_ruby_enabled:
        raise ValueError('Skia 竖排暂不支持右至左单元、独立列偏移或下方注音')


def validate_vertical_source(track):
    for line in track.lines:
        for char in line.chars:
            if not _supported_text(char.text) or char.role_label is not None:
                raise ValueError('Skia 竖排单元包含未验证字符、多码点或行内角色')


def validate_vertical_columns(style, line_styles):
    if not any(s.vertical and s.dual_line_layout for s in (style, *line_styles)):
        return
    for appearance in line_styles:
        if (appearance.vertical != style.vertical or appearance.dual_line_layout != style.dual_line_layout
                or len(appearance.line_alignments) != len(style.line_alignments)
                or (appearance.font_size_px, appearance.line_gap_px, appearance.line_y_margin_px)
                   != (style.font_size_px, style.line_gap_px, style.line_y_margin_px)):
            raise ValueError('Skia 多列竖排暂需统一的列宽、间距和边距')


def vertical_line_box(item, measured, count, width, height, ruby_allowance=0):
    style = item.layout_style
    size = float(style.font_size_px)
    ascent = float(measured.baseline_metrics['ascent'])
    descent = float(measured.baseline_metrics['descent'])
    cell_height = max(ascent + descent, 1.0)
    block = cell_height * count
    margin = float(style.line_y_margin_px)
    column = width - margin - ruby_allowance - size / 2
    if item.lane:
        column -= item.lane * max(size + float(style.line_gap_px) + ruby_allowance, 1.0)
    top = {'top': margin, 'center': max((height - block) / 2, 0.0),
           'bottom': height - margin - block}[style.line_y_position]
    return column, top, cell_height, ascent


def vertical_cells(local, measured, item, *, width, height, ruby_allowance=0, singing_scale=False):
    """Final cells of one vertical column in logical canvas pixels."""
    size = float(item.layout_style.font_size_px)
    column, top, cell_height, ascent = vertical_line_box(
        item, measured, len(local.cells), width, height, ruby_allowance)
    cells = []
    for index, cell in enumerate(local.cells):
        # The glyph keeps its offset from its own horizontal cell-box centre.
        text = item.render_line.chars[index].text
        offset = vertical_glyph_shift(text)
        x = column + measured.offsets[index] - measured.widths[index] / 2 + size * offset
        ink = cell['metrics']['ink']
        cell_top = top + index * cell_height
        baseline = cell_top + ascent - cell_height * offset
        if 'vector_glyph_id' in cell['metrics']:
            x = column - (ink[0] + ink[2]) / 2
            baseline = cell_top + (cell_height - ink[1] - ink[3]) / 2
        wipe_pad = measured.edge_sizes[index] // 2
        positioned = {**cell, 'origin_x': x, 'baseline_y': baseline,
                      'wipe_left': x + ink[0] - wipe_pad, 'wipe_right': x + ink[2] + wipe_pad,
                      'wipe_top': cell_top, 'wipe_bottom': cell_top + cell_height,
                      'wipe_end_position': 1.0}
        if vertical_glyph_rotates(text):
            positioned['rotation_pivot_x'] = column
        if singing_scale:
            positioned.update(layout_left=column, layout_right=column, pivot_y=cell_top + cell_height / 2)
        cells.append(positioned)
    return cells
