from __future__ import annotations

from pathlib import Path

import krok_helper  # noqa: F401 - ensures bundled SUG src is importable
import pytest
from strange_uta_game.backend.domain import (
    Character,
    Project,
    ProjectMetadata,
    Ruby,
    RubyPart,
    Sentence,
    Singer,
)
from strange_uta_game.backend.infrastructure.persistence.sug_io import SugProjectParser
from strange_uta_game.backend.infrastructure.parsers.annotated_text import (
    parse_timed_line,
)

from krok_helper.subtitle_render import sug_project as sug_project_module
from krok_helper.subtitle_render.engine.painter import _effective_track_time_ms
from krok_helper.subtitle_render.engine.timing.timeline import compute_char_intervals
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.native.protocol import track_to_ir
from krok_helper.subtitle_render.sug_project import (
    load_sug_timing_track,
    timing_track_from_sug_project,
)


def _sample_sug_project() -> Project:
    main = Singer(
        id="main",
        name="主唱",
        color="#ff0000",
        is_default=True,
        backend_number=1,
    )
    chorus = Singer(
        id="chorus",
        name="和声",
        color="#00ff00",
        backend_number=2,
    )
    ai = Character(
        char="愛",
        ruby=Ruby(parts=[RubyPart("あ"), RubyPart("い")]),
        check_count=2,
        timestamps=[1000, 1300],
        sentence_end_ts=1800,
        is_sentence_end=True,
        is_line_end=True,
        singer_id=main.id,
    )
    ai.set_offset(50)
    sora = Character(
        char="空",
        ruby=Ruby(parts=[RubyPart("そら")]),
        check_count=1,
        timestamps=[2200],
        sentence_end_ts=2600,
        is_sentence_end=True,
        is_line_end=True,
        singer_id=chorus.id,
    )
    sora.set_offset(50)
    return Project(
        metadata=ProjectMetadata(title="曲名", artist="作者", album="专辑"),
        singers=[main, chorus],
        sentences=[
            Sentence(singer_id=main.id, characters=[ai]),
            Sentence(singer_id=chorus.id, characters=[sora]),
        ],
        audio_duration_ms=3000,
        global_offset_ms=50,
    )


def test_timing_track_from_sug_project_preserves_timing_ruby_and_singers() -> None:
    track = timing_track_from_sug_project(_sample_sug_project())

    assert track.meta.title == "曲名"
    assert track.meta.artist == "作者"
    assert track.meta.album == "专辑"
    assert track.meta.offset_ms == 0
    assert len(track.lines) == 2

    first = track.lines[0]
    assert first.singer_label == "主唱"
    assert first.singer_id == 0
    assert first.end_ms == 1850
    assert [(ch.text, ch.start_ms, ch.pause_release_ms, ch.role_label) for ch in first.chars] == [
        ("愛", 1050, 1850, "主唱")
    ]

    second = track.lines[1]
    assert second.singer_label == "和声"
    assert second.singer_id == 1
    assert second.end_ms == 2650
    assert [(ch.text, ch.start_ms, ch.pause_release_ms, ch.role_label) for ch in second.chars] == [
        ("空", 2250, 2650, "和声")
    ]

    assert track.singer_options == [(0, "主唱"), (1, "和声")]
    assert track.role_options == ["主唱", "和声"]

    ruby = track.rubies[0]
    assert ruby.kanji == "愛"
    assert ruby.reading == "あい"
    assert ruby.reading_parts == ["あ", "い"]
    assert ruby.reading_part_ms == [300]
    assert ruby.pos_start_ms == 1050
    assert ruby.pos_end_ms == 1850


def test_load_sug_timing_track_reads_sug_file(tmp_path: Path) -> None:
    sug_path = tmp_path / "song.sug"
    SugProjectParser.save(_sample_sug_project(), str(sug_path))

    track = load_sug_timing_track(sug_path)

    assert track.meta.title == "曲名"
    assert [line.singer_label for line in track.lines] == ["主唱", "和声"]
    assert [line.chars[0].text for line in track.lines] == ["愛", "空"]


