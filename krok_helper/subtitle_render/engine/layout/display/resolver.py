"""Display-line resolution orchestration independent from concrete rendering."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, replace

from krok_helper.subtitle_render.engine.layout.display.diagnostics import (
    TimingCollisionAdjustment,
)
from krok_helper.subtitle_render.engine.layout.display.schedule import (
    SyncCollisionBands,
    apply_constrained_page_sync,
    collision_time_window_name,
    display_line_collision_time_window,
    display_line_static_collision_window,
)
from krok_helper.subtitle_render.engine.layout.line.style import (
    auto_entry_reserve_resolver,
    auto_exit_reserve_resolver,
    bottom_align_resolver,
    entry_animation_resolver,
    exit_animation_ms,
    exit_animation_resolver,
    force_top_bottom_resolver,
    lane_count,
    line_end_ms,
    line_start_ms,
    row_count_resolver,
    style_for_line,
    style_for_line_display_window,
    vertical_position_resolver,
)
from krok_helper.subtitle_render.engine.layout.page.placement import (
    LineVisualBand,
    PageVisualBands,
    bands_require_separation,
    bands_share_layout_axis,
    solve_page_axis_offsets,
    time_windows_overlap,
)
from krok_helper.subtitle_render.engine.layout.display.section_edges import (
    section_edge_context,
)
from krok_helper.subtitle_render.engine.layout.display.signal import (
    signal_head_context,
    signal_lead_in_ms,
)
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.engine.timing.show_time import (
    compression_floor_ms,
    protect_time_ms,
)
from krok_helper.subtitle_render.engine.render_progress import (
    clear_display_phase_head,
    report_render_progress,
    set_display_phase_head,
)
from krok_helper.subtitle_render.engine.value_signature import (
    lyric_layout_style_signature,
    value_signature,
)
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingLine, TimingTrack


DisplayLines = list[DisplayLine]
CollisionPairs = tuple[tuple[int, int], ...]
MeasuredCollisionBand = tuple[int, Hashable, LineVisualBand, float]
MeasuredCollisionBands = list[MeasuredCollisionBand]

_DISPLAY_RESOLUTION_PHASES = 7
"""``resolve_display_lines`` 的多趟步骤数，用作 display 阶段的进度刻度。"""


@dataclass(frozen=True)
class CollisionLineGeometry:
    """Renderer-supplied static ink geometry for one display line."""

    axis_min: float
    axis_max: float
    cross_min: float
    cross_max: float
    axis_anchor: float | None
    gap_px: float


def build_measured_collision_bands(
    display_lines: DisplayLines,
    style: Style,
    geometries: Sequence[CollisionLineGeometry | None],
    *,
    time_window: str | None = None,
) -> MeasuredCollisionBands:
    """Attach layout-owned timing and page identity to renderer geometry."""

    if time_window is None:
        time_window = (
            "stable" if style.allow_entry_exit_animation_overlap else "display"
        )
    measured: MeasuredCollisionBands = []
    for render_index, display_line in enumerate(display_lines):
        geometry = geometries[render_index] if render_index < len(geometries) else None
        if geometry is None:
            continue
        collision_start, collision_end = display_line_collision_time_window(
            display_line,
            style,
            time_window=time_window,
        )
        if collision_end <= collision_start:
            continue
        page_id = (
            int(display_line.section_index),
            int(display_line.page_index),
        )
        measured.append(
            (
                render_index,
                page_id,
                LineVisualBand(
                    line_id=render_index,
                    page_id=page_id,
                    display_start_ms=collision_start,
                    display_end_ms=collision_end,
                    axis_min=float(geometry.axis_min),
                    axis_max=float(geometry.axis_max),
                    entry_start_ms=int(display_line.display_start_ms),
                    axis_anchor=(
                        None
                        if geometry.axis_anchor is None
                        else float(geometry.axis_anchor)
                    ),
                    cross_min=float(geometry.cross_min),
                    cross_max=float(geometry.cross_max),
                ),
                max(float(geometry.gap_px), 0.0),
            )
        )
    return measured


def collision_squeeze_pairs(
    measured: MeasuredCollisionBands,
) -> CollisionPairs:
    """Return authored-position conflicts across distinct lyric pages."""

    conflicts: list[tuple[int, int]] = []
    for incoming_pos in range(len(measured)):
        (
            incoming_index,
            incoming_page,
            incoming_band,
            _incoming_gap,
        ) = measured[incoming_pos]
        for previous_pos in range(incoming_pos):
            (
                previous_index,
                previous_page,
                previous_band,
                _previous_gap,
            ) = measured[previous_pos]
            if previous_page == incoming_page:
                continue
            if not time_windows_overlap(incoming_band, previous_band):
                continue
            if not bands_require_separation(incoming_band, previous_band, 0.0):
                continue
            pair = (previous_index, incoming_index)
            if pair not in conflicts:
                conflicts.append(pair)
    return tuple(conflicts)


def secondary_displacement_squeeze_pairs(
    measured: MeasuredCollisionBands,
    display_lines: DisplayLines,
    style: Style,
    *,
    viewport_max: float,
) -> CollisionPairs:
    """Return cascade conflicts introduced by rigid page displacement."""

    if not measured:
        return ()
    if style.overlap_fallback_mode == "displace":
        # 「吃掉走字时长」永不抬升页面：没有刚性位移就没有位移引发的
        # 级联冲突，这一发现趟直接短路。
        return ()

    page_order: list[Hashable] = []
    page_entries: dict[Hashable, list[tuple[int, LineVisualBand, float]]] = {}
    page_styles: dict[Hashable, Style] = {}
    for render_index, page_id, band, gap in measured:
        if page_id not in page_entries:
            page_order.append(page_id)
            page_entries[page_id] = []
        page_entries[page_id].append((render_index, band, gap))
        page_styles.setdefault(
            page_id,
            style_for_line(style, display_lines[render_index].line),
        )

    pages: list[PageVisualBands] = []
    for page_id in page_order:
        page_style = page_styles[page_id]
        position = page_style.line_y_position
        anchor = (
            "start"
            if position == "top"
            else "center"
            if position == "center"
            else "end"
        )
        if style.vertical:
            anchor = "end"
        pages.append(
            PageVisualBands(
                page_id=page_id,
                bands=tuple(
                    band for _render_index, band, _gap in page_entries[page_id]
                ),
                gap_px=max(float(page_style.line_gap_px), 0.0),
                anchor=anchor,
            )
        )

    offsets = solve_page_axis_offsets(
        pages,
        viewport_min=0.0,
        viewport_max=float(viewport_max),
    )
    if not any(float(offset) != 0.0 for offset in offsets.values()):
        return ()

    conflicts: list[tuple[int, int]] = []
    for incoming_pos in range(len(measured)):
        (
            incoming_index,
            incoming_page,
            incoming_band,
            _incoming_gap,
        ) = measured[incoming_pos]
        incoming_offset = float(offsets.get(incoming_page, 0.0))
        if incoming_offset == 0.0:
            continue
        for previous_pos in range(incoming_pos):
            (
                previous_index,
                previous_page,
                previous_band,
                _previous_gap,
            ) = measured[previous_pos]
            if previous_page == incoming_page:
                continue
            if not time_windows_overlap(incoming_band, previous_band):
                continue
            previous_offset = float(offsets.get(previous_page, 0.0))
            if previous_offset == 0.0:
                continue
            if bands_require_separation(incoming_band, previous_band, 0.0):
                continue
            shifted_previous = previous_band.shifted(previous_offset)
            if not bands_require_separation(
                incoming_band,
                shifted_previous,
                0.0,
            ):
                continue
            pair = (previous_index, incoming_index)
            if pair not in conflicts:
                conflicts.append(pair)
    return tuple(conflicts)


def retime_measured_collision_bands(
    measured: MeasuredCollisionBands,
    display_lines: DisplayLines,
    style: Style,
    changed_indices: tuple[int, ...],
    *,
    time_window: str = "stable",
) -> MeasuredCollisionBands | None:
    """Reuse measured rectangles when only display boundaries changed."""

    changed = set(changed_indices)
    measured_indices = {
        render_index for render_index, _page, _band, _gap in measured
    }
    if not changed.issubset(measured_indices):
        return None
    retimed: MeasuredCollisionBands = []
    for render_index, page_id, band, gap in measured:
        if render_index not in changed:
            retimed.append((render_index, page_id, band, gap))
            continue
        collision_start, collision_end = display_line_collision_time_window(
            display_lines[render_index],
            style,
            time_window=time_window,
        )
        if collision_end <= collision_start:
            continue
        retimed.append(
            (
                render_index,
                page_id,
                replace(
                    band,
                    display_start_ms=int(collision_start),
                    display_end_ms=int(collision_end),
                    entry_start_ms=int(
                        display_lines[render_index].display_start_ms
                    ),
                ),
                gap,
            )
        )
    return retimed


def fill_section_time_from_measurements(
    display_lines: DisplayLines,
    style: Style,
    measured: MeasuredCollisionBands,
    *,
    time_window: str,
) -> DisplayLines:
    """Extend automatic exits using final measured page placement.

    匹配对 = **同一段内的相邻页、同位置句**：本页每句挂到下一段内下一页
    中与其共享布局轴（同一视觉行）的句子入场前；同视觉行有多句时按页内
    顺序保序一一配对。相邻页的界定要求段号相同（``next_page`` 里
    ``following[0] == page_id[0]``）——跨段的下一页视为无下一页，段尾
    页只对齐到本页最晚结束，绝不挂到别的段的句子上。不做几何最优分
    配——行盒高度 / 页位移排序会把配对交叉到别的行。
    """

    if not style.auto_fill_section_time or not display_lines:
        return display_lines
    bands = {
        render_index: band
        for render_index, _page_id, band, _gap in measured
    }
    if not bands:
        return display_lines

    page_order: list[tuple[int, int]] = []
    page_indices: dict[tuple[int, int], list[int]] = {}
    for index, item in enumerate(display_lines):
        page_id = (int(item.section_index), int(item.page_index))
        if page_id not in page_indices:
            page_order.append(page_id)
            page_indices[page_id] = []
        page_indices[page_id].append(index)
    next_page: dict[tuple[int, int], tuple[int, int] | None] = {}
    for position, page_id in enumerate(page_order):
        following = (
            page_order[position + 1]
            if position + 1 < len(page_order)
            else None
        )
        # 相邻页必须同段：段号不同的下一页按「无下一页」处理。
        next_page[page_id] = (
            following
            if following is not None and following[0] == page_id[0]
            else None
        )

    changed = list(display_lines)
    gap_ms = max(int(style.line_lane_gap_ms), 0)
    for page_id in page_order:
        indices = page_indices[page_id]
        following = next_page[page_id]
        if following is None:
            page_collision_end = max(
                (
                    int(bands[index].display_end_ms)
                    for index in indices
                    if index in bands
                ),
                default=None,
            )
            if page_collision_end is None:
                continue
            targets = {index: page_collision_end for index in indices}
        else:
            # 相邻页、同视觉行、组内保序；下一页没有同行的句子则不挂靠。
            candidates = [
                index for index in page_indices[following] if index in bands
            ]
            used: set[int] = set()
            targets = {}
            for index in indices:
                if index not in bands:
                    continue
                same_row = [
                    candidate
                    for candidate in candidates
                    if candidate not in used
                    and bands_share_layout_axis(
                        bands[index], bands[candidate]
                    )
                ]
                if not same_row:
                    continue
                matched = same_row[0]
                used.add(matched)
                targets[index] = int(bands[matched].display_start_ms) - gap_ms

        for index, collision_end in targets.items():
            item = changed[index]
            if item.line.display_end_override_ms is not None:
                continue
            full_end = int(collision_end)
            if time_window == "stable":
                full_end += exit_animation_ms(style, item.line)
            new_end = max(int(item.display_end_ms), full_end)
            if new_end != item.display_end_ms:
                changed[index] = replace(item, display_end_ms=new_end)
    return changed


def air_row_clamp_candidates(
    display_lines: DisplayLines,
) -> dict[tuple[int, int], tuple[int, int]]:
    """同段相邻且下一页更矮的页：``{page_id: following_page_id}``。

    只有「下一页行数更少」的页才可能有空气行——底部 / 顶部对齐时更矮页
    占的视觉行是本页行的子集；下一页不更矮就不会有缺后继的行。调用方用
    这个纯时间侧的快查决定是否值得跑几何测量。
    """

    page_order: list[tuple[int, int]] = []
    page_counts: dict[tuple[int, int], int] = {}
    for item in display_lines:
        page_id = (int(item.section_index), int(item.page_index))
        if page_id not in page_counts:
            page_order.append(page_id)
            page_counts[page_id] = 0
        page_counts[page_id] += 1
    candidates: dict[tuple[int, int], tuple[int, int]] = {}
    for position, page_id in enumerate(page_order):
        following = (
            page_order[position + 1]
            if position + 1 < len(page_order)
            else None
        )
        if (
            following is None
            or following[0] != page_id[0]
            or page_counts[following] >= page_counts[page_id]
        ):
            continue
        candidates[page_id] = following
    return candidates


def clamp_air_rows_to_page_turn(
    display_lines: DisplayLines,
    style: Style,
    measured: MeasuredCollisionBands,
    *,
    time_window: str = "display",
) -> DisplayLines:
    """缩行页切换时收紧旧页没有后继的「空气行」，不让它活过翻页点。

    旧页比新页高时（3→2、4→3、4→2…），顶部若干行在下一页没有同视觉行
    的后继：既不会被顶掉也不参与段内挂靠，于是停在自然退场（或同步退
    场）的终点——同页 T2 已被下一页顶掉、T1 还挂在画面上。这里按 N3
    TopLong 的口径（上行挂到下一页出现前）把空气行收到翻页点：显示至多
    延续到「下一页上屏时刻 − 同轨间隔」，即与本页第一波被顶掉的行一起
    退场。仍保住演唱中句子的走字与自动退场余量；手工消失时刻与段尾页
    （无同段下一页，交给段内填充 / 段末清屏）不参与。
    """

    candidates = air_row_clamp_candidates(display_lines)
    if not candidates or not measured:
        return display_lines
    bands = {
        render_index: band
        for render_index, _page_id, band, _gap in measured
    }
    if not bands:
        return display_lines

    page_indices: dict[tuple[int, int], list[int]] = {}
    for index, item in enumerate(display_lines):
        page_indices.setdefault(
            (int(item.section_index), int(item.page_index)), []
        ).append(index)

    gap_ms = max(int(style.line_lane_gap_ms), 0)
    floor_ms = style_compression_floor_ms(style)
    exit_protect = max(int(style.exit_anim_protect_ms), 0)

    changed = list(display_lines)
    for page_id, following in candidates.items():
        following_bands = [
            bands[index]
            for index in page_indices[following]
            if index in bands
        ]
        if not following_bands:
            continue
        boundary = min(band.display_start_ms for band in following_bands) - gap_ms
        for index in page_indices[page_id]:
            item = changed[index]
            if index not in bands or item.line.display_end_override_ms is not None:
                continue
            if any(
                bands_share_layout_axis(bands[index], band)
                for band in following_bands
            ):
                continue  # 有同视觉行后继：由守卫 / 填充按各自轨道处理
            # 与守卫同源的退场余量下限：正在唱的句子不能被砍进走字。
            exit_duration = exit_animation_ms(style, item.line)
            exit_stop = max(min(exit_duration, exit_protect), floor_ms)
            if time_window == "stable":
                exit_stop = max(exit_stop, exit_duration)
            new_end = max(
                min(int(item.display_end_ms), int(boundary)),
                int(item.display_start_ms),
                line_end_ms(item.line) + exit_stop,
            )
            if new_end != item.display_end_ms:
                changed[index] = replace(item, display_end_ms=new_end)
    return changed


class DisplayResolutionCache:
    """Bounded LRU cache that retains each display line's track owner."""

    def __init__(self, max_items: int = 24) -> None:
        self._max_items = max(int(max_items), 1)
        self._entries: OrderedDict[
            Hashable, tuple[object, tuple[DisplayLine, ...]]
        ] = OrderedDict()

    def get(self, key: Hashable) -> DisplayLines | None:
        cached = self._entries.get(key)
        if cached is None:
            return None
        self._entries.move_to_end(key)
        return list(cached[1])

    def put(
        self,
        key: Hashable,
        owner: object,
        display_lines: DisplayLines,
    ) -> None:
        self._entries[key] = (owner, tuple(display_lines))
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_items:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


