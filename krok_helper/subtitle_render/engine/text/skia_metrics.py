"""Batch unique font instances/cells and freeze canonical line timing metrics.

Discovery and IPC are supplied capabilities. Main-font baseline metrics remain
independent of the line's glyph contents, including an all-Latin or empty line.
"""
from dataclasses import dataclass
import hashlib
import json

from .skia_batch import TextMeasurementBatch
from .font_slots import font_style_for_cell
from .metric_snapshot import CellMetric, LineMetric, TextMetricSnapshot
from .skia_width import cell_geometry
from krok_helper.subtitle_render.engine.style.style_semantics import style_for_role_in_layout


@dataclass(frozen=True)
class MeasuredLine:
    cells: tuple
    run_indices: tuple[int, ...]
    widths: tuple[int, ...]
    offsets: tuple[float, ...]
    baseline_metrics: dict


@dataclass(frozen=True)
class MeasuredTrack:
    fonts: dict
    runs: tuple
    lines: tuple[MeasuredLine, ...]
    snapshot: TextMetricSnapshot
    identity: str


def measure_track(track, style, line_styles, *, resolve_font, measure) -> MeasuredTrack:
    batch = TextMeasurementBatch(resolve_font)
    instance = batch.instance

    references = []
    for line, appearance in zip(track.lines, line_styles, strict=True):
        main = instance(appearance)
        cells = []
        role_styles = {}
        for char in line.chars:
            if char.role_label not in role_styles:
                role_styles[char.role_label] = style_for_role_in_layout(appearance, char.role_label)
            selected, stretch = font_style_for_cell(role_styles[char.role_label], char.text)
            run = instance(selected, stretch)
            cells.append((run, batch.cell(run, char.text), selected))
        references.append((main, cells))
    measured = batch.measure(measure)
    lines, snapshots = [], []
    for line, (main, references) in zip(track.lines, references, strict=True):
        cells = tuple(measured[r]['cells'][c] for r, c, _ in references)
        geometry = [cell_geometry(char.text, cell, selected)
                    for char, cell, (_, _, selected) in zip(line.chars, cells, references, strict=True)]
        widths = tuple(g[0] for g in geometry)
        lines.append(MeasuredLine(cells, tuple(r for r, _, _ in references), widths,
                                  tuple(g[1] for g in geometry), measured[main]['font_metrics']))
        snapshots.append(LineMetric.capture(line, style,
            [CellMetric.from_payload(c.text, m) for c, m in zip(line.chars, cells, strict=True)], layout_widths=widths))
    snapshot = TextMetricSnapshot(tuple(snapshots))
    # A main-font replacement must invalidate even a line with only Latin glyphs.
    signature = {'cells': snapshot.identity, 'fonts': [r['font_metrics'] for r in measured]}
    identity = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    return MeasuredTrack(batch.fonts, batch.resolved_runs(),
                         tuple(lines), snapshot, identity)
