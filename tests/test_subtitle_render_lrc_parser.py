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
    assert [c.text for c in line.chars] == ["あ"]


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
