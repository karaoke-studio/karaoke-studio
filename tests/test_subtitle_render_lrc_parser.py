"""Tests for ``krok_helper.subtitle_render.sources.subtitles`` Nicokara LRC parser."""

from __future__ import annotations

import pytest

from krok_helper.subtitle_render.sources.subtitles import (
    load_nicokara_lrc,
    parse_nicokara_lrc,
)


# ---------------------------------------------------------------------------
# 基本时间戳 / 行结构
# ---------------------------------------------------------------------------


def test_parse_single_line_with_start_and_end_ts():
    text = "[00:01:00]あ[00:01:50]い[00:02:00]\n"
    track = parse_nicokara_lrc(text)

    assert len(track.lines) == 1
    line = track.lines[0]
    assert [c.text for c in line.chars] == ["あ", "い"]
    assert [c.start_ms for c in line.chars] == [1000, 1500]
    assert line.end_ms == 2000
    assert line.singer_label is None
    assert not line.is_blank


def test_minutes_two_digit_timestamps():
    # 12:34:56 → (12*60+34)*1000 + 56*10 = 754_560 ms
    text = "[12:34:56]終[12:35:00]\n"
    track = parse_nicokara_lrc(text)
    assert track.lines[0].chars[0].start_ms == 754_560
    assert track.lines[0].end_ms == 755_000


def test_multi_char_block_is_evenly_spread_until_next_timestamp():
    text = "[00:38:05]どう[00:38:32]し[00:38:37]\n"
    track = parse_nicokara_lrc(text)
    line = track.lines[0]

    assert [c.text for c in line.chars] == ["ど", "う", "し"]
    assert [c.start_ms for c in line.chars] == [38_050, 38_185, 38_320]
    assert all(c.source_span_start_ms is None for c in line.chars)
    assert line.chars[2].source_span_start_ms is None
    assert line.chars[2].source_span_count == 1
    assert line.end_ms == 38_370


def test_parser_preserves_explicit_n3_main_text_boundaries():
    """N3 only lets ruby retime the base text when its inner boundaries are absent."""
    text = "[00:06:22]メ[00:06:47]ロ[00:06:74]デ[00:06:92]ィー[00:07:61]\n"

    line = parse_nicokara_lrc(text).lines[0]

    assert [char.text for char in line.chars] == list("メロディー")
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


def test_blank_lines_preserved():
    text = "[00:01:00]あ[00:01:50]\n\n[00:02:00]い[00:02:50]\n"
    track = parse_nicokara_lrc(text)
    assert len(track.lines) == 3
    assert track.lines[1].is_blank
    assert track.lines[1].chars == []
    assert track.lines[1].end_ms is None


def test_char_count_and_non_blank_line_count():
    text = "[00:00:00]a[00:00:50]b[00:01:00]\n\n[00:02:00]c[00:02:50]\n"
    track = parse_nicokara_lrc(text)
    assert track.char_count == 3
    assert track.non_blank_line_count == 2


# ---------------------------------------------------------------------------
# 行内停顿释放 + 演唱者标签
# ---------------------------------------------------------------------------


def test_pause_release_timestamp_attached_to_previous_char():
    # 字 1 在 1.00 起、1.50 释放（呼吸）；字 2 在 1.80 重起
    text = "[00:01:00]あ[00:01:50][00:01:80]い[00:02:00]\n"
    track = parse_nicokara_lrc(text)
    chars = track.lines[0].chars
    assert chars[0].text == "あ"
    assert chars[0].start_ms == 1000
    assert chars[0].pause_release_ms == 1500
    assert chars[1].text == "い"
    assert chars[1].start_ms == 1800
    assert chars[1].pause_release_ms is None
    assert track.lines[0].end_ms == 2000


def test_singer_label_at_line_start():
    text = "【ボーカル】[00:01:00]あ[00:01:50]\n"
    track = parse_nicokara_lrc(text)
    line = track.lines[0]
    assert line.singer_label == "ボーカル"
    assert line.singer_id == 0
    assert [c.text for c in line.chars] == ["あ"]


