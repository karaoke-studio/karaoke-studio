"""Nicokara 逐字 LRC 解析器。

输入：SUG ``NicokaraExporter`` 产物（``.lrc``，UTF-8-BOM + CRLF + 含 ``@Ruby`` /
``@Offset`` / ``@Title`` 等元数据），输出 :class:`TimingTrack` 中间表示。

本模块照 ``nicokara_exporter.py`` 的格式规范实现，并对齐 NicoKaraMaker3 的 LRC
时间补全语义（尤其"绝不丢字"：行首/连读等无独立时间戳的字符必须保留）。
``[start]多字[next]`` 按非空白 Unicode 文本元素的数量等分；显式定时空格保留完整
区间，块内无独立时间的空格不消耗走字时间。这里直接产出最终字符起点，不把 LRC
共享块交给 Painter 按字体宽度二次分配；SUG ``.sug`` 直读仍保留原有宽度加权语义。
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

from dataclasses import replace
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Optional, Tuple

from krok_helper.subtitle_render.models import (
    GuideSymbol,
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
_EMOJI_TAG_RE = re.compile(r"^@Emoji\d*=(.*)$", re.IGNORECASE)
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
    track = parse_nicokara_lrc(text)
    body_lines, _tail_lines = _split_body_tail(_normalized_lines(text))
    _apply_emoji_guides(track, p.parent, body_lines)
    return track


def parse_nicokara_lrc(text: str) -> TimingTrack:
    """解析 Nicokara LRC 文本为 :class:`TimingTrack`。

    本函数假定输入已经是 ``str``（已去 BOM）。``load_nicokara_lrc`` 会负责 IO + 解码。
    """
    body_lines, tail_lines = _split_body_tail(_normalized_lines(text))

    timing_lines = _parse_body_lines(body_lines)
    meta, rubies = _parse_tail(tail_lines)
    # LRC 本身不保存分页和段落边界。这里只恢复歌词、空行和计时语义，
    # 进入字幕项目后再由该字幕源的加载设置统一生成 page_plan。
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


def _normalized_lines(text: str) -> list[str]:
    text = _strip_bom(text)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # 末尾空行（来自 trailing newline）丢掉，避免误判尾部
    if lines and lines[-1] == "":
        lines.pop()
    return lines


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


def _apply_emoji_guides(
    track: TimingTrack,
    base_dir: Path,
    body_lines: list[str],
) -> None:
    specs = _parse_emoji_specs(track.meta.custom, base_dir)
    if not specs:
        return
    for line, raw_line in zip(track.lines, body_lines):
        if line.is_blank or not line.chars:
            continue
        raw_text = str(raw_line)
        for spec in specs:
            trigger = spec["trigger"]
            if not trigger or trigger not in raw_text:
                continue
            inline_index = _line_text_index(line, trigger)
            if inline_index is not None:
                line.inline_guide_symbols[inline_index] = _emoji_guide_symbol(
                    spec, anchored=False
                )
                continue
            if _trigger_matches_line_singer(trigger, line):
                line.guide_symbol = replace(
                    _emoji_guide_symbol(spec, anchored=True),
                    role_label=line.singer_label,
                    role_labels=(line.singer_label,),
                )
                break


def _parse_emoji_specs(lines: Iterable[str], base_dir: Path) -> list[dict[str, object]]:
    specs: list[dict[str, object]] = []
    seen: set[str] = set()
    for line in lines:
        match = _EMOJI_TAG_RE.match(str(line).strip())
        if match is None:
            continue
        parts = [part.strip() for part in match.group(1).replace("，", ",").split(",")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        trigger = parts[0]
        if trigger in seen:
            continue
        seen.add(trigger)
        before = _resolve_emoji_image(parts[1], base_dir)
        after = _resolve_emoji_image(parts[2], base_dir) if len(parts) >= 3 and parts[2] else None
        spec: dict[str, object] = {
            "trigger": trigger,
            "before": str(before),
            "after": str(after) if after is not None else None,
            "zoom": 100,
            "fix": False,
            "no_decor": False,
            "force_wipe_decor": False,
            "margin_left": 0,
            "margin_right": 0,
            "margin_bottom": 0,
        }
        for raw_option in parts[3:]:
            option = raw_option.strip()
            if not option:
                continue
            key, sep, raw_value = option.partition("=")
            key_lower = key.strip().lower()
            value = raw_value.strip().rstrip("%")
            if sep and key_lower == "zoom":
                spec["zoom"] = max(_parse_int(value, int(spec["zoom"])), 1)
            elif key_lower == "fix":
                spec["fix"] = True
            elif key_lower == "nodecor":
                spec["no_decor"] = True
            elif key_lower == "forcewipedecor":
                spec["force_wipe_decor"] = True
            elif sep and key_lower == "marginleft":
                spec["margin_left"] = _parse_int(value, int(spec["margin_left"]))
            elif sep and key_lower == "marginright":
                spec["margin_right"] = _parse_int(value, int(spec["margin_right"]))
            elif sep and key_lower == "marginbottom":
                spec["margin_bottom"] = _parse_int(value, int(spec["margin_bottom"]))
        specs.append(spec)
    return specs


def _resolve_emoji_image(path_text: str, base_dir: Path) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else base_dir / path


def _emoji_guide_symbol(spec: dict[str, object], *, anchored: bool) -> GuideSymbol:
    trigger = str(spec.get("trigger") or "")
    return GuideSymbol(
        name=f"N3 Emoji {trigger}",
        kind="bitmap",
        bitmap_before_path=str(spec.get("before") or ""),
        bitmap_after_path=str(spec.get("after") or "") or None,
        bitmap_zoom_percent=max(int(spec.get("zoom") or 100), 1),
        bitmap_fix_size=bool(spec.get("fix")),
        bitmap_no_decor=bool(spec.get("no_decor")),
        bitmap_force_wipe_decor=bool(spec.get("force_wipe_decor")),
        bitmap_margin_left_px=int(spec.get("margin_left") or 0),
        bitmap_margin_right_px=int(spec.get("margin_right") or 0),
        bitmap_margin_bottom_px=int(spec.get("margin_bottom") or 0),
        prefix_timing="anchored" if anchored else "pre_roll",
    )


def _trigger_matches_line_singer(trigger: str, line: TimingLine) -> bool:
    return bool(line.singer_label) and trigger == f"【{line.singer_label}】"


def _line_text_index(line: TimingLine, trigger: str) -> Optional[int]:
    if not trigger:
        return None
    position = 0
    text = "".join(char.text for char in line.chars)
    found = text.find(trigger)
    if found < 0:
        return None
    for index, char in enumerate(line.chars):
        if position == found and char.text == trigger:
            return index
        position += len(char.text)
    return None


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
                chars[-1].explicit_end = True
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
                for ch in _text_elements(value):
                    leading_buffer.append((ch, active_role))
            continue
        next_ts = _next_token_ts(tokens, token_index)
        text_entries: list[tuple[str, Optional[str]]] = []
        role_for_entry = active_role
        for kind, value in parts:
            if kind == "role":
                role_for_entry = value
                continue
            text_entries.extend((element, role_for_entry) for element in _text_elements(value))
        visible_count = len(text_entries)
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
            for ch, role in leading_buffer:
                chars.append(
                    TimingChar(
                        text=ch,
                        start_ms=pending_ts,
                        role_label=role,
                    )
                )
            leading_buffer.clear()
        char_starts = _spread_text_starts(
            pending_ts,
            next_ts,
            [element for element, _role in text_entries],
        )
        unresolved_tail = visible_count > 1 and next_ts is None
        start_index = 0
        for kind, value in parts:
            if kind == "role":
                active_role = value
                if singer_label is None:
                    singer_label = active_role
                continue
            for ch in _text_elements(value):
                chars.append(
                    TimingChar(
                        text=ch,
                        start_ms=char_starts[start_index],
                        explicit_start=start_index == 0,
                        explicit_end=(
                            start_index == visible_count - 1 and next_ts is not None
                        ),
                        role_label=active_role,
                        # Temporary parser marker. Cross-line completion uses
                        # the following line start, then clears these fields so
                        # Painter cannot apply SUG's width-weighted span logic.
                        source_span_start_ms=pending_ts if unresolved_tail else None,
                        source_span_end_ms=None,
                        source_span_index=start_index if unresolved_tail else 0,
                        source_span_count=visible_count if unresolved_tail else 1,
                    )
                )
                start_index += 1
        pending_ts = None

    # tokens 用完后仍剩 pending_ts → 是行末结束时间戳
    end_ms = pending_ts
    if end_ms is not None and chars:
        chars[-1].explicit_end = True

    # 行内有时间戳、但行首缓存字符一直没机会补回（如 ` [ts]` 仅"行首文本 + 结束 ts"）：
    # 用行末 ts 作为起点补回，仍不丢字。完全无时间戳的纯文本行保持空行语义（丢弃缓存）。
    if leading_buffer and end_ms is not None:
        for ch, role in leading_buffer:
            chars.append(
                TimingChar(
                    text=ch,
                    start_ms=end_ms,
                    role_label=role,
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
    _normalize_trailing_unclosed_blocks(lines)


def _normalize_trailing_unclosed_blocks(lines: list[TimingLine]) -> None:
    """Spread a final multi-char block with no explicit line end to next line.

    N3 uses the next sentence's first timestamp as the final untimed group end.
    LRC export omits an explicit trailing timestamp for some lines (for example
    ``[01:04:48]love``), so apply the same count-based completion after the
    compatibility ``line.end_ms`` borrow runs.
    """

    for line in lines:
        if line.is_blank or not line.chars or line.end_ms is None:
            continue
        end_ms = line.end_ms
        chars = line.chars
        last = chars[-1]
        group_count = int(last.source_span_count)
        if (
            group_count <= 1
            or group_count > len(chars)
            or last.source_span_index != group_count - 1
            or last.source_span_end_ms is not None
        ):
            continue
        group_start = len(chars) - group_count
        group = chars[group_start:]
        start_ms = last.source_span_start_ms
        if start_ms is None or end_ms <= start_ms:
            for ch in group:
                _clear_source_span(ch)
            continue
        if any(
            ch.source_span_start_ms != start_ms
            or ch.source_span_end_ms is not None
            or ch.source_span_index != offset
            or ch.source_span_count != group_count
            for offset, ch in enumerate(group)
        ):
            continue
        starts = _spread_text_starts(
            start_ms,
            end_ms,
            [ch.text for ch in group],
        )
        for ch, resolved_start in zip(group, starts):
            ch.start_ms = resolved_start
            _clear_source_span(ch)


def _clear_source_span(ch: TimingChar) -> None:
    ch.source_span_start_ms = None
    ch.source_span_end_ms = None
    ch.source_span_index = 0
    ch.source_span_count = 1

def _borrow_missing_line_ends(lines: list[TimingLine]) -> None:
    for index, line in enumerate(lines):
        if line.is_blank or not line.chars or line.end_ms is not None:
            continue
        next_start = _next_line_leader_ms(lines, index + 1)
        if next_start is not None and next_start >= line.chars[-1].start_ms:
            line.end_ms = next_start
            continue
        # N3 的最后一行没有后继锚点时，把末字符结束补为最后一个有效起点，
        # 即未闭合尾组为零时长，而不是凭空延长到播放器末尾。
        line.end_ms = line.chars[-1].start_ms


def _next_line_leader_ms(lines: list[TimingLine], start_index: int) -> Optional[int]:
    for line in lines[start_index:]:
        if line.is_blank or not line.chars:
            continue
        return _line_leader_ms(line)
    return None


def _line_leader_ms(line: TimingLine) -> Optional[int]:
    if not line.chars:
        return None
    return line.chars[0].start_ms


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
    elements: list[str],
) -> list[int]:
    """Apply N3 ``ComplementTimes`` semantics to one timestamp-delimited block.

    Time is divided by the number of non-space text elements. Untimed spaces
    share the following boundary and consume no duration; a standalone timed
    space still retains the whole interval through the next character start.
    """
    if not elements:
        return []
    if next_ts_ms is None or next_ts_ms <= start_ms:
        return [start_ms] * len(elements)
    timed_count = sum(not element.isspace() for element in elements)
    if timed_count <= 0:
        return [start_ms] * len(elements)
    duration = next_ts_ms - start_ms
    completed = 0
    starts: list[int] = []
    for element in elements:
        starts.append(start_ms + (duration * completed) // timed_count)
        if not element.isspace():
            completed += 1
    return starts


def _text_elements(text: str) -> list[str]:
    """Split text like .NET ``StringInfo`` instead of iterating code points.

    N3 assigns time per Unicode text element. Python's standard library has no
    full grapheme-break iterator, but combining marks, variation selectors,
    emoji skin-tone modifiers and ZWJ sequences cover the forms used by LRC.
    """
    elements: list[str] = []
    join_next = False
    for char in text:
        codepoint = ord(char)
        combining = unicodedata.category(char) in {"Mn", "Mc", "Me"}
        variation = 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF
        emoji_modifier = 0x1F3FB <= codepoint <= 0x1F3FF
        if elements and (combining or variation or emoji_modifier or join_next or char == "\u200d"):
            elements[-1] += char
        else:
            elements.append(char)
        join_next = char == "\u200d"
    return elements


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