_DISPLAY_LINE_RESOLUTION_CACHE = DisplayResolutionCache(max_items=24)


def cached_display_line_resolution(key: Hashable) -> DisplayLines | None:
    return _DISPLAY_LINE_RESOLUTION_CACHE.get(key)


def store_display_line_resolution(
    key: Hashable,
    owner: object,
    display_lines: DisplayLines,
) -> None:
    _DISPLAY_LINE_RESOLUTION_CACHE.put(key, owner, display_lines)


def clear_display_line_resolution_cache() -> None:
    _DISPLAY_LINE_RESOLUTION_CACHE.clear()


@dataclass(frozen=True)
class DisplayResolutionPorts:
    """Concrete geometry and timing operations required by the resolver."""

    compute: Callable[..., DisplayLines]
    resolve_timing: Callable[..., DisplayLines]
    """接受 ``(items, enforce_gap, fill_section_time=...)``；填充回调在
    同步之后、守卫之前执行（预期时间阶段的一部分）。"""
    collision_pairs: Callable[[DisplayLines], CollisionPairs]
    secondary_collision_pairs: Callable[[DisplayLines], CollisionPairs]
    fill_section_time: Callable[[DisplayLines], DisplayLines]
    apply_animation_guard: Callable[[DisplayLines, bool], DisplayLines]
    clamp_air_rows: Callable[[DisplayLines], DisplayLines] | None = None
    """可选的「空气行翻页钳制」；None = 该调用方不提供几何测量，跳过。"""


