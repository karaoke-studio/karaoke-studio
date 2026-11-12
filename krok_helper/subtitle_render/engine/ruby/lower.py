"""Render-only romanized readings; never enter main-text timing resolution."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace

from krok_helper.subtitle_render.domain.models import (
    Style, SubtitleStyleScheme, materialize_ruby_decoration_changes,
    subtitle_style_scheme_from_dict, subtitle_style_scheme_to_dict,
)
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingLine
from krok_helper.subtitle_render.engine.ruby.selection import (
    effective_ruby_for_target, ruby_target_indices,
)
from krok_helper.subtitle_render.engine.ruby.style import (
    build_ruby_font, build_ruby_font_for_text, ruby_style_for_target_indices,
)
from krok_helper.subtitle_render.engine.ruby.timing import ruby_visual_units_and_intervals
from krok_helper.subtitle_render.engine.style.style_semantics import effective_ruby_karaoke_colors
from strange_uta_game.backend.infrastructure.parsers.romaji import (
    RomajiOptions, is_self_romanizable_kana, romanize_ruby_parts,
)


@dataclass
class LowerRuby(RubyAnnotation):
    render_style: Style = field(default_factory=Style, repr=False, compare=False)


LOWER_APPEARANCE_FIELDS = frozenset(
    item.name for item in fields(SubtitleStyleScheme)
    if item.name.startswith("ruby_") and item.name != "ruby_gap_px"
)


def _romanized_parts(parts: list[str]) -> tuple[list[str], list[str]]:
    original = romanize_ruby_parts(parts, options=RomajiOptions(repeat_long_vowels=False))
    timed = list(original)
    # The SUG converter groups digraphs/long marks on the leading part. Keep
    # its spelling and group, but retain the original checkpoint inside it.
    for index in range(1, len(parts)):
        if original[index] or not timed[index - 1] or not parts[index]:
            continue
        if parts[index] == "ー" and timed[index - 1].endswith("-"):
            timed[index - 1], timed[index] = timed[index - 1][:-1], "-"
        elif parts[index] in "ゃゅょぁぃぅぇぉャュョァィゥェォ":
            timed[index - 1], timed[index] = timed[index - 1][:-1], timed[index - 1][-1:]
    return original, timed


def lower_style_snapshot(style: Style) -> dict:
    """Freeze the effective upper reading appearance for independent editing."""
    japanese = build_ruby_font(style)
    latin = build_ruby_font_for_text(style, "a")
    frozen = replace(
        style,
        **materialize_ruby_decoration_changes(style),
        ruby_font_follow_main=False,
        ruby_font_family=japanese.family(),
        ruby_font_weight=int(japanese.weight()),
        ruby_font_family_latin=latin.family(),
        ruby_latin_font_size_px=latin.pixelSize(),
        ruby_latin_font_weight=int(latin.weight()),
        ruby_italic=japanese.italic(), ruby_latin_italic=latin.italic(),
        ruby_colors_follow_main=False,
        ruby_karaoke_colors=effective_ruby_karaoke_colors(style),
    )
    snapshot = SubtitleStyleScheme(**{key: getattr(frozen, key) for key in LOWER_APPEARANCE_FIELDS})
    return {key: value for key, value in subtitle_style_scheme_to_dict(snapshot).items()
            if key in LOWER_APPEARANCE_FIELDS}


def lower_style(style: Style) -> Style:
    if style.lower_ruby_follow_upper or not style.lower_ruby_style:
        return replace(style, ruby_gap_px=style.lower_ruby_gap_px)
    payload = {key: value for key, value in style.lower_ruby_style.items() if key in LOWER_APPEARANCE_FIELDS}
    parsed = subtitle_style_scheme_from_dict(payload)
    return replace(style, **{key: getattr(parsed, key) for key in payload},
                   ruby_gap_px=style.lower_ruby_gap_px)


def _romanized(
    ruby: RubyAnnotation, indices: list[int], line: TimingLine, style: Style,
    converted: list[str] | None = None,
    units: list[tuple[str, tuple[int, int]]] | None = None,
) -> LowerRuby | None:
    units = ruby_visual_units_and_intervals(ruby) if units is None else units
    if not units:
        return None
    if converted is None:
        _, converted = _romanized_parts([text for text, _ in units])
    parts: list[str] = []
    starts: list[int] = []
    cursor = ruby.pos_start_ms
    for text, (_source, (start, end)) in zip(converted, units):
        if start > cursor:
            parts.append("")
            starts.append(cursor)
        parts.append(text)
        starts.append(start)
        cursor = end
    if cursor < ruby.pos_end_ms:
        parts.append("")
        starts.append(cursor)
    reading = "".join(parts)
    if not reading:
        return None
    upper_style = ruby_style_for_target_indices(style, line, indices)
    return LowerRuby(
        kanji="".join(line.chars[index].text for index in indices),
        reading=reading,
        reading_parts=parts,
        reading_part_ms=[time - ruby.pos_start_ms for time in starts[1:]],
        pos_start_ms=ruby.pos_start_ms, pos_end_ms=ruby.pos_end_ms,
        target_line_index=line.track_line_index,
        target_char_start=min(indices), target_char_end=max(indices) + 1,
        render_style=lower_style(upper_style),
    )


def lower_rubies_for_line(
    line: TimingLine, rubies: list[RubyAnnotation],
    intervals: list[tuple[int, int]], style: Style,
) -> list[LowerRuby]:
    """Derive from authored readings first, then uncovered kana cells."""
    if not style.lower_ruby_enabled or style.vertical or line.wipe_reverse:
        return []
    sources: list[tuple[RubyAnnotation, list[int], bool]] = []
    covered: set[int] = set()
    for ruby in rubies:
        indices = ruby_target_indices(ruby, line, intervals)
        if not indices or covered.intersection(indices):
            continue
        covered.update(indices)
        sources.append((effective_ruby_for_target(ruby, indices, intervals), indices, True))
    for index, char in enumerate(line.chars):
        if index in covered or char.vector_glyph is not None or not is_self_romanizable_kana(char.text):
            continue
        start, end = intervals[index]
        sources.append((RubyAnnotation(char.text, char.text, pos_start_ms=start, pos_end_ms=end),
                        [index], False))
    sources.sort(key=lambda item: min(item[1]))
    result: list[LowerRuby] = []
    run_start = 0
    while run_start < len(sources):
        run_end = run_start + 1
        while run_end < len(sources) and max(sources[run_end - 1][1]) + 1 == min(sources[run_end][1]):
            run_end += 1
        run = sources[run_start:run_end]
        unit_groups = [ruby_visual_units_and_intervals(source) for source, _, _ in run]
        flat_units = [unit for units in unit_groups for unit in units]
        original, timed = _romanized_parts([text for text, _ in flat_units])
        starts = [0]
        for units in unit_groups:
            starts.append(starts[-1] + len(units))
        index = 0
        while index < len(run):
            end_index = index + 1
            # Only self-read kana cells may be combined. Authored upper groups
            # retain their exact scope, even across digraph/long-mark context.
            if not run[index][2]:
                while (end_index < len(run) and not run[end_index][2]
                       and starts[end_index] < starts[end_index + 1]
                       and not original[starts[end_index]]):
                    end_index += 1
            source, indices, _authored = run[index]
            units = flat_units[starts[index]:starts[end_index]]
            if end_index > index + 1:
                indices = [cell for _, cells, _ in run[index:end_index] for cell in cells]
                source = replace(source, kanji="".join(line.chars[cell].text for cell in indices),
                                 reading="".join(text for text, _ in units),
                                 pos_end_ms=run[end_index - 1][0].pos_end_ms)
            derived = _romanized(source, indices, line, style,
                                  timed[starts[index]:starts[end_index]], units)
            if derived is not None:
                result.append(derived)
            index = end_index
        run_start = run_end
    return result
