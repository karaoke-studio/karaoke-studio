"""Temporary adapter from layout diagnostics consumers to Painter geometry."""

import krok_helper.subtitle_render.engine.painter as painter_impl
from krok_helper.subtitle_render.engine.layout.layout_diagnostics import (
    LayoutMarginBox,
    LayoutMarginPorts,
    LayoutMarginWarning,
    resolve_layout_margin_warnings,
)
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingTrack


display_windows_for_style = painter_impl.display_windows_for_style
layout_timing_diagnostics_for_style = painter_impl.layout_timing_diagnostics_for_style


def check_layout_margins(
    track: TimingTrack,
    style: Style,
    img_w: int,
) -> list[LayoutMarginWarning]:
    """Bind Painter measurement operations to the layout-owned margin policy."""

    def measure_line(
        source_track: TimingTrack,
        source_style: Style,
        display_line: DisplayLine,
        width: int,
    ) -> LayoutMarginBox:
        line = display_line.line
        line_style = painter_impl._style_for_line(source_style, line)
        total_w = painter_impl._line_total_width(
            line,
            line_style,
            source_track.rubies,
        )
        lane = display_line.lane if line_style.dual_line_layout else None
        x0 = painter_impl._resolve_line_x_smart(
            width,
            total_w,
            source_track,
            line,
            line_style,
            lane,
            center_override=painter_impl._line_center_override(
                source_track,
                line,
                line_style,
            ),
        )
        return LayoutMarginBox(
            left=x0,
            right=x0 + total_w,
            margin_left=line_style.horizontal_margin_px,
            margin_right=line_style.horizontal_margin_px,
        )

    ports = LayoutMarginPorts(
        resolve_display_lines=lambda source_track, source_style, width: (
            painter_impl._display_lines_for_style(
                source_track,
                source_style,
                logical_w=width,
            )
        ),
        measure_line=measure_line,
    )
    return resolve_layout_margin_warnings(track, style, img_w, ports=ports)

__all__ = [
    "check_layout_margins",
    "display_windows_for_style",
    "layout_timing_diagnostics_for_style",
]
