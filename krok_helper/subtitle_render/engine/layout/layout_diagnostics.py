"""Public layout diagnostics contracts consumed by the subtitle editor UI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingTrack, line_visible_chars


@dataclass(frozen=True)
class LayoutTimingDiagnostic:
    """One user-facing explanation emitted by the timing/layout solver."""

    kind: str
    line_indices: tuple[int, ...]
    title: str
    summary: str
    detail: str


@dataclass(frozen=True)
class LayoutMarginWarning:
    """One line whose main-text box violates the configured viewport margin."""

    line_index: int
    text: str
    level: str
    left: int
    right: int


@dataclass(frozen=True)
class LayoutMarginBox:
    """Measured horizontal bounds and authored margins for one display line."""

    left: int
    right: int
    margin_left: int
    margin_right: int


@dataclass(frozen=True)
class LayoutMarginPorts:
    """Painter measurements required by the layout-owned margin policy."""

    resolve_display_lines: Callable[[TimingTrack, Style, int], list[DisplayLine]]
    measure_line: Callable[[TimingTrack, Style, DisplayLine, int], LayoutMarginBox]


def resolve_layout_margin_warnings(
    track: TimingTrack,
    style: Style,
    img_w: int,
    *,
    ports: LayoutMarginPorts,
) -> list[LayoutMarginWarning]:
    """Classify measured line boxes as viewport overflow or margin intrusion."""

    if style.vertical or not track.lines:
        return []
    if style.dual_line_layout:
        display_lines = ports.resolve_display_lines(track, style, img_w)
    else:
        display_lines = [
            DisplayLine(line=line, lane=0, display_start_ms=0, display_end_ms=0)
            for line in track.lines
            if not line.is_blank and line.chars
        ]
    line_indices = {id(line): index for index, line in enumerate(track.lines)}
    warnings: list[LayoutMarginWarning] = []
    for display_line in display_lines:
        line = display_line.line
        if not line.chars:
            continue
        box = ports.measure_line(track, style, display_line, img_w)
        if box.left < 0 or box.right > img_w:
            level = "overflow"
        elif box.left < box.margin_left or box.right > img_w - box.margin_right:
            level = "margin"
        else:
            continue
        warnings.append(
            LayoutMarginWarning(
                line_index=line_indices.get(id(line), -1),
                text="".join(ch.text for ch in line_visible_chars(line)),
                level=level,
                left=box.left,
                right=box.right,
            )
        )
    return warnings


__all__ = [
    "LayoutMarginBox",
    "LayoutMarginPorts",
    "LayoutMarginWarning",
    "LayoutTimingDiagnostic",
    "layout_pass",
    "resolve_layout_margin_warnings",
]
