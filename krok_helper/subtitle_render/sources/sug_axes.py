"""SUG 分色分轴：把新解析的轴集合对齐到渲染器当前槽位（纯逻辑）。

宿主的「主字幕 + 副字幕源」模型里，一个 ``.sug`` 按分组拆出多个轴：主分
组占主字幕槽位，其余组各占一个副字幕源。本模块只做无 UI 的重载规划——

- 单轴（项目无 ``axis_groups``）：所有槽位共用同一份完整解析，等价于既有
  LRC 多源共文件的重载语义（``plan_single_axis_reload``）；
- 分轴：副源按「分组名 → 歌手集合」两级匹配回新解析，匹配到的做本地编
  辑迁移（``merge_reloaded_track``），消失的组移除、新增的组追加
  （``plan_split_axis_reload``）。

时间口径：过滤只删行删字，所有保留元素与未拆分轨道逐毫秒一致；因此重载
合并可以直接复用单轴轨道的 ``merge_reloaded_track``。
"""

from __future__ import annotations

from dataclasses import dataclass

from krok_helper.subtitle_render.domain.timing import TimingTrack
from krok_helper.subtitle_render.sources.reload import (
    TrackReloadMerge,
    merge_reloaded_track,
)
from krok_helper.subtitle_render.sources.sug import SugAxisTrack


@dataclass
class AxisSlotState:
    """一个轴槽位（主字幕或某个副字幕源）的当前源侧状态。"""

    track: TimingTrack
    """当前轨道，可能叠加了渲染器本地编辑。"""

    baseline: TimingTrack
    """上一次接受的源解析（无本地编辑）；缺省时退化为 ``track``。"""

    name: str | None = None
    """分组名；主槽位与未分轴副源为 ``None``。"""

    singer_ids: frozenset[str] | None = None
    """轴的歌手集合；``None`` = 未过滤（整份内容）。"""


@dataclass(frozen=True)
class AxisExtraAddition:
    """重载后新出现、需要追加为副字幕源的轴。"""

    name: str
    singer_ids: frozenset[str]
    track: TimingTrack


@dataclass(frozen=True)
class AxisExtraUpdate:
    """一个已匹配副字幕源的就地更新：合并结果 + 纯解析候选。"""

    index: int
    merge: TrackReloadMerge
    candidate: TimingTrack
    """本次接受的源解析（无本地编辑）；应用后成为该源的新基线。"""


@dataclass(frozen=True)
class AxisReloadPlan:
    """一次 ``.sug`` 重载的完整应用计划。"""

    primary_merge: TrackReloadMerge | None = None
    extra_updates: tuple[AxisExtraUpdate, ...] = ()
    extra_additions: tuple[AxisExtraAddition, ...] = ()
    removed_extra_indices: tuple[int, ...] = ()
    conflicts: tuple[str, ...] = ()
    structure_changed: bool = False

    @property
    def changed(self) -> bool:
        return bool(
            self.primary_merge is not None
            or self.extra_updates
            or self.extra_additions
            or self.removed_extra_indices
        )

    @property
    def timing_only(self) -> bool:
        merges = (
            ((self.primary_merge,) if self.primary_merge is not None else ())
            + tuple(update.merge for update in self.extra_updates)
        )
        return bool(merges) and all(merge.timing_only for merge in merges)


def _merge_slot(slot: AxisSlotState, candidate: TimingTrack) -> TrackReloadMerge:
    baseline = slot.baseline if slot.baseline is not None else slot.track
    return merge_reloaded_track(slot.track, baseline, candidate)


def _merge_if_changed(
    slot: AxisSlotState, candidate: TimingTrack
) -> TrackReloadMerge | None:
    merge = _merge_slot(slot, candidate)
    return merge if merge.track != slot.track else None


def plan_single_axis_reload(
    *,
    primary: AxisSlotState | None,
    candidate: TimingTrack,
    extra_slots: list[tuple[int, AxisSlotState]],
) -> AxisReloadPlan:
    """单轴重载：主槽位与所有共文件副槽位共用同一份完整解析。"""

    primary_merge = (
        _merge_if_changed(primary, candidate) if primary is not None else None
    )
    extra_updates = tuple(
        update
        for index, slot in extra_slots
        if (merge := _merge_if_changed(slot, candidate)) is not None
        for update in (AxisExtraUpdate(index=index, merge=merge, candidate=candidate),)
    )
    merges = _plan_merges(primary_merge, extra_updates)
    return AxisReloadPlan(
        primary_merge=primary_merge,
        extra_updates=extra_updates,
        conflicts=tuple(
            dict.fromkeys(conflict for merge in merges for conflict in merge.conflicts)
        ),
        structure_changed=any(merge.structure_changed for merge in merges),
    )


def plan_split_axis_reload(
    *,
    primary: AxisSlotState | None,
    primary_axis: SugAxisTrack,
    axis_extra_slots: list[tuple[int, AxisSlotState]],
    axes: list[SugAxisTrack],
) -> AxisReloadPlan:
    """分轴重载：副源按名称/歌手集合匹配新分组，消失移除、新增追加。"""

    primary_merge = (
        _merge_if_changed(primary, primary_axis.track)
        if primary is not None
        else None
    )

    candidates = [axis for axis in axes if not axis.is_primary]
    matched_positions: set[int] = set()
    extra_updates: list[AxisExtraUpdate] = []
    removed_extra_indices: list[int] = []

    for extra_index, slot in axis_extra_slots:
        position = _match_axis_candidate(candidates, matched_positions, slot)
        if position is None:
            removed_extra_indices.append(extra_index)
            continue
        matched_positions.add(position)
        merge = _merge_if_changed(slot, candidates[position].track)
        if merge is not None:
            extra_updates.append(
                AxisExtraUpdate(
                    index=extra_index,
                    merge=merge,
                    candidate=candidates[position].track,
                )
            )

    additions = tuple(
        AxisExtraAddition(
            name=axis.name,
            singer_ids=axis.singer_ids,
            track=axis.track,
        )
        for position, axis in enumerate(candidates)
        if position not in matched_positions
    )

    merges = _plan_merges(primary_merge, extra_updates)
    return AxisReloadPlan(
        primary_merge=primary_merge,
        extra_updates=tuple(extra_updates),
        extra_additions=additions,
        removed_extra_indices=tuple(removed_extra_indices),
        conflicts=tuple(
            dict.fromkeys(conflict for merge in merges for conflict in merge.conflicts)
        ),
        structure_changed=bool(
            additions
            or removed_extra_indices
            or any(merge.structure_changed for merge in merges)
        ),
    )


def _plan_merges(
    primary_merge: TrackReloadMerge | None,
    extra_updates: list[AxisExtraUpdate] | tuple[AxisExtraUpdate, ...],
) -> tuple[TrackReloadMerge, ...]:
    return (
        ((primary_merge,) if primary_merge is not None else ())
        + tuple(update.merge for update in extra_updates)
    )


def _match_axis_candidate(
    candidates: list[SugAxisTrack],
    matched_positions: set[int],
    slot: AxisSlotState,
) -> int | None:
    """同名分组优先；改名后按歌手集合回退，尽量把本地编辑迁移过去。"""

    if slot.name:
        for position, axis in enumerate(candidates):
            if position in matched_positions:
                continue
            if axis.name == slot.name:
                return position
    if slot.singer_ids:
        for position, axis in enumerate(candidates):
            if position in matched_positions:
                continue
            if axis.singer_ids == slot.singer_ids:
                return position
    return None
