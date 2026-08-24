"""Display-line resolution orchestration independent from concrete rendering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine


DisplayLines = list[DisplayLine]
CollisionPairs = tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class DisplayResolutionPorts:
    """Concrete geometry and timing operations required by the resolver."""

    compute: Callable[..., DisplayLines]
    resolve_timing: Callable[[DisplayLines, bool], DisplayLines]
    collision_pairs: Callable[[DisplayLines], CollisionPairs]
    secondary_collision_pairs: Callable[[DisplayLines], CollisionPairs]
    fill_section_time: Callable[[DisplayLines], DisplayLines]
    apply_animation_guard: Callable[[DisplayLines, bool], DisplayLines]


def resolve_display_lines(
    *,
    avoid_collisions: bool,
    auto_fill_section_time: bool,
    ports: DisplayResolutionPorts,
) -> DisplayLines:
    """Run the stable multi-pass display-line resolution policy.

    The caller supplies rendering-specific measurement operations.  This module
    owns only the ordering and data flow between those operations, so layout
    policy no longer depends on the Painter implementation.
    """

    ideal = ports.compute(
        adjust_same_position=False,
        dynamic_single_page_reflow=not avoid_collisions,
        independent_line_entry=True,
    )
    resolved = ideal
    timing_resolved = False
    if avoid_collisions:
        # ForceBottom inspects the schedule only after automatic timing has
        # separated pages; measuring the raw lead/tail windows latches stale
        # spatial reflow decisions.
        resolved = ports.resolve_timing(resolved, True)
        timing_resolved = True
        force_bottom_pairs = ports.collision_pairs(resolved)
        if force_bottom_pairs:
            resolved = ports.compute(
                adjust_same_position=False,
                force_bottom_pairs=force_bottom_pairs,
                dynamic_single_page_reflow=True,
                independent_line_entry=True,
            )
            resolved = ports.resolve_timing(resolved, True)
        squeeze_pairs = ports.collision_pairs(resolved)
        if squeeze_pairs:
            resolved = ports.compute(
                adjust_same_position=False,
                squeeze_pairs=squeeze_pairs,
                force_bottom_pairs=force_bottom_pairs,
                dynamic_single_page_reflow=True,
                independent_line_entry=True,
            )
            resolved = ports.resolve_timing(resolved, True)
        secondary_pairs = ports.secondary_collision_pairs(resolved)
        if secondary_pairs:
            combined_pairs = tuple(dict.fromkeys((*squeeze_pairs, *secondary_pairs)))
            resolved = ports.compute(
                adjust_same_position=False,
                squeeze_pairs=combined_pairs,
                force_bottom_pairs=force_bottom_pairs,
                dynamic_single_page_reflow=True,
                independent_line_entry=True,
            )
            timing_resolved = False
    if not timing_resolved:
        resolved = ports.resolve_timing(resolved, avoid_collisions)
    # Geometry-dependent section filling is the final layout pass.  It may
    # extend windows, so restore the animation guard without changing geometry.
    if auto_fill_section_time:
        filled = ports.fill_section_time(resolved)
        if filled != resolved:
            resolved = ports.apply_animation_guard(filled, avoid_collisions)
    return resolved


__all__ = ["DisplayResolutionPorts", "resolve_display_lines"]
