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
    adjust_same_position: bool = True,
) -> ShowTimes:
    """按 N3 ``TopLongAdjuster`` 算出每个渲染行的 ``(上屏, 消失)``。

    ``sing_begins`` / ``sing_ends`` 与渲染行索引对齐；``pages`` 必须按播放顺序、
    连续覆盖全部渲染行。段（``section``）等价于 N3 的 ParagraphBreak 分组：页只与
    同段内的相邻页产生联动。

    ``overrides`` 是逐行手动时刻 ``(上屏, 消失)``（``None`` = 不覆盖）。N3 里
    ``ShowBeginTime`` / ``ShowEndTime`` 就是模型本体，用户改过之后下游页读到的是
    改后的值——所以覆盖必须参与本趟计算（ForceBottom 判定、下行入场、同位挤压），
    而不是等算完再往结果上盖。被覆盖的那一侧不再参与挤压。
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
        adjust_same_position=bool(adjust_same_position),
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
        adjust_same_position: bool,
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
        self.out = result

        total = len(sing_begins)
        self.start_override: list[Optional[int]] = [None] * total
        self.end_override: list[Optional[int]] = [None] * total
        if overrides is not None:
            for index, item in enumerate(overrides):
                if index >= total:
                    break
                begin, end = item
                self.start_override[index] = None if begin is None else int(begin)
                self.end_override[index] = None if end is None else int(end)
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

    def _enforce_auto_singing_bounds(self, line: int) -> None:
        """Keep automatically resolved display time outside the singing span."""

        if self.start_override[line] is None:
            self.out.starts[line] = min(self.out.starts[line], self.begins[line])
        if self.end_override[line] is None:
            self.out.ends[line] = max(self.out.ends[line], self.ends[line])

    def _adjust_same_position(self, line: int, is_bottom_align: bool) -> None:
        other = self._prev_page_same_position_line(line, is_bottom_align)
        if other is None:
            return
        if self.start_override[line] is not None or self.end_override[other] is not None:
            # 手动时刻是权威的，不参与自动挤压。
            return
        starts, ends = self.out.starts, self.out.ends
        pre, post, interval, protect = self.pre, self.post, self.interval, self.protect
        quarter = interval // 4
        if (
            ends[other] - self.ends[other] >= post
            and starts[line] - ends[other] >= interval
            and self.begins[line] - starts[line] >= pre
            and post + interval + pre <= self.begins[line] - self.ends[other]
            and starts[line] >= ends[other]
        ):
            return
        # 先把两边都拉回理想窗口，再按固定级序让出时间。
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

    # -- 主循环 ------------------------------------------------------------
    def run(self) -> ShowTimes:
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
                self._enforce_auto_singing_bounds(line)
                if adjusted_other is not None:
                    self._enforce_auto_singing_bounds(adjusted_other)
        # Automatic squeezing may consume PreTime/PostTime completely, but it
        # must never consume the singing interval itself.  Manual overrides
        # remain authoritative on the side explicitly edited by the user.
        for line in range(len(starts)):
            self._enforce_auto_singing_bounds(line)
        return self.out
