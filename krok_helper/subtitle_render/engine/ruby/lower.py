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
    RomajiOptions, is_self_romanizable_kana, romanize_sentence_in_place,
)


@dataclass
class LowerRuby(RubyAnnotation):
    render_style: Style = field(default_factory=Style, repr=False, compare=False)


LOWER_APPEARANCE_FIELDS = frozenset(
    item.name for item in fields(SubtitleStyleScheme)
    if item.name.startswith("ruby_") and item.name != "ruby_gap_px"
)


def read_sug_romaji_options() -> RomajiOptions:
    from strange_uta_game.frontend.settings.app_settings import AppSettings
    return RomajiOptions.from_mapping(AppSettings().get("auto_check", {}))


def snapshot_lower_romaji_options(style: Style | None) -> Style | None:
    """Freeze the embedded SUG choices before handing a scene/job to workers."""
    if style is None or not style.lower_ruby_enabled or style._lower_romaji_options is not None:
        return style
    options = read_sug_romaji_options()
    return replace(style, _lower_romaji_options=(options.repeat_long_vowels,
        options.link_long_vowels, options.link_sokuon, options.uppercase))


def _timed_parts(parts: list[str], original: list[str]) -> list[str]:
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
    return timed


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
    converted: list[str],
    units: list[tuple[str, tuple[int, int]]],
) -> LowerRuby | None:
    if not units:
        return None
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
    if not sources:
        return []
    # Transient SUG characters carry the complete written context for particle
    # detection. Conversion may link those copies, never the authored track.
    from strange_uta_game.backend.domain.models import Character, Ruby, RubyPart
    from strange_uta_game.backend.domain.entities import Sentence
    sentence = Sentence(singer_id="lower-romaji", id="lower-romaji",
                        characters=[Character(char=char.text) for char in line.chars])
    unit_groups = [ruby_visual_units_and_intervals(source) for source, _, _ in sources]
    for (_source, indices, _authored), units in zip(sources, unit_groups):
        sentence.characters[min(indices)].ruby = Ruby(parts=[RubyPart(text=text) for text, _ in units])
    options = (RomajiOptions(*style._lower_romaji_options)
               if style._lower_romaji_options is not None else read_sug_romaji_options())
    romanize_sentence_in_place(sentence, options=options)
    converted_groups = [[part.text for part in sentence.characters[min(indices)].ruby.parts]
                        for _source, indices, _authored in sources]
    result: list[LowerRuby] = []
    run_start = 0
    while run_start < len(sources):
        run_end = run_start + 1
        while run_end < len(sources) and max(sources[run_end - 1][1]) + 1 == min(sources[run_end][1]):
            run_end += 1
        run = sources[run_start:run_end]
        run_units = unit_groups[run_start:run_end]
        flat_units = [unit for units in run_units for unit in units]
        original = [part for group in converted_groups[run_start:run_end] for part in group]
        timed = _timed_parts([text for text, _ in flat_units], original)
        starts = [0]
        for units in run_units:
            starts.append(starts[-1] + len(units))
        index = 0
        while index < len(run):
            end_index = index + 1
            # Only self-read kana cells may be combined. Authored upper groups
            # retain their exact scope, even across digraph/long-mark context.
            if not run[index][2]:
                while (end_index < len(run) and not run[end_index][2]
                       and starts[end_index] < starts[end_index + 1]
                       and (not original[starts[end_index]]
                            or sentence.characters[max(run[end_index - 1][1])].linked_to_next)):
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
