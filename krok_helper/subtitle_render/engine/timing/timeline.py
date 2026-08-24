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

from krok_helper.subtitle_render.engine.timing.show_time import (
    ShowTimePage,
    compute_show_times,
    protect_time_ms,
)
from krok_helper.subtitle_render.timing import (
    TimingChar,
    TimingLine,
    TimingTrack,
    timing_line_start_ms,
)


@dataclass(frozen=True)
class DisplayLine:
    """A line with its computed display window and two-line lane."""

    line: TimingLine
    lane: int
    display_start_ms: int
    display_end_ms: int
    section_index: int = 0
    page_index: int = 0
    page_line_count: int = 1


def assign_lanes(
    render_lines: list[TimingLine],
    default_rows: int,
    row_count_of: Optional[Callable[[TimingLine], int]] = None,
    *,
    section_gap_ms: int = 0,
) -> tuple[list[int], list[int], list[int]]:
    """按页分配 lane（N3 页级布局联动）。

    页从页首行的布局行数决定：最多连续 ``rows`` 条可渲染行组成一页；若后续行
    带 ``break_before``（N3 PageBreak / ParagraphBreak），或演唱间隔超过
    ``section_gap_ms``，则提前结束当前页。页内行依次占 lane ``0..rows-1``。
    返回与 ``render_lines`` 对齐的 ``(lanes, page_starts, page_rows)``。
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
    section_ids = _compute_section_ids(
        render_lines, max(int(section_gap_ms), 0)
    )
    while index < total:
        rows = max(int(default_rows), 1)
        if row_count_of is not None:
            rows = max(int(row_count_of(render_lines[index])), 1)
        page_end = total if has_explicit_breaks else min(index + rows, total)
        for candidate in range(index + 1, page_end):
            if (
                getattr(render_lines[candidate], "break_before", "none") != "none"
                or section_ids[candidate] != section_ids[index]
            ):
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

    判定区间 = ``[行内首个可唱元素 - lead_in_ms, line_end_ms]``，闭区间；存在
    导唱符时，它的开始时刻优先于首个歌词字符。
    ``line_end_ms`` 取 ``line.end_ms`` 或末字符 ``start_ms`` + 1000 ms 作为安全兜底。
    ``lead_in_ms`` 只影响显示时机，不改变字符填充时间。
    """
    best_live: Optional[TimingLine] = None
    for line in track.lines:
        if line.is_blank or not line.chars:
            continue
        start = timing_line_start_ms(line)
        end = _line_end_ms(line)
        if start <= t_ms <= end:
            # 多行重叠时取最靠后开始的（合唱叠唱场景，更贴近"刚发声"那条）
            if best_live is None or start >= timing_line_start_ms(best_live):
                best_live = line
    if best_live is not None:
        return best_live

    best: Optional[TimingLine] = None
    lead = max(lead_in_ms, 0)
    for line in track.lines:
        if line.is_blank or not line.chars:
            continue
        start = timing_line_start_ms(line) - lead
        end = _line_end_ms(line)
        if start <= t_ms <= end:
            # 多行重叠时取最靠后开始的（合唱叠唱场景，更贴近"刚发声"那条）
            if best is None or timing_line_start_ms(line) >= timing_line_start_ms(best):
                best = line
    return best


