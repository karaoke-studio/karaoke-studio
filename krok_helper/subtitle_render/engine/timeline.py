"""时间 → 活跃行 / 字符级演唱区间查询。

字符级时间区间约定（与 :class:`paint_frame` 共用语义）：

- 每个字符的 ``start_ms`` 是 ``[ts]<char>`` 中的前导或兼容合成时间戳；如果同一个
  ``[ts]`` 后面跟多个字符（如 ``[00:38:05]どう[00:38:32]``），解析器还会保留
  ``source_span_*`` 共享块。Painter 传入字符布局宽度后，区间按像素宽度重新分配；
  无宽度的消费者继续使用兼容合成时间
- 字符 i 的 ``end_ms`` = 字符 i+1 的 ``start_ms``（行内）；行末字符 = ``line.end_ms``
- 如果某字符设了 ``pause_release_ms``（行内呼吸），它的 ``end_ms`` 取释放点；
  下一字起始前的空白段会保持填色不变，避免"呼吸期还在涨色"

无活跃行的 ``find_active_line`` 返回 ``None``。无字符 / 空行不参与查找。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from krok_helper.subtitle_render.models import TimingChar, TimingLine, TimingTrack


@dataclass(frozen=True)
class DisplayLine:
    """A line with its computed display window and two-line lane."""

    line: TimingLine
    lane: int
    display_start_ms: int
    display_end_ms: int


def assign_lanes(
    render_lines: list[TimingLine],
    default_rows: int,
    row_count_of: Optional[Callable[[TimingLine], int]] = None,
) -> tuple[list[int], list[int], list[int]]:
    """按页分配 lane（N3 页级布局联动）。

    页从页首行的布局行数决定：最多连续 ``rows`` 条可渲染行组成一页；若后续行
    带 ``break_before``（N3 PageBreak / ParagraphBreak），则提前结束
    当前页。页内行依次占 lane ``0..rows-1``。返回与 ``render_lines`` 对齐的
    ``(lanes, page_starts, page_rows)``。
    """
    lanes: list[int] = []
    page_starts: list[int] = []
    page_rows: list[int] = []
    index = 0
    total = len(render_lines)
    has_explicit_breaks = any(
        getattr(line, "break_before", "none") != "none"
        for line in render_lines[1:]
    )
    while index < total:
        rows = max(int(default_rows), 1)
        if row_count_of is not None:
            rows = max(int(row_count_of(render_lines[index])), 1)
        page_end = total if has_explicit_breaks else min(index + rows, total)
        for candidate in range(index + 1, page_end):
            if getattr(render_lines[candidate], "break_before", "none") != "none":
                page_end = candidate
                break
        page_size = max(page_end - index, 1)
        for offset in range(page_size):
            lanes.append(offset)
            page_starts.append(index)
            page_rows.append(page_size)
        index = page_end
    return lanes, page_starts, page_rows


def find_active_line(
    track: TimingTrack,
    t_ms: int,
    *,
    lead_in_ms: int = 0,
) -> Optional[TimingLine]:
    """返回 ``t_ms`` 时刻正在演唱的行；无则返回 ``None``。

    判定区间 = ``[line.chars[0].start_ms - lead_in_ms, line_end_ms]``，闭区间。
    ``line_end_ms`` 取 ``line.end_ms`` 或末字符 ``start_ms`` + 1000 ms 作为安全兜底。
    ``lead_in_ms`` 只影响显示时机，不改变字符填充时间。
    """
    best_live: Optional[TimingLine] = None
    for line in track.lines:
        if line.is_blank or not line.chars:
            continue
        start = line.chars[0].start_ms
        end = _line_end_ms(line)
        if start <= t_ms <= end:
            # 多行重叠时取最靠后开始的（合唱叠唱场景，更贴近"刚发声"那条）
            if best_live is None or start >= best_live.chars[0].start_ms:
                best_live = line
    if best_live is not None:
        return best_live

    best: Optional[TimingLine] = None
    lead = max(lead_in_ms, 0)
    for line in track.lines:
        if line.is_blank or not line.chars:
            continue
        start = line.chars[0].start_ms - lead
        end = _line_end_ms(line)
        if start <= t_ms <= end:
            # 多行重叠时取最靠后开始的（合唱叠唱场景，更贴近"刚发声"那条）
            if best is None or line.chars[0].start_ms >= best.chars[0].start_ms:
                best = line
    return best


def visible_display_lines(
    track: TimingTrack,
    t_ms: int,
    *,
    lead_in_ms: int,
    tail_ms: int,
    lane_gap_ms: int,
    max_hold_ms: int,
    continuity_snap_ms: int,
    pair_second_delay_ms: int = 3000,
    section_gap_ms: int = 0,
    sync_ending: bool = False,
    section_ending_mode: str = "hold",
    protect_ms: int = 0,
    lane_count: int = 2,
    row_count_of: Optional[Callable[[TimingLine], int]] = None,
) -> list[DisplayLine]:
    """Return lines whose display window contains ``t_ms``.

    The display window intentionally differs from singing time:

    - Lines rotate through lanes ``0..lane_count-1`` (lane 0 = top row).
    - Preferred display start is ``sing_start - lead_in_ms``.
    - When the same lane becomes available shortly before the preferred start,
      the next line snaps earlier to keep the lane visually continuous.
    - Display end is the page (``lane_count`` consecutive lines) singing end
      plus ``tail_ms``, capped by the next same-lane display start minus
      ``lane_gap_ms``.
    """
    layouts = compute_display_lines(
        track,
        lead_in_ms=lead_in_ms,
        tail_ms=tail_ms,
        protect_ms=protect_ms,
        lane_gap_ms=lane_gap_ms,
        max_hold_ms=max_hold_ms,
        continuity_snap_ms=continuity_snap_ms,
        pair_second_delay_ms=pair_second_delay_ms,
        section_gap_ms=section_gap_ms,
        sync_ending=sync_ending,
        section_ending_mode=section_ending_mode,
        lane_count=lane_count,
        row_count_of=row_count_of,
    )
    return [item for item in layouts if item.display_start_ms <= t_ms <= item.display_end_ms]


def compute_display_lines(
    track: TimingTrack,
    *,
    lead_in_ms: int,
    tail_ms: int,
    lane_gap_ms: int,
    max_hold_ms: int,
    continuity_snap_ms: int,
    pair_second_delay_ms: int = 3000,
    section_gap_ms: int = 0,
    sync_ending: bool = False,
    section_ending_mode: str = "hold",
    protect_ms: int = 0,
    lane_count: int = 2,
    row_count_of: Optional[Callable[[TimingLine], int]] = None,
) -> list[DisplayLine]:
    """Compute NicoKara-style display windows for all renderable lines.

    段落（section）按间奏间隔自动划分：相邻两句演唱空隙 > ``section_gap_ms`` 即开
    新段落。``sync_ending`` 时同段落内每个 lane 的末行延到段末一起退场；
    ``section_ending_mode == "clear"`` 时把每行结束钳到段末（不拖进间奏）。
    两项默认关闭时输出与原行为一致。

    ``row_count_of``（可选）按行返回其布局的行数：页从页首行的行数决定
    （N3 页级布局联动），未提供时全部页使用 ``lane_count``。
    """
    render_lines = [line for line in track.lines if not line.is_blank and line.chars]
    if not render_lines:
        return []

    lead = max(lead_in_ms, 0)
    tail = max(tail_ms, 0)
    protect = max(protect_ms, 0)
    lane_gap = max(lane_gap_ms, 0)
    max_hold = max(max_hold_ms, 0)
    snap = max(continuity_snap_ms, 0)
    pair_second_delay = max(pair_second_delay_ms, 0)
    section_gap = max(section_gap_ms, 0)
    section_ids = _compute_section_ids(render_lines, section_gap)
    section_end = _compute_section_ends(render_lines, section_ids, tail)

    lanes_total = max(int(lane_count), 1)
    lanes, page_starts, page_rows = assign_lanes(
        render_lines, lanes_total, row_count_of
    )

    starts: list[int] = []
    natural_ends: list[int] = []
    prev_lane_natural_end: dict[int, int] = {}

    for index, line in enumerate(render_lines):
        lane = lanes[index]
        preferred_start = max(line.chars[0].start_ms - lead, 0)
        if (
            lane != 0
            and starts
            and section_ids[index] == section_ids[index - 1]
        ):
            preferred_start = min(
                preferred_start,
                starts[index - 1] + pair_second_delay,
            )
        page_start = page_starts[index]
        pair_end = max(
            _line_end_ms(item)
            for item in render_lines[page_start : page_start + page_rows[index]]
        )
        natural_end = pair_end + tail
        if max_hold > 0:
            natural_end = min(natural_end, preferred_start + max_hold)
        natural_ends.append(natural_end)

        previous_end = prev_lane_natural_end.get(lane)
        if previous_end is None:
            display_start = preferred_start
        else:
            available_start = previous_end + lane_gap
            if abs(preferred_start - available_start) <= snap:
                display_start = available_start
            else:
                display_start = preferred_start
        starts.append(display_start)
        prev_lane_natural_end[lane] = natural_end

    display_ends: list[int] = []
    for index, line in enumerate(render_lines):
        own_sing_end = _line_end_ms(line)
        floor_end = own_sing_end + protect
        display_end = max(natural_ends[index], floor_end)
        if max_hold > 0:
            display_end = max(floor_end, min(display_end, starts[index] + max_hold))
        display_ends.append(display_end)

    _adjust_same_lane_display_windows(
        render_lines,
        starts,
        display_ends,
        lanes,
        lead=lead,
        protect=protect,
        lane_gap=lane_gap,
    )

    result: list[DisplayLine] = []
    for index, line in enumerate(render_lines):
        own_sing_end = _line_end_ms(line)
        floor_end = own_sing_end + protect
        display_end = max(display_ends[index], floor_end)
        if max_hold > 0:
            display_end = max(floor_end, min(display_end, starts[index] + max_hold))
        # 段落 / 同步退场
        sid = section_ids[index]
        if sync_ending and _is_last_in_lane_in_section(lanes, section_ids, index):
            display_end = max(display_end, section_end[sid])
        if section_ending_mode == "clear":
            display_end = max(floor_end, min(display_end, section_end[sid]))
        if display_end < starts[index]:
            display_end = starts[index]
        result.append(
            DisplayLine(
                line=line,
                lane=lanes[index],
                display_start_ms=starts[index],
                display_end_ms=display_end,
            )
        )
    return result


def _adjust_same_lane_display_windows(
    render_lines: list[TimingLine],
    starts: list[int],
    display_ends: list[int],
    lanes: list[int],
    *,
    lead: int,
    protect: int,
    lane_gap: int,
) -> None:
    """Compress adjacent same-lane display windows while preserving protect floors."""
    previous_by_lane: dict[int, int] = {}
    for index, line in enumerate(render_lines):
        lane = lanes[index]
        previous = previous_by_lane.get(lane)
        if previous is None:
            previous_by_lane[lane] = index
            continue

        if display_ends[previous] + lane_gap <= starts[index]:
            previous_by_lane[lane] = index
            continue

        previous_floor = _line_end_ms(render_lines[previous]) + protect
        current_protect_start = max(_line_start_ms(line) - protect, 0)

        # First give up the previous line's tail, but never below its protect floor.
        display_ends[previous] = max(previous_floor, starts[index] - lane_gap)
        if display_ends[previous] + lane_gap <= starts[index]:
            previous_by_lane[lane] = index
            continue

        # Then shorten the new line's lead-in, stopping at its protect point.
        target_start = display_ends[previous] + lane_gap
        latest_start = max(current_protect_start, starts[index])
        starts[index] = min(max(starts[index], target_start), latest_start)
        if display_ends[previous] + lane_gap <= starts[index]:
            previous_by_lane[lane] = index
            continue

        # If only the gap is missing, accept the shorter gap. When the protected
        # windows themselves overlap, keep both protected windows rather than
        # cutting the previous line's post-singing exit buffer.
        if display_ends[previous] <= starts[index]:
            previous_by_lane[lane] = index
            continue

        if display_ends[previous] <= current_protect_start:
            starts[index] = display_ends[previous]

        previous_by_lane[lane] = index


def _compute_section_ids(render_lines: list[TimingLine], section_gap: int) -> list[int]:
    """按间奏间隔给每行分配段落号（间隔 > section_gap 即开新段；阈值 0 = 单段）。"""
    section_ids: list[int] = []
    current = 0
    for index, line in enumerate(render_lines):
        if index > 0 and section_gap > 0:
            gap = line.chars[0].start_ms - _line_end_ms(render_lines[index - 1])
            if gap > section_gap:
                current += 1
        section_ids.append(current)
    return section_ids


def _compute_section_ends(
    render_lines: list[TimingLine],
    section_ids: list[int],
    tail: int,
) -> dict[int, int]:
    """每段落的统一结束点 = 段内最晚演唱结束 + tail。"""
    ends: dict[int, int] = {}
    for index, line in enumerate(render_lines):
        sid = section_ids[index]
        end = _line_end_ms(line) + tail
        ends[sid] = max(ends.get(sid, end), end)
    return ends


def _is_last_in_lane_in_section(
    lanes: list[int],
    section_ids: list[int],
    index: int,
) -> bool:
    """该行是否是其所在段落、所在 lane 的最后一行。"""
    lane = lanes[index]
    sid = section_ids[index]
    for candidate in range(index + 1, len(lanes)):
        if section_ids[candidate] != sid:
            break  # section_ids 单调不减，离开本段即可停
        if lanes[candidate] == lane:
            return False
    return True


def paragraph_last_line_flags(
    track: TimingTrack,
    *,
    threshold_ms: int,
) -> list[bool]:
    """标记每行是否是"段落"的最后一行（与 ``track.lines`` 等长）。

    段落划分逆向自 NicoKaraMaker3（EmptyLineBreaker + LineBreaker.SetParagraphBreaks）：

    1. **页**：歌词文件的空行是页边界（NKM3 在空行处插 PageBreak）；
    2. **段落**：页内从段落起点扫描，若「后续行的最早演唱开始」与「段落内已扫描行的
       最晚演唱结束」之差 ≥ ``threshold_ms``，则在此处开新段落
       （NKM3 阈值 = PreTime2 + PostTime2 + IntervalTime2，默认 1800+1000+300）；
    3. 每个段落（含单行段落）的最后一行标 True。空行恒为 False。

    本函数只描述段落边界；N3 的 SmartHorizon 单行页居中必须按页内行数另行判断，
    不能把段落最后一行等同于单行页。
    """
    flags = [False] * len(track.lines)
    threshold = max(int(threshold_ms), 0)

    def close_page(page: list[int]) -> None:
        if not page:
            return
        start = 0  # 当前段落在 page 内的起点
        for i in range(1, len(page)):
            prev_end = max(
                _line_end_ms(track.lines[page[j]]) for j in range(start, i)
            )
            next_begin = min(
                _line_start_ms(track.lines[page[j]]) for j in range(i, len(page))
            )
            if next_begin - prev_end >= threshold:
                flags[page[i - 1]] = True
                start = i
        flags[page[-1]] = True

    page: list[int] = []
    for index, line in enumerate(track.lines):
        if line.is_blank or not line.chars:
            close_page(page)
            page = []
        else:
            page.append(index)
    close_page(page)
    return flags


def apply_n3_seq_line_breaks(
    track: TimingTrack,
    *,
    seq: int = 2,
    pre_time_ms: int = 1800,
    post_time_ms: int = 1000,
    interval_time_ms: int = 300,
) -> list[str]:
    """按 N3 ``SeqLinesBreaker.SetBreaks`` 生成分页与段落分隔。

    N3 默认每 2 行分页。处理每个候选新行时，它先看从该行起最多 ``seq`` 行的
    最早演唱开始，与上一页当前已收集行的最晚演唱结束之差：若达到
    ``PreTime2 + PostTime2 + IntervalTime2``，优先插入 ``ParagraphBreak``；
    否则累计行数达到 ``seq`` 时插入 ``PageBreak``。

    返回值与 ``track.lines`` 对齐，值为 ``none/page/paragraph``；同时写回每行
    ``break_before``，供 lane、SmartHorizon、预览与导出统一消费。
    """
    page_size = max(1, min(int(seq), 10))
    threshold = (
        max(int(pre_time_ms), 0)
        + max(int(post_time_ms), 0)
        + max(int(interval_time_ms), 0)
    )
    result = ["none"] * len(track.lines)
    indexed = [
        (index, line)
        for index, line in enumerate(track.lines)
        if not line.is_blank and line.chars
    ]
    for line in track.lines:
        line.break_before = "none"
    if not indexed:
        return result

    count_in_page = 0
    page_start = 0
    render_lines = [line for _, line in indexed]
    for render_index, (track_index, line) in enumerate(indexed):
        kind = "none"
        if render_index == 0:
            count_in_page = 1
        else:
            lookahead_end = min(render_index + page_size, len(render_lines))
            next_begin = min(
                _line_start_ms(item)
                for item in render_lines[render_index:lookahead_end]
            )
            previous_end = max(
                _line_end_ms(item)
                for item in render_lines[page_start:render_index]
            )
            if next_begin - previous_end >= threshold:
                kind = "paragraph"
                count_in_page = 1
                page_start = render_index
            elif count_in_page >= page_size:
                kind = "page"
                count_in_page = 1
                page_start = render_index
            else:
                count_in_page += 1
        line.break_before = kind
        result[track_index] = kind
    return result


def find_upcoming_line(track: TimingTrack, t_ms: int) -> Optional[TimingLine]:
    """返回 ``t_ms`` 之后即将开始的最近一行。"""
    candidate: Optional[TimingLine] = None
    candidate_start: Optional[int] = None
    for line in track.lines:
        if line.is_blank or not line.chars:
            continue
        start = line.chars[0].start_ms
        if start <= t_ms:
            continue
        if candidate_start is None or start < candidate_start:
            candidate = line
            candidate_start = start
    return candidate


def compute_char_intervals(
    line: TimingLine,
    char_widths: Optional[Sequence[int | float]] = None,
) -> list[tuple[int, int]]:
    """返回 ``line`` 中每个字符的 ``(start_ms, end_ms)`` 区间序列。

    长度 == ``len(line.chars)``；末字符 ``end_ms`` 取 ``line.end_ms``，若 None
    则用末字 ``start_ms + 500`` 兜底。

    若传入与字符数等长的 ``char_widths``，解析器标记的
    ``[start]多字[next]`` 共享时间块会按正布局宽度加权切分。算法与 SUG
    ``KaraokePreview`` 一致：边界使用 ``int(start + duration * 累计宽度 / 总宽度)``。
    元数据不完整、区间无效或总宽度为 0 时保留兼容的 ``start_ms`` 区间。
    """
    chars = line.chars
    n = len(chars)
    if n == 0:
        return []
    result: list[tuple[int, int]] = []
    for i, ch in enumerate(chars):
        if i + 1 < n:
            next_start = chars[i + 1].start_ms
            if ch.pause_release_ms is not None:
                end = min(max(ch.pause_release_ms, ch.start_ms), next_start)
            else:
                end = next_start
        elif line.end_ms is not None:
            if ch.pause_release_ms is not None:
                end = min(max(ch.pause_release_ms, ch.start_ms), line.end_ms)
            else:
                end = line.end_ms
        else:
            end = ch.start_ms + 500
        # 容错：end 不应早于 start
        if end < ch.start_ms:
            end = ch.start_ms
        result.append((ch.start_ms, end))
    if char_widths is None or len(char_widths) != n:
        return result

    index = 0
    while index < n:
        first = chars[index]
        count = int(first.source_span_count)
        span_start = first.source_span_start_ms
        span_end = first.source_span_end_ms
        if (
            count <= 1
            or first.source_span_index != 0
            or span_start is None
            or span_end is None
            or span_end <= span_start
            or index + count > n
        ):
            index += 1
            continue

        group = chars[index : index + count]
        valid_group = all(
            ch.source_span_start_ms == span_start
            and ch.source_span_end_ms == span_end
            and ch.source_span_index == offset
            and ch.source_span_count == count
            for offset, ch in enumerate(group)
        )
        if not valid_group:
            index += 1
            continue

        weights = [max(float(char_widths[index + offset]), 0.0) for offset in range(count)]
        total_width = sum(weights)
        if total_width <= 0.0:
            index += count
            continue

        duration = span_end - span_start
        cumulative = 0.0
        for offset, width in enumerate(weights):
            char_start = int(span_start + duration * cumulative / total_width)
            cumulative += width
            char_end = (
                span_end
                if offset == count - 1
                else int(span_start + duration * cumulative / total_width)
            )
            result[index + offset] = (char_start, max(char_start, char_end))
        index += count

    return result


def char_fill_ratio(char_start_ms: int, char_end_ms: int, t_ms: int) -> float:
    """计算字符的演唱进度比例（0.0 完全未唱、1.0 完全已唱）。"""
    if t_ms <= char_start_ms:
        return 0.0
    if t_ms >= char_end_ms:
        return 1.0
    duration = max(char_end_ms - char_start_ms, 1)
    return (t_ms - char_start_ms) / duration


def track_duration_ms(track: TimingTrack) -> int:
    """估算字幕轨整体时长（毫秒），用于时间轴 / 滑块上限。"""
    best = 0
    for line in track.lines:
        if line.is_blank or not line.chars:
            continue
        end = _line_end_ms(line)
        if end > best:
            best = end
    return best


def _line_end_ms(line: TimingLine) -> int:
    if line.end_ms is not None:
        return line.end_ms
    if line.chars:
        return line.chars[-1].start_ms + 1000
    return 0


def _line_start_ms(line: TimingLine) -> int:
    if line.chars:
        return line.chars[0].start_ms
    return 0


def _pair_sing_end_ms(lines: list[TimingLine], index: int, lane_count: int = 2) -> int:
    """同页（连续 ``lane_count`` 行为一页）所有行的最晚演唱结束。"""
    lanes = max(int(lane_count), 1)
    page_start = (index // lanes) * lanes
    page = lines[page_start : page_start + lanes]
    return max(_line_end_ms(line) for line in page)