@dataclass(frozen=True)
class StyleDisplayResolutionPorts:
    """Backend factory for one concrete canvas display-resolution pass."""

    build: Callable[[int, int, dict[str, object]], DisplayResolutionPorts]


def style_compression_floor_ms(style: Style) -> int:
    """Resolve this style's protect time into a concrete compression floor."""

    return compression_floor_ms(
        style.line_lead_in_ms,
        style.line_tail_ms,
        style.line_protect_ms,
    )


def _reserve_with_floor(
    resolver: Callable[[TimingLine], int], floor_ms: int
) -> Callable[[TimingLine], int]:
    if floor_ms <= 0:
        return resolver
    return lambda line: max(int(resolver(line)), floor_ms)


def display_line_compute_kwargs(style: Style) -> dict[str, object]:
    """Build the frame-independent timeline configuration for one style."""

    return {
        "lead_in_ms": style.line_lead_in_ms,
        "tail_ms": style.line_tail_ms,
        "lane_gap_ms": style.line_lane_gap_ms,
        "section_gap_ms": style.section_gap_ms,
        "sync_entry": style.sync_entry,
        "sync_ending": style.sync_ending,
        "sync_each_page": style.sync_each_page,
        "auto_fill_section_time": style.auto_fill_section_time,
        "section_ending_mode": style.section_ending_mode,
        "protect_ms": protect_time_ms(
            style.line_lead_in_ms,
            style.line_tail_ms,
            style.line_protect_ms,
        ),
        "lane_count": lane_count(style),
        "row_count_of": row_count_resolver(style),
        "bottom_align_of": bottom_align_resolver(style),
        "vertical_position_of": vertical_position_resolver(style),
        "force_bottom_of": force_top_bottom_resolver(style),
        # 「保护时间」与入场/退场动画的自动下限取大：两者都是自动压缩必须在走字
        # 两侧留下的余量，求解器只认一个数。
        "auto_entry_reserve_ms_of": _reserve_with_floor(
            auto_entry_reserve_resolver(style), style_compression_floor_ms(style)
        ),
        "auto_exit_reserve_ms_of": _reserve_with_floor(
            auto_exit_reserve_resolver(style), style_compression_floor_ms(style)
        ),
        "entry_animation_ms_of": entry_animation_resolver(style),
        "exit_animation_ms_of": exit_animation_resolver(style),
    }


