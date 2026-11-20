"""Adapt resolved project titles to the canonical Skia text input."""

from functools import partial

from ...domain.models import GuideSymbol, Style, SubtitleStyleScheme, TimingChar, TimingLine, TimingTrack, title_line_units
from ...domain.paint import KaraokeColorState, KaraokeColors, PaintFill
from ...serialization.timing import guide_symbol_from_dict
from ..layout.line.skia_placement import LinePosition


def _title_position(local, item, measured, width, height, *, title, row_count):
    # Titles use the maximum fractional char box of their actual font slots.
    # The ordinary N3 lyric lanes use the base font box with integer descent.
    style = item.layout_style
    line = measured.lines[item.track_index]
    ascent = descent = 0
    for cell, run, metrics, edge in zip(line.cells, line.run_indices, line.slot_metrics, line.edge_sizes, strict=True):
        if cell.get('bitmap_guide'):
            continue
        size = measured.runs[run]['size']
        below = size * metrics['descent'] / max(metrics['ascent'] + metrics['descent'], 1)
        ascent = max(ascent, size - below + edge / 2)
        descent = max(descent, below + edge / 2)
    row = item.line.track_line_index
    step = ascent + descent + title['line_gap_px']
    block_height = step * row_count - title['line_gap_px']
    if style.line_y_position == 'top':
        top = style.line_y_margin_px
    elif style.line_y_position == 'bottom':
        top = height - style.line_y_margin_px - block_height
    else:
        top = (height - block_height) / 2
    alignments = title.get('row_alignments') or ()
    offsets = title.get('row_offsets_x') or ()
    align = alignments[row] if row < len(alignments) else style.line_alignments[0]
    row_offset = offsets[row] if row < len(offsets) else 0
    half_edge = style.stroke_width_px / 2
    if align == 'left':
        x = style.horizontal_margin_px + half_edge
    elif align == 'right':
        x = width - style.horizontal_margin_px - half_edge - local.layout_width
    else:
        x = (width - local.layout_width) / 2
    return LinePosition(x + row_offset, top + ascent + step * row)


def _title_color(title):
    return KaraokeColorState(text=PaintFill(color=title['fill']['color']),
                              stroke=PaintFill(color=title['stroke']['color']),
                              stroke2=PaintFill(color=title['stroke2']['color']),
                              shadow=PaintFill(color=title['shadow']['color']))


def solid_title_style(title):
    roles = title['role_styles']
    if (any(paint[part]['mode'] != 'solid' for paint in (title, *roles.values())
                   for part in ('fill', 'stroke', 'stroke2', 'shadow'))):
        return None
    guides = [entry[-1] for entry in (*title['guide_symbols'], *title['inline_guide_symbols']) if entry[-1]]
    if guides and (roles or title['decoration_kind'] != 'none' or title['stroke_width_px'] or title['stroke2_width_px']):
        return None
    if roles and any(paint['stroke_width_px'] or paint['stroke2_width_px'] or paint['decoration_kind'] != 'none'
                     or paint['letter_spacing_px'] != title['letter_spacing_px']
                     or (paint['font_family_latin'] is None and title['font_family_latin'] is not None)
                     for paint in (title, *roles.values())):
        return None
    anchor = title['anchor']
    horizontal = anchor.rsplit('_', 1)[-1]
    state = _title_color(title)
    font_fields = ('font_family', 'font_family_latin', 'font_size_px', 'font_weight', 'italic',
                   'latin_font_size_px', 'latin_font_weight', 'latin_font_stretch_pct')
    schemes = {}
    for label, appearance in roles.items():
        color = _title_color(appearance)
        schemes[label] = SubtitleStyleScheme(**{key: appearance[key] for key in font_fields},
                                            karaoke_colors=KaraokeColors(before=color, after=color))
    return Style(
        layout_semantics='n3_1074',
        **{key: title[key] for key in font_fields}, letter_spacing_px=title['letter_spacing_px'],
        stroke_width_px=title['stroke_width_px'], stroke2_width_px=title['stroke2_width_px'],
        stroke2_enabled=title['stroke2_width_px'] > 0, decoration_kind=title['decoration_kind'],
        decoration_before_sync=True, glow_radius_px=title['glow_radius_px'],
        glow_before_radius_px=title['glow_radius_px'], glow_after_radius_px=title['glow_radius_px'],
        glow_concentration_level=title['glow_concentration_level'],
        shadow_offset_x=title['shadow_offset_x'], shadow_offset_y=title['shadow_offset_y'],
        karaoke_colors=KaraokeColors(before=state, after=state),
        custom_style_schemes=schemes,
        dual_line_layout=False, smart_horizontal='none', line_horizontal_layout='asymmetric',
        line_alignments=[horizontal],
        line_y_position=anchor.split('_', 1)[0], line_y_margin_px=title['offset_y'],
        horizontal_margin_px=title['offset_x'],
        entry_anim='none', exit_anim='none', karaoke_anim='none', title_overlays=[],
        allow_inter_page_line_overlap=True, auto_fill_section_time=False,
    )