def visible_display_lines(
    track: TimingTrack,
    t_ms: int,
    *,
    lead_in_ms: int,
    tail_ms: int,
    lane_gap_ms: int,
    section_gap_ms: int = 0,
    sync_entry: bool = False,
    sync_ending: bool = False,
    sync_each_page: bool = False,
    auto_fill_section_time: bool = True,
    section_ending_mode: str = "hold",
    protect_ms: int = 0,
    lane_count: int = 2,
    row_count_of: Optional[Callable[[TimingLine], int]] = None,
    bottom_align_of: Optional[Callable[[TimingLine], bool]] = None,
    vertical_position_of: Optional[Callable[[TimingLine], str]] = None,
    auto_entry_reserve_ms_of: Optional[Callable[[TimingLine], int]] = None,
    auto_exit_reserve_ms_of: Optional[Callable[[TimingLine], int]] = None,
    entry_animation_ms_of: Optional[Callable[[TimingLine], int]] = None,
    exit_animation_ms_of: Optional[Callable[[TimingLine], int]] = None,
    adjust_same_position: bool = True,
) -> list[DisplayLine]:
    """Return lines whose display window contains ``t_ms``.

    See :func:`compute_display_lines` for the window semantics (N3
    ``TopLongAdjuster``).
    """
    layouts = compute_display_lines(
        track,
        lead_in_ms=lead_in_ms,
        tail_ms=tail_ms,
        protect_ms=protect_ms,
        lane_gap_ms=lane_gap_ms,
        section_gap_ms=section_gap_ms,
        sync_entry=sync_entry,
        sync_ending=sync_ending,
        sync_each_page=sync_each_page,
        auto_fill_section_time=auto_fill_section_time,
        section_ending_mode=section_ending_mode,
        lane_count=lane_count,
        row_count_of=row_count_of,
        bottom_align_of=bottom_align_of,
        vertical_position_of=vertical_position_of,
        auto_entry_reserve_ms_of=auto_entry_reserve_ms_of,
        auto_exit_reserve_ms_of=auto_exit_reserve_ms_of,
        entry_animation_ms_of=entry_animation_ms_of,
        exit_animation_ms_of=exit_animation_ms_of,
        adjust_same_position=adjust_same_position,
    )
    return [
        item
        for item in layouts
        if item.display_start_ms <= t_ms < item.display_end_ms
    ]


