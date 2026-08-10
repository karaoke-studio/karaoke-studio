"""Shared immutable layout-plan transport for Painter and GPU consumers."""

from __future__ import annotations

from dataclasses import dataclass

from krok_helper.subtitle_render.models import Style, TimingLine


LayoutOffsetWindow = tuple[int, int, float, float]


@dataclass(frozen=True)
class LineLayoutPlan:
    """Resolved, frame-independent semantics for one source timing line."""

    track_index: int
    line: TimingLine
    render_line: TimingLine
    layout_style: Style
    animation_style: Style
    resolved_intervals: tuple[tuple[int, int], ...]
    guide_anchor_bounds: tuple[int, int] | None
    page_index: int = -1
    page_line_count: int = 0
    section_index: int = -1
    display_page_index: int = -1
    display_page_line_count: int = 0
    display_section_index: int = -1
    lane: int = 0
    layout_lane: int = 0
    display_start_ms: int | None = None
    display_end_ms: int | None = None
    center_override: bool = False
    layout_offset_windows: tuple[LayoutOffsetWindow, ...] = ()


@dataclass(frozen=True)
class TrackLayoutPlan:
    """Complete resolved line plan shared by one CPU/GPU layout pass."""

    layout_semantics: str
    logical_width: int | None
    logical_height: int | None
    lines: tuple[LineLayoutPlan, ...]

    def line(self, track_index: int) -> LineLayoutPlan:
        return self.lines[track_index]