@dataclass(frozen=True)
class AnimationGuardPorts:
    """Geometry and animation measurements needed by the timing guard."""

    entry_animation_ms: Callable[[TimingLine], int]
    exit_animation_ms: Callable[[TimingLine], int]
    measure: Callable[[DisplayLines, str], MeasuredCollisionBands]
    retime: Callable[
        [MeasuredCollisionBands, DisplayLines, tuple[int, ...], str],
        MeasuredCollisionBands | None,
    ]


def _clamp_section_ending_clear(
    style: Style,
    display_lines: DisplayLines,
) -> DisplayLines:
    """「段末清屏」钳制：自动消失不越过本段结束点。

    段结束点 = 段内最晚演唱结束 + ``line_tail_ms``（与 timeline 计算口径
    一致）。该钳制是不变量：同步延长、段内填充和守卫的动画恢复都可能把
    结尾拉出去，因此每次守卫返回前都要重新施加。手工拖过消失时刻的句子
    不受限制；钳制后消失不早于自身上屏时刻。
    """

    if style.section_ending_mode != "clear" or not display_lines:
        return display_lines
    tail = max(int(style.line_tail_ms), 0)
    section_ends: dict[int, int] = {}
    for item in display_lines:
        section_index = int(item.section_index)
        end = line_end_ms(item.line) + tail
        section_ends[section_index] = max(section_ends.get(section_index, end), end)
    changed = False
    clamped = list(display_lines)
    for index, item in enumerate(clamped):
        if item.line.display_end_override_ms is not None:
            continue
        limit = section_ends.get(int(item.section_index))
        if limit is None:
            continue
        capped = max(min(int(item.display_end_ms), limit), int(item.display_start_ms))
        if capped != item.display_end_ms:
            clamped[index] = replace(item, display_end_ms=capped)
            changed = True
    return clamped if changed else display_lines


