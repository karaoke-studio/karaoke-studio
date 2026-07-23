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

from krok_helper.subtitle_render import sug_project as sug_project_module
from krok_helper.subtitle_render.engine.painter import _effective_track_time_ms
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.native_protocol import track_to_ir
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

    native_track = track_to_ir(track, Style())
    assert native_track["meta"]["offset_ms"] == 0
    assert native_track["lines"][0]["chars"][0]["start_ms"] == expected_start_ms


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
