"""Batch unique font instances/cells and freeze canonical line timing metrics.

Discovery and IPC are supplied capabilities. Main-font baseline metrics remain
independent of the line's glyph contents, including an all-Latin or empty line.
"""
from dataclasses import dataclass, replace, asdict
import hashlib
import json

from .skia_batch import TextMeasurementBatch
from .font_slots import font_style_for_cell, main_script_stroke_style
from .metric_snapshot import CellMetric, LineMetric, TextMetricSnapshot
from .skia_width import cell_geometry
from ..layout.line.skia_rows import RowFrame, source_row_frames
from krok_helper.subtitle_render.engine.style.style_semantics import style_for_role_in_layout
from krok_helper.subtitle_render.engine.guide.skia_vector import vector_guide_metrics


@dataclass(frozen=True)
class MeasuredLine:
    cells: tuple
    run_indices: tuple[int, ...]
    widths: tuple[float, ...]
    offsets: tuple[float, ...]
    edge_sizes: tuple[int, ...]
    baseline_metrics: dict
    row_frame: RowFrame | None = None
    slot_metrics: tuple = ()


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
            if char.vector_glyph is not None:
                # Guide outlines are not font glyphs: the main run only supplies line metrics.
                cells.append((main, None, appearance))
                continue
            if char.role_label not in role_styles:
                role_styles[char.role_label] = style_for_role_in_layout(appearance, char.role_label)
            appearance_slot = main_script_stroke_style(role_styles[char.role_label], char.text)
            selected, stretch = font_style_for_cell(appearance_slot, char.text)
            run = instance(selected, stretch, text=char.text)
            cells.append((run, batch.cell(run, char.text), selected))
        references.append((main, cells))
    measured = batch.measure(measure)
    lines, snapshots = [], []
    glyph_table = None
    for line, (main, references) in zip(track.lines, references, strict=True):
        row = []
        for char, (run, index, selected) in zip(line.chars, references, strict=True):
            if index is not None:
                row.append(measured[run]['cells'][index])
                continue
            if glyph_table is None:
                from krok_helper.subtitle_render.native.protocol import VectorGlyphTable
                glyph_table = VectorGlyphTable()
            glyph_id = glyph_table.reference(char.vector_glyph)
            if glyph_id is None:
                raise ValueError('Skia 导唱符缺少矢量轮廓')
            row.append(vector_guide_metrics(glyph_table.payload[glyph_id], selected.font_size_px,
                                            measured[run]['font_metrics'], glyph_id))
        cells = tuple(row)
        edge_sizes = tuple(max(int(selected.stroke_width_px), 0) for _, _, selected in references)
        geometry = [(cell['advance'], 0.0) if char.vector_glyph is not None
                    else cell_geometry(char.text, cell, selected, edge_size=edge)
                    for char, cell, (_, _, selected), edge in zip(line.chars, cells, references, edge_sizes, strict=True)]
        widths = tuple(g[0] for g in geometry)
        # Guide layout uses the GPU's fractional advance. The shared timeline
        # uses the existing integer guide width for both timing weights.
        timing_widths = tuple(max(round(width), 1) if char.vector_glyph is not None else width
                              for char, width in zip(line.chars, widths, strict=True))
        lines.append(MeasuredLine(cells, tuple(r for r, _, _ in references), widths,
                                  tuple(g[1] for g in geometry), edge_sizes, measured[main]['font_metrics'],
                                  slot_metrics=tuple(measured[r]['font_metrics'] for r, _, _ in references)))
        snapshots.append(LineMetric.capture(line, style,
            [CellMetric.from_payload(c.text, m) for c, m in zip(line.chars, cells, strict=True)], layout_widths=timing_widths))
    snapshot = TextMetricSnapshot(tuple(snapshots))
    def row_frames(requests):
        from ..ruby.skia_reference import measure_annotated_rows
        return measure_annotated_rows(requests, resolve_font=resolve_font, measure=measure)
    rows = source_row_frames(track, style, line_styles, lines,
        resolve_frames=row_frames if any(s.lower_ruby_enabled for s in line_styles) else None)
    lines = [replace(line, row_frame=row) for line, row in zip(lines, rows, strict=True)]
    # A main-font replacement must invalidate even a line with only Latin glyphs.
    signature = {'cells': snapshot.identity, 'fonts': [r['font_metrics'] for r in measured],
                 'rows': [asdict(row) if row is not None else None for row in rows]}
    if any(len(run['font_ids']) > 1 for run in batch.runs):
        signature['font_chains'] = [r['font_chain'] for r in measured]
    identity = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    return MeasuredTrack(batch.fonts, batch.resolved_runs(),
                         tuple(lines), snapshot, identity)
