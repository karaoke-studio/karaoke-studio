"""时间 → 活跃行 / 字符级演唱区间查询。

字符级时间区间约定（与 :class:`paint_frame` 共用语义）：

- 每个字符的 ``start_ms`` 是 ``[ts]<char>`` 中的前导时间戳；如果同一个
  ``[ts]`` 后面跟多个字符（如 ``[00:38:05]どう[00:38:32]``），解析器会把
  这段文本均分到下一个时间戳前
- 字符 i 的 ``end_ms`` = 字符 i+1 的 ``start_ms``（行内）；行末字符 = ``line.end_ms``
- 如果某字符设了 ``pause_release_ms``（行内呼吸），它的 ``end_ms`` 仍按下一字
  起始；释放点会让填充提前到位，避免"呼吸期还在涨色"

无活跃行的 ``find_active_line`` 返回 ``None``。无字符 / 空行不参与查找。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from krok_helper.subtitle_render.models import TimingChar, TimingLine, TimingTrack


@dataclass(frozen=True)
class DisplayLine:
    """A line with its computed display window and two-line lane."""

    line: TimingLine
    lane: int
    display_start_ms: int
    display_end_ms: int


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
) -> list[DisplayLine]:
    """Return lines whose display window contains ``t_ms``.

    The display window intentionally differs from singing time:

    - Lines alternate between lane 0 (upper) and lane 1 (lower).
    - Preferred display start is ``sing_start - lead_in_ms``.
    - When the same lane becomes available shortly before the preferred start,
      the next line snaps earlier to keep the lane visually continuous.
    - Display end is the paired two-line singing end plus ``tail_ms``, capped by
      the next same-lane display start minus ``lane_gap_ms``.
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
) -> list[DisplayLine]:
    """Compute NicoKara-style display windows for all renderable lines.

    段落（section）按间奏间隔自动划分：相邻两句演唱空隙 > ``section_gap_ms`` 即开
    新段落。``sync_ending`` 时同段落内每个 lane 的末行延到段末一起退场；
    ``section_ending_mode == "clear"`` 时把每行结束钳到段末（不拖进间奏）。
    两项默认关闭时输出与原行为一致。
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

    starts: list[int] = []
    natural_ends: list[int] = []
    lanes: list[int] = []
    prev_lane_natural_end: dict[int, int] = {}

    for index, line in enumerate(render_lines):
        lane = index % 2
        lanes.append(lane)
        preferred_start = max(line.chars[0].start_ms - lead, 0)
        if (
            index % 2 == 1
            and starts
            and section_ids[index] == section_ids[index - 1]
        ):
            preferred_start = min(
                preferred_start,
                starts[index - 1] + pair_second_delay,
            )
        pair_end = _pair_sing_end_ms(render_lines, index)
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


def compute_char_intervals(line: TimingLine) -> list[tuple[int, int]]:
    """返回 ``line`` 中每个字符的 ``(start_ms, end_ms)`` 区间序列。

    长度 == ``len(line.chars)``；末字符 ``end_ms`` 取 ``line.end_ms``，若 None
    则用末字 ``start_ms + 500`` 兜底。
    """
    chars = line.chars
    n = len(chars)
    if n == 0:
        return []
    result: list[tuple[int, int]] = []
    for i, ch in enumerate(chars):
        if i + 1 < n:
            end = chars[i + 1].start_ms
        elif line.end_ms is not None:
            end = line.end_ms
        else:
            end = ch.start_ms + 500
        # 容错：end 不应早于 start
        if end < ch.start_ms:
            end = ch.start_ms
        result.append((ch.start_ms, end))
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


def _pair_sing_end_ms(lines: list[TimingLine], index: int) -> int:
    pair_start = (index // 2) * 2
    pair = lines[pair_start : pair_start + 2]
    return max(_line_end_ms(line) for line in pair)