def test_singer_label_persists_until_next_label():
    text = (
        "【A】\n"
        "[00:01:00]あ[00:01:50]\n"
        "[00:02:00]い[00:02:50]\n"
        "【B】[00:03:00]う[00:03:50]\n"
    )
    track = parse_nicokara_lrc(text)

    assert track.lines[1].singer_label == "A"
    assert track.lines[1].singer_id == 0
    assert track.lines[2].singer_label == "A"
    assert track.lines[2].singer_id == 0
    assert track.lines[3].singer_label == "B"
    assert track.lines[3].singer_id == 1
    assert track.singer_options == [(0, "A"), (1, "B")]


# ---------------------------------------------------------------------------
# tail 元数据
# ---------------------------------------------------------------------------


def test_tail_metadata_parsed():
    text = (
        "[00:01:00]a[00:01:50]\n"
        "\n"
        "@Title=タイトル\n"
        "@Artist=歌手\n"
        "@Album=アルバム\n"
        "@TaggingBy=Me\n"
        "@SilencemSec=1500\n"
    )
    track = parse_nicokara_lrc(text)
    assert track.meta.title == "タイトル"
    assert track.meta.artist == "歌手"
    assert track.meta.album == "アルバム"
    assert track.meta.tagging_by == "Me"
    assert track.meta.silence_ms == 1500


def test_offset_positive_and_negative():
    pos = parse_nicokara_lrc("[00:00:00]a[00:00:50]\n\n@Offset=+250\n")
    neg = parse_nicokara_lrc("[00:00:00]a[00:00:50]\n\n@Offset=-300\n")
    zero = parse_nicokara_lrc("[00:00:00]a[00:00:50]\n")
    assert pos.meta.offset_ms == 250
    assert neg.meta.offset_ms == -300
    assert zero.meta.offset_ms == 0


def test_custom_tail_lines_preserved():
    text = (
        "[00:00:00]a[00:00:50]\n"
        "\n"
        "@Title=Foo\n"
        "% comment line\n"
        "@Offset=+100\n"
    )
    track = parse_nicokara_lrc(text)
    assert "% comment line" in track.meta.custom


def test_body_internal_blank_lines_not_swallowed_by_tail():
    # body 中也可以有空行（用户排版意图），不能被尾部元数据吸走
    text = (
        "[00:01:00]a[00:01:50]\n"
        "\n"
        "[00:02:00]b[00:02:50]\n"
        "\n"
        "@Title=Foo\n"
    )
    track = parse_nicokara_lrc(text)
    # body: 3 行（含中间一条空行）
    assert len(track.lines) == 3
    assert track.lines[1].is_blank
    assert track.meta.title == "Foo"


# ---------------------------------------------------------------------------
# @Ruby
# ---------------------------------------------------------------------------


def test_ruby_simple_entry():
    text = (
        "[00:03:00]漢[00:04:00]\n"
        "\n"
        "@Ruby1=漢,かん,[00:03:00],[00:04:00]\n"
    )
    track = parse_nicokara_lrc(text)
    assert len(track.rubies) == 1
    r = track.rubies[0]
    assert r.kanji == "漢"
    assert r.reading == "かん"
    assert r.reading_part_ms == []
    assert r.reading_parts == ["かん"]
    assert r.pos_start_ms == 3000
    assert r.pos_end_ms == 4000


def test_ruby_with_mora_timestamps_in_reading():
    text = (
        "[00:03:00]漢[00:04:00]字[00:05:00]\n"
        "\n"
        "@Ruby1=漢字,か[00:00:50]ん[00:01:50]じ,[00:03:00],[00:05:00]\n"
    )
    track = parse_nicokara_lrc(text)
    r = track.rubies[0]
    assert r.kanji == "漢字"
    # 读音内部的 mora ts 被剥离，但毫秒序列按 ruby 组起点保留为相对时间
    assert r.reading == "かんじ"
    assert r.reading_part_ms == [500, 1500]
    assert r.reading_parts == ["か", "ん", "じ"]
    assert r.pos_start_ms == 3000
    assert r.pos_end_ms == 5000


