"""Configuration-scoped canonical geometry adapter for shared page policies."""
from types import SimpleNamespace
from dataclasses import replace
from .resolver import CollisionLineGeometry, build_measured_collision_bands
from .schedule import collision_time_window_name, display_line_collision_time_window
from .signal import volume_signal_head_context
from ..line.skia_geometry import local_line_geometry, line_position, collision_bounds
from ..line.skia_vertical import vertical_line_box
from ..page.pagination import line_center_override
from ..page.skia_smart_horizontal import SmartHorizontalPages
from ..plan.page_offsets import MeasuredPageLine
from ...render_progress import report_display_measure_progress, report_render_progress
from ...guide.semantics import render_line_with_guide_symbols
from ...ruby.skia_geometry import ruby_line_position
from ...style.style_semantics import auto_appearance_basis
from ....domain.models import resolve_volume_appearance


class SkiaCollisionGeometry:
    def __init__(self, track, style, styles, measured, *, ruby_geometry=None):
        self.track, self.style, self.styles, self.measured = track, style, tuple(styles), measured
        self.ruby = ruby_geometry
        if ruby_geometry is not None and (ruby_geometry.main_identity != measured.identity
                or len(ruby_geometry.lines) != len(track.lines)):
            raise ValueError('Skia 注音几何与正文度量不一致或已过期')
        # Ruby clocks belong to the measured guide-expanded cells.
        if self.ruby is not None and any(
                tuple(measured.snapshot.resolve_intervals(render_line_with_guide_symbols(line), style)) != ruby.intervals
                for line, ruby in zip(track.lines, self.ruby.lines, strict=True)):
            raise ValueError('Skia 注音几何与正文时间区间不一致或已过期')
        self.identity = (('skia', measured.identity) if self.ruby is None
                         else ('skia-ruby', measured.identity, self.ruby.identity))
        self.indices = {id(line): i for i, line in enumerate(track.lines)}
        self.locals = (tuple(line.main for line in self.ruby.lines) if self.ruby is not None
                       else tuple(local_line_geometry(m, s) for m, s in zip(measured.lines, styles, strict=True)))
        heads = volume_signal_head_context(track, style) or frozenset()
        basis = auto_appearance_basis(style, track) if heads else None
        self.volume_widths = tuple(self._volume_width(resolve_volume_appearance(s, auto_basis=basis)) if i in heads else 0
                                   for i, s in enumerate(self.styles))
        anchors = None
        if any(self.volume_widths):
            anchors = tuple(self._horizontal_anchor(i) for i in range(len(self.locals)))
            self.identity += (self.volume_widths,)
        elif self.ruby is not None:
            anchors = tuple(line.anchor for line in self.ruby.lines)
        self.smart = SmartHorizontalPages(track, style, styles, self.locals,
            anchors=anchors)
        self.cache = {}

    @staticmethod
    def _volume_width(style):
        independent = style.volume_enabled
        if style.vertical or not (independent or (style.lit_enabled and style.lit_style == 'volume')):
            return 0
        duration = style.volume_duration_ms if independent else style.signals_duration_ms
        waiting = style.volume_waiting_time_ms if independent else style.lit_waiting_time_ms
        if duration <= max(waiting, 0):
            return 0
        count = min(max(int(style.volume_column_count), 1), 16)
        spacing = max(int(style.volume_column_spacing), 0)
        stroke = style.volume_stroke_width if independent else style.lit_stroke_width
        # Native volumeSignalGeometry reserves the pen on each side of a column.
        pitch = max(int(style.volume_column_width), 1) + spacing + 2 * max(stroke, 0)
        return count * pitch - spacing

    def _horizontal_anchor(self, index):
        local = self.locals[index]
        if self.ruby is not None:
            left, right = self.ruby.lines[index].anchor
        elif self.styles[index].layout_semantics == 'n3_1074' or local.ink is None:
            left, right = 0, max(local.layout_width, 1)
        else:
            left, _, right, _ = local.ink
        width = self.volume_widths[index]
        return (min(left, -width), max(right, 0)) if width else (left, right)

    def set_lanes(self, items):
        if self.smart.set_lanes(items):
            self.cache.clear()

    def position(self, item, width, height):
        i = self.indices[id(item.line)]
        position = (ruby_line_position(item, self.measured.lines[i], self.ruby.lines[i], width, height,
                                      inline_width=self.volume_widths[i])
                    if self.ruby is not None else line_position(item, self.measured.lines[i], self.locals[i], width,
                                                               height, inline_width=self.volume_widths[i]))
        return replace(position, offset_x=position.offset_x + self.smart.shift(item, width))

    def geometry(self, item, width, height):
        i = self.indices[id(item.line)]
        key = (width, height, i, item.lane, item.page_line_count)
        if key not in self.cache:
            style = self.styles[i]
            placement = SimpleNamespace(line=item.line, layout_style=style, lane=item.lane, page_line_count=item.page_line_count,
                center_override=line_center_override(self.track, item.line, style, respect_page_plan=True))
            local = self.locals[i]
            if style.vertical and style.dual_line_layout:
                # Page collisions operate along x for vertical columns, using
                # the same cell boxes and lane pitch as final layout.
                column, top, cell_height, _ = vertical_line_box(
                    placement, self.measured.lines[i], len(local.cells), width, height)
                primary = max(0, style.stroke_width_px)
                secondary = max(0, style.stroke2_width_px) if style.stroke2_enabled else 0
                outline_radius = (primary + secondary) / 2
                half_width = float(style.font_size_px) / 2 + outline_radius
                self.cache[key] = None if local.ink is None else CollisionLineGeometry(
                    column - half_width, column + half_width,
                    top - outline_radius, top + cell_height * len(local.cells) + outline_radius,
                    column, float(style.line_gap_px))
                return self.cache[key]
            position = self.position(placement, width, height)
            if self.ruby is not None:
                local = replace(local, ink=self.ruby.lines[i].placement_ink)
            bounds = collision_bounds(local, position)
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