def apply_animation_time_guard(
    style: Style,
    display_lines: DisplayLines,
    ports: AnimationGuardPorts,
    *,
    enforce_inter_page_gap: bool,
    adjustments: list[TimingCollisionAdjustment] | None = None,
) -> DisplayLines:
    """Restore animation windows and enforce measured collision separation.

    时间压缩两阶段到底后的残余冲突按 ``style.overlap_fallback_mode`` 处理：
    ``lift`` 留给空间避让（旧行为）；``displace`` 由将要演唱的下一句直接
    顶掉还在走字的上一句——顶掉边界是下一句最终上屏时刻，被顶掉的句子按
    其「出场动画保护时间」播放退场动画、恰好在边界结束（退场动画充当交接
    过渡，不再预留同轨间隔空白）。自动行把 ``display_end_ms`` 直接写到
    边界；手工行不参与自动压缩、``display_end_ms`` 保持原值，由
    ``takeover_end_ms`` 在显示调度侧钳制可见性；两种行都携带
    ``takeover_exit_ms`` 供计划组装覆写退场动画时长。两种模式都保留
    ForceBottom 行位上移；页面平移避让见页偏移解析。
    """

    if not display_lines:
        return display_lines

    guarded = list(display_lines)
    changed = False
    # 「保护时间」：自动压缩不得越过走字两侧的这个余量。
    floor_ms = style_compression_floor_ms(style)
    entry_durations: list[int] = []
    exit_durations: list[int] = []
    line_starts: list[int] = []
    line_ends: list[int] = []
    for index, item in enumerate(guarded):
        entry_duration = ports.entry_animation_ms(item.line)
        exit_duration = ports.exit_animation_ms(item.line)
        entry_durations.append(entry_duration)
        exit_durations.append(exit_duration)
        line_start = line_start_ms(item.line)
        line_end = line_end_ms(item.line)
        line_starts.append(line_start)
        line_ends.append(line_end)

        start = int(item.display_start_ms)
        end = int(item.display_end_ms)
        if item.line.display_start_override_ms is None and entry_duration > 0:
            start = min(start, max(line_start - entry_duration, 0))
        if item.line.display_end_override_ms is None and exit_duration > 0:
            end = max(end, line_end + exit_duration)
        if start != item.display_start_ms or end != item.display_end_ms:
            guarded[index] = replace(
                item,
                display_start_ms=start,
                display_end_ms=max(start, end),
            )
            changed = True

    # 唱字两侧自动压缩必须留下的余量 = max(出入场动画保护时间, 保护时间)。
    # 动画时长本身可被压缩到该下限（渲染端按窗口加速播放整段动画）；
    # 保护时间不可压缩。与 ``_reserve_with_floor`` 送进求解器的储备
    # 同源，守卫与求解器因此遵守同一条底线。
    entry_protect_ms = max(int(style.entry_anim_protect_ms), 0)
    exit_protect_ms = max(int(style.exit_anim_protect_ms), 0)
    entry_floors = [
        max(min(entry_durations[index], entry_protect_ms), floor_ms)
        for index in range(len(guarded))
    ]
    exit_floors = [
        max(min(exit_durations[index], exit_protect_ms), floor_ms)
        for index in range(len(guarded))
    ]

    if not enforce_inter_page_gap or style.allow_inter_page_line_overlap:
        return _clamp_section_ending_clear(
            style, guarded if changed else display_lines
        )

    time_window = (
        "stable" if style.allow_entry_exit_animation_overlap else "display"
    )
    measured = ports.measure(guarded, time_window)
    # 时间剪枝：冲突要求 incoming 起点早于 previous 终点 + 同轨间隔，
    # 早于此界的对可直接跳过（视觉行口径下所有冲突对的间隔要求一致，
    # 此界为精确等价条件）。
    max_lane_gap = max(int(style.line_lane_gap_ms), 0)
    for _pass in range(max(len(guarded) * 3, 1)):
        adjusted = False
        changed_indices: list[int] = []
        for incoming_pos in range(len(measured)):
            (
                incoming_index,
                incoming_page,
                incoming_band,
                _incoming_gap,
            ) = measured[incoming_pos]
            incoming = guarded[incoming_index]
            for previous_pos in range(incoming_pos):
                (
                    previous_index,
                    previous_page,
                    previous_band,
                    _previous_gap,
                ) = measured[previous_pos]
                if previous_page == incoming_page:
                    continue
                if (
                    style.overlap_fallback_mode == "displace"
                    and guarded[previous_index].takeover_end_ms is not None
                    and int(guarded[previous_index].takeover_end_ms)
                    <= int(guarded[incoming_index].display_start_ms)
                ):
                    # 该上一句已被顶掉（本句或更早的句子）：边界语义是
                    # 「上一句退场动画恰好在本句上屏时结束」，同轨间隔
                    # 判据对已顶掉的对不再适用，也不得再跑压缩阶梯
                    # （否则会把已定边界二次截短）。
                    continue
                if (
                    int(previous_band.display_end_ms) + max_lane_gap
                    <= int(incoming_band.display_start_ms)
                ):
                    continue
                if not bands_share_layout_axis(incoming_band, previous_band):
                    # 严格视觉行口径：布局轴（横排 Y / 竖排 X）墨迹带不相
                    # 叠 = 不同视觉行，画面不可能相撞，直接跳过。行位号
                    # （lane）在混排行数布局（2 行 / 3 行页混排）下与视觉
                    # 行不对应，不作为判据；主守卫与收尾守卫共用本判定。
                    continue
                previous = guarded[previous_index]
                required_gap = max(int(style.line_lane_gap_ms), 0)
                required_start = int(previous_band.display_end_ms) + required_gap
                if int(incoming_band.display_start_ms) >= required_start:
                    continue
                overlap_ms = required_start - int(incoming_band.display_start_ms)

                # 两阶段压缩。底线（两侧都不许越过）：
                #   stop = max(出入场动画保护时间, 保护时间)；stable 判碰
                #   窗口下再抬到完整动画时长——压缩动画不移动稳定段端点，
                #   白白缩短可见动画。
                # 阶段 A · 缓冲区（动画之外的余量，砍了不动动画）：
                #   依旧旧顺序贪心——先砍延迟退场缓冲，再砍提前入场缓冲。
                # 阶段 B · 动画区（两侧都只剩动画时间时）：左右循环对砍，
                #   各瞄准剩余量的一半，容量不足的一侧把差额让给另一侧，
                #   动画按窗口加速播放、不截断。手工覆盖的一侧两个阶段
                #   容量均为 0。
                # 两侧都到底后的残余冲突按 ``overlap_fallback_mode`` 处理：
                #   lift（默认）留给空间避让；displace 由下一句顶掉上一句。
                exit_stop = max(
                    exit_floors[previous_index],
                    exit_durations[previous_index]
                    if time_window == "stable"
                    else 0,
                )
                entry_stop = max(
                    entry_floors[incoming_index],
                    entry_durations[incoming_index]
                    if time_window == "stable"
                    else 0,
                )
                exit_margin = (
                    int(previous.display_end_ms) - line_ends[previous_index]
                )
                entry_margin = (
                    line_starts[incoming_index]
                    - int(incoming.display_start_ms)
                )
                if previous.line.display_end_override_ms is None:
                    exit_total = max(exit_margin - exit_stop, 0)
                    # 缓冲区 = 余量中动画完整保留仍可砍的部分。
                    exit_free = max(
                        exit_margin
                        - max(exit_durations[previous_index], exit_stop),
                        0,
                    )
                else:
                    exit_total = exit_free = 0
                exit_zone = exit_total - exit_free
                if incoming.line.display_start_override_ms is None:
                    entry_total = max(entry_margin - entry_stop, 0)
                    entry_free = max(
                        entry_margin
                        - max(entry_durations[incoming_index], entry_stop),
                        0,
                    )
                else:
                    entry_total = entry_free = 0
                entry_zone = entry_total - entry_free

                remaining = overlap_ms
                exit_free_take = min(remaining, exit_free)
                remaining -= exit_free_take
                entry_free_take = min(remaining, entry_free)
                remaining -= entry_free_take
                exit_zone_take = entry_zone_take = 0
                if remaining > 0:
                    # 只剩动画时间（+同轨间隔+保护时间）：两侧轮流对砍。
                    exit_zone_take = min(
                        exit_zone,
                        max(
                            (remaining + 1) // 2,
                            remaining - entry_zone,
                        ),
                    )
                    entry_zone_take = min(
                        entry_zone,
                        remaining - exit_zone_take,
                    )
                exit_take = exit_free_take + exit_zone_take
                entry_take = entry_free_take + entry_zone_take
                displace_residual = 0
                if style.overlap_fallback_mode == "displace":
                    # 「吃掉走字时长」：压缩到底仍有残余时，由将要演唱的
                    # 下一句直接顶掉还在走字的上一句——上一句不是瞬间消失，
                    # 而是按其「出场动画保护时间」播放退场动画，动画恰好在
                    # 下一句最终上屏时刻结束（走字显示到退场开始为止）。
                    # 退场动画本身充当交接过渡，不再为同轨间隔预留空白。
                    displace_residual = overlap_ms - exit_take - entry_take

                pair_changed: list[int] = []
                if exit_take > 0:
                    new_end = int(previous.display_end_ms) - exit_take
                    if adjustments is not None:
                        adjustments.append(
                            TimingCollisionAdjustment(
                                previous_index=previous_index,
                                incoming_index=incoming_index,
                                boundary="exit",
                                before_ms=int(previous.display_end_ms),
                                after_ms=int(new_end),
                            )
                        )
                    guarded[previous_index] = replace(
                        previous,
                        display_end_ms=max(
                            int(previous.display_start_ms),
                            new_end,
                        ),
                    )
                    pair_changed.append(previous_index)
                if entry_take > 0:
                    new_start = int(incoming.display_start_ms) + entry_take
                    latest_entry_start = max(
                        line_starts[incoming_index] - entry_stop,
                        0,
                    )
                    new_start = min(new_start, latest_entry_start)
                    if new_start != incoming.display_start_ms:
                        if adjustments is not None:
                            adjustments.append(
                                TimingCollisionAdjustment(
                                    previous_index=previous_index,
                                    incoming_index=incoming_index,
                                    boundary="entry",
                                    before_ms=int(incoming.display_start_ms),
                                    after_ms=new_start,
                                )
                            )
                        guarded[incoming_index] = replace(
                            incoming,
                            display_start_ms=new_start,
                        )
                        pair_changed.append(incoming_index)
                if displace_residual > 0:
                    # 顶掉边界 T = 下一句（经入场压缩后的）最终上屏时刻，
                    # 退场时长 P = 上一句的「出场动画保护时间」。自动行把
                    # ``display_end_ms`` 直接写到 T；手工行不参与自动压缩、
                    # ``display_end_ms`` 保持原值，仅由 ``takeover_end_ms``
                    # 在渲染调度钳制。两种行都记录 ``takeover_exit_ms`` 供
                    # 计划组装覆写退场动画时长。只收紧：更早的顶掉胜出，
                    # 重复趟幂等。
                    takeover_start = int(
                        guarded[incoming_index].display_start_ms
                    )
                    takeover_exit = max(
                        int(
                            style_for_line(
                                style, previous.line
                            ).exit_anim_protect_ms
                        ),
                        0,
                    )
                    target = max(
                        int(guarded[previous_index].display_start_ms),
                        takeover_start,
                    )
                    current = guarded[previous_index]
                    if current.takeover_end_ms is None or int(
                        current.takeover_end_ms
                    ) > target:
                        updates: dict[str, int] = {
                            "takeover_end_ms": target,
                            "takeover_exit_ms": takeover_exit,
                        }
                        if current.line.display_end_override_ms is None:
                            updates["display_end_ms"] = target
                        guarded[previous_index] = replace(current, **updates)
                        pair_changed.append(previous_index)
                if pair_changed:
                    adjusted = True
                    changed_indices.extend(pair_changed)
                    changed = True
                    break
            if adjusted:
                break
        if not adjusted:
            break
        if changed_indices:
            retimed = ports.retime(
                measured,
                guarded,
                tuple(changed_indices),
                time_window,
            )
            measured = (
                retimed
                if retimed is not None
                else ports.measure(guarded, time_window)
            )
    return _clamp_section_ending_clear(
        style, guarded if changed else display_lines
    )


