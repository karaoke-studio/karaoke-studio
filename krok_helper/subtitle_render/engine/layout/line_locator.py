"""Source-locator labels shared by every user-facing line reference.

A bare "第 17 行" does not tell the user where the line sits in the song
structure.  Every layout warning, diagnostic and source-reload conflict names
the line as ``S2 P3 T1（第 17 行）`` instead: section 2, page 3 of that section,
track row 1 of the page (top-to-bottom on screen), source line 17 (1-based,
counting blank / untimed rows exactly as the lyrics table numbers them).

The policy lives here so diagnostics and the source watcher agree with the
renderer:

* with an authoritative ``track.page_plan`` the persisted section/page partition
  is authoritative (``resolve_page_plan``),
* otherwise sections/pages are the implicit ones (section gap, explicit breaks,
  row count) computed by ``assign_lanes`` / ``_compute_section_ids``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from krok_helper.subtitle_render.domain.models import (
    Style,
    layout_capacity as _layout_capacity,
    layout_index_for_id as _layout_index_for_id,
)
from krok_helper.subtitle_render.domain.timing import TimingTrack
from krok_helper.subtitle_render.engine.layout.line.style import (
    lane_count as _lane_count,
    row_count_resolver as _row_count_resolver,
)


@dataclass(frozen=True)
class LineLocator:
    """Structural position of one source line: section, page, track row."""

    line_index: int
    section: int
    page: int
    track: int

    def label(self) -> str:
        """``S2 P3 T1（第 17 行）`` for line index 16."""
        return (
            f"S{self.section} P{self.page} T{self.track}"
            f"（第 {self.line_index + 1} 行）"
        )


def _renderable_indices(track: TimingTrack) -> list[int]:
    return [
        index
        for index, line in enumerate(track.lines)
        if not line.is_blank and line.chars
    ]


def _layout_position(style: Style, layout_id: Optional[str]) -> str:
    if layout_id is None:
        return style.line_y_position
    index = _layout_index_for_id(style, layout_id)
    if index <= 0:
        return style.line_y_position
    return style.layouts[index - 1].line_y_position


def _screen_rows(
    style: Style,
    layout_id: Optional[str],
    page_size: int,
) -> list[int]:
    """1-based track rows of one page, top-to-bottom on screen."""
    page_size = max(int(page_size), 1)
    position = _layout_position(style, layout_id)
    if layout_id is None:
        capacity = page_size
    else:
        capacity = max(_layout_capacity(style, layout_id), page_size)
    offset = 0
    if position == "bottom":
        # 单行页的静态行位恒为天然 T1（ForceBottom 是渲染期机制）。
        if not (page_size == 1 and capacity > 1):
            offset = max(capacity - page_size, 0)
    elif position == "center":
        offset = max((capacity - page_size + 1) // 2, 0)
    return [offset + row for row in range(1, page_size + 1)]


@dataclass(frozen=True)
class _PageSpan:
    section: int
    """0-based section index."""
    page: int
    """0-based page index inside the section."""
    start: int
    """First renderable index (into ``renderable``) of the page."""
    size: int
    layout_id: Optional[str] = None


def _page_spans(track: TimingTrack, style: Style) -> list[_PageSpan]:
    if track.page_plan is not None:
        # 可渲染行数作上限：损坏 / 过期的页计划不能把空行或未打轴行算进页。
        remaining = len(_renderable_indices(track))
        spans: list[_PageSpan] = []
        cursor = 0
        for section_index, section in enumerate(track.page_plan.sections):
            for page_index, page in enumerate(section.pages):
                try:
                    size = max(0, min(int(page.line_count), 8, remaining - cursor))
                except (TypeError, ValueError):
                    size = 0
                if size <= 0:
                    continue
                spans.append(
                    _PageSpan(
                        section=section_index,
                        page=page_index,
                        start=cursor,
                        size=size,
                        layout_id=str(page.layout_id or "default"),
                    )
                )
                cursor += size
        if spans:
            return spans

    from krok_helper.subtitle_render.engine.timing.timeline import (
        _compute_section_ids,
        assign_lanes,
    )

    render_lines = [line for line in track.lines if not line.is_blank and line.chars]
    if not render_lines:
        return []
    section_ids = _compute_section_ids(render_lines, max(int(style.section_gap_ms), 0))
    _lanes, page_starts, page_rows = assign_lanes(
        render_lines,
        _lane_count(style),
        _row_count_resolver(style),
        section_gap_ms=style.section_gap_ms,
    )
    # assign_lanes 每页开在 section gap 处，页内行同段；页首换段即 P 归 1。
    spans: list[_PageSpan] = []
    section_token = 0
    page_token = 0
    for render_index, page_start in enumerate(page_starts):
        if render_index and page_start == page_starts[render_index - 1]:
            continue
        if render_index and section_ids[render_index] != section_ids[render_index - 1]:
            section_token += 1
            page_token = 0
        elif render_index:
            page_token += 1
        spans.append(
            _PageSpan(
                section=section_token,
                page=page_token,
                start=render_index,
                size=page_rows[render_index],
            )
        )
    return spans


def line_locators(track: TimingTrack, style: Style) -> dict[int, LineLocator]:
    """Map every renderable ``track.lines`` index to its S/P/T locator."""

    renderable = _renderable_indices(track)
    locators: dict[int, LineLocator] = {}
    for span in _page_spans(track, style):
        members = renderable[span.start : span.start + span.size]
        if not members:
            continue
        rows = _screen_rows(style, span.layout_id, len(members))
        for line_index, row in zip(members, rows):
            locators[line_index] = LineLocator(
                line_index=line_index,
                section=span.section + 1,
                page=span.page + 1,
                track=row,
            )
    return locators


def line_label(track: TimingTrack, style: Style, line_index: int) -> str:
    """``S2 P3 T1（第 17 行）``; empty lines only get their source line."""

    locator = line_locators(track, style).get(int(line_index))
    if locator is None:
        return f"第 {int(line_index) + 1} 行"
    return locator.label()


def line_labels(
    track: TimingTrack,
    style: Style,
    line_indices: tuple[int, ...] | list[int],
) -> list[str]:
    """Labels for several lines, computed from one page resolution."""

    locators = line_locators(track, style)
    labels = []
    for line_index in line_indices:
        locator = locators.get(int(line_index))
        labels.append(
            locator.label() if locator is not None else f"第 {int(line_index) + 1} 行"
        )
    return labels


__all__ = [
    "LineLocator",
    "line_label",
    "line_labels",
    "line_locators",
]
