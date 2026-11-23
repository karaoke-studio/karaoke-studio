"""Prepare vertical readings and reserve their width before page placement."""
from dataclasses import dataclass, replace
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
    authored_units: bool


def extended_reading_unit(unit):
    if len(unit) == 1:
        return unit == '・' or is_n3_latin_text(unit)
    return (len(unit) == 2 and unit[1] in '\u3099\u309a'
            and ('\u3041' <= unit[0] <= '\u3096' or '\u30a1' <= unit[0] <= '\u30fa'))


def prepare_vertical_readings(track, style, styles, measured, *, dpr, resolve_font, measure, rendered_lines=None):
    annotation_indices = {id(ruby): i for i, ruby in enumerate(track.rubies)}
    entries, readings = [], []
    version = 1
    shifted = rotated = False
    extended = False
    authored_units = False
    rendered_lines = track.lines if rendered_lines is None else rendered_lines
    for index, (line, rendered, look) in enumerate(zip(track.lines, rendered_lines, styles, strict=True)):
        intervals = measured.snapshot.resolve_intervals(rendered, style)
        prefix = len(rendered.chars) - len(line.chars)
        targets = sorted(upper_ruby_targets(track, index, line, look, intervals[prefix:], annotation_indices),
                         key=lambda target: min(target.indices))
        if prefix:
            targets = [replace(target, indices=tuple(i + prefix for i in target.indices)) for target in targets]
        timing = prepare_ruby_target_timing(rendered, targets, look.ruby_main_progress_mode)
        groups, previous_end = [], 0
        for target, clock in zip(targets, timing.readings, strict=True):
            if min(target.indices) < previous_end:
                raise ValueError('Skia 竖排同列注音的正文范围不能重叠')
            previous_end = max(target.indices) + 1
            glyph_units = clock.glyph_units
            if (not glyph_units
                    or any(not (upright_vertical_text(unit) or vertical_glyph_shift(unit) or vertical_glyph_rotates(unit)
                                or extended_reading_unit(unit))
                           for unit in glyph_units)):
                raise ValueError('Skia 竖排注音包含未验证的读音字符')
            extended |= any(extended_reading_unit(unit) for unit in glyph_units)
            authored_units |= clock.units != glyph_units
            shifted |= any(vertical_glyph_shift(unit) for unit in glyph_units)
            rotated |= any(vertical_glyph_rotates(unit) for unit in glyph_units)
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
    return PreparedVerticalReadings(tuple(entries), ruby, tuple(allowances), version, shifted, rotated,
                                    extended, authored_units)