def test_sug_adapter_merges_nicokara_metadata_and_custom_tags() -> None:
    track = timing_track_from_sug_project(
        _sample_sug_project(),
        nicokara_tags={
            "title": "标签曲名",
            "artist": "标签作者",
            "album": "标签专辑",
            "tagging_by": "打轴者",
            "silence_ms": "1250",
            "custom": ["@Emoji=主唱", "@Custom=保留"],
        },
    )

    assert track.meta.title == "标签曲名"
    assert track.meta.artist == "标签作者"
    assert track.meta.album == "标签专辑"
    assert track.meta.tagging_by == "打轴者"
    assert track.meta.silence_ms == 1250
    assert track.meta.custom == ["@Emoji=主唱", "@Custom=保留"]
    assert track.meta.offset_ms == 0


@pytest.mark.parametrize(
    ("offset_ms", "expected_start_ms"),
    [(200, 1200), (-200, 800)],
)
def test_sug_adapter_bakes_global_offset_exactly_once_for_cpu_and_gpu(
    offset_ms: int,
    expected_start_ms: int,
) -> None:
    project = _sample_sug_project()
    project.global_offset_ms = offset_ms

    track = timing_track_from_sug_project(project)

    assert track.lines[0].chars[0].start_ms == expected_start_ms
    assert track.meta.offset_ms == 0
    assert _effective_track_time_ms(track, expected_start_ms, Style()) == (
        expected_start_ms
    )

    native_track = track_to_ir(track)
    assert native_track["meta"]["offset_ms"] == 0
    assert native_track["lines"][0]["chars"][0]["start_ms"] == expected_start_ms


@pytest.mark.parametrize("offset_ms", [200, -200])
def test_sug_adapter_software_compensation_shifts_all_absolute_times(
    offset_ms: int,
) -> None:
    """软件导出补偿叠加在导出偏移之上，平移全部绝对时间，mora 相对值不动。"""
    track = timing_track_from_sug_project(
        _sample_sug_project(), software_compensation_ms=offset_ms
    )

    # 基线：烘焙导出偏移后 愛=1050 起 / 行尾 1850 / ruby 1050..1850。
    assert track.lines[0].chars[0].start_ms == 1050 + offset_ms
    assert track.lines[0].chars[0].pause_release_ms == 1850 + offset_ms
    assert track.lines[0].end_ms == 1850 + offset_ms
    ruby = track.rubies[0]
    assert ruby.pos_start_ms == 1050 + offset_ms
    assert ruby.pos_end_ms == 1850 + offset_ms
    # mora 时间戳是相对 ruby 起点的差值，不随补偿平移。
    assert ruby.reading_part_ms == [300]
    assert track.meta.offset_ms == 0


def test_sug_adapter_software_compensation_double_clamps_like_export_service() -> None:
    """两段独立钳 0：先 max(0, raw+导出偏移)，再 max(0, +补偿)，非单段合并。"""
    singer = Singer(id="main", name="主唱", is_default=True)
    char = Character(
        char="歌",
        check_count=1,
        timestamps=[100],
        sentence_end_ts=150,
        is_sentence_end=True,
        is_line_end=True,
        singer_id=singer.id,
    )
    project = Project(
        singers=[singer],
        sentences=[Sentence(singer_id=singer.id, characters=[char])],
        global_offset_ms=-200,
    )

    track = timing_track_from_sug_project(project, software_compensation_ms=150)

    # 导出偏移先把 100 钳到 0，补偿再加 150 → 150；
    # 单段合并 max(0, 100-200+150) 会得到 50，与 export_service 口径不符。
    assert track.lines[0].chars[0].start_ms == 150


def test_sug_adapter_software_compensation_zero_is_noop() -> None:
    project = _sample_sug_project()

    plain = timing_track_from_sug_project(project)
    zero = timing_track_from_sug_project(project, software_compensation_ms=0)

    assert plain == zero


def test_sug_adapter_export_offset_is_cumulative_with_style_offset() -> None:
    """导出偏移+软件补偿烘焙进时间戳；@Offset 槽位与样式「偏移」独立叠加。"""
    project = _sample_sug_project()
    project.global_offset_ms = 200

    track = timing_track_from_sug_project(project, software_compensation_ms=-300)
    style = Style(timing_offset_ms=150)

    assert track.lines[0].chars[0].start_ms == 900
    assert track.meta.offset_ms == 0
    # 播放 1050ms 采样到 900ms 起始的字符：三层偏移各退各的，互不覆盖。
    assert _effective_track_time_ms(track, 1050, style) == 900


