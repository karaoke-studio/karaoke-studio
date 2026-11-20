"""Canonical ruby measurements; target selection and timing remain separate.

The caller supplies each reading's resolved target/role style. All readings use
one CPU measurement batch, independent of GPU/device availability. These values
are preparation inputs, not permission to render an unsupported ruby profile.
"""
from dataclasses import dataclass, asdict
import hashlib
import json

from strange_uta_game.backend.domain.graphemes import split_graphemes

from krok_helper.subtitle_render.domain.models import effective_karaoke_zoom_pulse
from krok_helper.subtitle_render.engine.text.skia_batch import TextMeasurementBatch
from krok_helper.subtitle_render.engine.text.skia_width import cell_geometry
from krok_helper.subtitle_render.engine.text.metric_snapshot import CellMetric
from .style_semantics import font_style_for_ruby_cell, ruby_script_stroke_style, ruby_stroke_width
from .skia_standalone_marks import starts_with_mark, supported_standalone_mark, validate_mark_metrics


@dataclass(frozen=True)
class MeasuredRubyReading:
    cells: tuple
    run_indices: tuple[int, ...]
    unit_layouts: tuple[tuple[str, float, float, float, float], ...]
    baseline_metrics: dict
    zoom_center_offset: float | None = None


@dataclass(frozen=True)
class MeasuredRubyReadings:
    fonts: dict
    runs: tuple
    readings: tuple[MeasuredRubyReading, ...]
    identity: str


def measure_readings(readings, *, resolve_font, measure, visual_units=None) -> MeasuredRubyReadings:
    """Measure (reading, effective Style) entries without Qt font measurement.

    Graphemes, rather than mora or authored timing parts, own geometry. Empty
    pause parts are handled by the shared timing policy, never shaped as glyphs.
    The Japanese ruby slot owns the baseline even for all-Latin/empty readings.
    """
    readings = tuple(readings)
    if visual_units is None:
        visual_units = [tuple(split_graphemes(text)) for text, _ in readings]
    else:
        visual_units = tuple(visual_units)
        for (reading, _), units in zip(readings, visual_units, strict=True):
            if any(not isinstance(u, str) or not u for u in units) or ''.join(units) != reading:
                raise ValueError('Skia 注音计时单元与来源文本不一致')
    batch, references = TextMeasurementBatch(resolve_font), []
    for (reading, style), text_units in zip(readings, visual_units, strict=True):
        main, _ = font_style_for_ruby_cell(style, '')
        baseline_run = batch.instance(main)
        zoom_reference = None
        if (effective_karaoke_zoom_pulse(style) or style.entry_anim in {'stretch_in', 'glow_in'}
                or style.exit_anim in {'spin_flip', 'scatter_out', 'converge_out', 'stretch_out', 'glow_out', 'flame'}):
            slot, stretch = font_style_for_ruby_cell(style, reading)
            zoom_reference = (batch.instance(slot, stretch), slot.font_size_px)
        units = []
        stroke = ruby_stroke_width(ruby_script_stroke_style(style, reading))
        for unit in text_units:
            selected, stretch = font_style_for_ruby_cell(style, unit)
            run = batch.instance(selected, stretch, text=unit)
            units.append((unit, run, batch.cell(run, unit), selected, stroke))
        references.append((baseline_run, units, zoom_reference))
    measured = batch.measure(measure)
    results = []
    for baseline_run, units, zoom_reference in references:
        cells, indices, layouts = [], [], []
        for unit, run, index, selected, stroke in units:
            cell = measured[run]['cells'][index]
            CellMetric.from_payload(unit, cell)
            width, offset = cell_geometry(unit, cell, selected, edge_size=stroke)
            left, top, right, bottom = cell['ink']
            ink_left, ink_right = ((offset + left, offset + right) if right > left and bottom > top
                                   else (offset, offset))
            cells.append(cell)
            indices.append(run)
            layouts.append((unit, float(width), offset, ink_left, ink_right))
        center = None
        if zoom_reference is not None:
            run, size = zoom_reference
            font = measured[run]['font_metrics']
            # The transform pivot uses the integer reference font box, shared by
            # the whole reading even when its cells use different faces.
            center = int(size * font['descent'] / max(font['ascent'] + font['descent'], 1)) - size / 2
        results.append(MeasuredRubyReading(tuple(cells), tuple(indices), tuple(layouts),
                                          measured[baseline_run]['font_metrics'], center))
    # Content-based metrics and geometry, independent of machine-local file paths.
    signature = [asdict(r) for r in results]
    for row in signature:
        if row['zoom_center_offset'] is None:
            del row['zoom_center_offset']
    if any(len(run['font_ids']) > 1 for run in batch.runs):
        signature.append({'font_chains': [r['font_chain'] for r in measured]})
    identity = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    return MeasuredRubyReadings(batch.fonts, batch.resolved_runs(), tuple(results), identity)


def measure_timed_readings(readings, *, resolve_font, measure):
    """Measure the GPU's part-local clock units, normalized independently.

    The clock retains authored text; normalization is only the glyph boundary.
    Validated isolated marks keep their own face, origin and authored clock.
    """
    from .skia_timing import ruby_unit_clock
    readings = tuple(readings)
    clocks = [ruby_unit_clock(ruby, index) for index, (ruby, _) in enumerate(readings)]
    units = [clock.glyph_units for clock in clocks]
    if any(starts_with_mark(unit) and not supported_standalone_mark(unit) for row in units for unit in row):
        raise ValueError('Skia 注音 part 以独立组合标记开头，尚未完成 GPU 字形兼容')
    # Normalizing each unit can differ from normalizing the concatenated text.
    result = measure_readings([(''.join(row), style) for row, (_, style) in zip(units, readings, strict=True)],
                              visual_units=units, resolve_font=resolve_font, measure=measure)
    for row, measured in zip(units, result.readings, strict=True):
        for unit, metrics in zip(row, measured.cells, strict=True):
            if starts_with_mark(unit):
                validate_mark_metrics(unit, metrics)
    return result