def title_rows(title):
    for text, labels in zip(title['text'].split('\n'), title['resolved_role_labels'], strict=True):
        yield list(zip(title_line_units(text), labels, strict=True))


def _title_guide(payload):
    if payload['kind'] == 'bitmap':
        return GuideSymbol(kind='bitmap', count=payload['count'], prefix_timing='anchored',
                           bitmap_before_path=payload['before_path'], bitmap_after_path=payload['after_path'],
                           bitmap_fix_size=payload['fix_size'], bitmap_zoom_percent=payload['zoom_percent'],
                           bitmap_margin_left_px=payload['margin_left_px'],
                           bitmap_margin_right_px=payload['margin_right_px'],
                           bitmap_margin_bottom_px=payload['margin_bottom_px'])
    result = guide_symbol_from_dict(payload['vector_glyph'])
    if result is None:
        raise ValueError('Skia 标题导唱符轮廓无效')
    return result


def _title_lines(title, rows):
    prefixes = {row: guide for row, guide in title['guide_symbols'] if guide}
    replacements = {(row, column): guide for row, column, guide in title['inline_guide_symbols'] if guide}
    for row, cells in enumerate(rows):
        chars = []
        prefix = prefixes.get(row)
        if prefix:
            if not 1 <= prefix['count'] <= 64:
                raise ValueError('Skia 标题的行首导唱符数量超限')
            symbol = _title_guide(prefix)
            chars.extend(TimingChar('\uFFFC', row, vector_glyph=symbol) for _ in range(prefix['count']))
        for column, (unit, label) in enumerate(cells):
            inline = replacements.get((row, column))
            if inline:
                chars.append(TimingChar('\uFFFC', row, role_label=label, vector_glyph=_title_guide(inline)))
            else:
                chars.append(TimingChar(unit, row, role_label=label))
        if chars:
            yield TimingLine(chars=chars, track_line_index=row, end_ms=row + 1,
                             display_start_override_ms=row, display_end_override_ms=row + 1)


def build_title_sources(titles, build_source, **kwargs):
    sources = []
    for title in titles:
        windows = [window for window in title['windows'] if window[0] < window[1]]
        if not windows:
            continue
        style = solid_title_style(title)
        if style is None:
            raise ValueError('Skia 标题包含尚未支持的外观、角色组合或导唱符')
        rows = list(title_rows(title))
        if len(rows) > 32:
            raise ValueError('Skia 单个标题最多支持 32 行')
        lines = list(_title_lines(title, rows))
        position = partial(_title_position, title=title, row_count=len(rows))
        source = build_source(TimingTrack(lines=lines), style, adjust_position=position,
                              **{**kwargs, 'roll_shift_ms': 0})
        sources.append({'windows': windows, 'source': source})
        if len(sources) > 16:
            raise ValueError('Skia 最多支持 16 个标题条目')
    return sources
