"""Authoritative subtitle section/page planning.

The source line order never changes.  A :class:`TrackPagePlan` only partitions
the renderable lines (non-blank lines with timed characters) into sections and
pages.  Legacy ``layout_index`` / ``break_before`` fields are maintained as a
projection for old projects and consumers.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Iterable, Optional

from krok_helper.subtitle_render.domain.timing import (
    SubtitleLoadingSettings,
    TimingLine,
    TimingTrack,
    TrackPage,
    TrackPagePlan,
    TrackSection,
    timing_line_start_ms,
)
from krok_helper.subtitle_render.models import (
    Style,
    ensure_page_layout_defaults,
    layout_capacity,
    layout_id_for_index,
    layout_index_for_id,
)


@dataclass(frozen=True)
class ResolvedLine:
    track_line_index: int
    render_line_index: int
    section_index: int
    page_index_in_section: int
    global_page_index: int
    lane: int
    page_line_count: int
    layout_id: str


@dataclass(frozen=True)
class ResolvedPage:
    section_index: int
    page_index_in_section: int
    global_page_index: int
    line_count: int
    layout_id: str
    track_line_indices: tuple[int, ...]


@dataclass(frozen=True)
class ResolvedSection:
    section_index: int
    pages: tuple[ResolvedPage, ...]


@dataclass(frozen=True)
class ResolvedPagePlan:
    sections: tuple[ResolvedSection, ...]
    pages: tuple[ResolvedPage, ...]
    lines: tuple[ResolvedLine, ...]

    def line_for_track_index(self, track_line_index: int) -> Optional[ResolvedLine]:
        return next(
            (
                item
                for item in self.lines
                if item.track_line_index == int(track_line_index)
            ),
            None,
        )


def renderable_line_indices(track: TimingTrack) -> list[int]:
    return [
        index
        for index, line in enumerate(track.lines)
        if not line.is_blank and bool(line.chars)
    ]


def _line_end_ms(line: TimingLine) -> int:
    if line.end_ms is not None:
        return int(line.end_ms)
    if line.chars:
        return int(line.chars[-1].start_ms) + 1000
    return 0


def _default_layout_id(style: Style, line_count: int) -> str:
    style = ensure_page_layout_defaults(style)
    count = max(1, min(int(line_count), 8))
    return style.default_layout_by_row_count.get(count, "default")


def build_page_plan(
    track: TimingTrack,
    settings: SubtitleLoadingSettings,
    style: Style,
) -> TrackPagePlan:
    """Build a plan from source semantics and saved loading rules."""

    indexed = renderable_line_indices(track)
    if not indexed:
        return TrackPagePlan()
    rows_per_page = max(1, min(int(settings.rows_per_page), 4))
    sections_counts: list[list[int]] = [[]]
    current_page_count = 0
    previous_track_index: Optional[int] = None
    previous_line: Optional[TimingLine] = None

    def start_section() -> None:
        nonlocal current_page_count
        if sections_counts[-1]:
            sections_counts.append([])
        current_page_count = 0

    def start_page() -> None:
        nonlocal current_page_count
        if current_page_count:
            sections_counts[-1].append(current_page_count)
        current_page_count = 0

    for render_index, track_index in enumerate(indexed):
        line = track.lines[track_index]
        if render_index:
            explicit = str(getattr(line, "break_before", "none"))
            blank_between = bool(
                settings.blank_line_section_enabled
                and previous_track_index is not None
                and any(
                    track.lines[index].is_blank or not track.lines[index].chars
                    for index in range(previous_track_index + 1, track_index)
                )
            )
            timed_gap = bool(
                settings.time_gap_section_enabled
                and previous_line is not None
                and timing_line_start_ms(line) - _line_end_ms(previous_line)
                > max(int(settings.section_gap_ms), 0)
            )
            if explicit == "paragraph" or blank_between or timed_gap:
                start_page()
                start_section()
            elif explicit == "page":
                start_page()
            elif current_page_count >= rows_per_page:
                start_page()
        current_page_count += 1
        previous_track_index = track_index
        previous_line = line
    start_page()

    allocate_by_actual_rows = bool(settings.allocate_layout_by_actual_rows)
    base_layout_id = _default_layout_id(style, rows_per_page)
    sections: list[TrackSection] = []
    for counts in sections_counts:
        pages = [
            TrackPage(
                count,
                _default_layout_id(style, count)
                if allocate_by_actual_rows
                else base_layout_id,
            )
            for count in counts
            if count > 0
        ]
        if pages:
            sections.append(TrackSection(pages))
    return TrackPagePlan(sections)


def build_legacy_page_plan(
    track: TimingTrack,
    style: Style,
    *,
    section_gap_ms: int = 0,
) -> TrackPagePlan:
    """Migrate schema-v1 line fields without changing their visible grouping."""

    style = ensure_page_layout_defaults(style)
    indexed = renderable_line_indices(track)
    if not indexed:
        return TrackPagePlan()
    sections: list[TrackSection] = []
    pages: list[TrackPage] = []
    current_count = 0
    current_layout_id = "default"
    previous_line: Optional[TimingLine] = None

    def close_page() -> None:
        nonlocal current_count
        if current_count:
            pages.append(TrackPage(current_count, current_layout_id))
        current_count = 0

    def close_section() -> None:
        nonlocal pages
        close_page()
        if pages:
            sections.append(TrackSection(pages))
        pages = []

    for render_index, track_index in enumerate(indexed):
        line = track.lines[track_index]
        line_layout_id = layout_id_for_index(style, int(line.layout_index or 0))
        explicit = str(getattr(line, "break_before", "none"))
        timed_section = bool(
            render_index
            and section_gap_ms > 0
            and previous_line is not None
            and timing_line_start_ms(line) - _line_end_ms(previous_line)
            > int(section_gap_ms)
        )
        if render_index and (explicit == "paragraph" or timed_section):
            close_section()
        elif render_index and (
            explicit == "page"
            or line_layout_id != current_layout_id
            or current_count >= layout_capacity(style, current_layout_id)
        ):
            close_page()
        if current_count == 0:
            current_layout_id = line_layout_id
        current_count += 1
        previous_line = line
    close_section()
    return normalize_page_plan(track, style, TrackPagePlan(sections))


def normalize_page_plan(
    track: TimingTrack,
    style: Style,
    plan: Optional[TrackPagePlan] = None,
) -> TrackPagePlan:
    """Repair malformed/old plans while preserving page order where possible."""

    style = ensure_page_layout_defaults(style)
    valid_layout_ids = {"default", *(layout.layout_id for layout in style.layouts)}
    remaining = len(renderable_line_indices(track))
    if remaining <= 0:
        return TrackPagePlan()
    source = deepcopy(plan if plan is not None else track.page_plan)
    sections: list[TrackSection] = []
    if source is not None:
        for section in source.sections:
            pages: list[TrackPage] = []
            for page in section.pages:
                if remaining <= 0:
                    break
                try:
                    count = max(0, min(int(page.line_count), 8, remaining))
                except (TypeError, ValueError):
                    continue
                if count <= 0:
                    continue
                layout_id = str(page.layout_id or "default")
                if (
                    layout_id not in valid_layout_ids
                    or layout_capacity(style, layout_id) < count
                ):
                    layout_id = _default_layout_id(style, count)
                pages.append(TrackPage(count, layout_id))
                remaining -= count
            if pages:
                sections.append(TrackSection(pages))
            if remaining <= 0:
                break
    if not sections:
        sections.append(TrackSection())
    while remaining > 0:
        count = min(remaining, 8)
        sections[-1].pages.append(
            TrackPage(count, _default_layout_id(style, count))
        )
        remaining -= count
    return TrackPagePlan(sections)


def resolve_page_plan(
    track: TimingTrack,
    style: Style,
    plan: Optional[TrackPagePlan] = None,
) -> ResolvedPagePlan:
    """Resolve persisted counts to concrete source-line indices."""

    normalized = normalize_page_plan(track, style, plan)
    indexed = renderable_line_indices(track)
    cursor = 0
    global_page_index = 0
    render_index = 0
    resolved_sections: list[ResolvedSection] = []
    resolved_pages: list[ResolvedPage] = []
    resolved_lines: list[ResolvedLine] = []
    for section_index, section in enumerate(normalized.sections):
        section_pages: list[ResolvedPage] = []
        for page_index, page in enumerate(section.pages):
            members = tuple(indexed[cursor : cursor + page.line_count])
            cursor += len(members)
            resolved_page = ResolvedPage(
                section_index=section_index,
                page_index_in_section=page_index,
                global_page_index=global_page_index,
                line_count=len(members),
                layout_id=page.layout_id,
                track_line_indices=members,
            )
            section_pages.append(resolved_page)
            resolved_pages.append(resolved_page)
            capacity = layout_capacity(style, page.layout_id)
            position = _layout_position(style, page.layout_id)
            if position == "bottom":
                lane_offset = max(capacity - len(members), 0)
            elif position == "center":
                lane_offset = max((capacity - len(members) + 1) // 2, 0)
            else:
                lane_offset = 0
            for lane_in_page, track_index in enumerate(members):
                resolved_lines.append(
                    ResolvedLine(
                        track_line_index=track_index,
                        render_line_index=render_index,
                        section_index=section_index,
                        page_index_in_section=page_index,
                        global_page_index=global_page_index,
                        lane=lane_offset + lane_in_page,
                        page_line_count=len(members),
                        layout_id=page.layout_id,
                    )
                )
                render_index += 1
            global_page_index += 1
        if section_pages:
            resolved_sections.append(
                ResolvedSection(section_index, tuple(section_pages))
            )
    return ResolvedPagePlan(
        tuple(resolved_sections), tuple(resolved_pages), tuple(resolved_lines)
    )


def _layout_position(style: Style, layout_id: str) -> str:
    if layout_id == "default":
        return style.line_y_position
    index = layout_index_for_id(style, layout_id)
    if index <= 0:
        return style.line_y_position
    return style.layouts[index - 1].line_y_position


def project_page_plan_to_legacy_fields(track: TimingTrack, style: Style) -> None:
    """Dual-write the authoritative plan into schema-v1 per-line fields."""

    normalized = normalize_page_plan(track, style)
    track.page_plan = normalized
    for line in track.lines:
        if not line.is_blank and line.chars:
            line.layout_index = 0
            line.break_before = "none"
    resolved = resolve_page_plan(track, style, normalized)
    for item in resolved.lines:
        line = track.lines[item.track_line_index]
        line.layout_index = layout_index_for_id(style, item.layout_id)
        if item.render_line_index == 0:
            line.break_before = "none"
        elif item.page_index_in_section == 0 and item.lane == resolved.lines[
            item.render_line_index
        ].lane:
            # First line of every non-first section.
            page = resolved.pages[item.global_page_index]
            if item.track_line_index == page.track_line_indices[0]:
                line.break_before = "paragraph"
        else:
            page = resolved.pages[item.global_page_index]
            if item.track_line_index == page.track_line_indices[0]:
                line.break_before = "page"


def section_head_line_indices(
    track: TimingTrack,
    style: Style,
    *,
    section_gap_ms: int = 0,
) -> frozenset[int]:
    """每段第一页第一行的 ``track.lines`` 索引集合。

    音量柱（SignalsLits 的 volume 样式）只挂在这些行上。page_plan 存在时
    以权威结构为准；裸解析的 track 没有 plan，按行间奏间隔推导段边界，
    口径与 :func:`krok_helper.subtitle_render.engine.timing.timeline._compute_section_ids`
    一致。段的行序连续展开，因此段首行必然是段第一页的第一行。
    """

    if track.page_plan is not None:
        resolved = resolve_page_plan(track, style)
        heads: set[int] = set()
        for section in resolved.sections:
            if section.pages and section.pages[0].track_line_indices:
                heads.add(section.pages[0].track_line_indices[0])
        return frozenset(heads)
    heads = set()
    previous: Optional[TimingLine] = None
    for track_index in renderable_line_indices(track):
        line = track.lines[track_index]
        if previous is None or (
            section_gap_ms > 0
            and timing_line_start_ms(line) - _line_end_ms(previous) > section_gap_ms
        ):
            heads.add(track_index)
        previous = line
    return frozenset(heads)


def page_for_track_line(
    track: TimingTrack, style: Style, track_line_index: int
) -> Optional[ResolvedPage]:
    resolved = resolve_page_plan(track, style)
    line = resolved.line_for_track_index(track_line_index)
    return resolved.pages[line.global_page_index] if line is not None else None


def set_pages_layout(
    track: TimingTrack,
    style: Style,
    global_page_indices: Iterable[int],
    layout_id: str,
) -> bool:
    plan = normalize_page_plan(track, style)
    selected = {int(index) for index in global_page_indices}
    if not selected:
        return False
    capacity = layout_capacity(style, layout_id)
    changed = False
    global_index = 0
    for section in plan.sections:
        for page in section.pages:
            if global_index in selected and capacity >= page.line_count:
                if page.layout_id != layout_id:
                    page.layout_id = layout_id
                    changed = True
            global_index += 1
    if changed:
        track.page_plan = plan
        project_page_plan_to_legacy_fields(track, style)
    return changed


def use_default_layouts(track: TimingTrack, style: Style) -> bool:
    plan = normalize_page_plan(track, style)
    changed = False
    for section in plan.sections:
        for page in section.pages:
            layout_id = _default_layout_id(style, page.line_count)
            if page.layout_id != layout_id:
                page.layout_id = layout_id
                changed = True
    if changed:
        track.page_plan = plan
        project_page_plan_to_legacy_fields(track, style)
    return changed


def reflow_pages_for_layout_capacity(
    track: TimingTrack,
    style: Style,
    layout_id: str,
) -> tuple[int, int]:
    """Split pages that no longer fit a referenced layout after it shrinks.

    Returns ``(affected_pages, added_pages)``.  Growing a layout is a no-op and
    never pulls lyrics from following pages.
    """

    plan = deepcopy(track.page_plan)
    if plan is None:
        plan = normalize_page_plan(track, style)
    capacity = layout_capacity(style, layout_id)
    affected = 0
    added = 0
    for section in plan.sections:
        rebuilt: list[TrackPage] = []
        for page in section.pages:
            if page.layout_id != layout_id or page.line_count <= capacity:
                rebuilt.append(page)
                continue
            affected += 1
            remaining = page.line_count
            first = True
            while remaining > 0:
                count = min(remaining, capacity)
                rebuilt.append(
                    TrackPage(
                        count,
                        layout_id if first else _default_layout_id(style, count),
                    )
                )
                if not first:
                    added += 1
                first = False
                remaining -= count
        section.pages = rebuilt
    if affected:
        track.page_plan = normalize_page_plan(track, style, plan)
        project_page_plan_to_legacy_fields(track, style)
    return affected, added


def insert_boundary(
    track: TimingTrack,
    style: Style,
    track_line_index: int,
    *,
    kind: str,
) -> bool:
    """Insert a page or section boundary before one renderable source line."""

    if kind not in {"page", "paragraph"}:
        return False
    resolved = resolve_page_plan(track, style)
    line = resolved.line_for_track_index(track_line_index)
    if line is None:
        return False
    resolved_page = resolved.pages[line.global_page_index]
    if (
        not resolved_page.track_line_indices
        or resolved_page.track_line_indices[0] == track_line_index
    ):
        return False
    plan = normalize_page_plan(track, style)
    section = plan.sections[line.section_index]
    page = section.pages[line.page_index_in_section]
    offset = next(
        index
        for index, item in enumerate(resolved_page.track_line_indices)
        if item == track_line_index
    )
    left = TrackPage(offset, _default_layout_id(style, offset))
    right_count = page.line_count - offset
    right = TrackPage(right_count, _default_layout_id(style, right_count))
    if kind == "page":
        section.pages[line.page_index_in_section : line.page_index_in_section + 1] = [
            left,
            right,
        ]
    else:
        before_pages = section.pages[: line.page_index_in_section] + [left]
        after_pages = [right] + section.pages[line.page_index_in_section + 1 :]
        plan.sections[line.section_index : line.section_index + 1] = [
            TrackSection(before_pages),
            TrackSection(after_pages),
        ]
    track.page_plan = normalize_page_plan(track, style, plan)
    project_page_plan_to_legacy_fields(track, style)
    return True


def delete_boundary(
    track: TimingTrack,
    style: Style,
    track_line_index: int,
    *,
    kind: str,
) -> bool:
    """Delete the page/section boundary immediately before a line."""

    resolved = resolve_page_plan(track, style)
    line = resolved.line_for_track_index(track_line_index)
    if line is None:
        return False
    plan = normalize_page_plan(track, style)
    if kind == "page":
        if line.page_index_in_section <= 0:
            return False
        section = plan.sections[line.section_index]
        left = section.pages[line.page_index_in_section - 1]
        right = section.pages[line.page_index_in_section]
        count = left.line_count + right.line_count
        if count > 8:
            return False
        oversized = layout_capacity(style, left.layout_id) > left.line_count
        layout_id = (
            left.layout_id
            if oversized and layout_capacity(style, left.layout_id) >= count
            else _default_layout_id(style, count)
        )
        section.pages[line.page_index_in_section - 1 : line.page_index_in_section + 1] = [
            TrackPage(count, layout_id)
        ]
    elif kind == "paragraph":
        if line.section_index <= 0 or line.page_index_in_section != 0:
            return False
        previous = plan.sections[line.section_index - 1]
        current = plan.sections[line.section_index]
        previous.pages.extend(current.pages)
        del plan.sections[line.section_index]
    else:
        return False
    track.page_plan = normalize_page_plan(track, style, plan)
    project_page_plan_to_legacy_fields(track, style)
    return True


def move_page_boundary(
    track: TimingTrack,
    style: Style,
    section_index: int,
    page_index: int,
    *,
    direction: int,
) -> bool:
    """Move one page boundary while preserving lyric order.

    ``direction > 0`` pulls the following page's first line into ``page_index``.
    Later pages keep their requested counts and therefore flow forward; the
    destination section's final page loses one line.  ``direction < 0`` performs
    the inverse and grows (or creates) that section's final page.  The adjacent
    page may belong to the next section; sections left without renderable lines
    are removed so all following section indices stay contiguous.
    """

    plan = normalize_page_plan(track, style)
    if not 0 <= section_index < len(plan.sections):
        return False
    section = plan.sections[section_index]
    if not 0 <= page_index < len(section.pages):
        return False
    if direction > 0:
        target = section.pages[page_index]
        if page_index + 1 < len(section.pages):
            destination = section
        elif section_index + 1 < len(plan.sections):
            destination = plan.sections[section_index + 1]
        else:
            return False
        tail = destination.pages[-1]
        if target.line_count >= 8 or tail.line_count <= 0:
            return False
        _resize_page_with_instant_layout_rule(target, target.line_count + 1, style)
        _resize_page_with_instant_layout_rule(tail, tail.line_count - 1, style)
    elif direction < 0:
        target = section.pages[page_index]
        if target.line_count <= 0:
            return False
        _resize_page_with_instant_layout_rule(target, target.line_count - 1, style)
        if page_index + 1 < len(section.pages):
            destination = section
        elif section_index + 1 < len(plan.sections):
            destination = plan.sections[section_index + 1]
        else:
            destination = section
        tail = destination.pages[-1]
        if tail is target or tail.line_count >= 8:
            destination.pages.append(
                TrackPage(1, _default_layout_id(style, 1))
            )
        else:
            _resize_page_with_instant_layout_rule(tail, tail.line_count + 1, style)
    else:
        return False
    section.pages = [page for page in section.pages if page.line_count > 0]
    plan.sections = [item for item in plan.sections if item.pages]
    track.page_plan = normalize_page_plan(track, style, plan)
    project_page_plan_to_legacy_fields(track, style)
    return True


def _resize_page_with_instant_layout_rule(
    page: TrackPage, new_count: int, style: Style
) -> None:
    old_count = int(page.line_count)
    old_capacity = layout_capacity(style, page.layout_id)
    page.line_count = max(0, min(int(new_count), 8))
    if page.line_count <= 0:
        return
    was_oversized = old_capacity > old_count
    if not (was_oversized and old_capacity >= page.line_count):
        page.layout_id = _default_layout_id(style, page.line_count)


def page_plan_signature(track: TimingTrack) -> tuple[object, ...]:
    if track.page_plan is None:
        return ()
    return tuple(
        (
            tuple((int(page.line_count), str(page.layout_id)) for page in section.pages),
        )
        for section in track.page_plan.sections
    )


def page_plan_has_manual_changes(track: TimingTrack, style: Style) -> bool:
    """Derive whether refresh would replace page/layout edits.

    No manual/source flag is persisted.  Legacy projection fields are cleared
    on a temporary copy so they cannot make every current boundary look
    source-owned.
    """

    if track.page_plan is None:
        return False
    candidate = deepcopy(track)
    for line in candidate.lines:
        line.break_before = "none"
        line.layout_index = 0
    candidate.page_plan = build_page_plan(
        candidate,
        track.loading_settings_snapshot,
        style,
    )
    return page_plan_signature(track) != page_plan_signature(candidate)