def test_load_sug_timing_track_applies_software_compensation(
    tmp_path: Path,
) -> None:
    sug_path = tmp_path / "offset.sug"
    SugProjectParser.save(_sample_sug_project(), str(sug_path))

    plain = load_sug_timing_track(sug_path)
    compensated = load_sug_timing_track(sug_path, software_compensation_ms=-300)

    assert plain.lines[0].chars[0].start_ms == 1050
    assert compensated.lines[0].chars[0].start_ms == 750
    assert compensated.lines[0].end_ms == 1550
    assert compensated.meta.offset_ms == 0


def test_load_sug_timing_track_reads_nicokara_extras(tmp_path: Path) -> None:
    sug_path = tmp_path / "song-with-tags.sug"
    SugProjectParser.save(
        _sample_sug_project(),
        str(sug_path),
        nicokara_tags={
            "title": "文件标签曲名",
            "artist": "文件标签作者",
            "tagging_by": "标签作者",
            "custom": ["@Emoji=和声"],
        },
    )

    track = load_sug_timing_track(sug_path)

    assert track.meta.title == "文件标签曲名"
    assert track.meta.artist == "文件标签作者"
    assert track.meta.album == "专辑"
    assert track.meta.tagging_by == "标签作者"
    assert track.meta.custom == ["@Emoji=和声"]


def test_sug_adapter_preserves_n3_main_text_boundary_provenance() -> None:
    """Keep the self-contained メロディー/melody N3 regression after fixtures move."""
    singer = Singer(id="main", name="主唱", color="#ff0000", is_default=True)
    chars = [
        Character(
            char="メ",
            ruby=Ruby(parts=[RubyPart("me")]),
            check_count=1,
            timestamps=[6_220],
            linked_to_next=True,
            singer_id=singer.id,
        ),
        Character(
            char="ロ",
            ruby=Ruby(parts=[RubyPart("lo")]),
            check_count=1,
            timestamps=[6_470],
            linked_to_next=True,
            singer_id=singer.id,
        ),
        Character(
            char="デ",
            ruby=Ruby(parts=[RubyPart("d")]),
            check_count=1,
            timestamps=[6_740],
            linked_to_next=True,
            singer_id=singer.id,
        ),
        Character(
            char="ィ",
            ruby=Ruby(parts=[RubyPart("y")]),
            check_count=1,
            timestamps=[6_920],
            linked_to_next=True,
            singer_id=singer.id,
        ),
        Character(
            char="ー",
            check_count=0,
            sentence_end_ts=7_610,
            is_sentence_end=True,
            is_line_end=True,
            singer_id=singer.id,
        ),
    ]
    track = timing_track_from_sug_project(
        Project(
            singers=[singer],
            sentences=[Sentence(singer_id=singer.id, characters=chars)],
        )
    )

    line = track.lines[0]
    assert [char.text for char in line.chars] == list("メロディー")
    assert [char.start_ms for char in line.chars] == [
        6_220,
        6_470,
        6_740,
        6_920,
        7_265,
    ]
    assert [char.explicit_start for char in line.chars] == [
        True,
        True,
        True,
        True,
        False,
    ]
    assert [char.explicit_end for char in line.chars] == [
        True,
        True,
        True,
        False,
        True,
    ]
    ruby = track.rubies[0]
    assert (ruby.kanji, ruby.reading) == ("メロディー", "melody")
    assert ruby.reading_parts == ["me", "lo", "d", "y"]
    assert ruby.reading_part_ms == [250, 520, 700]


