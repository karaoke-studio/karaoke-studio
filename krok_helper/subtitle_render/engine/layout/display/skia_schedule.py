"""Skia's initial multi-row timing adapter; geometry avoidance stays gated.

The shared display resolver still owns lead/tail, page synchronization, short
pages and show-time reflow. No separate pager or timing rules are implemented.
"""
from .resolver import (
    AnimationGuardPorts, DisplayResolutionPorts, StyleDisplayResolutionPorts,
    apply_animation_time_guard, display_line_compute_kwargs, enforce_page_exit_order,
    resolve_display_lines_for_style, resolve_display_timing,
)
from ..line.style import entry_animation_ms, exit_animation_ms
from ...timing.timeline import compute_display_lines


def unsupported_geometry(*args, **kwargs):
    raise ValueError('Skia 多行布局暂不支持跨页避让或段内填充，请启用允许跨页重叠并关闭段内填充')


def display_lines(track, style, *, logical_w=None, logical_h=None):
    if not style.allow_inter_page_line_overlap or style.auto_fill_section_time:
        unsupported_geometry()

    def build(width, height, compute_kwargs):
        guard = AnimationGuardPorts(
            lambda line: entry_animation_ms(style, line),
            lambda line: exit_animation_ms(style, line),
            unsupported_geometry, unsupported_geometry,
        )
        return DisplayResolutionPorts(
            compute=lambda **overrides: compute_display_lines(track, **compute_kwargs, **overrides),
            resolve_timing=lambda items, enforce_gap: resolve_display_timing(
                style, items, guard, enforce_inter_page_gap=enforce_gap),
            collision_pairs=unsupported_geometry, secondary_collision_pairs=unsupported_geometry,
            fill_section_time=unsupported_geometry,
            apply_animation_guard=lambda items, enforce_gap: apply_animation_time_guard(
                style, items, guard, enforce_inter_page_gap=enforce_gap),
            clamp_page_exit_order=lambda items: enforce_page_exit_order(style=style, display_lines=items),
        )

    return resolve_display_lines_for_style(track, style, display_line_compute_kwargs(style),
        StyleDisplayResolutionPorts(build), logical_w=logical_w, logical_h=logical_h)
