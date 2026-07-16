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
    assert track.meta.offset_ms == 50
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