def test_sug_linked_ruby_merges_untimed_parts_like_lrc_export() -> None:
    singer = Singer(id="main", name="主唱", color="#ff0000", is_default=True)
    chars = [
        Character(
            char="メ",
            ruby=Ruby(parts=[RubyPart("me")]),
            check_count=1,
            timestamps=[2_776],
            linked_to_next=True,
            singer_id=singer.id,
        ),
        Character(
            char="リ",
            ruby=Ruby(parts=[RubyPart("rry")]),
            check_count=1,
            timestamps=[2_951],
            linked_to_next=True,
            singer_id=singer.id,
        ),
        Character(
            char="ー",
            ruby=Ruby(parts=[RubyPart("-")]),
            check_count=0,
            linked_to_next=True,
            singer_id=singer.id,
        ),
        Character(
            char="ゴ",
            ruby=Ruby(parts=[RubyPart("go")]),
            check_count=1,
            timestamps=[3_192],
            linked_to_next=True,
            singer_id=singer.id,
        ),
        Character(
            char="ー",
            ruby=Ruby(parts=[RubyPart("-")]),
            check_count=0,
            linked_to_next=True,
            singer_id=singer.id,
        ),
        Character(
            char="ラ",
            ruby=Ruby(parts=[RubyPart("rou")]),
            check_count=1,
            timestamps=[3_447],
            linked_to_next=True,
            singer_id=singer.id,
        ),
        Character(
            char="ン",
            ruby=Ruby(parts=[RubyPart("n")]),
            check_count=0,
            linked_to_next=True,
            singer_id=singer.id,
        ),
        Character(
            char="ド",
            ruby=Ruby(parts=[RubyPart("d")]),
            check_count=0,
            sentence_end_ts=3_960,
            is_sentence_end=True,
            singer_id=singer.id,
        ),
        Character(char=" ", check_count=0, singer_id=singer.id),
        Character(
            char="回",
            ruby=Ruby(parts=[RubyPart("まわ")]),
            check_count=1,
            timestamps=[4_058],
            sentence_end_ts=4_634,
            is_sentence_end=True,
            is_line_end=True,
            singer_id=singer.id,
        ),
    ]
    track = timing_track_from_sug_project(
        Project(
            singers=[singer],
            sentences=[Sentence(singer_id=singer.id, characters=chars)],
        )
    )

    line = track.lines[0]
    assert [char.text for char in line.chars[:9]] == list("メリーゴーランド ")
    assert [char.start_ms for char in line.chars[:9]] == [
        2_776,
        2_951,
        3_071,
        3_192,
        3_319,
        3_447,
        3_618,
        3_789,
        3_960,
    ]
    assert line.chars[7].pause_release_ms == 3_960
    assert line.chars[8].explicit_start is True
    assert line.chars[8].explicit_end is True

    ruby = track.rubies[0]
    assert (ruby.kanji, ruby.reading) == ("メリーゴーランド", "merry-go-round")
    assert ruby.reading_parts == ["me", "rry-", "go-", "round"]
    assert ruby.reading_part_ms == [175, 416, 671]


@pytest.mark.parametrize("placeholder_name", ["未命名", "Untitled"])
def test_default_sug_placeholder_singer_uses_global_style(
    placeholder_name: str,
) -> None:
    singer = Singer(
        id="placeholder",
        name=placeholder_name,
        color="#ff6b6b",
        is_default=True,
        is_placeholder=placeholder_name == "未命名",
    )
    char = Character(
        char="歌",
        check_count=1,
        timestamps=[1000],
        singer_id=singer.id,
    )
    project = Project(
        singers=[singer],
        sentences=[Sentence(singer_id=singer.id, characters=[char])],
    )

    track = timing_track_from_sug_project(project)

    assert track.lines[0].singer_label is None
    assert track.lines[0].singer_id is None
    assert track.lines[0].chars[0].role_label is None
    assert track.singer_options == []
    assert track.role_options == []


def test_non_default_singer_named_unnamed_remains_a_project_role() -> None:
    default = Singer(
        id="main",
        name="主唱",
        color="#ff0000",
        is_default=True,
    )
    unnamed = Singer(
        id="unnamed",
        name="未命名",
        color="#00ff00",
        is_default=False,
    )
    char = Character(
        char="歌",
        check_count=1,
        timestamps=[1000],
        singer_id=unnamed.id,
    )
    project = Project(
        singers=[default, unnamed],
        sentences=[Sentence(singer_id=unnamed.id, characters=[char])],
    )

    track = timing_track_from_sug_project(project)

    assert track.lines[0].singer_label == "未命名"
    assert track.lines[0].singer_id == 1
    assert track.lines[0].chars[0].role_label == "未命名"
    assert track.role_options == ["未命名"]


