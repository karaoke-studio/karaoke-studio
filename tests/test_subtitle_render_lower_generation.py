"""User-visible semantics of generated lower romaji readings (下层罗马音).

These tests pin the observable contract instead of implementation details:

* a lower reading covers exactly the kana cells it romanizes, once each, with
  the original cell timing;
* generated groups merge only where SUG's own converter links the cells
  (digraph / long vowel / sokuon / small-kana links, per the active options);
* authored upper readings never merge with generated groups;
* symbols, kanji and latin text never receive generated readings;
* derivation is independent of line order and of when global SUG options are
  sampled inside one layout pass;
* converter output is never silently truncated.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from PyQt6.QtGui import QFontMetrics

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import (
    RubyAnnotation,
    TimingChar,
    TimingLine,
)
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.render.elements.horizontal.ruby import (
    layout_rubies,
)
from krok_helper.subtitle_render.engine.ruby import lower as lower_adapter
from krok_helper.subtitle_render.engine.ruby import lower_semantics
from krok_helper.subtitle_render.engine.ruby.lower import (
    LowerRuby,
    lower_rubies_for_line,
    snapshot_lower_romaji_options,
)
from krok_helper.subtitle_render.engine.ruby.style import build_ruby_font
from krok_helper.subtitle_render.engine.ruby.timing import (
    ruby_visual_units_and_intervals,
)
from strange_uta_game.backend.domain.entities import Sentence
from strange_uta_game.backend.domain.models import Character, Ruby, RubyPart
from strange_uta_game.backend.infrastructure.parsers.romaji import (
    RomajiOptions,
    is_self_romanizable_kana,
    romanize_sentence_in_place,
)

CELL_MS = 200
FIRST_MS = 1000

# Runtime choices of the embedded SUG converter.  The default matches a fresh
# install (long vowels repeated, no extra linking); the others exercise every
# grouping switch the lower layer must honour.
DEFAULT_OPTIONS = RomajiOptions()
OPTION_CASES = {
    "default": DEFAULT_OPTIONS,
    "keep_long_vowel_mark": RomajiOptions(repeat_long_vowels=False),
    "link_long_vowels": RomajiOptions(link_long_vowels=True),
    "link_sokuon": RomajiOptions(link_sokuon=True),
    "link_both": RomajiOptions(link_long_vowels=True, link_sokuon=True),
    "uppercase": RomajiOptions(uppercase=True),
    "free_linking": RomajiOptions(
        repeat_long_vowels=False, link_long_vowels=True, link_sokuon=True
    ),
}


def _options_tuple(options: RomajiOptions) -> tuple[bool, bool, bool, bool]:
    return (
        options.repeat_long_vowels,
        options.link_long_vowels,
        options.link_sokuon,
        options.uppercase,
    )


def _style(options: RomajiOptions = DEFAULT_OPTIONS, **kwargs) -> Style:
    return Style(
        font_family="Meiryo",
        font_size_px=64,
        ruby_font_size_px=24,
        lower_ruby_enabled=True,
        _lower_romaji_options=_options_tuple(options),
        **kwargs,
    )


def _line(text: str, line_index: int = 0, first_ms: int = FIRST_MS) -> TimingLine:
    return TimingLine(
        chars=[
            TimingChar(ch, first_ms + index * CELL_MS, explicit_start=True)
            for index, ch in enumerate(text)
        ],
        end_ms=first_ms + len(text) * CELL_MS,
        track_line_index=line_index,
    )


def _intervals(text: str, first_ms: int = FIRST_MS) -> list[tuple[int, int]]:
    return [
        (first_ms + index * CELL_MS, first_ms + (index + 1) * CELL_MS)
        for index in range(len(text))
    ]


def _derive(text, options=DEFAULT_OPTIONS, rubies=(), *, line_index=0,
            first_ms=FIRST_MS, **style_kwargs):
    line = _line(text, line_index=line_index, first_ms=first_ms)
    return lower_rubies_for_line(
        line, list(rubies), _intervals(text, first_ms),
        _style(options, **style_kwargs),
    )


def _sug_reference(text: str, options: RomajiOptions):
    """SUG's own auto-romaji conversion of a kana-only line.

    Returns ``(converted_per_cell, linked_to_next_per_cell)``.  Authored text is
    not part of this reference; only self-romanizable kana cells get a reading,
    exactly like the editor's "convert all to romaji" action.
    """
    sentence = Sentence(
        singer_id="reference", id="reference",
        characters=[Character(char=ch) for ch in text],
    )
    for index, ch in enumerate(text):
        if is_self_romanizable_kana(ch):
            sentence.characters[index].ruby = Ruby(parts=[RubyPart(text=ch)])
    romanize_sentence_in_place(sentence, options=options)
    converted = [
        "".join(part.text for part in ch.ruby.parts) if ch.ruby else None
        for ch in sentence.characters
    ]
    return converted, [ch.linked_to_next for ch in sentence.characters]


def _sug_groups(text: str, options: RomajiOptions):
    """Partition kana cells the way the converter's own links dictate."""
    converted, linked = _sug_reference(text, options)
    groups: list[list[int]] = []
    current: list[int] = []
    for index, cell_text in enumerate(converted):
        if cell_text is None:
            if current:
                groups.append(current)
                current = []
            continue
        if not current:
            current = [index]
        elif linked[current[-1]] or converted[index] == "":
            current.append(index)
        else:
            groups.append(current)
            current = [index]
    if current:
        groups.append(current)
    return [(group, "".join(converted[index] for index in group)) for group in groups]


