"""GUI-only discovery inputs for Skia's explicit physical fallback resources.

This deliberately collects a superset of font/text pairs. It does not choose
ruby targets, allocate time, or compute layout. The canonical compiler retains
those responsibilities and only reads the prepared resource table on workers.
"""
from collections import defaultdict

from krok_helper.subtitle_render.domain.models import style_for_track
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.style.style_semantics import style_for_role_in_layout
from krok_helper.subtitle_render.engine.text.font_slots import font_style_for_cell
from krok_helper.subtitle_render.engine.ruby.style_semantics import font_style_for_ruby_cell
from krok_helper.subtitle_render.engine.ruby.lower_semantics import lower_style
from krok_helper.subtitle_render.engine.ruby.skia_timing import ruby_unit_clock
from krok_helper.subtitle_render.engine.ruby.skia_reference import MAIN_REFERENCE, UPPER_REFERENCE, LOWER_REFERENCE


def fallback_requests(track, style):
    buckets = defaultdict(set)

    def add(appearance, text):
        if text:
            buckets[appearance.font_family, appearance.font_weight, appearance.italic].add(text)

    # Lower romanization is emitted as individual visual units. Keep resource
    # discovery independent of target/clock selection, including long vowels.
    latin_units = LOWER_REFERENCE + LOWER_REFERENCE.upper() + "0123456789'-āīūēōâîûêô"
    track_style = style_for_track(style, track)
    owned, unowned = defaultdict(list), []
    for ruby in track.rubies:
        units = ruby_unit_clock(ruby, 0).glyph_units
        (unowned if ruby.target_line_index is None else owned[ruby.target_line_index]).extend(units)
    for index, line in enumerate(track.lines):
        appearance = style_for_line(track_style, line)
        roles = {char.role_label for char in line.chars} | {None}
        for char in line.chars:
            selected, _ = font_style_for_cell(style_for_role_in_layout(appearance, char.role_label), char.text)
            add(selected, char.text)
        readings = [*unowned, *owned[line.track_line_index if line.track_line_index is not None else index]]
        for role in roles:
            effective = style_for_role_in_layout(appearance, role)
            for unit in readings:
                selected, _ = font_style_for_ruby_cell(effective, unit)
                add(selected, unit)
            if effective.lower_ruby_enabled:
                lower = lower_style(effective)
                for unit in [*readings, *latin_units]:
                    selected, _ = font_style_for_ruby_cell(lower, unit)
                    add(selected, unit)
                add(effective, MAIN_REFERENCE)
                add(font_style_for_ruby_cell(effective, '')[0], UPPER_REFERENCE)
                add(font_style_for_ruby_cell(lower, 'a')[0], LOWER_REFERENCE)
    return buckets


def iter_prepare_skia_fallbacks(track, style):
    from krok_helper.subtitle_render.engine.text.font_fallback import prewarm_fallback, grapheme_clusters
    from .skia_font_aliases import gui_font_instance
    for (family, weight, italic), texts in fallback_requests(track, style).items():
        instance = gui_font_instance(family, weight, italic)  # Includes verified localized aliases.
        texts = texts | {unit for text in texts for unit in grapheme_clusters(text)}
        prewarm_fallback(instance.family, weight, italic, sorted(texts))
        yield
