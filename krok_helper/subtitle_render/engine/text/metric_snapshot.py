"""Immutable metric values injected into semantic timing; no Qt measurement calls.

Raw font advances are deliberately distinct from layout widths. The caller supplies
the calibrated width policy (P05.4), rather than silently treating advance as N3 width.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingLine
from krok_helper.subtitle_render.engine.layout.line.geometry import resolve_char_intervals
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.value_signature import value_signature


@dataclass(frozen=True)
class CellMetric:
    text: str
    font_identity: str
    advance: float
    ink: tuple[float, float, float, float]
    ascent: float
    descent: float
    clusters: tuple[tuple[int, int], ...]

    @classmethod
    def from_payload(cls, text: str, payload: dict) -> CellMetric:
        ink = tuple(float(v) for v in payload['ink'])
        values = tuple(float(payload[key]) for key in ('advance', 'ascent', 'descent'))
        if len(ink) != 4 or not all(math.isfinite(v) for v in (*ink, *values)) or not payload['font_identity']:
            raise ValueError("字体度量包含无效或非有限值")
        encoded = text.encode('utf-8')
        boundaries = {len(encoded)} | {i for i, b in enumerate(encoded) if b & 0xc0 != 0x80}
        clusters = tuple((int(g['cluster_start']), int(g['cluster_end'])) for g in payload['glyphs'])
        if not clusters or any(a not in boundaries or b not in boundaries or not 0 <= a < b <= len(encoded) for a, b in clusters):
            raise ValueError("字体度量包含无效 cluster 映射")
        return cls(text, str(payload['font_identity']), values[0], ink, values[1], values[2], clusters)


@dataclass(frozen=True)
class LineMetric:
    metrics: tuple[CellMetric, ...]
    style_signature: object
    layout_widths: tuple[int, ...]
    ink_widths: tuple[int, ...]

    @classmethod
    def capture(cls, line: TimingLine, style: Style, metrics, *, layout_widths) -> LineMetric:
        cells = tuple(metrics)
        widths = tuple(layout_widths)
        if tuple(c.text for c in cells) != tuple(c.text for c in line.chars) or len(widths) != len(cells):
            raise ValueError("度量快照与歌词单元不一致")
        if any(type(w) is not int or w < 0 for w in widths):
            raise ValueError("布局宽度必须为非负整数")
        return cls(cells, value_signature(style_for_line(style, line)), widths,
                   tuple(max(math.ceil(c.ink[2] - c.ink[0]), 0) for c in cells))


@dataclass(frozen=True)
class TextMetricSnapshot:
    lines: tuple[LineMetric, ...]

    def __post_init__(self):
        object.__setattr__(self, 'lines', tuple(self.lines))

    @property
    def identity(self) -> str:
        # Includes physical font identities and measured values; a reused path or
        # equal project style cannot resurrect a plan measured from older bytes.
        return hashlib.sha256(repr(self.lines).encode('utf-8')).hexdigest()

    def resolve_intervals(self, line: TimingLine, style: Style) -> list[tuple[int, int]]:
        texts = tuple(c.text for c in line.chars)
        signature = value_signature(style_for_line(style, line))
        candidates = [m for m in self.lines if tuple(c.text for c in m.metrics) == texts and m.style_signature == signature]
        if not candidates or any(m != candidates[0] for m in candidates):
            raise ValueError("规范度量快照缺失、过期或存在冲突")
        measured = candidates[0]
        return resolve_char_intervals(line, style, lambda *_: measured.layout_widths,
                                      char_ink_widths_for=lambda *_: measured.ink_widths)