def _cell_groups(rubies):
    return [
        (list(range(ruby.target_char_start, ruby.target_char_end)), ruby.reading)
        for ruby in rubies
    ]


@pytest.mark.parametrize(
    "text",
    [
        "カッター",
        "ユーヘイじゃ",
        "幽閉ストップ知ってないし",
        "ラララブウラッタッタ",
        "きゃきゅきょ",
        "しゃしゅしょしぇ",
        "あぁいぃ",
        "ヴァージンハッピーショー",
        "パッパッパッ",
        "ふぁふぃふぇふぉ",
        "ちぇっちゃ",
        "カーッ",
        "オーッ",
        "きゃー",
        "じゃじゃじゃ",
        "ニューヨーク",
        "アーイーウー",
        "すぃてぃずぃ",
        "んーん",
        "っかっか",
    ],
)
def test_generated_readings_match_sug_auto_romaji_grouping(qapp, text):
    """Lower groups follow SUG's own link decisions for every option case."""
    for case, options in OPTION_CASES.items():
        expected = _sug_groups(text, options)
        actual = _cell_groups(_derive(text, options))
        assert actual == expected, f"{text!r} / {case}"


def test_kana_only_line_uses_whole_run_of_linked_cells(qapp):
    """じ+ゃ is one mora: one lower group over both cells, never two split keys."""
    groups = _cell_groups(_derive("ユーヘイじゃ"))
    assert groups == [
        ([0], "yu"), ([1], "u"), ([2], "he"), ([3], "i"), ([4, 5], "ja"),
    ]
    ruby = _derive("ユーヘイじゃ")[-1]
    assert ruby.reading_parts == ["j", "a"]
    assert ruby.reading_part_ms == [_intervals("ユーヘイじゃ")[5][0] - FIRST_MS - 4 * CELL_MS]


@pytest.mark.parametrize(
    "options,expected",
    [
        (DEFAULT_OPTIONS, [([0], "ka"), ([1], "t"), ([2], "ta"), ([3], "a")]),
        (RomajiOptions(repeat_long_vowels=False),
         [([0], "ka"), ([1], "t"), ([2, 3], "ta-")]),
        (RomajiOptions(link_sokuon=True),
         [([0], "ka"), ([1, 2], "tta"), ([3], "a")]),
        (RomajiOptions(repeat_long_vowels=False, link_sokuon=True),
         [([0], "ka"), ([1, 2, 3], "tta-")]),
    ],
)
def test_sokuon_and_long_vowel_links_control_merging(qapp, options, expected):
    assert _cell_groups(_derive("カッター", options)) == expected


def test_authored_reading_scope_is_never_merged_with_generated_cells(qapp):
    """Authored 知(し) keeps its own cell even with sokuon linking enabled."""
    line = _line("知って")
    authored = RubyAnnotation(
        "知", "し", pos_start_ms=FIRST_MS, pos_end_ms=FIRST_MS + CELL_MS,
        target_line_index=0, target_char_start=0, target_char_end=1,
    )
    intervals = _intervals("知って")

    linked = lower_rubies_for_line(
        line, [authored], intervals, _style(RomajiOptions(link_sokuon=True))
    )
    assert _cell_groups(linked) == [([0], "shi"), ([1, 2], "tte")]

    plain = lower_rubies_for_line(line, [authored], intervals, _style())
    assert _cell_groups(plain) == [([0], "shi"), ([1], "t"), ([2], "te")]


