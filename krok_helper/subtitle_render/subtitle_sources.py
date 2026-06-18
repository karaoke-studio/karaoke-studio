"""Nicokara 逐字 LRC 解析器。

输入：SUG ``NicokaraExporter`` 产物（``.lrc``，UTF-8-BOM + CRLF + 含 ``@Ruby`` /
``@Offset`` / ``@Title`` 等元数据），输出 :class:`TimingTrack` 中间表示。

SUG submodule 自身没有 Nicokara LRC 的解析器，本模块照 ``nicokara_exporter.py``
的格式规范反向实现。规范要点（详见导出器源码）：

- 时间戳 ``[MM:SS:CC]`` 厘秒精度
- 每个字符前有一个起始时间戳；行末附加结束时间戳
- 行内"呼吸/演唱停顿"在字符后立即追加一个释放时间戳（产生 ``[ts前]字[ts后][ts下一]`` 形式）
- 演唱者切换通过 ``【演唱者名】`` 标签标注
- 文件尾部依次为：空行 + ``@Title=...`` / ``@Artist=...`` / ``@Album=...``
  / ``@TaggingBy=...`` / ``@SilencemSec=...`` / 用户自定义行 / ``@Offset=±N``
  / ``@RubyN=漢字,読み[t]...,pos1,pos2``
- 文件编码 UTF-8-BOM、CRLF 行尾、末尾换行
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional, Tuple

from krok_helper.subtitle_render.models import (
    RubyAnnotation,
    TimingChar,
    TimingLine,
    TimingTrack,
    TimingTrackMeta,
)

# ``[MM:SS:CC]`` —— 厘秒，分:秒:厘秒；M 可以是多位
_TS_RE = re.compile(r"\[(\d+):(\d+):(\d+)\]")
_SINGER_LABEL_RE = re.compile(r"【([^】]+)】")
_META_PATTERN = re.compile(
    r"^\s*@(Title|Artist|Album|TaggingBy|SilencemSec|Offset|Ruby\d+)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def load_nicokara_lrc(path: str | Path) -> TimingTrack:
    """从磁盘读取 Nicokara LRC 文件并解析。"""
    p = Path(path)
    raw = p.read_bytes()
    text = _decode_with_bom(raw)
    return parse_nicokara_lrc(text)


def parse_nicokara_lrc(text: str) -> TimingTrack:
    """解析 Nicokara LRC 文本为 :class:`TimingTrack`。

    本函数假定输入已经是 ``str``（已去 BOM）。``load_nicokara_lrc`` 会负责 IO + 解码。
    """
    text = _strip_bom(text)
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # 末尾空行（来自 trailing newline）丢掉，避免误判尾部
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()

    body_lines, tail_lines = _split_body_tail(raw_lines)

    timing_lines = _parse_body_lines(body_lines)
    meta, rubies = _parse_tail(tail_lines)
    return TimingTrack(meta=meta, lines=timing_lines, rubies=rubies)


# ---------------------------------------------------------------------------
# 内部：编码 / 文本预处理
# ---------------------------------------------------------------------------


def _decode_with_bom(raw: bytes) -> str:
    # 兼容 UTF-8 with/without BOM；其他编码不在 Nicokara 规范内
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8")
    return raw.decode("utf-8")


def _strip_bom(text: str) -> str:
    if text.startswith("﻿"):
        return text[1:]
    return text


# ---------------------------------------------------------------------------
# 内部：body / tail 切分
# ---------------------------------------------------------------------------


def _split_body_tail(lines: list[str]) -> Tuple[list[str], list[str]]:
    """切分 body 与 tail。

    策略：找第一条匹配 ``@Title|@Artist|@Album|@TaggingBy|@SilencemSec|@Offset|@RubyN``
    的元数据行；其向前回溯所有空行作为分隔，分隔之前是 body、之后是 tail。
    """
    first_meta = next(
        (i for i, ln in enumerate(lines) if _META_PATTERN.match(ln)),
        None,
    )
    if first_meta is None:
        return list(lines), []
    boundary = first_meta
    while boundary > 0 and lines[boundary - 1].strip() == "":
        boundary -= 1
    return lines[:boundary], lines[boundary:]


# ---------------------------------------------------------------------------
# 内部：body 行解析
# ---------------------------------------------------------------------------


def _ts_to_ms(minutes: str, seconds: str, centiseconds: str) -> int:
    return (int(minutes) * 60 + int(seconds)) * 1000 + int(centiseconds) * 10


def _tokenize_line(line: str) -> list[tuple[str, object]]:
    """把行切成 ``('ts', ms)`` / ``('text', str)`` 交替序列。"""
    tokens: list[tuple[str, object]] = []
    pos = 0
    n = len(line)
    while pos < n:
        m = _TS_RE.match(line, pos)
        if m:
            tokens.append(("ts", _ts_to_ms(*m.groups())))
            pos = m.end()
            continue
        nxt = _TS_RE.search(line, pos)
        end = nxt.start() if nxt else n
        text = line[pos:end]
        if text:
            tokens.append(("text", text))
        pos = end
    return tokens


def _parse_body_lines(lines: Iterable[str]) -> list[TimingLine]:
    timing_lines: list[TimingLine] = []
    current_singer_label: Optional[str] = None
    singer_ids: dict[str, int] = {}

    for raw_line in lines:
        line = _parse_body_line(raw_line)
        if line.singer_label is not None:
            current_singer_label = line.singer_label
        elif line.chars and current_singer_label is not None:
            line.singer_label = current_singer_label

        if line.singer_label is not None:
            if line.singer_label not in singer_ids:
                singer_ids[line.singer_label] = len(singer_ids)
            line.singer_id = singer_ids[line.singer_label]
        timing_lines.append(line)
    return timing_lines


def _parse_body_line(line: str) -> TimingLine:
    """解析一条 body 行。

    支持 ``[ts]字[ts]字...[ts_end]``、行首/行中 ``【演唱者】`` 标签、行内停顿释放。
    完全没有时间戳和字符的行视为 ``is_blank``。
    """
    tokens = _tokenize_line(line)

    chars: list[TimingChar] = []
    singer_label: Optional[str] = None
    pending_ts: Optional[int] = None

    for token_index, (ttype, tval) in enumerate(tokens):
        if ttype == "ts":
            ts = int(tval)  # type: ignore[arg-type]
            if pending_ts is not None and chars:
                # 两个连续 [ts] 且前面已有字符 → 前一个 [ts] 是上一字的释放点
                chars[-1].pause_release_ms = pending_ts
            pending_ts = ts
            continue
        # text token
        text = str(tval)
        sm = _SINGER_LABEL_RE.fullmatch(text)
        if sm:
            # 【演唱者】标签：第一个出现的记作 line.singer_label；后续中段切换暂不区分
            if singer_label is None:
                singer_label = sm.group(1)
            continue
        # 普通字符：使用前面 pending 的 [ts] 作为起点
        if pending_ts is None:
            # text 前面没有时间戳：根据格式不该出现；忽略，避免崩
            continue
        next_ts = _next_token_ts(tokens, token_index)
        char_starts = _spread_text_starts(pending_ts, next_ts, len(text))
        for i, ch in enumerate(text):
            chars.append(
                TimingChar(
                    text=ch,
                    start_ms=char_starts[i],
                )
            )
        pending_ts = None

    # tokens 用完后仍剩 pending_ts → 是行末结束时间戳
    end_ms = pending_ts

    raw = line.strip()
    is_blank = not chars and end_ms is None and singer_label is None and raw == ""

    return TimingLine(
        chars=chars,
        end_ms=end_ms,
        singer_label=singer_label,
        is_blank=is_blank,
    )


def _next_token_ts(tokens: list[tuple[str, object]], token_index: int) -> Optional[int]:
    """Return the timestamp token immediately after a text token, if present."""
    next_index = token_index + 1
    if next_index >= len(tokens):
        return None
    next_type, next_value = tokens[next_index]
    if next_type != "ts":
        return None
    return int(next_value)  # type: ignore[arg-type]


def _spread_text_starts(
    start_ms: int,
    next_ts_ms: Optional[int],
    char_count: int,
) -> list[int]:
    """Evenly distribute multiple chars in ``[start]text[next]``.

    Nicokara can put a mora such as ``どう`` between two timestamps. In that
    case the whole text block should wipe uniformly from start to next, so each
    codepoint receives a synthetic start time inside the span.
    """
    if char_count <= 0:
        return []
    if char_count == 1 or next_ts_ms is None or next_ts_ms <= start_ms:
        return [start_ms] * char_count
    duration = next_ts_ms - start_ms
    return [start_ms + (duration * i) // char_count for i in range(char_count)]


# ---------------------------------------------------------------------------
# 内部：tail 元数据 + @Ruby 解析
# ---------------------------------------------------------------------------


def _parse_tail(tail_lines: Iterable[str]) -> Tuple[TimingTrackMeta, list[RubyAnnotation]]:
    meta = TimingTrackMeta()
    rubies: list[RubyAnnotation] = []
    for raw in tail_lines:
        ln = raw.strip()
        if ln == "":
            continue
        # @RubyN=...
        m_ruby = re.match(r"^@Ruby(\d+)\s*=\s*(.*)$", ln, re.IGNORECASE)
        if m_ruby:
            entry = _parse_ruby_entry(m_ruby.group(2))
            if entry is not None:
                rubies.append(entry)
            continue
        # @Title= / @Artist= / @Album= / @TaggingBy= / @SilencemSec= / @Offset=
        m_kv = re.match(r"^@([A-Za-z]+)\s*=\s*(.*)$", ln)
        if m_kv:
            key = m_kv.group(1).lower()
            val = m_kv.group(2).strip()
            if key == "title":
                meta.title = val
            elif key == "artist":
                meta.artist = val
            elif key == "album":
                meta.album = val
            elif key == "taggingby":
                meta.tagging_by = val
            elif key == "silencemsec":
                meta.silence_ms = _parse_int(val, 0)
            elif key == "offset":
                meta.offset_ms = _parse_signed_int(val, 0)
            else:
                # 未知 @标签：原样进 custom 便于 round-trip
                meta.custom.append(ln)
            continue
        # 非 @ 行：用户自定义，原样保留
        meta.custom.append(ln)
    return meta, rubies


def _parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_signed_int(value: str, default: int) -> int:
    # @Offset 可能形如 "+1200" / "-300"
    v = value.strip()
    sign = 1
    if v.startswith("+"):
        v = v[1:]
    elif v.startswith("-"):
        sign = -1
        v = v[1:]
    n = _parse_int(v, default)
    return sign * n


def _parse_ruby_entry(payload: str) -> Optional[RubyAnnotation]:
    """解析单条 ``@RubyN`` 的右值：``漢字,読み[t1][t2]...,pos1,pos2``。

    - ``pos1`` / ``pos2`` 是 ``[MM:SS:CC]`` 格式（含中括号）
    - ``読み`` 中可能内嵌 mora 级时间戳 ``[t]``
    - 漢字 / 読み 内一般不含逗号；按规范以逗号切分
    """
    parts = payload.split(",")
    if len(parts) < 4:
        return None
    kanji = parts[0]
    reading_raw = parts[1]
    pos1_raw = parts[2]
    pos2_raw = parts[3]

    # 读音内 mora 时间戳：去掉它们得到 reading，单独收集毫秒
    # SUG exports these timestamps relative to pos_start_ms, not the global
    # song timeline. Keep them relative; the painter adds pos_start_ms.
    reading_part_ms: list[int] = []
    reading = _TS_RE.sub("", reading_raw)
    for m in _TS_RE.finditer(reading_raw):
        reading_part_ms.append(_ts_to_ms(*m.groups()))

    pos_start_ms = _extract_first_ts(pos1_raw) or 0
    pos_end_ms = _extract_first_ts(pos2_raw) or pos_start_ms

    return RubyAnnotation(
        kanji=kanji,
        reading=reading,
        reading_part_ms=reading_part_ms,
        pos_start_ms=pos_start_ms,
        pos_end_ms=pos_end_ms,
    )


def _extract_first_ts(raw: str) -> Optional[int]:
    m = _TS_RE.search(raw)
    if not m:
        return None
    return _ts_to_ms(*m.groups())