def test_ruby_entry_without_position_resolves_to_its_occurrence():
    """A position-less entry is still pinned to the character it landed on.

    N3 runs every entry -- bounded or not -- through the same position-driven
    scan, so the annotation ends up owning a concrete character range and its
    own wipe interval.  Leaving it "global" is what let a short entry's reading
    be drawn on top of a longer entry's base text.
    """

    text = (
        "[00:03:00]哀[00:04:00]\n"
        "\n"
        "@Ruby1=哀,か[00:00:29]な\n"
    )
    track = parse_nicokara_lrc(text)
    assert len(track.rubies) == 1
    r = track.rubies[0]
    assert r.kanji == "哀"
    assert r.reading == "かな"
    assert r.reading_part_ms == [290]
    assert r.reading_parts == ["か", "な"]
    assert r.pos_start_ms == 3000
    assert r.pos_end_ms == 4000
    assert (r.target_char_start, r.target_char_end) == (0, 1)


def test_ruby_consecutive_timestamps_preserve_empty_parts():
    text = (
        "[00:03:00]寿[00:04:00]\n"
        "\n"
        "@Ruby1=寿,す[00:00:15][00:00:30],[00:03:00],[00:04:00]\n"
    )
    ruby = parse_nicokara_lrc(text).rubies[0]

    assert ruby.reading == "す"
    assert ruby.reading_part_ms == [150, 300]
    assert ruby.reading_parts == ["す", "", ""]


def test_multiple_ruby_entries():
    text = (
        "[00:00:00]a[00:00:50]\n"
        "\n"
        "@Ruby1=漢,かん,[00:01:00],[00:02:00]\n"
        "@Ruby2=字,じ,[00:03:00],[00:04:00]\n"
    )
    track = parse_nicokara_lrc(text)
    assert len(track.rubies) == 2
    assert track.rubies[0].kanji == "漢"
    assert track.rubies[1].kanji == "字"


def test_rl_open_ruby_ranges_include_boundary_and_later_entry_wins():
    text = (
        "[00:01:00]子[00:02:00]\n"
        "[00:03:00]迷[00:04:00]子[00:05:00]\n"
        "\n"
        "@Ruby1=子,こ,,[00:04:00]\n"
        "@Ruby2=子,ご,[00:04:00]\n"
    )

    track = parse_nicokara_lrc(text)

    assert [
        (ruby.reading, ruby.pos_start_ms, ruby.pos_end_ms)
        for ruby in track.rubies
    ] == [
        ("こ", 1_000, 2_000),
        ("ご", 4_000, 5_000),
    ]


# ---------------------------------------------------------------------------
# 编码 / 换行
# ---------------------------------------------------------------------------


def test_bom_stripped():
    text = "﻿[00:01:00]a[00:01:50]\n"
    track = parse_nicokara_lrc(text)
    assert track.lines[0].chars[0].text == "a"
    assert track.lines[0].chars[0].start_ms == 1000


def test_crlf_line_endings():
    text = "[00:01:00]a[00:01:50]\r\n\r\n@Title=Foo\r\n"
    track = parse_nicokara_lrc(text)
    assert track.lines[0].chars[0].text == "a"
    assert track.meta.title == "Foo"


def test_load_nicokara_lrc_from_file(tmp_path):
    # 与 SUG NicokaraExporter 一致：UTF-8-BOM + CRLF + 末尾换行
    body = "[00:01:00]a[00:01:50]b[00:02:00]"
    tail = "@Title=Foo\r\n@Offset=+100\r\n"
    content = body + "\r\n\r\n" + tail
    raw = b"\xef\xbb\xbf" + content.encode("utf-8")
    path = tmp_path / "demo.lrc"
    path.write_bytes(raw)

    track = load_nicokara_lrc(path)
    assert len(track.lines) == 1
    assert track.char_count == 2
    assert track.meta.title == "Foo"
    assert track.meta.offset_ms == 100


# ---------------------------------------------------------------------------
# 健壮性
# ---------------------------------------------------------------------------


def test_empty_string_yields_empty_track():
    track = parse_nicokara_lrc("")
    assert track.lines == []
    assert track.rubies == []
    assert track.meta.title is None