def compute_display_lines(
    track: TimingTrack,
    *,
    lead_in_ms: int,
    tail_ms: int,
    lane_gap_ms: int,
    section_gap_ms: int = 0,
    signal_head_indexes: Optional[set[int]] = None,
    signal_lead_ms: int = 0,
    sync_entry: bool = False,
    sync_ending: bool = False,
    sync_each_page: bool = False,
    auto_fill_section_time: bool = True,
    section_ending_mode: str = "hold",
    protect_ms: int = 0,
    lane_count: int = 2,
    row_count_of: Optional[Callable[[TimingLine], int]] = None,
    bottom_align_of: Optional[Callable[[TimingLine], bool]] = None,
    vertical_position_of: Optional[Callable[[TimingLine], str]] = None,
    auto_entry_reserve_ms_of: Optional[Callable[[TimingLine], int]] = None,
    auto_exit_reserve_ms_of: Optional[Callable[[TimingLine], int]] = None,
    entry_animation_ms_of: Optional[Callable[[TimingLine], int]] = None,
    exit_animation_ms_of: Optional[Callable[[TimingLine], int]] = None,
    adjust_same_position: bool = True,
    squeeze_pairs: Optional[Sequence[tuple[int, int]]] = None,
    force_bottom_pairs: Optional[Sequence[tuple[int, int]]] = None,
    dynamic_single_page_reflow: bool = True,
    independent_line_entry: bool = False,
) -> list[DisplayLine]:
    """Compute NicoKara display windows for all renderable lines.

    窗口由 :mod:`krok_helper.subtitle_render.engine.timing.show_time` 里的 N3
    ``TopLongAdjuster`` 移植算出：``lead_in_ms`` = ``PreTime``、``tail_ms`` =
    ``PostTime``、``lane_gap_ms`` = ``IntervalTime``、``protect_ms`` =
    ``ProtectTime``。页由页计划（或按 ``lane_count`` / ``row_count_of`` 自动
    分页）决定，段（section）等价于 N3 的 ParagraphBreak 分组：页只与同段内相邻
    的页产生联动。

    ``protect_ms`` 传 0 表示按 N3 规则自动推导（``min(PreTime, PostTime) / 2``）。

    ``signal_head_indexes``（``track.lines`` 索引）+ ``signal_lead_ms`` 描述只
    提前部分行的信号窗口（音量柱只挂每段第一行）：命中的行 lead 取
    ``max(lead_in_ms, signal_lead_ms)``，其余行保持 ``lead_in_ms``。省略时全部
    行使用同一 lead。``protect_ms`` 仍按未扩展的 ``lead_in_ms`` 推导，信号扩展
    不撑大碰撞保护。

    ``sync_entry`` / ``sync_ending`` 是同步页内各个自动 T 的最长单向延长候选；
    默认只作用于段首页入场和段尾页退场，``sync_each_page`` 开启后作用于每页；
    带实际画布的渲染路径随后按「先压前句退场、再压后句入场」逐对消除碰撞，
    不会把一行的压缩结果传播给未参与碰撞的页内兄弟行。
    ``section_ending_mode`` 仍是段落级清屏选项；逐行手动覆盖（字幕轨道拖动）
    优先于全部自动结果。
    """
    render_lines = [line for line in track.lines if not line.is_blank and line.chars]
    if not render_lines:
        return []

    tail = max(tail_ms, 0)
    section_gap = max(section_gap_ms, 0)
    base_pre = max(lead_in_ms, 0)
    if signal_head_indexes:
        extended_pre = max(base_pre, max(int(signal_lead_ms), 0))
        pre_per_line = [
            extended_pre if track_index in signal_head_indexes else base_pre
            for track_index, _line in enumerate(track.lines)
            if not _line.is_blank and _line.chars
        ]
        pre_argument: int | list[int] = pre_per_line
    else:
        pre_argument = base_pre
    explicit_structure = _assign_lanes_from_page_plan(track, render_lines)
    if explicit_structure is None:
        section_ids = _compute_section_ids(render_lines, section_gap)
        lanes, page_starts, page_rows = assign_lanes(
            render_lines,
            max(int(lane_count), 1),
            row_count_of,
            section_gap_ms=section_gap,
        )
        page_ids = _page_ids_from_starts(page_starts)
    else:
        lanes, page_starts, page_rows, section_ids, page_ids = explicit_structure
    section_end = _compute_section_ends(render_lines, section_ids, tail)

    pages = _show_time_pages(
        render_lines,
        page_rows,
        section_ids,
        max(int(lane_count), 1),
        row_count_of,
        bottom_align_of,
        vertical_position_of,
    )
    show_times = compute_show_times(
        [timing_line_start_ms(line) for line in render_lines],
        [_line_end_ms(line) for line in render_lines],
        pages,
        pre_time_ms=pre_argument,
        post_time_ms=tail,
        interval_ms=max(lane_gap_ms, 0),
        # ``protect_ms`` 0 = 按 N3 规则自动推导（min(pre, post) / 2）。
        protect_ms=protect_time_ms(lead_in_ms, tail, protect_ms),
        # 手动时刻要参与本趟计算：N3 的 ForceBottom / 下行入场都读模型里的
        # ShowBegin / ShowEnd，用户改过就该被下游页看见。
        overrides=[_effective_override(line) for line in render_lines],
        auto_entry_reserve_ms=[
            max(int(auto_entry_reserve_ms_of(line)), 0)
            if auto_entry_reserve_ms_of is not None
            else 0
            for line in render_lines
        ],
        auto_exit_reserve_ms=[
            max(int(auto_exit_reserve_ms_of(line)), 0)
            if auto_exit_reserve_ms_of is not None
            else 0
            for line in render_lines
        ],
        entry_animation_ms=(
            [
                max(int(entry_animation_ms_of(line)), 0)
                for line in render_lines
            ]
            if entry_animation_ms_of is not None
            else None
        ),
        exit_animation_ms=(
            [
                max(int(exit_animation_ms_of(line)), 0)
                for line in render_lines
            ]
            if exit_animation_ms_of is not None
            else None
        ),
        adjust_same_position=adjust_same_position,
        squeeze_pairs=squeeze_pairs,
        force_bottom_pairs=force_bottom_pairs,
        dynamic_single_page_reflow=dynamic_single_page_reflow,
        independent_line_entry=independent_line_entry,
        auto_fill_section_time=auto_fill_section_time,
    )
    starts = show_times.starts
    display_ends = show_times.ends
    _synchronize_page_boundaries(
        starts,
        display_ends,
        pages,
        render_lines,
        sync_entry=sync_entry,
        sync_ending=sync_ending,
        sync_each_page=sync_each_page,
    )

    _apply_page_lane_offsets(pages, lanes, show_times.force_bottom)

    result: list[DisplayLine] = []
    for index, line in enumerate(render_lines):
        display_end = display_ends[index]
        # 段末清屏仍是 section 级；同步入退场已在 show-time 页级求解中完成。
        sid = section_ids[index]
        if section_ending_mode == "clear":
            display_end = min(display_end, section_end[sid])
        # 逐行手动覆盖（字幕轨道拖动写入）优先于所有自动布局调整
        display_start, display_end = apply_display_overrides(
            line, starts[index], display_end
        )
        if display_end < display_start:
            display_end = display_start
        result.append(
            DisplayLine(
                line=line,
                lane=lanes[index],
                display_start_ms=display_start,
                display_end_ms=display_end,
                section_index=section_ids[index],
                page_index=page_ids[index],
                page_line_count=page_rows[index],
            )
        )
    return result