def test_symbols_kanji_and_latin_never_receive_generated_readings(qapp):
    assert _derive("HAHA YOU ARE KING") == []
    assert _derive("★｡.:*☆") == []
    assert _derive("漢字") == []
    result = _derive("★は漢カ")
    assert _cell_groups(result) == [([1], "ha"), ([3], "ka")]


def test_generated_coverage_is_exact_and_keeps_cell_timing(qapp):
    text = "幽閉ストップ知ってないし"
    intervals = _intervals(text)
    line = _line(text)
    authored = [
        RubyAnnotation(
            "幽", "ゆう", pos_start_ms=intervals[0][0], pos_end_ms=intervals[0][1],
            target_line_index=0, target_char_start=0, target_char_end=1,
        ),
        RubyAnnotation(
            "閉", "へい", pos_start_ms=intervals[1][0], pos_end_ms=intervals[1][1],
            target_line_index=0, target_char_start=1, target_char_end=2,
        ),
    ]
    result = lower_rubies_for_line(
        line, authored, intervals, _style(RomajiOptions(link_sokuon=True))
    )
    covered: list[int] = []
    for ruby in result:
        cells = list(range(ruby.target_char_start, ruby.target_char_end))
        covered.extend(cells)
        assert ruby.pos_start_ms == intervals[cells[0]][0]
        assert ruby.pos_end_ms == intervals[cells[-1]][1]
        assert "".join(ruby.reading_parts) == ruby.reading
        assert len(ruby.reading_parts) == len(ruby.reading_part_ms) + 1
        assert all(
            later >= earlier
            for earlier, later in zip(ruby.reading_part_ms, ruby.reading_part_ms[1:])
        )
        window = ruby.pos_end_ms - ruby.pos_start_ms
        assert all(0 <= offset <= window for offset in ruby.reading_part_ms)
        assert ruby.kanji == "".join(line.chars[index].text for index in cells)
    # Every covered cell (authored or generated) appears exactly once, in order.
    expected = [
        index for index, char in enumerate(text)
        if index in {0, 1} or is_self_romanizable_kana(char)
    ]
    assert covered == expected


def test_derivation_does_not_depend_on_line_order(qapp):
    texts = ["ユーヘイじゃ", "カッター", "幽閉ストップ知ってないし", "きゃー"]
    forward = {
        index: _derive(text, line_index=index)
        for index, text in enumerate(texts)
    }
    backward = {
        index: _derive(text, line_index=index)
        for index, text in reversed(list(enumerate(texts)))
    }
    assert backward == forward


def test_convert_all_to_romaji_options_do_not_change_reading_text(qapp, monkeypatch):
    """Freezing the SUG snapshot must not alter what the converter produces."""
    text = "ヴァージンハッピーショー"
    for case, options in OPTION_CASES.items():
        monkeypatch.setattr(
            lower_adapter, "read_sug_romaji_options", lambda: options
        )
        frozen = snapshot_lower_romaji_options(
            replace(_style(options), _lower_romaji_options=None)
        )
        assert frozen._lower_romaji_options == _options_tuple(options), case
        eager = lower_rubies_for_line(
            _line(text), [], _intervals(text), frozen
        )
        assert _cell_groups(eager) == _cell_groups(_derive(text, options)), case


def test_converter_output_is_never_silently_truncated(qapp, monkeypatch):
    """A converter that emits extra parts must not lose that text."""
    original = lower_semantics.romanize_sentence_in_place

    def converter_with_extra_part(sentence, *, options=None):
        original(sentence, options=options)
        for char in sentence.characters:
            if char.ruby and char.ruby.parts:
                char.ruby.parts.append(RubyPart(text="Z"))

    monkeypatch.setattr(
        lower_semantics, "romanize_sentence_in_place", converter_with_extra_part
    )
    result = _derive("カタ")
    readings = [ruby.reading for ruby in result]
    assert readings == ["kaZ", "taZ"], readings
    for ruby in result:
        assert "".join(ruby.reading_parts) == ruby.reading
        units = ruby_visual_units_and_intervals(ruby)
        assert len(ruby.reading_parts) == len(ruby.reading_part_ms) + 1
        assert units