def test_only_metadata_no_body():
    track = parse_nicokara_lrc("@Title=Foo\n@Artist=Bar\n")
    assert track.lines == []
    assert track.meta.title == "Foo"
    assert track.meta.artist == "Bar"


def test_parser_does_not_precompute_page_or_section_breaks():
    track = parse_nicokara_lrc(
        "[00:00:00]a[00:00:50]\n"
        "[00:01:00]b[00:01:50]\n"
        "[00:10:00]c[00:10:50]\n"
    )

    assert [line.break_before for line in track.lines] == ["none", "none", "none"]


def test_malformed_ruby_entry_silently_skipped():
    # 字段数 < 4 → 跳过，不抛
    text = "[00:00:00]a[00:00:50]\n\n@Ruby1=incomplete\n"
    track = parse_nicokara_lrc(text)
    assert track.rubies == []


# ---------------------------------------------------------------------------
# 角色 / 配色 标签（行内 【N配色】，逐字 role_label）
# ---------------------------------------------------------------------------


def test_role_label_assigned_per_char_and_switches_midline():
    # 一行内从 1配色 切到 2配色（标签前后都有 [ts]，与实际格式一致）
    text = "【1配色】[00:01:00]あ[00:01:50]い[00:02:00]【2配色】[00:02:50]う[00:03:00]\n"
    track = parse_nicokara_lrc(text)
    line = track.lines[0]
    assert [(c.text, c.role_label) for c in line.chars] == [
        ("あ", "1配色"),
        ("い", "1配色"),
        ("う", "2配色"),
    ]


def test_role_label_embedded_after_space_is_not_rendered_as_text():
    text = (
        "【1配色】[01:23:66]今[01:24:61] 【3配色】[01:25:19]歩[01:25:94]き[01:26:58]\n"
    )
    track = parse_nicokara_lrc(text)
    line = track.lines[0]

    assert "".join(c.text for c in line.chars) == "今 歩き"
    assert [(c.text, c.role_label) for c in line.chars] == [
        ("今", "1配色"),
        (" ", "1配色"),
        ("歩", "3配色"),
        ("き", "3配色"),
    ]
    assert line.chars[1].start_ms == 84_610
    assert line.chars[2].start_ms == 85_190


def test_role_label_carries_across_lines():
    # 第二行没有标签，应继承第一行的 1配色
    text = "【1配色】[00:01:00]あ[00:01:50]\n[00:02:00]い[00:02:50]\n"
    track = parse_nicokara_lrc(text)
    assert track.lines[0].chars[0].role_label == "1配色"
    assert track.lines[1].chars[0].role_label == "1配色"


def test_track_role_options_dedup_in_order():
    text = (
        "【1配色】[00:01:00]あ[00:01:50]\n"
        "【2配色】[00:02:00]い[00:02:50]\n"
        "【1配色】[00:03:00]う[00:03:50]\n"
    )
    track = parse_nicokara_lrc(text)
    assert track.role_options == ["1配色", "2配色"]


# ---------------------------------------------------------------------------
# 真实 nicokara3 文件兼容（对照 SUG submodule NicokaraParser）
# ---------------------------------------------------------------------------


def test_dot_separated_timestamps_are_parsed():
    # 标准 LRC 点号厘秒 [MM:SS.CC]——旧实现只认冒号，整篇匹配不到 ts → 正文全丢。
    track = parse_nicokara_lrc("[00:01.00]あ[00:01.50]い[00:02.00]\n")
    line = track.lines[0]
    assert "".join(c.text for c in line.chars) == "あい"
    assert line.chars[0].start_ms == 1_000
    assert line.chars[1].start_ms == 1_500
    assert line.end_ms == 2_000


def test_millisecond_three_digit_timestamps():
    # 点号 3 位 = 毫秒（原样），2 位 = 厘秒（×10）。
    track = parse_nicokara_lrc("[00:01.250]あ[00:01.500]\n")
    assert track.lines[0].chars[0].start_ms == 1_250
    assert track.lines[0].end_ms == 1_500


