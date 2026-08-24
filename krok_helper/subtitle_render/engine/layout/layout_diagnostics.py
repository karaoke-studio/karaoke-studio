"""Public layout diagnostics contracts consumed by the subtitle editor UI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.layout.line_style import (
    line_end_ms,
    line_start_ms,
)
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import (
    TimingLine,
    TimingTrack,
    line_visible_chars,
)


@dataclass(frozen=True)
class LayoutTimingDiagnostic:
    """One user-facing explanation emitted by the timing/layout solver."""

    kind: str
    line_indices: tuple[int, ...]
    title: str
    summary: str
    detail: str


@dataclass(frozen=True)
class TimingCollisionAdjustment:
    """One automatic display-boundary adjustment made to avoid a collision."""

    previous_index: int
    incoming_index: int
    boundary: str
    before_ms: int
    after_ms: int


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


def format_diagnostic_ms(value: int) -> str:
    total = max(int(value), 0)
    return f"{total // 60_000:02d}:{(total % 60_000) // 1_000:02d}.{total % 1_000:03d}"


def diagnostic_line_text(line: TimingLine) -> str:
    return " ".join("".join(char.text for char in line.chars).split()) or "（空歌词）"


def build_timing_window_diagnostics(
    track: TimingTrack,
    style: Style,
    *,
    ideal: list[DisplayLine],
    synchronized: list[DisplayLine],
    animation_candidate: list[DisplayLine],
    final: list[DisplayLine],
    adjustments: list[TimingCollisionAdjustment],
    entry_animation_ms_of: Callable[[Style, TimingLine], int],
    auto_exit_reserve_ms_of: Callable[[Style, TimingLine], int],
) -> list[LayoutTimingDiagnostic]:
    """Explain automatic entry/exit compression from resolved timing snapshots."""

    track_index_of = {id(line): index for index, line in enumerate(track.lines)}
    final_by_track = {
        track_index_of[id(item.line)]: item
        for item in final
        if id(item.line) in track_index_of
    }
    ideal_by_track = {
        track_index_of[id(item.line)]: item
        for item in ideal
        if id(item.line) in track_index_of
    }
    sync_by_track = {
        track_index_of[id(item.line)]: item
        for item in synchronized
        if id(item.line) in track_index_of
    }
    animation_by_track = {
        track_index_of[id(item.line)]: item
        for item in animation_candidate
        if id(item.line) in track_index_of
    }
    render_to_track = {
        render_index: track_index_of[id(item.line)]
        for render_index, item in enumerate(synchronized)
        if id(item.line) in track_index_of
    }
    actions_by_track: dict[int, list[str]] = {}
    for action in adjustments:
        previous = render_to_track.get(action.previous_index)
        incoming = render_to_track.get(action.incoming_index)
        if previous is None or incoming is None:
            continue
        previous_text = diagnostic_line_text(track.lines[previous])
        incoming_text = diagnostic_line_text(track.lines[incoming])
        if action.boundary == "exit":
            affected = previous
            final_item = final_by_track.get(affected)
            if (
                final_item is None
                or int(final_item.display_end_ms) >= int(action.before_ms)
            ):
                continue
            final_boundary = int(final_item.display_end_ms)
            message = (
                f"与第 {incoming + 1} 行「{incoming_text}」发生行盒时间碰撞，"
                f"按优先级先压缩本行退场：{format_diagnostic_ms(action.before_ms)}"
                f" → {format_diagnostic_ms(final_boundary)}。"
            )
        else:
            affected = incoming
            final_item = final_by_track.get(affected)
            if (
                final_item is None
                or int(final_item.display_start_ms) <= int(action.before_ms)
            ):
                continue
            final_boundary = int(final_item.display_start_ms)
            message = (
                f"第 {previous + 1} 行「{previous_text}」的退场已无法继续压缩，"
                f"因此推迟本行入场：{format_diagnostic_ms(action.before_ms)}"
                f" → {format_diagnostic_ms(final_boundary)}。"
            )
        actions_by_track.setdefault(affected, []).append(message)

    diagnostics: list[LayoutTimingDiagnostic] = []
    for track_index, item in final_by_track.items():
        ideal_item = ideal_by_track.get(track_index)
        sync_item = sync_by_track.get(track_index)
        animation_item = animation_by_track.get(track_index)
        if ideal_item is None or sync_item is None or animation_item is None:
            continue
        compressed_entry = int(item.display_start_ms) > int(
            animation_item.display_start_ms
        )
        compressed_exit = int(item.display_end_ms) < int(
            animation_item.display_end_ms
        )
        if not (compressed_entry or compressed_exit or track_index in actions_by_track):
            continue
        line = item.line
        text = diagnostic_line_text(line)
        changes = []
        if compressed_entry:
            changes.append("入场被推迟")
        if compressed_exit:
            changes.append("退场被提前")
        detail_lines = [
            f"第 {track_index + 1} 行「{text}」",
            f"演唱区间：{format_diagnostic_ms(line_start_ms(line))} – "
            f"{format_diagnostic_ms(line_end_ms(line))}",
            "N3 初始排期窗口（已受换页约束）："
            f"{format_diagnostic_ms(ideal_item.display_start_ms)} – "
            f"{format_diagnostic_ms(ideal_item.display_end_ms)}",
            f"同步最长候选：{format_diagnostic_ms(sync_item.display_start_ms)} – "
            f"{format_diagnostic_ms(sync_item.display_end_ms)}",
            f"保留完整动画后：{format_diagnostic_ms(animation_item.display_start_ms)} – "
            f"{format_diagnostic_ms(animation_item.display_end_ms)}",
            f"最终消费窗口：{format_diagnostic_ms(item.display_start_ms)} – "
            f"{format_diagnostic_ms(item.display_end_ms)}",
            f"动画保护：入场 {entry_animation_ms_of(style, line)} ms；"
            f"退场至少 {auto_exit_reserve_ms_of(style, line)} ms",
            f"自动窗口参数：提前 {max(int(style.line_lead_in_ms), 0)} ms；"
            f"尾留 {max(int(style.line_tail_ms), 0)} ms（上限，换页时可压缩稳定停留段）",
            f"手动覆盖：入场 {line.display_start_override_ms if line.display_start_override_ms is not None else '无'}；"
            f"退场 {line.display_end_override_ms if line.display_end_override_ms is not None else '无'}",
        ]
        detail_lines.extend(actions_by_track.get(track_index, ()))
        diagnostics.append(
            LayoutTimingDiagnostic(
                kind="timing",
                line_indices=(track_index,),
                title="时间窗口自动压缩",
                summary=f"第 {track_index + 1} 行「{text}」：{'、'.join(changes) or '窗口已调整'}",
                detail="\n".join(detail_lines),
            )
        )
    return diagnostics


__all__ = [
    "TimingCollisionAdjustment",
    "build_timing_window_diagnostics",
    "diagnostic_line_text",
    "format_diagnostic_ms",
    "LayoutMarginBox",
    "LayoutMarginPorts",
    "LayoutMarginWarning",
    "LayoutTimingDiagnostic",
    "layout_pass",
    "resolve_layout_margin_warnings",
]
