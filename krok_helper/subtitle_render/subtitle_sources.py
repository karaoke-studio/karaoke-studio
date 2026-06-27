"""Nicokara 逐字 LRC 解析器。

输入：SUG ``NicokaraExporter`` 产物（``.lrc``，UTF-8-BOM + CRLF + 含 ``@Ruby`` /
``@Offset`` / ``@Title`` 等元数据），输出 :class:`TimingTrack` 中间表示。

本模块照 ``nicokara_exporter.py`` 的格式规范实现，并对齐 SUG submodule 既有的权威
解析器 ``strange_uta_game.backend.infrastructure.parsers.lyric_parser.NicokaraParser``
的关键语义（尤其"绝不丢字"：行首/连读等无独立时间戳的字符必须保留）。本模块会保留
``[start]多字[next]`` 的共享时间块元数据；解析阶段仍生成兼容旧消费者的
等分 ``start_ms``，Python Painter 再按当前字体的字符布局宽度重新分时，以对齐 SUG
``KaraokePreview`` 的无独立时间戳多字走字。
规范要点（详见导出器源码）：

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

# 时间戳：``[MM:SS:CC]``（冒号厘秒，nicokara）/ ``[MM:SS.CC]``（点号厘秒，标准 LRC）/
# ``[MM:SS.mmm]``（点号毫秒，3 位）。秒与子秒间允许 ``:`` 或 ``.``；子秒 2 位=厘秒、3 位=毫秒。
# 对齐 submodule ``NicokaraParser.FLEXIBLE_TS_PATTERN``——旧实现只认冒号厘秒，导致点号
# 格式文件整篇匹配不到时间戳、正文被整体丢弃（漏字主因）。
_TS_RE = re.compile(r"\[(\d+):(\d+)[:.](\d{2,3})\]")
_SINGER_LABEL_RE = re.compile(r"【([^】]+)】")
# 尾部元数据边界：任意 ``@<key>=`` 行（@Title/@Artist/@Album/@TaggingBy/@SilencemSec/
# @Offset/@RubyN/@Emoji/未知）都视为元数据起点。旧实现只认固定几个标签，导致 @Emoji
# 等行被当成正文（幻影空行 + 丢失歌手定义）。正文行总以 ``【…】`` 或 ``[ts]`` 开头，
# 不以 ``@`` 开头，故按 ``@key=`` 判定边界是安全的。
_META_PATTERN = re.compile(r"^\s*@\w+\s*=", re.IGNORECASE)


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


def _ts_to_ms(minutes: str, seconds: str, sub: str) -> int:
    # 子秒 2 位=厘秒（×10→ms），3 位=毫秒（原样）。与 submodule _parse_nicokara_timestamp 一致。
    millis = int(sub) * 10 if len(sub) == 2 else int(sub)
    return (int(minutes) * 60 + int(seconds)) * 1000 + millis


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
    # 「角色 / 配色」标签跨行延续：上一行末尾生效的标签继续作用到下一行，直到下次切换。
    active_role: Optional[str] = None

    for raw_line in lines:
        line, active_role = _parse_body_line(raw_line, active_role)
        if line.singer_label is not None:
            current_singer_label = line.singer_label
        elif line.chars and current_singer_label is not None:
            line.singer_label = current_singer_label

        if line.singer_label is not None:
            if line.singer_label not in singer_ids:
                singer_ids[line.singer_label] = len(singer_ids)
            line.singer_id = singer_ids[line.singer_label]
        timing_lines.append(line)
    _normalize_cross_line_anchors(timing_lines)
    return timing_lines


def _parse_body_line(
    line: str, active_role: Optional[str] = None
) -> tuple[TimingLine, Optional[str]]:
    """解析一条 body 行。返回 ``(TimingLine, 行末生效的角色标签)``。

    支持 ``[ts]字[ts]字...[ts_end]``、行首/行中 ``【N配色】`` 角色标签、行内停顿释放。
    ``【...】`` 在一行内可多次出现，每次切换其后字符的 ``role_label``；``active_role``
    由调用方跨行透传（标签会延续到下一次切换）。完全没有时间戳和字符的行视为
    ``is_blank``。``line.singer_label`` 仍记该行第一个标签（向后兼容现有歌手机制）。
    """
    tokens = _tokenize_line(line)

    chars: list[TimingChar] = []
    singer_label: Optional[str] = None
    pending_ts: Optional[int] = None
    # 行首（第一个 [ts] 之前）的可见字符缓存：连读字 / 行首空格等无独立起始时间戳的
    # 字符，nicokara 规范里是"与后一字共享时间"，不能丢（旧实现直接忽略 → 正文漏字）。
    leading_buffer: list[tuple[str, Optional[str]]] = []

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
        parts = _split_role_labels(text)
        if not parts:
            continue
        if all(kind == "role" for kind, _value in parts):
            for _kind, label in parts:
                active_role = label
                if singer_label is None:
                    singer_label = active_role
            continue
        # 普通字符：使用前面 pending 的 [ts] 作为起点
        if pending_ts is None:
            # text 在第一个 [ts] 之前：角色标签照常生效；可见字符**先缓存**（连读 / 行首
            # 空格等无独立时间戳的字符），等第一个时间戳到来时以该 ts 作为起点补回，
            # 不再直接丢弃（修复正文行首漏字，对齐 submodule NicokaraParser 的"绝不丢字"）。
            for kind, value in parts:
                if kind == "role":
                    active_role = value
                    if singer_label is None:
                        singer_label = active_role
                    continue
                for ch in value:
                    leading_buffer.append((ch, active_role))
            continue
        next_ts = _next_token_ts(tokens, token_index)
        visible_count = sum(len(value) for kind, value in parts if kind == "text")
        if visible_count <= 0:
            for kind, value in parts:
                if kind != "role":
                    continue
                active_role = value
                if singer_label is None:
                    singer_label = active_role
            continue
        # 行首缓存字符补回：以本组的起点 ts 作为它们的起始（与本组首字共享时间）。
        if leading_buffer:
            leading_count = len(leading_buffer)
            for offset, (ch, role) in enumerate(leading_buffer):
                chars.append(
                    TimingChar(
                        text=ch,
                        start_ms=pending_ts,
                        role_label=role,
                        source_span_start_ms=pending_ts,
                        source_span_end_ms=pending_ts,
                        source_span_index=offset,
                        source_span_count=leading_count,
                    )
                )
            leading_buffer.clear()
        char_starts = _spread_text_starts(pending_ts, next_ts, visible_count)
        shared_span = (
            visible_count > 1
            and next_ts is not None
            and next_ts > pending_ts
        )
        start_index = 0
        for kind, value in parts:
            if kind == "role":
                active_role = value
                if singer_label is None:
                    singer_label = active_role
                continue
            for ch in value:
                chars.append(
                    TimingChar(
                        text=ch,
                        start_ms=char_starts[start_index],
                        role_label=active_role,
                        source_span_start_ms=pending_ts if shared_span else None,
                        source_span_end_ms=next_ts if shared_span else None,
                        source_span_index=start_index if shared_span else 0,
                        source_span_count=visible_count if shared_span else 1,
                    )
                )
                start_index += 1
        pending_ts = None

    # tokens 用完后仍剩 pending_ts → 是行末结束时间戳
    end_ms = pending_ts

    # 行内有时间戳、但行首缓存字符一直没机会补回（如 ` [ts]` 仅"行首文本 + 结束 ts"）：
    # 用行末 ts 作为起点补回，仍不丢字。完全无时间戳的纯文本行保持空行语义（丢弃缓存）。
    if leading_buffer and end_ms is not None:
        leading_count = len(leading_buffer)
        for offset, (ch, role) in enumerate(leading_buffer):
            chars.append(
                TimingChar(
                    text=ch,
                    start_ms=end_ms,
                    role_label=role,
                    source_span_start_ms=end_ms,
                    source_span_end_ms=end_ms,
                    source_span_index=offset,
                    source_span_count=leading_count,
                )
            )
        leading_buffer.clear()

    raw = line.strip()
    is_blank = not chars and end_ms is None and singer_label is None and raw == ""

    return (
        TimingLine(
            chars=chars,
            end_ms=end_ms,
            singer_label=singer_label,
            is_blank=is_blank,
        ),
        active_role,
    )


def _normalize_cross_line_anchors(lines: list[TimingLine]) -> None:
    _borrow_missing_line_ends(lines)

    previous_end_ms: Optional[int] = None
    for line in lines:
        if line.is_blank or not line.chars:
            continue
        leading_count = _leading_unanchored_count(line)
        leader_ms = _line_leader_ms(line)
        if (
            leading_count > 0
            and leader_ms is not None
            and previous_end_ms is not None
            and previous_end_ms < leader_ms
        ):
            starts = _spread_text_starts(previous_end_ms, leader_ms, leading_count)
            for offset, ch in enumerate(line.chars[:leading_count]):
                ch.start_ms = starts[offset]
                ch.source_span_start_ms = previous_end_ms
                ch.source_span_end_ms = leader_ms
                ch.source_span_index = offset
                ch.source_span_count = leading_count
        if line.end_ms is not None:
            previous_end_ms = line.end_ms


def _borrow_missing_line_ends(lines: list[TimingLine]) -> None:
    for index, line in enumerate(lines):
        if line.is_blank or not line.chars or line.end_ms is not None:
            continue
        next_start = _next_line_leader_ms(lines, index + 1)
        if next_start is None or next_start < line.chars[-1].start_ms:
            continue
        line.end_ms = next_start


def _next_line_leader_ms(lines: list[TimingLine], start_index: int) -> Optional[int]:
    for line in lines[start_index:]:
        if line.is_blank or not line.chars:
            continue
        return _line_leader_ms(line)
    return None


def _line_leader_ms(line: TimingLine) -> Optional[int]:
    if not line.chars:
        return None
    leading_count = _leading_unanchored_count(line)
    if leading_count > 0:
        end_ms = line.chars[0].source_span_end_ms
        if end_ms is not None:
            return end_ms
    return line.chars[0].start_ms


def _leading_unanchored_count(line: TimingLine) -> int:
    if not line.chars:
        return 0
    first = line.chars[0]
    count = int(first.source_span_count)
    start = first.source_span_start_ms
    end = first.source_span_end_ms
    if count <= 0 or start is None or end is None or start != end:
        return 0
    if count > len(line.chars):
        return 0
    for offset, ch in enumerate(line.chars[:count]):
        if (
            ch.source_span_start_ms != start
            or ch.source_span_end_ms != end
            or ch.source_span_index != offset
            or ch.source_span_count != count
        ):
            return 0
    return count


def _split_role_labels(text: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    pos = 0
    for match in _SINGER_LABEL_RE.finditer(text):
        if match.start() > pos:
            parts.append(("text", text[pos:match.start()]))
        parts.append(("role", match.group(1)))
        pos = match.end()
    if pos < len(text):
        parts.append(("text", text[pos:]))
    return parts


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
    """Generate compatibility starts for multiple chars in ``[start]text[next]``.

    ``TimingChar.source_span_*`` preserves the original shared block. Horizontal
    Python rendering uses those fields to redistribute the span by glyph layout
    width; these equal starts remain for consumers that do not have font metrics.
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
    if len(parts) < 2:
        return None
    kanji = parts[0]
    reading_raw = parts[1]
    pos1_raw = parts[2] if len(parts) >= 3 else ""
    pos2_raw = parts[3] if len(parts) >= 4 else ""

    # 读音内 mora 时间戳：去掉它们得到 reading，单独收集毫秒
    # SUG exports these timestamps relative to pos_start_ms, not the global
    # song timeline. Keep them relative; the painter adds pos_start_ms.
    reading_part_ms: list[int] = []
    reading_parts: list[str] = []
    cursor = 0
    for m in _TS_RE.finditer(reading_raw):
        reading_parts.append(reading_raw[cursor:m.start()])
        reading_part_ms.append(_ts_to_ms(*m.groups()))
        cursor = m.end()
    reading_parts.append(reading_raw[cursor:])
    reading = "".join(reading_parts)

    pos_start_ms = _extract_first_ts(pos1_raw) or 0
    pos_end_ms = _extract_first_ts(pos2_raw) or pos_start_ms

    return RubyAnnotation(
        kanji=kanji,
        reading=reading,
        reading_part_ms=reading_part_ms,
        pos_start_ms=pos_start_ms,
        pos_end_ms=pos_end_ms,
        reading_parts=reading_parts,
    )


def _extract_first_ts(raw: str) -> Optional[int]:
    m = _TS_RE.search(raw)
    if not m:
        return None
    return _ts_to_ms(*m.groups())