def _sync_collision_bands(
    style: Style,
    display_lines: DisplayLines,
    animation_ports: AnimationGuardPorts,
    *,
    enforce_inter_page_gap: bool,
) -> SyncCollisionBands | None:
    """Measure static ink geometry so entry sync can see cross-lane overlap.

    Only the geometry is consumed; the clamp reads its timing from the lines
    being resolved, so measuring once against the pre-sync windows is enough.
    """

    if not style.sync_entry:
        # Only entry synchronization consults the clamp; measuring for the
        # ending side would cost a full ink pass per resolution round for
        # nothing.
        return None
    if not enforce_inter_page_gap or style.allow_inter_page_line_overlap:
        return None
    if not display_lines:
        return None
    measured = animation_ports.measure(
        display_lines,
        collision_time_window_name(style),
    )
    return {
        int(index): (band, float(gap))
        for index, _page_id, band, gap in measured
    }


def resolve_display_timing(
    style: Style,
    display_lines: DisplayLines,
    animation_ports: AnimationGuardPorts,
    *,
    enforce_inter_page_gap: bool,
    adjustments: list[TimingCollisionAdjustment] | None = None,
) -> DisplayLines:
    """Apply page synchronization before measured animation-window guarding.

    自动填充段内时间**不在**这里：填充挂在冲突解完之后（见
    :func:`resolve_display_lines` 尾部），随后由兜底守卫收口——把填充
    提前进每轮会引发实际工程的显示窗错误。
    """

    synchronized = apply_constrained_page_sync(
        display_lines,
        style,
        collision_bands=_sync_collision_bands(
            style,
            display_lines,
            animation_ports,
            enforce_inter_page_gap=enforce_inter_page_gap,
        ),
        enforce_inter_page_gap=enforce_inter_page_gap,
    )
    return apply_animation_time_guard(
        style,
        synchronized,
        animation_ports,
        enforce_inter_page_gap=enforce_inter_page_gap,
        adjustments=adjustments,
    )