def test_leading_text_before_first_timestamp_is_kept():
    # 行首在第一个 [ts] 之前的字符（连读 / 空格）不能被丢（正文漏字修复）。
    track = parse_nicokara_lrc(" [00:00:50]あ[00:00:80]い[00:01:00]\n")
    line = track.lines[0]
    assert "".join(c.text for c in line.chars) == " あい"
    assert line.chars[0].text == " "
    assert line.chars[0].start_ms == 500  # 行首字符以第一个 ts 为起点


def test_leading_text_starts_at_first_timestamp_like_n3():
    track = parse_nicokara_lrc(
        "[00:01:00]あ[00:02:00]\n"
        " い[00:03:00]う[00:04:00]\n"
    )
    line = track.lines[1]

    assert "".join(c.text for c in line.chars) == " いう"
    assert [c.start_ms for c in line.chars[:2]] == [3_000, 3_000]
    assert all(c.source_span_start_ms is None for c in line.chars)


def test_leading_text_is_not_mistaken_for_unclosed_trailing_block():
    track = parse_nicokara_lrc("xy[00:03:00]う[00:04:00]\n")
    line = track.lines[0]

    assert [c.text for c in line.chars] == ["x", "y", "う"]
    assert [c.start_ms for c in line.chars] == [3_000, 3_000, 3_000]
    assert line.end_ms == 4_000


def test_missing_line_end_borrows_next_line_start():
    track = parse_nicokara_lrc(
        "[00:01:00]あ\n"
        "\n"
        "[00:03:00]い[00:04:00]\n"
    )

    assert track.lines[0].end_ms == 3_000


def test_trailing_multi_char_block_without_line_end_uses_n3_count_split():
    track = parse_nicokara_lrc(
        "[00:01:00]This [00:01:20]love\n"
        "[00:02:00]Next[00:02:50]\n"
    )
    line = track.lines[0]

    assert "".join(ch.text for ch in line.chars) == "This love"
    assert line.end_ms == 2_000
    assert [
        (
            ch.text,
            ch.start_ms,
            ch.source_span_start_ms,
        )
        for ch in line.chars[-4:]
    ] == [
        ("l", 1_200, None),
        ("o", 1_400, None),
        ("v", 1_600, None),
        ("e", 1_800, None),
    ]


def test_explicit_timed_space_keeps_its_own_n3_interval():
    track = parse_nicokara_lrc("[00:01:00]も[00:01:40] [00:02:00]憧[00:02:50]\n")
    line = track.lines[0]

    assert "".join(ch.text for ch in line.chars) == "も 憧"
    assert [
        (
            ch.text,
            ch.start_ms,
            ch.source_span_start_ms,
        )
        for ch in line.chars[:2]
    ] == [
        ("も", 1_000, None),
        (" ", 1_400, None),
    ]
    assert line.chars[2].start_ms == 2_000


def test_untimed_space_inside_shared_block_consumes_no_n3_wipe_time():
    track = parse_nicokara_lrc("[00:01:00]a b[00:02:00]\n")
    line = track.lines[0]

    assert [ch.text for ch in line.chars] == ["a", " ", "b"]
    assert [ch.start_ms for ch in line.chars] == [1_000, 1_500, 1_500]
    assert line.end_ms == 2_000


def test_combining_sequence_is_one_n3_timed_text_element():
    track = parse_nicokara_lrc("[00:01:00]か\u3099き[00:02:00]\n")
    line = track.lines[0]

    assert [ch.text for ch in line.chars] == ["か\u3099", "き"]
    assert [ch.start_ms for ch in line.chars] == [1_000, 1_500]


def test_emoji_variation_and_non_bmp_symbols_are_single_timed_elements():
    track = parse_nicokara_lrc("[00:01:00]❄️🔯[00:02:00]\n")
    line = track.lines[0]

    assert [ch.text for ch in line.chars] == ["❄️", "🔯"]
    assert [ch.start_ms for ch in line.chars] == [1_000, 1_500]