def test_sug_project_preserves_untimed_characters_in_timed_spans() -> None:
    singer = Singer(id="main", name="主唱", color="#ff0000", is_default=True)
    chars = [
        Character(char="I", check_count=1, timestamps=[1000], singer_id=singer.id),
        Character(char="'", check_count=0, singer_id=singer.id, linked_to_next=True),
        Character(char="v", check_count=0, singer_id=singer.id, linked_to_next=True),
        Character(char="e", check_count=0, singer_id=singer.id),
        Character(char=" ", check_count=0, singer_id=singer.id),
        Character(char="n", check_count=1, timestamps=[1500], singer_id=singer.id),
        Character(char="e", check_count=0, singer_id=singer.id, linked_to_next=True),
        Character(char="v", check_count=1, timestamps=[1700], singer_id=singer.id),
        Character(char="e", check_count=0, singer_id=singer.id, linked_to_next=True),
        Character(char="r", check_count=0, singer_id=singer.id),
        Character(char=" ", check_count=0, singer_id=singer.id),
        Character(char="s", check_count=1, timestamps=[1900], singer_id=singer.id),
        Character(char="e", check_count=0, singer_id=singer.id, linked_to_next=True),
        Character(char="e", check_count=0, singer_id=singer.id, linked_to_next=True),
        Character(char="n", check_count=0, singer_id=singer.id),
        Character(char=" ", check_count=0, singer_id=singer.id),
        Character(char="a", check_count=1, timestamps=[2400], singer_id=singer.id),
        Character(char=" ", check_count=0, singer_id=singer.id),
        Character(char="l", check_count=1, timestamps=[2600], singer_id=singer.id),
        Character(char="i", check_count=0, singer_id=singer.id, linked_to_next=True),
        Character(char="a", check_count=0, singer_id=singer.id, linked_to_next=True),
        Character(
            char="r",
            check_count=1,
            timestamps=[3000],
            sentence_end_ts=3400,
            is_sentence_end=True,
            is_line_end=True,
            singer_id=singer.id,
        ),
    ]
    project = Project(
        singers=[singer],
        sentences=[Sentence(singer_id=singer.id, characters=chars)],
    )

    track = timing_track_from_sug_project(project)

    line = track.lines[0]
    assert "".join(ch.text for ch in line.chars) == "I've never seen a liar"
    assert line.end_ms == 3400
    assert line.chars[-1].pause_release_ms == 3400
    assert [
        (
            ch.text,
            ch.source_span_start_ms,
            ch.source_span_end_ms,
            ch.source_span_index,
            ch.source_span_count,
        )
        for ch in line.chars[:5]
    ] == [
        ("I", 1000, 1500, 0, 5),
        ("'", 1000, 1500, 1, 5),
        ("v", 1000, 1500, 2, 5),
        ("e", 1000, 1500, 3, 5),
        (" ", 1000, 1500, 4, 5),
    ]


def test_sug_ruby_without_sentence_end_borrows_next_line_start() -> None:
    singer = Singer(id="main", name="主唱", color="#ff0000", is_default=True)
    first = Character(
        char="青",
        ruby=Ruby(parts=[RubyPart("あお")]),
        check_count=1,
        timestamps=[1000],
        singer_id=singer.id,
    )
    second = Character(
        char="空",
        check_count=1,
        timestamps=[2400],
        singer_id=singer.id,
    )
    project = Project(
        singers=[singer],
        sentences=[
            Sentence(singer_id=singer.id, characters=[first]),
            Sentence(singer_id=singer.id, characters=[second]),
        ],
    )

    track = timing_track_from_sug_project(project)

    assert track.rubies[0].pos_start_ms == 1000
    assert track.rubies[0].pos_end_ms == 2400


