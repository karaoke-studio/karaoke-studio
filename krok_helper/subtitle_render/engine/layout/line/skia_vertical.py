"""Simple vertical solid main text: the existing GPU's fixed-cell column.

Each authored cell is one fixed box ``font_size``
wide and ``ascent + descent`` tall, stacked from the column top; the glyph keeps
its horizontal cell-box offset around the column centre and sits on
``cell top + ascent``. The column centre and block top follow the GPU's
margin-only placement. Japanese punctuation and small kana may have a separate
glyph transform. The vertical wipe always follows the original cell box.
"""
from dataclasses import replace
import unicodedata

from krok_helper.subtitle_render.domain.timing import TimingTrack, remap_track_role_labels
from .alignment import lane_offset_x

_SMALL_KANA = frozenset('ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ')
_CORNER_PUNCTUATION = frozenset('、。')
_ROTATED_PUNCTUATION = frozenset('「」『』ー〜')
_TRANSFORMED_GLYPHS = _SMALL_KANA | _CORNER_PUNCTUATION | _ROTATED_PUNCTUATION
_BODY_CORNER_PUNCTUATION = frozenset('，．')
_BODY_ROTATED_PUNCTUATION = _ROTATED_PUNCTUATION | frozenset('←→‐‑‒–—―～〈〉《》【】〔〕（）［］｛｝ｰ<>()[]{}')
_BLANK_CELLS = frozenset(('', ' ', '\u3000'))


def _extended_text(text):
    return text in _BLANK_CELLS or (len(text) == 1 and (
        '0' <= text <= '9' or 'A' <= text <= 'Z' or 'a' <= text <= 'z'
        or 'À' <= text <= 'Ö' or 'Ø' <= text <= 'ö' or 'ø' <= text <= 'ÿ'))


def _supported_text(text):
    if _extended_text(text):
        return True
    return upright_vertical_text(text) or text in _TRANSFORMED_GLYPHS


def vertical_text_cells(track):
    return any(not _supported_text(char.text) for line in track.lines for char in line.chars)


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
    # Vertical lines derive no lower readings (shared lower semantics) and
    # keep their column order whatever the horizontal direction says.
    return None


def validate_vertical_source(track):
    for line in track.lines:
        for char in line.chars:
            if any(unicodedata.category(scalar) in {'Cc', 'Cs'} for scalar in char.text):
                raise ValueError('Skia 竖排单元包含控制字符或无效码点')


def use_vertical_line_styles(track: TimingTrack) -> TimingTrack:
    labels = track.role_options
    if not labels:
        return track
    # Vertical captions use the resolved line style. The authored role
    # assignments stay in the source track for switching back to horizontal.
    lines = [replace(line, chars=[replace(char) for char in line.chars],
                     inline_guide_symbols=dict(line.inline_guide_symbols)) for line in track.lines]
    projected = replace(track, lines=lines)
    remap_track_role_labels(projected, dict.fromkeys(labels))
    return projected


def validate_vertical_columns(style, line_styles):
    if not any(s.vertical and s.dual_line_layout for s in (style, *line_styles)):
        return
    for appearance in line_styles:
        if (appearance.vertical != style.vertical or appearance.dual_line_layout != style.dual_line_layout
                or len(appearance.line_alignments) != len(style.line_alignments)):
            raise ValueError('Skia 多列竖排暂需统一的列数')


def vertical_line_box(item, measured, count, width, height, ruby_allowance=0):
    style = item.layout_style
    size = float(style.font_size_px)
    ascent = float(measured.baseline_metrics['ascent'])
    descent = float(measured.baseline_metrics['descent'])
    cell_height = max(ascent + descent, 1.0)
    block = cell_height * count
    margin = float(style.line_y_margin_px)
    page_count = item.page_line_count if style.dual_line_layout and style.line_horizontal_layout == 'asymmetric' else 0
    column = width - margin - ruby_allowance - size / 2 + lane_offset_x(style, item.lane, page_count)
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
        offset = 0.28 if text in _BODY_CORNER_PUNCTUATION else vertical_glyph_shift(text)
        x = column + measured.offsets[index] - measured.widths[index] / 2 + size * offset
        ink = cell['metrics']['ink']
        cell_top = top + index * cell_height
        baseline = cell_top + ascent - cell_height * offset
        bitmap = cell['metrics'].get('bitmap_guide', False)
        if bitmap or 'vector_glyph_id' in cell['metrics']:
            x = column - (ink[0] + ink[2]) / 2
            baseline = cell_top + (cell_height - ink[1] - ink[3]) / 2
        wipe_pad = measured.edge_sizes[index] // 2
        positioned = {**cell, 'origin_x': x, 'baseline_y': baseline,
                      'wipe_left': x + ink[0] - wipe_pad, 'wipe_right': x + ink[2] + wipe_pad,
                      'wipe_top': baseline + ink[1] if bitmap else cell_top,
                      'wipe_bottom': baseline + ink[3] if bitmap else cell_top + cell_height,
                      'wipe_end_position': 1.0}
        if text in _BODY_ROTATED_PUNCTUATION:
            positioned['rotation_pivot_x'] = column
        if singing_scale:
            positioned.update(layout_left=column, layout_right=column, pivot_y=cell_top + cell_height / 2)
        cells.append(positioned)
    return cells