def resolve_display_lines(
    *,
    avoid_collisions: bool,
    auto_fill_section_time: bool,
    ports: DisplayResolutionPorts,
) -> DisplayLines:
    """Run the stable multi-pass display-line resolution policy.

    The caller supplies rendering-specific measurement operations.  This module
    owns only the ordering and data flow between those operations, so layout
    policy no longer depends on the Painter implementation.

    顺序即契约（旧流程）：每轮 ``resolve_timing`` 只做 同步 → 守卫 的
    冲突解算；**自动填充段内时间挂在全部冲突解完之后**，再由与填充无关的
    无条件兜底守卫收口——填充造出的重叠当场兜掉，守卫内循环有趟数
    上限，密集冲突时最后一轮可能未完全收敛，输出前统一再兜一次底。
    （把填充提前进每轮曾引发实际工程的显示窗错误，已回退。）最后一步
    是「空气行翻页钳制」：缩行页切换时旧页没有后继的行以同页其余行的
    最终退场为上界收紧，钳制只依赖最终边界，因此必须收在末尾。

    每个多趟步骤完成后按 ``display`` 阶段上报进度（步骤即真实工作量：
    逐趟碰撞测量占整轨重排的大头）。步骤开始前还会登记当前槽位，供
    ``measure_collision_bands`` 的逐行回调把进度折算进槽位内连续推进。
    """

    def timing(items: DisplayLines, enforce_gap: bool) -> DisplayLines:
        return ports.resolve_timing(items, enforce_gap)

    try:
        set_display_phase_head(0, _DISPLAY_RESOLUTION_PHASES)
        ideal = ports.compute(
            adjust_same_position=False,
            dynamic_single_page_reflow=not avoid_collisions,
            independent_line_entry=True,
        )
        report_render_progress("display", 1, _DISPLAY_RESOLUTION_PHASES)
        resolved = ideal
        timing_resolved = False
        if avoid_collisions:
            # ForceBottom inspects the schedule only after automatic timing has
            # separated pages; measuring the raw lead/tail windows latches stale
            # spatial reflow decisions.
            set_display_phase_head(1, _DISPLAY_RESOLUTION_PHASES)
            resolved = timing(resolved, True)
            timing_resolved = True
            report_render_progress("display", 2, _DISPLAY_RESOLUTION_PHASES)
            set_display_phase_head(2, _DISPLAY_RESOLUTION_PHASES)
            force_bottom_pairs = ports.collision_pairs(resolved)
            if force_bottom_pairs:
                resolved = ports.compute(
                    adjust_same_position=False,
                    force_bottom_pairs=force_bottom_pairs,
                    dynamic_single_page_reflow=True,
                    independent_line_entry=True,
                )
                resolved = timing(resolved, True)
            report_render_progress("display", 3, _DISPLAY_RESOLUTION_PHASES)
            set_display_phase_head(3, _DISPLAY_RESOLUTION_PHASES)
            squeeze_pairs = ports.collision_pairs(resolved)
            if squeeze_pairs:
                resolved = ports.compute(
                    adjust_same_position=False,
                    squeeze_pairs=squeeze_pairs,
                    force_bottom_pairs=force_bottom_pairs,
                    dynamic_single_page_reflow=True,
                    independent_line_entry=True,
                )
                resolved = timing(resolved, True)
            report_render_progress("display", 4, _DISPLAY_RESOLUTION_PHASES)
            set_display_phase_head(4, _DISPLAY_RESOLUTION_PHASES)
            secondary_pairs = ports.secondary_collision_pairs(resolved)
            if secondary_pairs:
                combined_pairs = tuple(dict.fromkeys((*squeeze_pairs, *secondary_pairs)))
                resolved = ports.compute(
                    adjust_same_position=False,
                    squeeze_pairs=combined_pairs,
                    force_bottom_pairs=force_bottom_pairs,
                    dynamic_single_page_reflow=True,
                    independent_line_entry=True,
                )
                timing_resolved = False
            report_render_progress("display", 5, _DISPLAY_RESOLUTION_PHASES)
        if not timing_resolved:
            set_display_phase_head(5, _DISPLAY_RESOLUTION_PHASES)
            resolved = timing(resolved, avoid_collisions)
        report_render_progress("display", 6, _DISPLAY_RESOLUTION_PHASES)
        # 冲突全部解完后再自动填充段内时间（挂尾巴），与旧流程一致。
        set_display_phase_head(6, _DISPLAY_RESOLUTION_PHASES)
        if auto_fill_section_time:
            filled = ports.fill_section_time(resolved)
            if filled != resolved:
                resolved = filled
        # 与填充无关的无条件兜底守卫：既兜填充造出的重叠，也兜守卫内循
        # 环未收敛的残余（含段末清屏钳制）。
        resolved = ports.apply_animation_guard(resolved, avoid_collisions)
        # 缩行页切换的「空气行」翻页钳制挂在最后：钳制边界取下一页的上屏
        # 时刻，必须等填充与兜底守卫都落定后再算；它只收紧显示窗口，
        # 不会造出新的冲突。
        if ports.clamp_air_rows is not None:
            resolved = ports.clamp_air_rows(resolved)
        report_render_progress("display", 7, _DISPLAY_RESOLUTION_PHASES)
        return resolved
    finally:
        clear_display_phase_head()