def test_sug_nicokara_inline_ruby_keeps_prefix_and_trailing_release_bounds() -> None:
    """Keep SUG's checkpoint boundaries around an untimed harmony wrapper."""

    main = Singer(id="sv9", name="sv9", is_default=True)
    sub = Singer(id="sv9sub", name="sv9sub")
    main_chars, _ = parse_timed_line(
        "【sv9】[03:46.59]レ[03:46.72]キ[03:46.84]レ[03:46.96]キ"
        "{煉||[03:47.08]れん}{獄||[03:47.30]ご|[03:47.41]く}"
        "{天||[03:47.52]てん}{神||[03:47.72]しん[>03:47.92]}",
        name_to_singer_id={"sv9": main.id, "sv9sub": sub.id},
        default_singer_id=main.id,
    )
    sub_chars, _ = parse_timed_line(
        "【sv9sub】<{天||[03:47.97]てん}{神||[03:48.17]しん}>[>03:48.43]",
        name_to_singer_id={"sv9": main.id, "sv9sub": sub.id},
        default_singer_id=main.id,
    )
    track = timing_track_from_sug_project(
        Project(
            singers=[main, sub],
            sentences=[
                Sentence(singer_id=main.id, characters=main_chars),
                Sentence(singer_id=sub.id, characters=sub_chars),
            ],
        )
    )

    harmony = track.lines[1]
    assert [(char.text, char.start_ms) for char in harmony.chars] == [
        ("<", 227920),
        ("天", 227970),
        ("神", 228170),
        (">", 228300),
    ]
    assert harmony.end_ms == 228430
    assert [
        (
            ruby.kanji,
            ruby.reading,
            ruby.pos_start_ms,
            ruby.pos_end_ms,
            ruby.target_char_start,
            ruby.target_char_end,
        )
        for ruby in track.rubies[-2:]
    ] == [
        ("天", "てん", 227970, 228170, 1, 2),
        ("神", "しん", 228170, 228430, 2, 3),
    ]


def test_sug_untimed_plain_follower_stops_at_next_checkpoint() -> None:
    """Match SUG's leader/follower timing for ``[ts]そう[next-ts]な``."""

    singer = Singer(id="main", name="主唱", is_default=True)
    chars, _ = parse_timed_line(
        "{嗚呼||[02:24.37]ああ,}[02:25.90]、"
        "{狂||[02:26.09]く|[02:26.28]る}{い||[02:26.48]ゆ}"
        "[02:26.99]そう[02:27.83]な[>02:28.37] "
        "{愛||[02:28.47]あ|[02:28.69]い}"
        "{情||[02:28.86]じょ|[02:29.11]う}[02:29.56]に[>02:32.91]",
        default_singer_id=singer.id,
    )
    track = timing_track_from_sug_project(
        Project(
            singers=[singer],
            sentences=[Sentence(singer_id=singer.id, characters=chars)],
        )
    )

    line = track.lines[0]
    sou_index = next(
        index
        for index in range(len(line.chars) - 1)
        if line.chars[index].text == "そ" and line.chars[index + 1].text == "う"
    )
    sou = line.chars[sou_index : sou_index + 2]
    assert [char.text for char in sou] == ["そ", "う"]
    assert [char.source_span_start_ms for char in sou] == [146990, 146990]
    assert [char.source_span_end_ms for char in sou] == [147830, 147830]
    assert [char.source_span_index for char in sou] == [0, 1]
    assert [char.source_span_count for char in sou] == [2, 2]

    # Equal widths demonstrate the same leader/follower split used by SUG;
    # real painting supplies glyph widths and therefore uses visual proportions.
    assert compute_char_intervals(line, [1] * len(line.chars))[sou_index : sou_index + 2] == [
        (146990, 147410),
        (147410, 147830),
    ]


