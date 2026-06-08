from __future__ import annotations

from pathlib import Path

from krok_helper.lyrics import extract_lyrics_query_from_file


def test_extract_query_prefers_lrc_ti_and_ar_tags(tmp_path: Path) -> None:
    lrc = tmp_path / "track01.lrc"
    lrc.write_text(
        "[ti:春日影]\n"
        "[ar:MyGO!!!!!]\n"
        "[al:迷跡波]\n"
        "[00:00.000]わたし まちがえた\n",
        encoding="utf-8",
    )
    assert extract_lyrics_query_from_file(lrc) == "春日影 MyGO!!!!!"


def test_extract_query_falls_back_to_title_only_when_artist_missing(tmp_path: Path) -> None:
    lrc = tmp_path / "noartist.lrc"
    lrc.write_text("[ti:Only Title Here]\n[00:00.00]lyric\n", encoding="utf-8")
    assert extract_lyrics_query_from_file(lrc) == "Only Title Here"


def test_extract_query_uses_filename_stem_for_plain_text(tmp_path: Path) -> None:
    txt = tmp_path / "夜に駆ける.txt"
    txt.write_text("夜に駆ける\n二人だけのストーリー\n", encoding="utf-8")
    assert extract_lyrics_query_from_file(txt) == "夜に駆ける"


def test_extract_query_uses_filename_stem_when_lrc_has_no_ti(tmp_path: Path) -> None:
    # 没有 [ti:] 头部时回落到文件名，避免把第一行歌词当成歌名
    lrc = tmp_path / "Pretender.lrc"
    lrc.write_text(
        "[ar:Official髭男dism]\n"
        "[00:00.000]君とのラブストーリー\n",
        encoding="utf-8",
    )
    assert extract_lyrics_query_from_file(lrc) == "Pretender"


def test_extract_query_tolerates_bom_and_extra_whitespace(tmp_path: Path) -> None:
    lrc = tmp_path / "bom.lrc"
    lrc.write_bytes(
        "﻿[ti:  Spaced Title  ]\n[ar:  Spaced Artist ]\n".encode("utf-8")
    )
    assert extract_lyrics_query_from_file(lrc) == "Spaced Title Spaced Artist"


def test_extract_query_handles_unreadable_binary_file(tmp_path: Path) -> None:
    # 故意写一个无法按 utf-8 解析的二进制（虽然 errors=replace 不会抛，但仍走 stem 兜底）
    binary = tmp_path / "weird-binary.lrc"
    binary.write_bytes(b"\x00\x01\x02\xff\xfe")
    # 无 [ti:] / [ar:] → 退回到 stem
    assert extract_lyrics_query_from_file(binary) == "weird-binary"
