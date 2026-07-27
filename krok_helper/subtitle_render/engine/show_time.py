"""N3 ``TopLongAdjuster`` 的上屏 / 消失时刻移植。

逆向来源 ``NicoKaraMaker3.dll``（10.74.80.0）：

- ``Models.AddOns.ShowTimeAdjusters.TopLongAdjuster.AdjustShowTimes``
  —— 默认调整器（``EnvModel.ShowTimeAdjusters[0]``，「上段歌詞を長めに表示する」）；
- ``ShowTimeAdjuster.BottomLineShowBeginTime`` / ``AdjustSamePositionShowTimesIfNeeded``；
- ``LyricsInfoModel.IsHeadPage`` / ``IsTailPage`` / ``PrevPageSamePositionLineIndex``。

N3 的窗口是**按页**算的，而且上行 / 下行规则不对称：

- **上行上屏** = 页内最早演唱 − ``PreTime``（段首页把下行也算进"页内"，
  非段首页只算到倒数第二行）；
- **上行消失** = **下一页的上屏时刻 − ``IntervalTime``**（这就是 TopLong 的本意：
  上行一直挂到下一页出现前）；本段最后一页退化为 下行演唱结束 + ``PostTime``；
- **下行上屏** = 段首页与上行同时入场；否则 = 上一页下行消失 + ``IntervalTime``；
  ≥3 行的页则回到 页首行演唱开始 − ``PreTime``；
- **下行消失** = 自身演唱结束 + ``PostTime``。

随后每行再跑一次 ``AdjustSamePositionShowTimesIfNeeded``：只有当上一页**同屏幕行位**
的那句与本句的理想窗口真的相撞时才挤压，挤压所需的最小间隔是 ``IntervalTime / 4``
（不是完整的 ``IntervalTime``），并按固定 6 级降级让出时间。

N3 没有"最长挂屏"上限，也没有"同行吸附"，所以本模块同样没有。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

MAX_SHOW_TIME_MS = 5_999_990
"""N3 的时刻上限哨兵（``Nkm3Constants``：5999990 ms）。"""

MIN_AUTO_ENTRY_ANIMATION_MS = 250
"""自动压缩非零入场动画时采用的人类视觉反应时间下限。"""


@dataclass(frozen=True)
class ShowTimePage:
    """一页的结构信息；``lines`` 是渲染行索引，按页内顺序（0 = 最上行）。"""

    lines: tuple[int, ...]
    section: int
    configured_rows: int = 1
    vertical_position: str = "bottom"


@dataclass
class ShowTimes:
    starts: list[int] = field(default_factory=list)
    ends: list[int] = field(default_factory=list)
    force_bottom: list[bool] = field(default_factory=list)
    """仅单行底部对齐页有意义：True = 占最下行，False = 被上一页顶上去一行。"""


def protect_time_ms(
    pre_time_ms: int, post_time_ms: int, manual_protect_ms: int = 0
) -> int:
    """N3 ``WipeTimingSettingsModel.ProtectTime``。

    手动值 > 0 时取 ``min(manual, min(pre, post))``，否则 ``min(pre, post) / 2``。
    注意 N3 **不把淡入淡出时长算进保护时间**——动画整段落在显示窗口内部。
    """

    pre = max(int(pre_time_ms), 0)
    post = max(int(post_time_ms), 0)
    manual = max(int(manual_protect_ms), 0)
    if manual > 0:
        return min(manual, min(pre, post))
    return min(pre, post) // 2


def compute_show_times(
    sing_begins: Sequence[int],
    sing_ends: Sequence[int],
    pages: Sequence[ShowTimePage],
    *,
    pre_time_ms: int,
    post_time_ms: int,
    interval_ms: int,
    protect_ms: int,
    overrides: Optional[Sequence[tuple[Optional[int], Optional[int]]]] = None,
    auto_entry_reserve_ms: Optional[Sequence[int]] = None,
    entry_animation_ms: Optional[Sequence[int]] = None,
    exit_animation_ms: Optional[Sequence[int]] = None,
    adjust_same_position: bool = True,
    squeeze_pairs: Optional[Sequence[tuple[int, int]]] = None,
    dynamic_single_page_reflow: bool = True,
    independent_line_entry: bool = False,
) -> ShowTimes:
    """按 N3 ``TopLongAdjuster`` 算出每个渲染行的 ``(上屏, 消失)``。

    ``sing_begins`` / ``sing_ends`` 与渲染行索引对齐；``pages`` 必须按播放顺序、
    连续覆盖全部渲染行。段（``section``）等价于 N3 的 ParagraphBreak 分组：页只与
    同段内的相邻页产生联动。

    ``overrides`` 是逐行手动时刻 ``(上屏, 消失)``（``None`` = 不覆盖）。N3 里
    ``ShowBeginTime`` / ``ShowEndTime`` 就是模型本体，用户改过之后下游页读到的是
    改后的值——所以覆盖必须参与本趟计算（ForceBottom 判定、下行入场、同位挤压），
    而不是等算完再往结果上盖。被覆盖的那一侧不再参与挤压。

    ``auto_entry_reserve_ms`` 是自动压缩后必须保留在走字开始之前的入场动画时间；
    手工上屏覆盖不受该下限约束。退场不保留自动下限，可以压缩到走字结束即消失。

    ``entry_animation_ms`` / ``exit_animation_ms`` 用于区分稳定绘制与纯动画时段：
    自动压缩优先消除稳定绘制的跨页重叠，入场和退场动画彼此重叠是允许的。
    自动压缩不得反转同一页中压缩前已经确定的句子上屏先后关系。

    ``squeeze_pairs`` 由渲染器在完成逐行像素测量后提供。存在该参数时，只压缩
    明确发生时间与空间双重冲突的 ``(旧行, 新行)``，不再用逻辑行位猜测冲突。
    ``dynamic_single_page_reflow`` 仅供旧式 N3 ForceBottom 路径使用；新的像素
    避让路径关闭它，避免单行页先按逻辑行位上移、随后又被空间求解器移动一次。
    ``independent_line_entry`` 关闭 TopLong 隐式的页内同步入场：每行先按自己的
    走字开始减 ``pre_time_ms`` 建立理想窗口，显式同步由上层开关单独处理。
    """

    total = len(sing_begins)
    result = ShowTimes(
        starts=[0] * total, ends=[0] * total, force_bottom=[False] * total
    )
    if total == 0 or not pages:
        return result
    return _Solver(
        sing_begins,
        sing_ends,
        pages,
        pre=max(int(pre_time_ms), 0),
        post=max(int(post_time_ms), 0),
        interval=max(int(interval_ms), 0),
        protect=max(int(protect_ms), 0),
        overrides=overrides,
        auto_entry_reserve_ms=auto_entry_reserve_ms,
        entry_animation_ms=entry_animation_ms,
        exit_animation_ms=exit_animation_ms,
        adjust_same_position=bool(adjust_same_position),
        squeeze_pairs=squeeze_pairs,
        dynamic_single_page_reflow=bool(dynamic_single_page_reflow),
        independent_line_entry=bool(independent_line_entry),
        result=result,
    ).run()


class _Solver:
    """一趟顺序求解；N3 的 ForceBottom / 挤压都依赖前面页已写定的时刻。"""

    def __init__(
        self,
        sing_begins: Sequence[int],
        sing_ends: Sequence[int],
        pages: Sequence[ShowTimePage],
        *,
        pre: int,
        post: int,
        interval: int,
        protect: int,
        overrides: Optional[Sequence[tuple[Optional[int], Optional[int]]]],
        auto_entry_reserve_ms: Optional[Sequence[int]],
        entry_animation_ms: Optional[Sequence[int]],
        exit_animation_ms: Optional[Sequence[int]],
        adjust_same_position: bool,
        squeeze_pairs: Optional[Sequence[tuple[int, int]]],
        dynamic_single_page_reflow: bool,
        independent_line_entry: bool,
        result: ShowTimes,
    ) -> None:
        self.begins = sing_begins
        self.ends = sing_ends
        self.pages = pages
        self.pre = pre
        self.post = post
        self.interval = interval
        self.protect = protect
        self.adjust_same_position = adjust_same_position
        self.squeeze_pairs = tuple(
            (int(other), int(line))
            for other, line in (squeeze_pairs or ())
            if 0 <= int(other) < len(sing_begins)
            and 0 <= int(line) < len(sing_begins)
            and int(other) != int(line)
        )
        self.dynamic_single_page_reflow = dynamic_single_page_reflow
        self.independent_line_entry = independent_line_entry
        animation_windows_supplied = (
            entry_animation_ms is not None or exit_animation_ms is not None
        )
        self.out = result

        total = len(sing_begins)
        self.start_override: list[Optional[int]] = [None] * total
        self.end_override: list[Optional[int]] = [None] * total
        self.auto_entry_reserve_ms = [0] * total
        self.entry_animation_ms = [0] * total
        self.exit_animation_ms = [0] * total
        if overrides is not None:
            for index, item in enumerate(overrides):
                if index >= total:
                    break
                begin, end = item
                self.start_override[index] = None if begin is None else int(begin)
                self.end_override[index] = None if end is None else int(end)
        if auto_entry_reserve_ms is not None:
            for index, duration in enumerate(auto_entry_reserve_ms):
                if index >= total:
                    break
                self.auto_entry_reserve_ms[index] = max(int(duration), 0)
        for target, source in (
            (self.entry_animation_ms, entry_animation_ms),
            (self.exit_animation_ms, exit_animation_ms),
        ):
            if source is None:
                continue
            for index, duration in enumerate(source):
                if index >= total:
                    break
                target[index] = max(int(duration), 0)
        self.animation_windows_active = animation_windows_supplied and (
            any(self.entry_animation_ms) or any(self.exit_animation_ms)
        )
        # 渲染行 → 页号 / 页内位次；页 → 段内前后页（跨段即视为无）。
        self.page_of = [0] * total
        self.slot_of = [0] * total
        for page_index, page in enumerate(pages):
            for slot, line in enumerate(page.lines):
                self.page_of[line] = page_index
                self.slot_of[line] = slot
        self.prev_page: list[int] = []
        self.next_page: list[int] = []
        for page_index, page in enumerate(pages):
            previous = page_index - 1
            self.prev_page.append(
                previous
                if previous >= 0 and pages[previous].section == page.section
                else -1
            )
            following = page_index + 1
            self.next_page.append(
                following
                if following < len(pages) and pages[following].section == page.section
                else -1
            )

    # -- N3 LyricsInfoModel ------------------------------------------------
    def _is_head_page(self, page_index: int) -> bool:
        return self.prev_page[page_index] < 0

    def _is_tail_page(self, page_index: int) -> bool:
        return self.next_page[page_index] < 0

    # -- N3 TopLongAdjuster ------------------------------------------------
    def _top_show_begin(self, page_index: int) -> int:
        page = self.pages[page_index]
        lines = page.lines
        best = self.begins[lines[0]]
        # num2 = bottom（段首页）/ bottom-1（非段首页且非顶部对齐）/ 不扫描（顶部对齐）
        if self._is_head_page(page_index):
            scan = lines[1:]
        elif page.vertical_position != "top":
            scan = lines[1:-1]
        else:
            scan = ()
        for line in scan:
            best = min(best, self.begins[line])
        return max(best - self.pre, 0)

    def _top_show_end(self, page_index: int) -> int:
        lines = self.pages[page_index].lines
        if self._is_tail_page(page_index):
            target = lines[0] if len(lines) == 1 else lines[-1]
            return min(self.ends[target] + self.post, MAX_SHOW_TIME_MS)
        following = self.next_page[page_index]
        if len(lines) == 1 and len(self.pages[following].lines) == 1:
            return min(self.ends[lines[0]] + self.post, MAX_SHOW_TIME_MS)
        return max(self._top_show_begin(following) - self.interval, 0)

    def _bottom_show_begin(self, page_index: int, is_bottom_align: bool) -> int:
        page = self.pages[page_index]
        top = page.lines[0]
        if self._is_head_page(page_index):
            return self.out.starts[top]
        if len(page.lines) <= 2:
            previous = self.prev_page[page_index]
            previous_lines = self.pages[previous].lines
            if len(previous_lines) == 1 and (
                not is_bottom_align
                or self._prev_page_overlap_line(previous_lines[0], True) is not None
            ):
                return self.out.starts[top]
            return min(
                self.out.ends[previous_lines[-1]] + self.interval, MAX_SHOW_TIME_MS
            )
        # N3 在这一支不做 0 下限钳制。
        return self.begins[top] - self.pre

    def _prev_page_overlap_line(
        self, line: int, is_bottom_align: bool
    ) -> Optional[int]:
        candidate = self._prev_page_same_position_line(line, is_bottom_align)
        if candidate is None:
            return None
        if self.out.ends[candidate] < self.out.starts[line]:
            return None
        return candidate

    def _prev_page_same_position_line(
        self, line: int, is_bottom_align: bool
    ) -> Optional[int]:
        page_index = self.page_of[line]
        previous = self.prev_page[page_index]
        if previous < 0:
            return None
        current_lines = self.pages[page_index].lines
        previous_lines = self.pages[previous].lines
        if not is_bottom_align:
            slot = self.slot_of[line]
            if len(previous_lines) < slot + 1:
                return None
            return previous_lines[slot]

        current_count = len(current_lines)
        previous_count = len(previous_lines)
        if current_count == 1 and previous_count == 1:
            if bool(self.out.force_bottom[current_lines[0]]) != bool(
                self.out.force_bottom[previous_lines[0]]
            ):
                return None
            return previous_lines[0]
        if current_count == 1:
            back = 0 if self.out.force_bottom[line] else 1
        else:
            back = current_count - self.slot_of[line] - 1
            if previous_count == 1:
                wanted = 0 if self.out.force_bottom[previous_lines[0]] else 1
                return previous_lines[0] if back == wanted else None
            if previous_count < back + 1:
                return None
        # N3 用 PrevLyricsLineIndex 从上一页末行往回数，不受页边界限制。
        target = previous_lines[-1] - back
        return target if target >= 0 else None

    # -- N3 ShowTimeAdjuster.AdjustSamePositionShowTimesIfNeeded -----------
    def _apply_override(self, line: int) -> None:
        begin = self.start_override[line]
        if begin is not None:
            self.out.starts[line] = begin
        end = self.end_override[line]
        if end is not None:
            self.out.ends[line] = end

    def _enforce_auto_wipe_bounds(self, line: int) -> None:
        """Protect the wipe span and the minimum automatic entry animation."""

        if self.start_override[line] is None:
            latest_start = max(
                self.begins[line] - self.auto_entry_reserve_ms[line],
                0,
            )
            self.out.starts[line] = min(self.out.starts[line], latest_start)
        if self.end_override[line] is None:
            self.out.ends[line] = max(self.out.ends[line], self.ends[line])

    def _stable_start(self, line: int) -> int:
        margin = max(self.begins[line] - self.out.starts[line], 0)
        animation = min(self.entry_animation_ms[line], margin)
        return self.out.starts[line] + animation

    def _stable_end(self, line: int) -> int:
        margin = max(self.out.ends[line] - self.ends[line], 0)
        animation = min(self.exit_animation_ms[line], margin)
        return self.out.ends[line] - animation

    def _page_order_start_ceiling(
        self,
        line: int,
        reference: Sequence[int],
    ) -> int:
        """Latest start for ``line`` without changing its page entry order.

        This is only a bound on the conflicting incoming line.  It never
        rewrites neighbouring lines and therefore stays separate from the
        two-line compression calculation.
        """

        reference_start = int(reference[line])
        ceiling = MAX_SHOW_TIME_MS
        for sibling in self.pages[self.page_of[line]].lines:
            if sibling == line:
                continue
            sibling_start = int(reference[sibling])
            if sibling_start == reference_start:
                # An originally synchronized group must remain synchronized.
                return reference_start
            if sibling_start > reference_start:
                ceiling = min(ceiling, sibling_start)
        return ceiling

    def _squeeze_pair(
        self,
        other: int,
        line: int,
        *,
        entry_order_reference: Sequence[int] | None = None,
    ) -> None:
        """Compress only the two supplied conflicting lines.

        ``other`` supplies the old line's stable end and ``line`` supplies the
        incoming line's stable start.  Page neighbours never participate in
        the overlap amount or receive rewritten times.
        """

        starts, ends = self.out.starts, self.out.ends
        overlap = self._stable_end(other) - self._stable_start(line)
        if overlap <= 0:
            return

        if self.end_override[other] is None:
            # 先压缩上一行的稳定退场余量；纯退场动画允许与下一行入场重叠。
            capacity = max(self._stable_end(other) - self.ends[other], 0)
            delta = min(overlap, capacity)
            ends[other] -= delta
            overlap = self._stable_end(other) - self._stable_start(line)
        if overlap <= 0:
            return

        if self.start_override[line] is None:
            # 缩短提前入场，但保留该行自动入场动画的视觉反应下限。
            latest_start = max(
                self.begins[line] - self.auto_entry_reserve_ms[line],
                0,
            )
            target_stable_start = min(
                self._stable_start(line) + overlap,
                self.begins[line],
            )
            animation = self.entry_animation_ms[line]
            proposed_start = (
                latest_start
                if target_stable_start >= self.begins[line]
                else target_stable_start - animation
            )
            starts[line] = min(
                max(starts[line], proposed_start),
                latest_start,
            )
            if entry_order_reference is not None:
                starts[line] = min(
                    starts[line],
                    self._page_order_start_ceiling(
                        line,
                        entry_order_reference,
                    ),
                )

    def _adjust_same_position(self, line: int, is_bottom_align: bool) -> None:
        other = self._prev_page_same_position_line(line, is_bottom_align)
        if other is None:
            return
        if self.animation_windows_active:
            self._squeeze_pair(other, line)
            return
        if (
            self.start_override[line] is not None
            or self.end_override[other] is not None
        ):
            # 手动时刻是权威的，不参与自动挤压。
            return

        # 未提供动画窗口的调用继续保持 N3 原始六级降级算法，避免改变
        # timeline 公共 API 的既有语义。正式渲染路径总会提供动画窗口。
        starts, ends = self.out.starts, self.out.ends
        pre, post, interval, protect = (
            self.pre,
            self.post,
            self.interval,
            self.protect,
        )
        quarter = interval // 4
        if (
            ends[other] - self.ends[other] >= post
            and starts[line] - ends[other] >= interval
            and self.begins[line] - starts[line] >= pre
            and post + interval + pre <= self.begins[line] - self.ends[other]
            and starts[line] >= ends[other]
        ):
            return
        ends[other] = self.ends[other] + post
        starts[line] = self.begins[line] - pre
        over = ends[other] + quarter - starts[line]
        if over <= 0:
            return
        if over <= post - protect:
            ends[other] -= over
            return
        ends[other] = self.ends[other] + protect
        over -= post - protect
        if over <= pre - protect:
            starts[line] += over
            return
        starts[line] += pre - protect
        over -= pre - protect
        if over <= quarter:
            return
        over -= quarter
        if over <= protect:
            ends[other] -= over
            return
        ends[other] = self.ends[other]
        over -= protect
        if over <= protect:
            starts[line] += over
            return
        starts[line] += protect
        ends[other] = starts[line]

    def _preserve_page_entry_order(self, reference: Sequence[int]) -> None:
        """Preserve each page's pre-compression entry ordering."""

        def delay(line: int, target: int) -> bool:
            if self.start_override[line] is not None:
                return False
            latest_start = max(
                self.begins[line] - self.auto_entry_reserve_ms[line],
                0,
            )
            if target > latest_start:
                return False
            self.out.starts[line] = max(self.out.starts[line], target)
            return True

        def can_delay(line: int, target: int) -> bool:
            if self.out.starts[line] >= target:
                return True
            if self.start_override[line] is not None:
                return False
            latest_start = max(
                self.begins[line] - self.auto_entry_reserve_ms[line],
                0,
            )
            return target <= latest_start

        def advance(line: int, target: int) -> bool:
            if self.start_override[line] is not None:
                return False
            self.out.starts[line] = min(self.out.starts[line], target)
            return True

        starts = self.out.starts
        for page in self.pages:
            lines = page.lines
            # A small fixed-point pass is enough for the at-most-eight-line page
            # while allowing a correction to propagate through equal-time groups.
            for _pass in range(len(lines)):
                changed = False
                for left, right in zip(lines, lines[1:]):
                    reference_left = int(reference[left])
                    reference_right = int(reference[right])
                    if (
                        reference_left < reference_right
                        and starts[left] > starts[right]
                    ):
                        pair_changed = delay(right, starts[left]) or advance(
                            left, starts[right]
                        )
                        changed = pair_changed or changed
                        continue
                    if (
                        reference_left > reference_right
                        and starts[left] < starts[right]
                    ):
                        pair_changed = delay(left, starts[right]) or advance(
                            right, starts[left]
                        )
                        changed = pair_changed or changed
                        continue
                    if (
                        reference_left == reference_right
                        and starts[left] != starts[right]
                    ):
                        target = max(starts[left], starts[right])
                        if can_delay(left, target) and can_delay(right, target):
                            before = (starts[left], starts[right])
                            delay(left, target)
                            delay(right, target)
                            pair_changed = before != (starts[left], starts[right])
                        else:
                            target = min(starts[left], starts[right])
                            before = (starts[left], starts[right])
                            advance(left, target)
                            advance(right, target)
                            pair_changed = before != (starts[left], starts[right])
                        changed = pair_changed or changed
                if not changed:
                    break

    # -- 主循环 ------------------------------------------------------------
    def run(self) -> ShowTimes:
        entry_order_reference: Sequence[int] | None = None
        if self.squeeze_pairs or (
            self.animation_windows_active and self.adjust_same_position
        ):
            entry_order_reference = compute_show_times(
                self.begins,
                self.ends,
                self.pages,
                pre_time_ms=self.pre,
                post_time_ms=self.post,
                interval_ms=self.interval,
                protect_ms=self.protect,
                overrides=list(zip(self.start_override, self.end_override)),
                auto_entry_reserve_ms=self.auto_entry_reserve_ms,
                adjust_same_position=False,
                dynamic_single_page_reflow=self.dynamic_single_page_reflow,
                independent_line_entry=self.independent_line_entry,
            ).starts
        starts, ends, force_bottom = (
            self.out.starts,
            self.out.ends,
            self.out.force_bottom,
        )
        for page_index, page in enumerate(self.pages):
            lines = page.lines
            if not lines:
                continue
            top, bottom = lines[0], lines[-1]
            is_bottom_align = page.vertical_position == "bottom"
            for line in lines:
                force_bottom[line] = False
                if len(lines) == 1 and is_bottom_align:
                    starts[line] = self._top_show_begin(page_index)
                    self._apply_override(line)
                    # N3 先假定占最下行再探测重叠，顺序不能颠倒：
                    # PrevPageSamePositionLineIndex 的取行位置依赖这个值。
                    force_bottom[line] = True
                    if self.dynamic_single_page_reflow:
                        force_bottom[line] = (
                            self._prev_page_overlap_line(line, True) is None
                        )
                if line == top and force_bottom[line] is False:
                    starts[line] = self._top_show_begin(page_index)
                    ends[line] = self._top_show_end(page_index)
                elif line == bottom:
                    starts[line] = self._bottom_show_begin(page_index, is_bottom_align)
                    ends[line] = min(self.ends[line] + self.post, MAX_SHOW_TIME_MS)
                elif page.vertical_position == "top":
                    starts[line] = self._bottom_show_begin(page_index, is_bottom_align)
                    ends[line] = min(self.ends[bottom] + self.post, MAX_SHOW_TIME_MS)
                else:
                    starts[line] = starts[top]
                    ends[line] = ends[top]
                self._apply_override(line)
                adjusted_other = None
                if self.adjust_same_position:
                    adjusted_other = self._prev_page_same_position_line(
                        line, is_bottom_align
                    )
                    self._adjust_same_position(line, is_bottom_align)
                self._apply_override(line)
                self._enforce_auto_wipe_bounds(line)
                if adjusted_other is not None:
                    self._enforce_auto_wipe_bounds(adjusted_other)
        if self.independent_line_entry:
            for line in range(len(starts)):
                if self.start_override[line] is None:
                    starts[line] = max(self.begins[line] - self.pre, 0)
                self._apply_override(line)
                self._enforce_auto_wipe_bounds(line)
        # 不同布局或段落边界两侧，N3 的 same-position 规则可能看不到仍会
        # 发生像素冲突的行。补做“相同页内序号”以及“上一页末行 / 下一页首行”
        # 两组保守压缩；真正无法消除的静态冲突再交给空间避让。
        if self.adjust_same_position and self.animation_windows_active:
            previous_page: ShowTimePage | None = None
            for page in self.pages:
                if previous_page is not None and previous_page.lines and page.lines:
                    pairs = list(zip(previous_page.lines, page.lines))
                    boundary_pair = (previous_page.lines[-1], page.lines[0])
                    if boundary_pair not in pairs:
                        pairs.append(boundary_pair)
                    for other, line in pairs:
                        self._squeeze_pair(other, line)
                        self._enforce_auto_wipe_bounds(other)
                        self._enforce_auto_wipe_bounds(line)
                previous_page = page
        for other, line in self.squeeze_pairs:
            self._squeeze_pair(
                other,
                line,
                entry_order_reference=entry_order_reference,
            )
            self._enforce_auto_wipe_bounds(other)
            self._enforce_auto_wipe_bounds(line)
        if entry_order_reference is not None:
            if not (self.squeeze_pairs and not self.adjust_same_position):
                self._preserve_page_entry_order(entry_order_reference)
        # Automatic squeezing may consume PreTime/PostTime completely, but it
        # must never consume the wipe interval itself.  Non-zero automatic
        # entry animation also retains its requested visual reaction reserve;
        # exit animation may shrink to zero.  Manual overrides remain
        # authoritative on the side explicitly edited by the user.
        for line in range(len(starts)):
            self._enforce_auto_wipe_bounds(line)
        return self.out