def test_sug_mid_line_pause_does_not_truncate_line_or_later_ruby() -> None:
    singer = Singer(id="main", name="主唱", color="#ff0000", is_default=True)
    first = Sentence(
        singer_id=singer.id,
        characters=[
            Character(
                char="前",
                ruby=Ruby(parts=[RubyPart("まえ")]),
                check_count=1,
                timestamps=[1000],
                sentence_end_ts=1300,
                is_sentence_end=True,
                singer_id=singer.id,
            ),
            Character(
                char="意",
                ruby=Ruby(parts=[RubyPart("い")]),
                check_count=1,
                timestamps=[1600],
                is_line_end=True,
                singer_id=singer.id,
            ),
        ],
    )
    following = Sentence(
        singer_id=singer.id,
        characters=[
            Character(
                char="次",
                check_count=1,
                timestamps=[2400],
                is_line_end=True,
                singer_id=singer.id,
            )
        ],
    )
    project = Project(singers=[singer], sentences=[first, following])

    track = timing_track_from_sug_project(project)

    assert track.lines[0].chars[0].pause_release_ms == 1300
    assert track.lines[0].end_ms == 2400
    ruby_values = [
        (ruby.kanji, ruby.reading, ruby.pos_start_ms, ruby.pos_end_ms)
        for ruby in track.rubies[:2]
    ]
    assert ruby_values == [
        ("前", "まえ", 1000, 1300),
        ("意", "い", 1600, 2400),
    ]


def test_sug_ruby_ignores_configured_pause_char_and_preserves_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sug_project_module, "get_ruby_pause_char", lambda: "~")
    singer = Singer(id="main", name="主唱", color="#ff0000", is_default=True)
    project = Project(
        singers=[singer],
        sentences=[
            Sentence(
                singer_id=singer.id,
                characters=[
                    Character(
                        char="英",
                        ruby=Ruby(parts=[RubyPart("~")]),
                        check_count=1,
                        timestamps=[1000],
                        singer_id=singer.id,
                    ),
                    Character(
                        char="意",
                        ruby=Ruby(
                            parts=[RubyPart("い"), RubyPart("~"), RubyPart("み")]
                        ),
                        check_count=3,
                        timestamps=[1500, 1600, 1700],
                        sentence_end_ts=1900,
                        is_sentence_end=True,
                        is_line_end=True,
                        singer_id=singer.id,
                    ),
                ],
            )
        ],
    )

    track = timing_track_from_sug_project(project)

    assert len(track.rubies) == 2
    marker, ruby = track.rubies
    assert marker.kanji == "英"
    assert marker.reading == " "
    assert marker.reading_parts == [" "]
    assert ruby.kanji == "意"
    assert ruby.reading == "いみ"
    assert ruby.reading_parts == ["い", "", "み"]
    assert ruby.reading_part_ms == [100, 200]
    assert all("~" not in item.reading for item in track.rubies)


def test_sug_per_character_ruby_keeps_its_own_target(tmp_path):
    """``.sug`` stores ruby per character, so each repeat keeps its own range.

    Deriving the target by searching the line for the base text collapses every
    け onto the first ケ in a line like ケロケロケロ…, because that search can
    only return one occurrence.
    """

    import json

    from krok_helper.subtitle_render.sug_project import load_sug_timing_track

    characters = []
    for index, char in enumerate("ケロケロケロ"):
        characters.append(
            {
                "char": char,
                "timestamps": [1_000 + index * 100],
                "sentence_end_ts": 1_600 if index == 5 else None,
                "linked_to_next": False,
                "ruby": {"parts": [{"text": "け" if char == "ケ" else "ろ",
                                    "offset_ms": 0}]},
            }
        )
    payload = {
        "version": "0.3.0",
        "metadata": {"title": "", "artist": ""},
        "audio_duration_ms": 5_000,
        "singers": [{"id": "s1", "name": "x", "is_default": True}],
        "sentences": [{"id": "l1", "singer_id": "s1", "characters": characters}],
        "global_offset_ms": 0,
    }
    path = tmp_path / "kero.sug"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    track = load_sug_timing_track(path)

    assert [
        (ruby.target_char_start, ruby.target_char_end, ruby.reading)
        for ruby in track.rubies
    ] == [
        (0, 1, "け"),
        (1, 2, "ろ"),
        (2, 3, "け"),
        (3, 4, "ろ"),
        (4, 5, "け"),
        (5, 6, "ろ"),
    ]


