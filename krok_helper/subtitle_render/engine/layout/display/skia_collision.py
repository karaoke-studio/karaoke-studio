"""Configuration-scoped canonical geometry adapter for shared page policies."""
from types import SimpleNamespace
from dataclasses import replace
from .resolver import CollisionLineGeometry, build_measured_collision_bands
from .schedule import collision_time_window_name, display_line_collision_time_window
from ..line.skia_geometry import local_line_geometry, line_position, collision_bounds
from ..page.pagination import line_center_override
from ..page.skia_smart_horizontal import SmartHorizontalPages
from ..plan.page_offsets import MeasuredPageLine
from ...render_progress import report_display_measure_progress, report_render_progress
from ...ruby.skia_geometry import ruby_line_position


class SkiaCollisionGeometry:
    def __init__(self, track, style, styles, measured, *, ruby_geometry=None):
        self.track, self.style, self.styles, self.measured = track, style, tuple(styles), measured
        self.ruby = ruby_geometry
        if ruby_geometry is not None and (ruby_geometry.main_identity != measured.identity
                or len(ruby_geometry.lines) != len(track.lines)):
            raise ValueError('Skia 注音几何与正文度量不一致或已过期')
        if self.ruby is not None and any(tuple(measured.snapshot.resolve_intervals(line, style)) != ruby.intervals
                for line, ruby in zip(track.lines, self.ruby.lines, strict=True)):
            raise ValueError('Skia 注音几何与正文时间区间不一致或已过期')
        self.identity = (('skia', measured.identity) if self.ruby is None
                         else ('skia-ruby', measured.identity, self.ruby.identity))
        self.indices = {id(line): i for i, line in enumerate(track.lines)}
        self.locals = (tuple(line.main for line in self.ruby.lines) if self.ruby is not None
                       else tuple(local_line_geometry(m, s) for m, s in zip(measured.lines, styles, strict=True)))
        self.smart = SmartHorizontalPages(track, style, styles, self.locals,
            anchors=tuple(line.anchor for line in self.ruby.lines) if self.ruby is not None else None)
        self.cache = {}

    def set_lanes(self, items):
        if self.smart.set_lanes(items):
            self.cache.clear()

    def position(self, item, width, height):
        i = self.indices[id(item.line)]
        position = (ruby_line_position(item, self.measured.lines[i], self.ruby.lines[i], width, height)
                    if self.ruby is not None else line_position(item, self.measured.lines[i], self.locals[i], width, height))
        return replace(position, offset_x=position.offset_x + self.smart.shift(item, width))

    def geometry(self, item, width, height):
        i = self.indices[id(item.line)]
        key = (width, height, i, item.lane, item.page_line_count)
        if key not in self.cache:
            style = self.styles[i]
            placement = SimpleNamespace(line=item.line, layout_style=style, lane=item.lane, page_line_count=item.page_line_count,
                center_override=line_center_override(self.track, item.line, style, respect_page_plan=True))
            position = self.position(placement, width, height)
            bounds = collision_bounds(self.locals[i], position)
            self.cache[key] = None if bounds is None else CollisionLineGeometry(
                bounds[1], bounds[3], bounds[0], bounds[2], position.baseline_y, float(style.line_gap_px))
        return self.cache[key]

    def measure(self, width, height, items, *, time_window=None):
        self.set_lanes(items)
        geometries = []
        for index, item in enumerate(items):
            report_display_measure_progress(index, len(items))
            geometries.append(self.geometry(item, width, height))
        report_display_measure_progress(len(items), len(items))
        return build_measured_collision_bands(items, self.style, geometries, time_window=time_window)

    def page_lines(self, width, height, track, style, items):
        self.set_lanes(items)
        measured = []
        for index, item in enumerate(items):
            report_render_progress('page_offsets', index, len(items))
            geometry = self.geometry(item, width, height)
            start, end = display_line_collision_time_window(item, style, time_window=collision_time_window_name(style))
            measured.append(MeasuredPageLine(
                track_index=self.indices[id(item.line)], page_id=(item.section_index, item.page_index),
                display_start_ms=item.display_start_ms, display_end_ms=item.display_end_ms,
                page_style=self.styles[self.indices[id(item.line)]],
                collision_start_ms=start if geometry else None, collision_end_ms=end if geometry else None,
                axis_bounds=(geometry.axis_min, geometry.axis_max) if geometry else None,
                cross_bounds=(geometry.cross_min, geometry.cross_max) if geometry else None,
                axis_anchor=geometry.axis_anchor if geometry else None))
        report_render_progress('page_offsets', len(items), len(items))
        return measured
