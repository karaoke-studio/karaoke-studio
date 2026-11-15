"""Canonical lower-reading preparation; no baseline placement or drawing.

Source romanization retains the shared authored-part policy. Final visual-unit
clocks use the GPU contract, including floor division inside authored parts.
Lower readings never own main-text wipe curves; settings must be frozen.
"""
from dataclasses import dataclass, replace
import hashlib

from strange_uta_game.backend.domain.graphemes import split_graphemes

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.engine.value_signature import value_signature
from .lower_semantics import LowerRuby, lower_rubies_for_line
from .selection import active_rubies_for_line, ruby_target_indices
from .skia_metrics import MeasuredRubyReading, MeasuredRubyReadings, measure_readings
from .skia_timing import RubyUnitClock, ruby_unit_clock
from .style_semantics import ruby_script_stroke_style


@dataclass(frozen=True)
class PreparedLowerReading:
    indices: tuple[int, ...]
    effective: LowerRuby
    style: Style
    clock: RubyUnitClock
    metrics: MeasuredRubyReading


@dataclass(frozen=True)
class PreparedLowerReadings:
    measured: MeasuredRubyReadings
    lines: tuple[tuple[PreparedLowerReading, ...], ...]
    identity: str
    main_identity: str


def prepare_lower_readings(track, style, line_styles, measured, *, resolve_font, measure):
    """Select from canonical main intervals and batch all explicit ruby fonts.

    This preparation port deliberately does not enable lower ruby in product
    IR. Baseline references, source row frames and D2D GPU parity remain gates
    for that later integration. Spatial spacing cannot change these clocks.
    """
    pending, readings = [], []
    for index, (line, appearance, _main) in enumerate(zip(
            track.lines, line_styles, measured.lines, strict=True)):
        if not appearance.lower_ruby_enabled:
            pending.append([])
            continue
        if appearance.vertical or appearance.right_to_left or line.wipe_reverse:
            raise ValueError('Skia 下方注音准备仅支持正向横排')
        options = appearance._lower_romaji_options
        if options is None or len(options) != 4 or any(type(v) is not bool for v in options):
            raise ValueError('Skia 下方注音准备需要已冻结的罗马音选项')
        intervals = measured.snapshot.resolve_intervals(line, style)
        owner = line if line.track_line_index is not None else replace(line, track_line_index=index)
        authored = active_rubies_for_line(track.rubies, owner, intervals=intervals)
        derived = lower_rubies_for_line(owner, authored, list(intervals), appearance)
        groups = []
        for ruby in derived:
            indices = tuple(ruby_target_indices(ruby, owner, intervals))
            if not indices:
                raise ValueError('Skia 下方注音缺少有效正文目标')
            effective_style = ruby_script_stroke_style(ruby.render_style, ruby.reading)
            clock = ruby_unit_clock(ruby, len(readings))
            if clock.units != tuple(split_graphemes(ruby.reading)):
                raise ValueError('Skia 下方注音的分段边界拆开了塑形单元')
            groups.append((indices, ruby, effective_style, clock))
            readings.append((ruby.reading, effective_style))
        pending.append(groups)
    metrics = measure_readings(readings, resolve_font=resolve_font, measure=measure)
    offset, lines = 0, []
    for groups in pending:
        result = []
        for indices, ruby, appearance, clock in groups:
            result.append(PreparedLowerReading(indices, ruby, appearance, clock, metrics.readings[offset]))
            offset += 1
        lines.append(tuple(result))
    signature = (measured.identity, metrics.identity, value_signature(line_styles),
                 value_signature(track.rubies), value_signature(pending))
    identity = hashlib.sha256(repr(signature).encode('utf-8')).hexdigest()
    return PreparedLowerReadings(metrics, tuple(lines), identity, measured.identity)