def test_emoji_tag_not_parsed_as_body_and_kept_in_custom():
    # @Emoji 行（歌手→图定义）应归入尾部元数据，不污染正文，并保留以便 round-trip。
    text = (
        "【sv1】[00:00:50]あ[00:01:00]\n"
        "\n"
        "@Emoji=【sv1】,sv1.png,,zoom=110\n"
        "@Ruby1=亜,あ\n"
    )
    track = parse_nicokara_lrc(text)
    # 正文只有 1 条有字符的行，没有把 @Emoji 当成正文
    body_text = ["".join(c.text for c in ln.chars) for ln in track.lines if ln.chars]
    assert body_text == ["あ"]
    assert any("@Emoji=" in c for c in track.meta.custom)
    assert len(track.rubies) == 1


def test_load_lrc_applies_emoji_role_tag_to_explicit_singer_line(tmp_path):
    lrc = tmp_path / "demo.lrc"
    (tmp_path / "lead.png").write_bytes(b"fake")
    lrc.write_text(
        "【A】[00:01:00]●[00:01:50]a[00:02:00]\n"
        "[00:03:00]b[00:04:00]\n"
        "\n"
        "@Emoji=【A】,lead.png,,Zoom=120,NoDecor,MarginRight=7,MarginBottom=20\n",
        encoding="utf-8",
    )

    track = load_nicokara_lrc(lrc)

    first = track.lines[0].guide_symbol
    assert first is not None
    assert first.kind == "bitmap"
    assert first.prefix_timing == "anchored"
    assert first.bitmap_before_path == str(tmp_path / "lead.png")
    assert first.bitmap_zoom_percent == 120
    assert first.bitmap_no_decor is True
    assert first.bitmap_margin_right_px == 7
    assert first.bitmap_margin_bottom_px == 20
    assert track.lines[1].singer_label == "A"
    assert track.lines[1].guide_symbol is None


def test_load_lrc_applies_single_visible_emoji_token_inline(tmp_path):
    lrc = tmp_path / "demo.lrc"
    (tmp_path / "note.png").write_bytes(b"fake")
    lrc.write_text(
        "[00:01:00]♪[00:01:50]a[00:02:00]\n"
        "\n"
        "@Emoji=♪,note.png,,NoDecor\n",
        encoding="utf-8",
    )

    track = load_nicokara_lrc(lrc)

    symbol = track.lines[0].inline_guide_symbols[0]
    assert symbol.kind == "bitmap"
    assert symbol.bitmap_before_path == str(tmp_path / "note.png")


def test_longer_ruby_base_wins_and_blocks_the_shorter_entry():
    """``呼吸`` claims both characters, so ``呼``'s own reading cannot join it.

    N3 sorts entries by base length descending and skips any shorter candidate
    once a longer one matched, then jumps past the whole group.  Searching the
    line per entry instead let both annotations resolve onto 呼 and drew
    「よ」「い」「き」 side by side above 呼吸.  ``呼`` must still win where it
    stands alone.
    """

    text = (
        "[00:01:00]呼[00:01:20]ん[00:01:40]で[00:02:00]\n"
        "[00:03:00]呼吸[00:03:40]も[00:04:00]\n"
        "\n"
        "@Ruby1=呼,よ\n"
        "@Ruby2=呼吸,い[00:00:17]き\n"
    )

    track = parse_nicokara_lrc(text)

    assert [
        (
            ruby.kanji,
            ruby.reading,
            ruby.target_char_start,
            ruby.target_char_end,
        )
        for ruby in track.rubies
    ] == [
        ("呼", "よ", 0, 1),
        ("呼吸", "いき", 0, 2),
    ]
    assert track.rubies[0].pos_start_ms == 1_000
    assert track.rubies[1].pos_start_ms == 3_000


def test_repeated_ruby_base_in_one_line_gets_one_annotation_each():
    """Every repeat of a base text owns its own annotation and character range.

    A single entry used to resolve to one occurrence only, so a line built from
    the same pair over and over stacked all of its readings on the first match.
    """

    text = (
        "[00:01:00]ケ[00:01:20]ロ[00:01:40]ケ[00:01:60]ロ[00:02:00]\n"
        "\n"
        "@Ruby1=ケ,け\n"
        "@Ruby2=ロ,ろ\n"
    )

    track = parse_nicokara_lrc(text)

    assert sorted(
        (ruby.target_char_start, ruby.reading) for ruby in track.rubies
    ) == [(0, "け"), (1, "ろ"), (2, "け"), (3, "ろ")]
