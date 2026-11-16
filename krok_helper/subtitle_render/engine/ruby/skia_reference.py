"""Stable annotation font references, measured once without Qt or lyric ink."""
from dataclasses import dataclass, asdict
import hashlib
import json
import math

from ..text.skia_batch import TextMeasurementBatch
from ..text.metric_snapshot import CellMetric
from ..layout.line.skia_rows import RowFrame, fixed_row_frame
from .lower_semantics import lower_style
from .style_semantics import font_style_for_ruby_cell, ruby_script_stroke_style, ruby_stroke_width, ruby_stroke2_width


MAIN_REFERENCE = '国'
UPPER_REFERENCE = 'あいうえおアイウエオ'
LOWER_REFERENCE = 'abcdefghijklmnopqrstuvwxyz'


def _pixel(value):
    return math.floor(max(float(value), 0)+.5)


def _box(metrics, size, side, stroke=0):
    up, down = _pixel(metrics['ascent']), _pixel(metrics['descent'])
    return max(int(size), 1)*(up if side == 'ascent' else down)/max(up+down, 1)+max(stroke, 0)/2


@dataclass(frozen=True)
class RubyReference:
    main_metrics: dict
    upper_metrics: dict
    lower_metrics: dict
    main_ink: tuple
    upper_ink: tuple
    lower_ink: tuple
    main_size: int
    upper_size: int
    main_stroke: int
    upper_stroke: int
    font_chains: tuple = ()

    @property
    def zero_upper_baseline(self):
        return -_box(self.main_metrics, self.main_size, 'ascent', self.main_stroke)-_box(
            self.upper_metrics, self.upper_size, 'descent', self.upper_stroke)

    def lower_baseline(self, gap):
        zero_gap = self.main_ink[1]-self.zero_upper_baseline-self.upper_ink[3]
        return self.main_ink[3]+zero_gap+_pixel(self.lower_metrics['cap_height'])+gap

    @property
    def identity(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def measure_references(pairs, *, resolve_font, measure):
    """Measure explicit (upper appearance, effective lower appearance) pairs.

    Whole reference strings own their ink envelopes. Font slot selection is
    explicit; a missing reference glyph must not discover an implicit fallback.
    """
    batch, pending = TextMeasurementBatch(resolve_font), []
    for upper, lower in pairs:
        japanese, _ = font_style_for_ruby_cell(upper, '')
        latin, stretch = font_style_for_ruby_cell(lower, 'a')
        # Baseline metrics stay on the requested slot; reference ink may use
        # an explicitly supplied fallback, just like actual reading units.
        main_run = batch.instance(upper, text=MAIN_REFERENCE)
        upper_run = batch.instance(japanese, text=UPPER_REFERENCE)
        lower_run = batch.instance(latin, stretch, text=LOWER_REFERENCE)
        pending.append((upper, japanese, main_run, batch.cell(main_run, MAIN_REFERENCE),
                        upper_run, batch.cell(upper_run, UPPER_REFERENCE),
                        lower_run, batch.cell(lower_run, LOWER_REFERENCE)))
    result, references = batch.measure(measure), []
    for upper, japanese, mr, mi, ur, ui, lr, li in pending:
        cells = [result[r]['cells'][i] for r, i in ((mr, mi), (ur, ui), (lr, li))]
        for text, cell in zip((MAIN_REFERENCE, UPPER_REFERENCE, LOWER_REFERENCE), cells, strict=True):
            CellMetric.from_payload(text, cell)
            if not cell.get('outline_available') or cell['ink'][2] <= cell['ink'][0] or cell['ink'][3] <= cell['ink'][1]:
                raise ValueError('Skia 注音参考字形缺少可用轮廓')
        lower_metrics = result[lr]['font_metrics']
        if 'cap_height' not in lower_metrics:
            raise ValueError('Skia sidecar 缺少字体大写高度，请重新构建')
        if not math.isfinite(float(lower_metrics['cap_height'])) or lower_metrics['cap_height'] <= 0:
            raise ValueError('Skia 下方注音缺少有效字体大写高度')
        references.append(RubyReference(result[mr]['font_metrics'], result[ur]['font_metrics'], lower_metrics,
            *(tuple(c['ink']) for c in cells), upper.font_size_px, japanese.font_size_px,
            upper.stroke_width_px, ruby_stroke_width(upper),
            tuple(tuple(result[r].get('font_chain', ())) for r in (mr, ur, lr))
            if any(len(batch.runs[r]['font_ids']) > 1 for r in (mr, ur, lr)) else ()))
    return tuple(references)


def measure_annotated_rows(requests, *, resolve_font, measure):
    """Enclose stable upper/lower reference ink in each used source font box."""
    active = [(style, ruby_script_stroke_style(lower_style(style), 'a'))
              for style, _metrics in requests if style.lower_ruby_enabled]
    references = iter(measure_references(active, resolve_font=resolve_font, measure=measure))
    frames = []
    for style, metrics in requests:
        frame = fixed_row_frame(style, metrics)
        if style.lower_ruby_enabled:
            reference = next(references)
            lower = ruby_script_stroke_style(lower_style(style), 'a')
            # Outline drawing remains gated. Preserve the reference box rule
            # here so the spatial envelope has one owner when it is enabled.
            upper_edge = math.ceil((ruby_stroke_width(style)+ruby_stroke2_width(style))/2)
            lower_edge = math.ceil((ruby_stroke_width(lower)+ruby_stroke2_width(lower))/2)
            top = min(-frame.ascent, math.floor(reference.zero_upper_baseline-style.ruby_gap_px
                                               +reference.upper_ink[1]-upper_edge))
            bottom = max(frame.descent, math.ceil(reference.lower_baseline(lower.ruby_gap_px)
                                                  +reference.lower_ink[3]+lower_edge))
            frame = RowFrame(-top, bottom, 0, reference.identity)
        frames.append(frame)
    return tuple(frames)
