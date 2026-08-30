"""Section edge-page animation replacement shared by render and list views."""

from __future__ import annotations

from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
from krok_helper.subtitle_render.engine.layout.page.plan import (
    page_plan_signature,
    section_edge_page_line_indices,
)
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingTrack


def section_edge_line_flags(
    track: TimingTrack, style: Style
) -> tuple[frozenset[int], frozenset[int]] | None:
    """段首页/段尾页行索引集合（首、尾）；功能关闭时返回 ``None``。

    渲染管线（``style_for_line``）与歌词列表「特效」列共用这一份判定，
    保证两侧显示与实际渲染一致。单页段的行同时出现在两个集合里。
    """

    if not style.section_edge_anim_enabled:
        return None
    return section_edge_page_line_indices(
        track,
        style,
        section_gap_ms=max(style.section_gap_ms, 0),
    )


def apply_section_edge_animation(
    style: Style, on_head_page: bool, on_tail_page: bool
) -> Style:
    """按段边缘页标记替换入退场动画；手动逐行覆盖在更外层套用、优先级更高。

    默认各页只替换自己一侧：段首页换入场、段尾页换退场；单页段既是首
    又是尾、两侧都换。``section_edge_both_animations``（同时设置出入场）
    开启后，任何段边缘页都两侧一起换。
    """

    both = bool(style.section_edge_both_animations)
    replace_entry = on_head_page or (both and on_tail_page)
    replace_exit = on_tail_page or (both and on_head_page)
    if not (replace_entry or replace_exit):
        return style
    changes: dict[str, object] = {}
    if replace_entry:
        changes["entry_anim"] = style.section_head_anim
    if replace_exit:
        changes["exit_anim"] = style.section_tail_anim
    return style.with_timing(**changes)


def section_edge_context(
    track: TimingTrack,
    style: Style,
) -> dict[int, tuple[bool, bool]]:
    """``id(line)`` → (在段首页, 在段尾页) 的映射（layout pass 内缓存）。"""

    if not style.section_edge_anim_enabled:
        return {}
    cache = getattr(_LAYOUT_PASS, "section_edges", None)
    key = None
    if cache is not None:
        key = (id(track), page_plan_signature(track), max(style.section_gap_ms, 0))
        hit = cache.get(key)
        if hit is not None:
            return hit
    heads, tails = section_edge_line_flags(track, style) or (frozenset(), frozenset())
    by_line_id = {
        id(line): (index in heads, index in tails)
        for index, line in enumerate(track.lines)
        if index in heads or index in tails
    }
    if cache is not None:
        cache[key] = by_line_id
        # The key contains id(track), so retain the owners for the pass.
        _LAYOUT_PASS.tracks.append(track)
        _LAYOUT_PASS.lines.extend(track.lines)
    return by_line_id


def line_section_edge_flags(line: object) -> tuple[bool, bool]:
    """当前 layout pass 内某行的段边缘页标记；无 pass 或未注册时 (False, False)。"""

    cache = getattr(_LAYOUT_PASS, "section_edges", None)
    if not cache:
        return (False, False)
    line_id = id(line)
    for by_line_id in cache.values():
        flags = by_line_id.get(line_id)
        if flags is not None:
            return flags
    return (False, False)


__all__ = [
    "apply_section_edge_animation",
    "line_section_edge_flags",
    "section_edge_context",
    "section_edge_line_flags",
]
