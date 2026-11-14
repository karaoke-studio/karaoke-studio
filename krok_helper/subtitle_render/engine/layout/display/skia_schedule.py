"""Canonical geometry ports for the existing multi-row display policy.

The shared display resolver still owns lead/tail, page synchronization, short
pages and show-time reflow. No separate pager or timing rules are implemented.
"""
from .resolver import (
    AnimationGuardPorts, DisplayResolutionPorts, StyleDisplayResolutionPorts,
    apply_animation_time_guard, display_line_compute_kwargs, enforce_page_exit_order,
    resolve_display_lines_for_style, resolve_display_timing,
    collision_squeeze_pairs, secondary_displacement_squeeze_pairs,
    fill_section_time_from_measurements, retime_measured_collision_bands,
)
from .schedule import collision_time_window_name
from ..line.style import entry_animation_ms, exit_animation_ms
from ...timing.timeline import compute_display_lines


def display_lines(track, style, geometry, *, logical_w=None, logical_h=None):

    def build(width, height, compute_kwargs):
        def measure(items, time_window=None):
            return geometry.measure(width, height, items, time_window=time_window)
        guard = AnimationGuardPorts(
            lambda line: entry_animation_ms(style, line),
            lambda line: exit_animation_ms(style, line),
            measure, lambda measured, items, indices, window: retime_measured_collision_bands(
                measured, items, style, indices, time_window=window),
        )
        return DisplayResolutionPorts(
            compute=lambda **overrides: compute_display_lines(track, **compute_kwargs, **overrides),
            resolve_timing=lambda items, enforce_gap: resolve_display_timing(
                style, items, guard, enforce_inter_page_gap=enforce_gap),
            collision_pairs=lambda items: collision_squeeze_pairs(measure(items)),
            secondary_collision_pairs=lambda items: secondary_displacement_squeeze_pairs(
                measure(items), items, style, viewport_max=float(height)),
            fill_section_time=lambda items: fill_section_time_from_measurements(
                items, style, measure(items, collision_time_window_name(style)),
                time_window=collision_time_window_name(style)),
            apply_animation_guard=lambda items, enforce_gap: apply_animation_time_guard(
                style, items, guard, enforce_inter_page_gap=enforce_gap),
            clamp_page_exit_order=lambda items: enforce_page_exit_order(
                style=style, display_lines=items, time_window=collision_time_window_name(style)),
        )

    return resolve_display_lines_for_style(track, style, display_line_compute_kwargs(style),
        StyleDisplayResolutionPorts(build), logical_w=logical_w, logical_h=logical_h,
        metrics_identity=geometry.identity)
