"""Batch unique font instances/cells and freeze canonical line timing metrics.

Discovery and IPC are supplied capabilities. Main-font baseline metrics remain
independent of the line's glyph contents, including an all-Latin or empty line.
"""
from dataclasses import dataclass
import hashlib
import json
import math

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
    fonts, runs, instance_indices, text_indices = {}, [], {}, []

    def instance(appearance, stretch=1.0):
        key = (appearance.font_family, appearance.font_weight, appearance.italic,
               appearance.font_size_px, stretch)
        if key not in instance_indices:
            resource, options = resolve_font(appearance)
            font_id = hashlib.sha256(json.dumps(resource, sort_keys=True).encode()).hexdigest()
            fonts[font_id] = resource
            options = dict(options, scale_x=options.get('scale_x', 1) * stretch)
            instance_indices[key] = len(runs)
            runs.append({'font_ids': [font_id], 'size': appearance.font_size_px, 'cells': [], **options})
            text_indices.append({})
        return instance_indices[key]

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
            if char.text not in text_indices[run]:
                text_indices[run][char.text] = len(runs[run]['cells'])
                runs[run]['cells'].append(char.text)
            cells.append((run, text_indices[run][char.text], selected))
        references.append((main, cells))
    measured = measure(fonts, runs) if runs else []
    if len(measured) != len(runs): raise ValueError('Skia 批量度量字体实例数量失配')
    for run, result in zip(runs, measured, strict=True):
        baseline = result.get('font_metrics')
        if not isinstance(baseline, dict):
            raise ValueError('Skia sidecar 缺少字体基线度量，请重新构建')
        if (len(result['cells']) != len(run['cells']) or not baseline.get('font_identity')
                or not all(math.isfinite(float(baseline[k])) for k in ('ascent', 'descent', 'leading'))):
            raise ValueError('Skia 批量度量单元或主字基线无效')
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
    return MeasuredTrack(fonts, tuple({k: v for k, v in r.items() if k != 'cells'} for r in runs),
                         tuple(lines), snapshot, identity)
