"""Prepare canonical geometry and feed the existing shared page/layout policy.

Ruby is supported at this preparation boundary. The product IR entry controls
which drawing profiles may consume the prepared geometry and clocks.
"""
from dataclasses import dataclass

from ..layout_context import layout_pass
from ..display.section_edges import section_edge_context
from ..display.skia_collision import SkiaCollisionGeometry
from ..display.skia_schedule import display_lines
from ..line.style import style_for_line
from ...ruby.skia_geometry import prepare_ruby_geometry
from ..line.skia_vertical import validate_vertical_columns
from ...text.skia_metrics import MeasuredTrack, measure_track
from ...text.skia_batch import font_snapshot
from .model import TrackLayoutPlan
from .page_offsets import PageOffsetResolvers, resolve_page_offset_windows
from .semantic import LayoutPlanResolvers, build_track_layout_plan
from dataclasses import replace
from krok_helper.subtitle_render.engine.guide.semantics import render_line_with_guide_symbols


@dataclass(frozen=True)
class PreparedSkiaLayout:
    plan: TrackLayoutPlan
    measured: MeasuredTrack
    geometry: SkiaCollisionGeometry


def prepare_skia_layout(track, style, *, measure, resolve_font, **plan_kwargs):
    with layout_pass(), font_snapshot(resolve_font):
        section_edge_context(track, style)
        styles = [style_for_line(style, line) for line in track.lines]
        validate_vertical_columns(track, style, styles)
        # Measure the same guide-expanded lines the shared plan lays out.
        rendered = replace(track, lines=[render_line_with_guide_symbols(line) for line in track.lines])
        measured = measure_track(rendered, style, styles, measure=measure, resolve_font=resolve_font)
        ruby = (prepare_ruby_geometry(track, style, styles, measured, rendered_lines=rendered.lines,
                                      measure=measure, resolve_font=resolve_font)
                if not style.vertical and (track.rubies or any(s.lower_ruby_enabled for s in styles)) else None)
        geometry = SkiaCollisionGeometry(track, style, styles, measured, ruby_geometry=ruby)

        def display(source, appearance, **size):
            return display_lines(source, appearance, geometry, **size)

        def offsets(width, height, source, appearance):
            return resolve_page_offset_windows(width, height, source, appearance,
                PageOffsetResolvers(display, geometry.page_lines), metrics_identity=geometry.identity)

        ports = LayoutPlanResolvers(display, offsets, measured.snapshot.resolve_intervals, geometry.identity,
                                    respect_page_plan=True)
        plan = build_track_layout_plan(track, style, ports, **plan_kwargs)
        geometry.set_lanes(plan.lines)
        return PreparedSkiaLayout(plan, measured, geometry)
