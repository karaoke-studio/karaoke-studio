"""Adapt resolved project titles to the canonical Skia text input."""

from functools import partial

from ...domain.models import Style, TimingChar, TimingLine, TimingTrack, title_line_units
from ...domain.paint import KaraokeColorState, KaraokeColors, PaintFill
from ..layout.line.skia_placement import LinePosition


def _title_position(local, item, measured, width, height, *, title, row_count):
    # Titles use the maximum fractional char box of their actual font slots.
    # The ordinary N3 lyric lanes use the base font box with integer descent.
    style = item.layout_style
    line = measured.lines[item.track_index]
    ascent = descent = 0
    for run, metrics, edge in zip(line.run_indices, line.slot_metrics, line.edge_sizes, strict=True):
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


def solid_title_style(title):
    if (title['role_styles'] or title['guide_symbols'] or title['inline_guide_symbols']
            or any(title[part]['mode'] != 'solid' for part in ('fill', 'stroke', 'stroke2', 'shadow'))):
        return None
    anchor = title['anchor']
    horizontal = anchor.rsplit('_', 1)[-1]
    state = KaraokeColorState(text=PaintFill(color=title['fill']['color']),
                              stroke=PaintFill(color=title['stroke']['color']),
                              stroke2=PaintFill(color=title['stroke2']['color']),
                              shadow=PaintFill(color=title['shadow']['color']))
    return Style(
        layout_semantics='n3_1074',
        font_family=title['font_family'], font_family_latin=title['font_family_latin'],
        font_size_px=title['font_size_px'], font_weight=title['font_weight'], italic=title['italic'],
        latin_font_size_px=title['latin_font_size_px'], latin_font_weight=title['latin_font_weight'],
        latin_font_stretch_pct=title['latin_font_stretch_pct'], letter_spacing_px=title['letter_spacing_px'],
        stroke_width_px=title['stroke_width_px'], stroke2_width_px=title['stroke2_width_px'],
        stroke2_enabled=title['stroke2_width_px'] > 0, decoration_kind=title['decoration_kind'],
        decoration_before_sync=True, glow_radius_px=title['glow_radius_px'],
        glow_before_radius_px=title['glow_radius_px'], glow_after_radius_px=title['glow_radius_px'],
        glow_concentration_level=title['glow_concentration_level'],
        shadow_offset_x=title['shadow_offset_x'], shadow_offset_y=title['shadow_offset_y'],
        karaoke_colors=KaraokeColors(before=state, after=state),
        dual_line_layout=False, smart_horizontal='none', line_horizontal_layout='asymmetric',
        line_alignments=[horizontal],
        line_y_position=anchor.split('_', 1)[0], line_y_margin_px=title['offset_y'],
        horizontal_margin_px=title['offset_x'],
        entry_anim='none', exit_anim='none', karaoke_anim='none', title_overlays=[],
        allow_inter_page_line_overlap=True, auto_fill_section_time=False,
    )


def build_title_sources(titles, build_source, **kwargs):
    sources = []
    for title in titles:
        windows = [window for window in title['windows'] if window[0] < window[1]]
        if not windows:
            continue
        style = solid_title_style(title)
        if style is None:
            raise ValueError('Skia 标题当前支持实色外观')
        rows = title['text'].split('\n')
        if len(rows) > 32:
            raise ValueError('Skia 单个标题最多支持 32 行')
        lines = [TimingLine(chars=[TimingChar(unit, row) for unit in title_line_units(text)],
                            track_line_index=row, end_ms=row + 1,
                            display_start_override_ms=row, display_end_override_ms=row + 1)
                 for row, text in enumerate(rows) if text]
        position = partial(_title_position, title=title, row_count=len(rows))
        source = build_source(TimingTrack(lines=lines), style, adjust_position=position,
                              **{**kwargs, 'roll_shift_ms': 0})
        sources.append({'windows': windows, 'source': source})
        if len(sources) > 16:
            raise ValueError('Skia 最多支持 16 个标题条目')
    return sources