def test_overlapping_lines_keep_their_own_ruby(tmp_path):
    """A harmony line's ruby must not land on the lead line it overlaps.

    Both sentences carry 空 at index 1 and the harmony line's span sits inside
    the lead line's, so matching by ``pos_start_ms`` / ``pos_end_ms`` alone lets
    the harmony annotation resolve onto the lead line as well -- the lead's 空
    then renders 「そら」 twice.
    """

    import json

    from krok_helper.subtitle_render.engine import painter as subtitle_painter
    from krok_helper.subtitle_render.engine.timing.timeline import compute_char_intervals
    from krok_helper.subtitle_render.sug_project import load_sug_timing_track

    def char(text, ts, *, ruby=None, end=None):
        return {
            "char": text,
            "check_count": 1,
            "timestamps": [ts],
            "sentence_end_ts": end,
            "linked_to_next": False,
            "ruby": {"parts": [{"text": ruby, "offset_ms": 0}]} if ruby else None,
        }

    payload = {
        "version": "0.3.0",
        "metadata": {"title": "", "artist": ""},
        "audio_duration_ms": 20_000,
        "singers": [
            {"id": "lead", "name": "lead", "is_default": True},
            {"id": "harmony", "name": "harmony", "is_default": False},
        ],
        "sentences": [
            {
                "id": "lead-line",
                "singer_id": "lead",
                # Spans 1000..9000, i.e. right across the harmony line below.
                "characters": [
                    char("て", 1_000),
                    char("空", 2_000, ruby="そ"),
                    char("へ", 8_000, end=9_000),
                ],
            },
            {
                "id": "harmony-line",
                "singer_id": "harmony",
                "characters": [
                    char("に", 4_000),
                    char("空", 5_000, ruby="く"),
                    char("の", 6_000, end=7_000),
                ],
            },
        ],
        "global_offset_ms": 0,
    }
    path = tmp_path / "overlap.sug"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    track = load_sug_timing_track(path)

    assert [
        (ruby.target_line_index, ruby.target_char_start, ruby.reading)
        for ruby in track.rubies
    ] == [(0, 1, "そ"), (1, 1, "く")]

    for line_index, expected in ((0, ["そ"]), (1, ["く"])):
        line = track.lines[line_index]
        intervals = compute_char_intervals(line)
        active = subtitle_painter._active_rubies_for_line(track.rubies, line)
        readings = [
            ruby.reading
            for ruby in active
            if 1 in subtitle_painter._ruby_target_indices(ruby, line, intervals)
        ]
        assert readings == expected, (line_index, readings)


def test_overlapping_nicokara_repeat_keeps_each_ruby_on_its_source_character() -> None:
    singer = Singer(id="main", name="main", is_default=True)
    raw_lines = [
        "{燃||[01:38.77]も}[01:38.99]え[01:39.23]よ[01:39.44]ド"
        "[01:39.67]ラ[01:40.10]ゴン[>01:40.35]{悪||[01:40.52]あ|[01:40.72]く}"
        "{魔||[01:40.84]ま}[01:40.96]の{罠||[01:41.21]わ|[01:41.39]な[>01:42.33]}",
        "<{燃||[01:42.27]も}[01:42.50]え[01:42.73]よ[01:42.95]ド"
        "[01:43.16]ラ[01:43.59]ゴン[>01:43.76]{悪||[01:44.06]あ|[01:44.23]く}"
        "{魔||[01:44.35]ま}[01:44.47]の{罠||[01:44.68]わ|[01:44.91]な}>[>01:45.67]",
    ]
    sentences = []
    for raw_line in raw_lines:
        chars, _ = parse_timed_line(raw_line, default_singer_id=singer.id)
        sentences.append(Sentence(singer_id=singer.id, characters=chars))

    track = timing_track_from_sug_project(
        Project(singers=[singer], sentences=sentences)
    )

    assert ["".join(char.text for char in line.chars) for line in track.lines] == [
        "燃えよドラゴン悪魔の罠",
        "<燃えよドラゴン悪魔の罠>",
    ]
    assert [
        (
            ruby.target_line_index,
            ruby.target_char_start,
            ruby.target_char_end,
            ruby.kanji,
        )
        for ruby in track.rubies
    ] == [
        (0, 0, 1, "燃"),
        (0, 7, 8, "悪"),
        (0, 8, 9, "魔"),
        (0, 10, 11, "罠"),
        (1, 1, 2, "燃"),
        (1, 8, 9, "悪"),
        (1, 9, 10, "魔"),
        (1, 11, 12, "罠"),
    ]
