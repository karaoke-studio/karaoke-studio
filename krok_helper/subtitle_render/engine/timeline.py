"""时间 → 活跃行 / 字符级演唱区间查询。

字符级时间区间约定（与 :class:`paint_frame` 共用语义）：

- 每个字符的 ``start_ms`` 是 ``[ts]<char>`` 中的前导时间戳
- 字符 i 的 ``end_ms`` = 字符 i+1 的 ``start_ms``（行内）；行末字符 = ``line.end_ms``
- 如果某字符设了 ``pause_release_ms``（行内呼吸），它的 ``end_ms`` 仍按下一字
  起始；释放点会让填充提前到位，避免"呼吸期还在涨色"

无活跃行的 ``find_active_line`` 返回 ``None``。无字符 / 空行不参与查找。
"""

from __future__ import annotations

from typing import Optional

from krok_helper.subtitle_render.models import TimingChar, TimingLine, TimingTrack


def find_active_line(track: TimingTrack, t_ms: int) -> Optional[TimingLine]:
    """返回 ``t_ms`` 时刻正在演唱的行；无则返回 ``None``。

    判定区间 = ``[line.chars[0].start_ms, line_end_ms]``，闭区间。``line_end_ms``
    取 ``line.end_ms`` 或末字符 ``start_ms`` + 1000 ms 作为安全兜底。
    """
    best: Optional[TimingLine] = None
    for line in track.lines:
        if line.is_blank or not line.chars:
            continue
        start = line.chars[0].start_ms
        end = _line_end_ms(line)
        if start <= t_ms <= end:
            # 多行重叠时取最靠后开始的（合唱叠唱场景，更贴近"刚发声"那条）
            if best is None or start >= best.chars[0].start_ms:
                best = line
    return best


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
