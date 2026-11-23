"""Prepare vertical readings and reserve their width before page placement."""
from dataclasses import dataclass
import math

from ..layout.line.skia_vertical import upright_vertical_text, vertical_glyph_rotates, vertical_glyph_shift
from ..text.font_slots import is_n3_latin_text
from .skia_geometry import upper_ruby_targets
from .skia_metrics import MeasuredRubyReadings, measure_timed_readings
from .skia_timing import prepare_ruby_target_timing


@dataclass(frozen=True)
class PreparedVerticalReadings:
    entries: tuple
    measured: MeasuredRubyReadings
    allowances: tuple[float, ...]
    version: int
    shifted: bool
    rotated: bool
    extended: bool


def extended_reading_unit(unit):
    return len(unit) == 1 and (unit == '・' or is_n3_latin_text(unit))


def prepare_vertical_readings(track, style, styles, measured, *, dpr, resolve_font, measure):
    annotation_indices = {id(ruby): i for i, ruby in enumerate(track.rubies)}
    entries, readings = [], []
    version = 1
    shifted = rotated = False
    extended = False
    for index, (line, look) in enumerate(zip(track.lines, styles, strict=True)):
        intervals = measured.snapshot.resolve_intervals(line, style)
        targets = sorted(upper_ruby_targets(track, index, line, look, intervals, annotation_indices),
                         key=lambda target: min(target.indices))
        timing = prepare_ruby_target_timing(line, targets, look.ruby_main_progress_mode)
        groups, previous_end = [], 0
        for target, clock in zip(targets, timing.readings, strict=True):
            if min(target.indices) < previous_end:
                raise ValueError('Skia 竖排同列注音的正文范围不能重叠')
            previous_end = max(target.indices) + 1
            if (not clock.units or clock.units != clock.glyph_units
                    or any(not (upright_vertical_text(unit) or vertical_glyph_shift(unit) or vertical_glyph_rotates(unit)
                                or extended_reading_unit(unit))
                           for unit in clock.units)):
                raise ValueError('Skia 竖排注音包含未验证的读音字符')
            extended |= any(extended_reading_unit(unit) for unit in clock.units)
            shifted |= any(vertical_glyph_shift(unit) for unit in clock.units)
            rotated |= any(vertical_glyph_rotates(unit) for unit in clock.units)
            if len(clock.units) > 1:
                version = 2
            groups.append((target, clock, len(readings)))
            readings.append((target.effective, target.style))
        entries.append((timing.main, tuple(groups)))

    ruby = measure_timed_readings(readings, resolve_font=resolve_font, measure=measure)
    heights = [float(r.baseline_metrics['ascent']) + float(r.baseline_metrics['descent']) for r in ruby.readings]
    allowances = []
    for (_, groups), look in zip(entries, styles, strict=True):
        # Round the reading slot on the device grid before adding the authored gap.
        height = max((heights[index] for _, _, index in groups), default=0.0)
        allowances.append(max(math.floor(height * dpr + 0.5) / dpr + look.ruby_gap_px, 0.0) if groups else 0.0)
    return PreparedVerticalReadings(tuple(entries), ruby, tuple(allowances), version, shifted, rotated, extended)
