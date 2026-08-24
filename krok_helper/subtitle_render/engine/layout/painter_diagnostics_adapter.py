"""Bind layout diagnostic policies to Painter geometry and scheduling ports."""

import krok_helper.subtitle_render.engine.painter as painter_impl
from krok_helper.subtitle_render.engine.layout.display_schedule import (
    apply_constrained_page_sync,
)
from krok_helper.subtitle_render.engine.layout.layout_diagnostics import (
    LayoutMarginBox,
    LayoutMarginPorts,
    LayoutMarginWarning,
    LayoutTimingDiagnostic,
    TimingCollisionAdjustment,
    build_force_bottom_diagnostics,
    build_page_shift_diagnostics,
    build_timing_window_diagnostics,
    resolve_layout_margin_warnings,
)
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingTrack


display_windows_for_style = painter_impl.display_windows_for_style


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


def layout_timing_diagnostics_for_style(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
) -> list[LayoutTimingDiagnostic]:
    """Bind Painter measurements to layout-owned diagnostic policies."""

    if not style.dual_line_layout:
        return []
    # CPU and GPU both consume the layout plan built from this effective
    # signal-window style. Diagnostics must inspect the same display window.
    style = painter_impl._display_style_for_signal_window(style)
    collision_window_label = (
        "稳定主文字行盒"
        if style.allow_entry_exit_animation_overlap
        else "完整显示行盒"
    )
    logical_w = max(int(logical_w), 1)
    logical_h = max(int(logical_h), 1)
    base_kwargs = {
        **painter_impl._display_line_compute_kwargs(style),
        "sync_entry": False,
        "sync_ending": False,
        "auto_fill_section_time": False,
    }
    signal_heads = painter_impl._signal_head_context(track, style)
    if signal_heads is not None:
        base_kwargs["signal_head_indexes"] = signal_heads
        base_kwargs["signal_lead_ms"] = painter_impl._signal_lead_in_ms(style)
    ideal = painter_impl.compute_display_lines(
        track,
        **base_kwargs,
        adjust_same_position=False,
        dynamic_single_page_reflow=False,
        independent_line_entry=True,
    )
    synchronized = apply_constrained_page_sync(ideal, style)
    animation_candidate = painter_impl._apply_animation_time_guard(
        logical_w,
        logical_h,
        track,
        style,
        synchronized,
        enforce_inter_page_gap=False,
    )
    adjustments: list[TimingCollisionAdjustment] = []
    collision_guarded = painter_impl._apply_animation_time_guard(
        logical_w,
        logical_h,
        track,
        style,
        synchronized,
        enforce_inter_page_gap=not style.allow_inter_page_line_overlap,
        adjustments=adjustments,
    )
    final = painter_impl._display_lines_for_style(
        track,
        style,
        logical_w=logical_w,
        logical_h=logical_h,
    )
    diagnostics = build_timing_window_diagnostics(
        track,
        style,
        ideal=ideal,
        synchronized=synchronized,
        animation_candidate=animation_candidate,
        final=final,
        adjustments=adjustments,
        entry_animation_ms_of=painter_impl._entry_animation_ms,
        auto_exit_reserve_ms_of=painter_impl._auto_exit_reserve_ms,
    )
    guarded_measurements = painter_impl._measure_collision_bands(
        logical_w,
        logical_h,
        track,
        style,
        collision_guarded,
    )
    diagnostics.extend(
        build_force_bottom_diagnostics(
            track,
            collision_window_label=collision_window_label,
            before=collision_guarded,
            after=final,
            measured=guarded_measurements,
            collision_pairs=painter_impl._pixel_collision_squeeze_pairs(
                logical_w,
                logical_h,
                track,
                style,
                collision_guarded,
            ),
        )
    )

    offset_windows = painter_impl.resolved_page_offset_windows_for_style(
        logical_w,
        logical_h,
        track,
        style,
    )
    track_index_of = {id(line): index for index, line in enumerate(track.lines)}
    page_offsets: dict[tuple[int, int], float] = {}
    for item in final:
        track_index = track_index_of.get(id(item.line))
        if track_index is None:
            continue
        windows = offset_windows.get(track_index, ())
        offset = (
            float(windows[0][2] if style.vertical else windows[0][3])
            if windows
            else 0.0
        )
        page_offsets[(int(item.section_index), int(item.page_index))] = offset

    measured = painter_impl._measure_collision_bands(
        logical_w,
        logical_h,
        track,
        style,
        final,
    )
    diagnostics.extend(
        build_page_shift_diagnostics(
            track,
            style,
            collision_window_label=collision_window_label,
            synchronized=synchronized,
            measured=measured,
            page_offsets=page_offsets,
        )
    )
    return diagnostics

__all__ = [
    "check_layout_margins",
    "display_windows_for_style",
    "layout_timing_diagnostics_for_style",
]