def resolve_display_lines_for_style(
    track: TimingTrack,
    style: Style,
    compute_kwargs: dict[str, object],
    ports: StyleDisplayResolutionPorts,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> DisplayLines:
    """Resolve and cache one style's display lines on a normalized canvas.

    整场解析包在可重入的 :func:`layout_pass` 内：多轮发现 / 填充 / 守卫
    共享同一份区间缓存（行布局、墨迹、段边缘与信号上下文）。直接调用方
    （轨道视图刷新、诊断）不再逐趟重付全价；嵌套在绘制 / IR 的既有区间
    内时复用同一组映射，行为不变。
    """

    with layout_pass():
        base_kwargs = {
            **compute_kwargs,
            "sync_entry": False,
            "sync_ending": False,
            "auto_fill_section_time": False,
        }
        signal_heads = signal_head_context(track, style)
        if signal_heads is not None:
            base_kwargs["signal_head_indexes"] = signal_heads
            base_kwargs["signal_lead_ms"] = signal_lead_in_ms(style)
        # 段首/段尾页标记供逐行动画解析（style_for_line）读取；此处注册后，
        # 本函数产出的显示窗口与后续布局计划看到的替换结果保持一致。
        section_edge_context(track, style)
        if logical_w is None or logical_h is None:
            default_h = max(int(style.layout_reference_height), 1)
            default_w = max(int(round(default_h * 16 / 9)), 1)
            logical_w = default_w if logical_w is None else logical_w
            logical_h = default_h if logical_h is None else logical_h
        logical_w = max(int(logical_w), 1)
        logical_h = max(int(logical_h), 1)
        cache_key = (
            logical_w,
            logical_h,
            id(track),
            value_signature(track),
            lyric_layout_style_signature(style),
        )
        cached = cached_display_line_resolution(cache_key)
        if cached is not None:
            return cached
        resolved = resolve_display_lines(
            avoid_collisions=not style.allow_inter_page_line_overlap,
            auto_fill_section_time=style.auto_fill_section_time,
            ports=ports.build(logical_w, logical_h, base_kwargs),
        )
        store_display_line_resolution(cache_key, track, resolved)
        return resolved


__all__ = [
    "AnimationGuardPorts",
    "DisplayResolutionCache",
    "DisplayResolutionPorts",
    "StyleDisplayResolutionPorts",
    "apply_animation_time_guard",
    "air_row_clamp_candidates",
    "cached_display_line_resolution",
    "clamp_air_rows_to_page_turn",
    "clear_display_line_resolution_cache",
    "display_line_compute_kwargs",
    "resolve_display_lines",
    "resolve_display_lines_for_style",
    "resolve_display_timing",
    "store_display_line_resolution",
]
