"""Canonical ruby measurements; target selection and timing remain separate.

The caller supplies each reading's resolved target/role style. All readings use
one CPU measurement batch, independent of GPU/device availability. These values
are preparation inputs, not permission to render an unsupported ruby profile.
"""
from dataclasses import dataclass, asdict
import hashlib
import json

from strange_uta_game.backend.domain.graphemes import split_graphemes

from krok_helper.subtitle_render.engine.text.skia_batch import TextMeasurementBatch
from krok_helper.subtitle_render.engine.text.skia_width import cell_geometry
from krok_helper.subtitle_render.engine.text.metric_snapshot import CellMetric
from .style_semantics import font_style_for_ruby_cell, ruby_script_stroke_style, ruby_stroke_width


@dataclass(frozen=True)
class MeasuredRubyReading:
    cells: tuple
    run_indices: tuple[int, ...]
    unit_layouts: tuple[tuple[str, float, float, float, float], ...]
    baseline_metrics: dict


@dataclass(frozen=True)
class MeasuredRubyReadings:
    fonts: dict
    runs: tuple
    readings: tuple[MeasuredRubyReading, ...]
    identity: str


def measure_readings(readings, *, resolve_font, measure) -> MeasuredRubyReadings:
    """Measure (reading, effective Style) entries without Qt font measurement.

    Graphemes, rather than mora or authored timing parts, own geometry. Empty
    pause parts are handled by the shared timing policy, never shaped as glyphs.
    The Japanese ruby slot owns the baseline even for all-Latin/empty readings.
    """
    batch, references = TextMeasurementBatch(resolve_font), []
    for reading, style in readings:
        main, _ = font_style_for_ruby_cell(style, '')
        baseline_run = batch.instance(main)
        units = []
        for unit in split_graphemes(reading):
            selected, stretch = font_style_for_ruby_cell(style, unit)
            stroke = ruby_stroke_width(ruby_script_stroke_style(style, unit))
            run = batch.instance(selected, stretch)
            units.append((unit, run, batch.cell(run, unit), selected, stroke))
        references.append((baseline_run, units))
    measured = batch.measure(measure)
    results = []
    for baseline_run, units in references:
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
        results.append(MeasuredRubyReading(tuple(cells), tuple(indices), tuple(layouts),
                                          measured[baseline_run]['font_metrics']))
    # Content-based metrics and geometry, independent of machine-local file paths.
    signature = [asdict(r) for r in results]
    identity = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    return MeasuredRubyReadings(batch.fonts, batch.resolved_runs(), tuple(results), identity)
