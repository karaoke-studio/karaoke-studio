"""Tests for ``krok_helper.subtitle_render.subtitle_sources`` Nicokara LRC parser."""

from __future__ import annotations

import pytest

from krok_helper.subtitle_render.subtitle_sources import (
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
    assert [
        (
            c.source_span_start_ms,
            c.source_span_end_ms,
            c.source_span_index,
            c.source_span_count,
        )
        for c in line.chars[:2]
    ] == [
        (38_050, 38_320, 0, 2),
        (38_050, 38_320, 1, 2),
    ]
    assert line.chars[2].source_span_start_ms is None
    assert line.chars[2].source_span_count == 1
    assert line.end_ms == 38_370


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
    assert r.pos_start_ms == 3000
    assert r.pos_end_ms == 5000


def test_ruby_entry_without_position_is_kept_as_global_annotation():
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
    assert r.pos_start_ms == 0
    assert r.pos_end_ms == 0


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