def _show_time_pages(
    render_lines: list[TimingLine],
    page_rows: list[int],
    section_ids: list[int],
    default_rows: int,
    row_count_of: Optional[Callable[[TimingLine], int]],
    bottom_align_of: Optional[Callable[[TimingLine], bool]],
    vertical_position_of: Optional[Callable[[TimingLine], str]],
) -> list[ShowTimePage]:
    """把 lane 结构折叠成 :class:`ShowTimePage` 序列（渲染行索引空间）。

    页的垂直配置取页首行的布局；两个解析回调都缺席时（竖排 / 纯时间学单测）
    按顶部对齐处理，此时 N3 的下行 / ForceBottom 分支不参与。
    """

    pages: list[ShowTimePage] = []
    total = len(render_lines)
    index = 0
    while index < total:
        page_size = max(int(page_rows[index]), 1)
        page_end = min(index + page_size, total)
        first = render_lines[index]
        configured_rows = max(
            int(row_count_of(first)) if row_count_of is not None else int(default_rows),
            1,
        )
        if vertical_position_of is not None:
            position = str(vertical_position_of(first))
        elif bottom_align_of is not None:
            position = "bottom" if bottom_align_of(first) else "top"
        else:
            position = "top"
        pages.append(
            ShowTimePage(
                lines=tuple(range(index, page_end)),
                section=section_ids[index],
                configured_rows=configured_rows,
                vertical_position=position,
            )
        )
        index = page_end
    return pages


