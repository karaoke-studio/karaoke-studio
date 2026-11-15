"""Prepare canonical geometry and feed the existing shared page/layout policy.

Ruby is supported at this preparation boundary. The product IR entry keeps its
own drawing feature gates until the final ruby glyph/clip protocol is complete.
"""
from dataclasses import dataclass

from ..layout_context import layout_pass
from ..display.section_edges import section_edge_context
from ..display.skia_collision import SkiaCollisionGeometry
from ..display.skia_schedule import display_lines
from ..line.style import style_for_line
from ...ruby.skia_geometry import prepare_ruby_geometry
from ...text.skia_metrics import MeasuredTrack, measure_track
from .model import TrackLayoutPlan
from .page_offsets import PageOffsetResolvers, resolve_page_offset_windows
from .semantic import LayoutPlanResolvers, build_track_layout_plan


@dataclass(frozen=True)
class PreparedSkiaLayout:
    plan: TrackLayoutPlan
    measured: MeasuredTrack
    geometry: SkiaCollisionGeometry


def prepare_skia_layout(track, style, *, measure, resolve_font, **plan_kwargs):
    with layout_pass():
        section_edge_context(track, style)
        styles = [style_for_line(style, line) for line in track.lines]
        measured = measure_track(track, style, styles, measure=measure, resolve_font=resolve_font)
        ruby = (prepare_ruby_geometry(track, style, styles, measured, measure=measure, resolve_font=resolve_font)
                if track.rubies else None)
        geometry = SkiaCollisionGeometry(track, style, styles, measured, ruby_geometry=ruby)

        def display(source, appearance, **size):
            return display_lines(source, appearance, geometry, **size)

        def offsets(width, height, source, appearance):
            return resolve_page_offset_windows(width, height, source, appearance,
                PageOffsetResolvers(display, geometry.page_lines), metrics_identity=geometry.identity)

        ports = LayoutPlanResolvers(display, offsets, measured.snapshot.resolve_intervals, geometry.identity)
        plan = build_track_layout_plan(track, style, ports, **plan_kwargs)
        geometry.set_lanes(plan.lines)
        return PreparedSkiaLayout(plan, measured, geometry)