def test_lower_options_are_frozen_once_per_layout_pass(qapp, monkeypatch):
    """Mid-pass SUG option changes cannot alter readings already laid out."""
    responses = [
        RomajiOptions(),
        RomajiOptions(uppercase=True),
    ]
    seen: list[int] = []

    def read_options():
        index = min(len(seen), len(responses) - 1)
        seen.append(index)
        return responses[index]

    monkeypatch.setattr(lower_adapter, "read_sug_romaji_options", read_options)
    style = replace(_style(), _lower_romaji_options=None)
    line = _line("カタ")
    intervals = _intervals("カタ")

    with layout_pass():
        first = lower_rubies_for_line(line, [], intervals, style)
        second = lower_rubies_for_line(line, [], intervals, style)
        assert [ruby.reading for ruby in first] == ["ka", "ta"]
        assert second == first
        assert len(seen) == 1

    # Outside a layout pass a fresh settings read is allowed on the next pass.
    third = lower_rubies_for_line(line, [], intervals, style)
    assert [ruby.reading for ruby in third] == ["KA", "TA"]
    assert len(seen) == 2


class _Ports:
    """Minimum ruby-layout ports: no wipe geometry needed for placement checks."""

    @staticmethod
    def ruby_wipe_geometry(*_args, **_kwargs):
        return (), 0.0, 0.0, ()


def test_layout_places_each_lower_group_over_its_own_cells(qapp):
    text = "ユーヘイじゃ"
    line = _line(text)
    intervals = _intervals(text)
    style = _style(RomajiOptions())
    widths = [40] * len(text)
    char_x_ranges = [(index * 40, index * 40 + 40) for index in range(len(text))]
    layouts = layout_rubies(
        QFontMetrics(build_ruby_font(style)),
        line,
        intervals,
        char_x_ranges,
        260,
        [],
        style,
        _Ports(),
    )
    lower = [layout for layout in layouts if isinstance(layout.ruby, LowerRuby)]
    assert [(tuple(layout.indices), layout.ruby.reading) for layout in lower] == [
        ((0,), "yu"), ((1,), "u"), ((2,), "he"), ((3,), "i"), ((4, 5), "ja"),
    ]
    for layout in lower:
        assert layout.x == char_x_ranges[layout.indices[0]][0]
        assert layout.target_width == (
            char_x_ranges[layout.indices[-1]][1] - char_x_ranges[layout.indices[0]][0]
        )
    merged = lower[-1]
    assert merged.target_width == 80


def test_lower_reading_grouping_is_stable_across_repeated_derivations(qapp):
    text = "幽閉ストップ知ってないし"
    line = _line(text)
    intervals = _intervals(text)
    style = _style(RomajiOptions(link_sokuon=True, link_long_vowels=True))
    first = lower_rubies_for_line(line, [], intervals, style)
    for _ in range(5):
        assert lower_rubies_for_line(line, [], intervals, style) == first


def test_generated_readings_never_use_another_lines_chars(qapp):
    style = _style(RomajiOptions(link_sokuon=True))
    for line in (
        _line("カッター", line_index=0, first_ms=0),
        _line("きゃー", line_index=1, first_ms=50_000),
    ):
        first_ms = line.chars[0].start_ms
        intervals = _intervals(
            "".join(char.text for char in line.chars), first_ms
        )
        result = lower_rubies_for_line(line, [], intervals, style)
        assert [ruby.reading for ruby in result] == (
            ["ka", "tta", "a"] if line.track_line_index == 0 else ["kya", "a"]
        )
        for ruby in result:
            assert ruby.target_line_index == line.track_line_index
            assert 0 <= ruby.target_char_start < ruby.target_char_end <= len(line.chars)
            assert ruby.kanji == "".join(
                line.chars[index].text
                for index in range(ruby.target_char_start, ruby.target_char_end)
            )
            assert ruby.pos_start_ms == intervals[ruby.target_char_start][0]
            assert ruby.pos_end_ms == intervals[ruby.target_char_end - 1][1]


def test_derivation_does_not_mutate_line_or_authored_rubies(qapp):
    from copy import deepcopy

    text = "知ってユー"
    line = _line(text)
    intervals = _intervals(text)
    authored = [RubyAnnotation(
        "知", "し", pos_start_ms=intervals[0][0], pos_end_ms=intervals[0][1],
        target_line_index=0, target_char_start=0, target_char_end=1,
    )]
    before = deepcopy((line, authored, intervals))
    style = _style(RomajiOptions(link_sokuon=True))
    lower_rubies_for_line(line, authored, intervals, style)
    assert (line, authored, intervals) == before
    assert style._lower_romaji_options == _options_tuple(RomajiOptions(link_sokuon=True))