def _apply_page_lane_offsets(
    pages: Sequence[ShowTimePage],
    lanes: list[int],
    force_bottom: Sequence[bool],
) -> None:
    """把不满页映射到布局的底部 / 居中位置，并复现 N3 ForceBottom 的上移。

    单行底部页正常占最下行；若上一页与它按稳定时间和最终二维行盒确认冲突
    （``force_bottom`` 为 False），N3 把它上移一行，后面再重叠的单行页又能用回
    最下行。
    """

    for page in pages:
        page_size = len(page.lines)
        if page_size <= 0 or page_size >= max(int(page.configured_rows), 1):
            continue
        configured_rows = max(int(page.configured_rows), 1)
        if page.vertical_position == "bottom":
            shift = configured_rows - page_size
        elif page.vertical_position == "center":
            shift = max((configured_rows - page_size + 1) // 2, 0)
        else:
            continue
        for line in page.lines:
            lanes[line] += shift
        if (
            page.vertical_position == "bottom"
            and page_size == 1
            and configured_rows > 1
            and not force_bottom[page.lines[0]]
        ):
            first = page.lines[0]
            lanes[first] = max(lanes[first] - 1, 0)


def _page_ids_from_starts(page_starts: list[int]) -> list[int]:
    page_ids: list[int] = []
    previous: Optional[int] = None
    page_id = -1
    for start in page_starts:
        if previous is None or start != previous:
            page_id += 1
            previous = start
        page_ids.append(page_id)
    return page_ids


def _assign_lanes_from_page_plan(
    track: TimingTrack,
    render_lines: list[TimingLine],
) -> Optional[tuple[list[int], list[int], list[int], list[int], list[int]]]:
    """Resolve the authoritative page counts without re-running legacy breaks."""

    plan = getattr(track, "page_plan", None)
    if plan is None:
        return None
    total = len(render_lines)
    if total == 0:
        return ([], [], [], [], [])
    lanes: list[int] = []
    page_starts: list[int] = []
    page_rows: list[int] = []
    section_ids: list[int] = []
    page_ids: list[int] = []
    cursor = 0
    global_page = 0
    for section_index, section in enumerate(plan.sections):
        for page in section.pages:
            if cursor >= total:
                break
            count = max(0, min(int(page.line_count), 8, total - cursor))
            if count <= 0:
                continue
            for offset in range(count):
                lanes.append(offset)
                page_starts.append(cursor)
                page_rows.append(count)
                section_ids.append(section_index)
                page_ids.append(global_page)
            cursor += count
            global_page += 1
        if cursor >= total:
            break
    # Corrupt/truncated plans must not drop lyrics.  Keep the recovery local to
    # this read path; project normalization will persist the repaired shape.
    while cursor < total:
        count = min(max(1, total - cursor), 8)
        section_index = section_ids[-1] if section_ids else 0
        for offset in range(count):
            lanes.append(offset)
            page_starts.append(cursor)
            page_rows.append(count)
            section_ids.append(section_index)
            page_ids.append(global_page)
        cursor += count
        global_page += 1
    return lanes, page_starts, page_rows, section_ids, page_ids


def apply_display_overrides(
    line: TimingLine, display_start: int, display_end: int
) -> tuple[int, int]:
    """应用逐行「上屏 / 消失时刻」手动覆盖。

    上屏覆盖不晚于开始走字（首字符起点），消失覆盖不早于走字结束——
    两个虚线把手只编辑演唱区间外侧的余量，不会吃进演唱本体。
    """
    start_override = getattr(line, "display_start_override_ms", None)
    if start_override is not None:
        display_start = max(0, min(int(start_override), _line_start_ms(line)))
    end_override = getattr(line, "display_end_override_ms", None)
    if end_override is not None:
        display_end = max(int(end_override), _line_end_ms(line))
    return display_start, display_end


def _effective_override(line: TimingLine) -> tuple[Optional[int], Optional[int]]:
    """该行手动覆盖后的 ``(上屏, 消失)``；未覆盖的一侧为 ``None``。

    钳制规则必须与 :func:`apply_display_overrides` 完全一致——N3 pass 用这个值
    参与计算，最终结果又由 ``apply_display_overrides`` 再盖一次。
    """
    start_override = getattr(line, "display_start_override_ms", None)
    end_override = getattr(line, "display_end_override_ms", None)
    return (
        None
        if start_override is None
        else max(0, min(int(start_override), _line_start_ms(line))),
        None if end_override is None else max(int(end_override), _line_end_ms(line)),
    )


def _compute_section_ids(render_lines: list[TimingLine], section_gap: int) -> list[int]:
    """按间奏间隔给每行分配段落号（间隔 > section_gap 即开新段；阈值 0 = 单段）。"""
    section_ids: list[int] = []
    current = 0
    for index, line in enumerate(render_lines):
        if index > 0 and section_gap > 0:
            gap = timing_line_start_ms(line) - _line_end_ms(render_lines[index - 1])
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


def _synchronize_page_boundaries(
    starts: list[int],
    ends: list[int],
    pages: Sequence[ShowTimePage],
    render_lines: Sequence[TimingLine],
    *,
    sync_entry: bool,
    sync_ending: bool,
    sync_each_page: bool,
) -> None:
    """Apply raw page-level sync candidates without crossing manual overrides.

    Pixel-aware callers intentionally compute their ordinary schedule with
    both flags disabled, then constrain these candidates against other pages'
    already resolved bands.
    """

    first_page_by_section: dict[int, ShowTimePage] = {}
    last_page_by_section: dict[int, ShowTimePage] = {}
    for page in pages:
        first_page_by_section.setdefault(page.section, page)
        last_page_by_section[page.section] = page

    for page in pages:
        if not page.lines:
            continue
        if sync_entry and (
            sync_each_page
            or first_page_by_section.get(page.section) is page
        ):
            common_start = min(starts[line] for line in page.lines)
            for line in page.lines:
                if render_lines[line].display_start_override_ms is None:
                    starts[line] = common_start
        if sync_ending and (
            sync_each_page
            or last_page_by_section.get(page.section) is page
        ):
            common_end = max(ends[line] for line in page.lines)
            for line in page.lines:
                if render_lines[line].display_end_override_ms is None:
                    ends[line] = common_end


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
        start = timing_line_start_ms(line)
        if start <= t_ms:
            continue
        if candidate_start is None or start < candidate_start:
            candidate = line
            candidate_start = start
    return candidate


def _char_has_no_wipe_ink(ch: TimingChar) -> bool:
    """零墨水字符判定（空格 / 全角空格 / NBSP 等无渲染字符）。

    与 Painter 的 ``_char_ink_x_ranges`` / ``_role_char_ink_ranges_by_index``
    同口径：空白字符的墨水边界是零宽 ``(left, left)``，走字扫光在其上没有可见
    推进；``\\uFFFC`` 虚拟图片字符有真实墨水（且非空白），不在此列。
    """
    return ch.vector_glyph is None and (not ch.text or ch.text.isspace())


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
    空格等零墨水字符权重为 0（零时长窗口、瞬时跨过），时长由可见字符瓜分——
    否则空格凭排版宽度分走一段真实走字时间，绘制层却因无墨水跳过它，表现为
    走字停顿、后续字符窗口被压缩；整段全空白时回退布局宽度加权。
    元数据不完整、区间无效或总宽度为 0 时保留兼容的 ``start_ms`` 区间。

    **区间可以重叠。** 源里显式写了释放点（``pause_release_ms``）时就以它为准，
    哪怕它晚于下一个字的起点 —— 一行里同时有两处在走字是合法的打轴，SUG 的
    预览一直是这么放的（它逐字独立算比例，没有"夹到下一字"这一步）。这里原先
    会 ``min(..., next_start)`` 把它压掉，表现是前一段瞬间跳满：比如
    ``で``（20880 → 释放 21230）被压成 40ms，后面的和声段接着开始。

    没有 ``pause_release_ms`` 时仍然接到下一字起点，因此**只有源数据真的重叠**
    才会与从前不同；LRC 来源在结构上不可能重叠（``[t1]字[t2]`` 里释放点必早于
    下一字起点），所以那条路径一帧都不会变。
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
                end = max(ch.pause_release_ms, ch.start_ms)
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

        # 零墨水字符（空格 / 全角空格 / NBSP 等无渲染字符）权重为 0：得到零时长
        # 窗口、瞬时跨过，整段时长由可见字符按宽度瓜分。整段全空白时回退布局
        # 宽度加权，保证各字符仍有有限时间窗口。
        weights = [
            0.0
            if _char_has_no_wipe_ink(group[offset])
            else max(float(char_widths[index + offset]), 0.0)
            for offset in range(count)
        ]
        total_width = sum(weights)
        if total_width <= 0.0:
            weights = [
                max(float(char_widths[index + offset]), 0.0) for offset in range(count)
            ]
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
    return timing_line_start_ms(line)
