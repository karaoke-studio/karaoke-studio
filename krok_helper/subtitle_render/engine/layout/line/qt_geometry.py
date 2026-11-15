"""Qt text measurement adapter for backend-independent line geometry policy."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from threading import Lock

from PyQt6.QtGui import QFontMetrics

from krok_helper.subtitle_render.engine.guide import render_line_with_guide_symbols, vector_glyph_width
from krok_helper.subtitle_render.engine.layout.line.geometry import (
    line_has_role_labels,
    resolve_char_intervals,
)
from krok_helper.subtitle_render.engine.text import (
    build_role_text_layout,
    role_char_geometry_by_index,
)
from krok_helper.subtitle_render.engine.text import (
    build_font,
    build_latin_font,
    char_ink_width,
    char_layout_width,
    make_font_for,
)
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingLine, TimingTrack


_ROW_GEOMETRIES: OrderedDict[tuple, tuple[int, int, int, int]] = OrderedDict()
_ROW_GEOMETRIES_LOCK = Lock()


def source_layout_row_geometry(
    track: TimingTrack,
    style: Style,
    layout_index: int,
    geometry_for_style: Callable[[Style], tuple[int, int, int, int]],
) -> tuple[int, int, int, int]:
    """Resolve a stable text row box for one source/layout, across all pages.

    Only fonts referenced by nonblank text cells participate. Guide pictures,
    guide outlines and unrelated defaults cannot resize the row grid. The
    supplied adapter owns legacy/N3 and annotation geometry semantics.
    """
    from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
    from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
    from krok_helper.subtitle_render.engine.style.style_semantics import style_for_role_in_layout
    from krok_helper.subtitle_render.engine.text.font_capabilities import capabilities_generation
    from krok_helper.subtitle_render.engine.text.font_slots import font_style_for_cell, is_n3_latin_text
    from krok_helper.subtitle_render.engine.text.layout import main_script_stroke_style
    from krok_helper.subtitle_render.engine.value_signature import (
        lyric_layout_style_signature, track_signature_for_windows,
    )

    cache = getattr(_LAYOUT_PASS, "row_geometries", None)
    font_generation = capabilities_generation()
    local_key = (id(track), id(style), layout_index, geometry_for_style, font_generation)
    if cache is not None and local_key in cache:
        return cache[local_key]
    key = (font_generation, track_signature_for_windows(track),
           lyric_layout_style_signature(style), layout_index, geometry_for_style)
    with _ROW_GEOMETRIES_LOCK:
        result = _ROW_GEOMETRIES.get(key)
        if result is not None:
            _ROW_GEOMETRIES.move_to_end(key)
    if result is None:
        candidates = {}
        line_styles = {}
        for line in track.lines:
            if int(line.layout_index or 0) != layout_index or line.is_blank:
                continue
            for char in render_line_with_guide_symbols(line).chars:
                if char.vector_glyph is not None or not char.text.strip():
                    continue
                slot = (line.singer_id, char.role_label, is_n3_latin_text(char.text))
                if slot in candidates:
                    continue
                if line.singer_id not in line_styles:
                    line_styles[line.singer_id] = style_for_line(style, line)
                active = style_for_role_in_layout(line_styles[line.singer_id], char.role_label)
                active = main_script_stroke_style(active, char.text)
                active, _ = font_style_for_cell(active, char.text)
                candidates[slot] = geometry_for_style(active)
        boxes = list(candidates.values()) or [geometry_for_style(style)]
        ascent = max(box[1] for box in boxes)
        descent = max(box[2] for box in boxes)
        result = (ascent + descent, ascent, descent, max(box[3] for box in boxes))
        with _ROW_GEOMETRIES_LOCK:
            _ROW_GEOMETRIES[key] = result
            _ROW_GEOMETRIES.move_to_end(key)
            while len(_ROW_GEOMETRIES) > 128:
                _ROW_GEOMETRIES.popitem(last=False)
    if cache is not None:
        cache[local_key] = result
        _LAYOUT_PASS.tracks.append(track)
        _LAYOUT_PASS.styles.append(style)
    return result


def char_widths_for_intervals(
    line: TimingLine,
    style: Style,
) -> list[int]:
    """Measure interval weights for normal, role-styled and guide glyphs."""
    if line_has_role_labels(line):
        layout = build_role_text_layout(line, style, x0=0, baseline_y=0)
        widths, _ranges = role_char_geometry_by_index(line, layout)
        return widths
    font = build_font(style)
    metrics = QFontMetrics(font)
    latin_font = build_latin_font(style)
    font_for = make_font_for(style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    return [
        (
            vector_glyph_width(char.vector_glyph, style)
            if char.vector_glyph is not None
            else char_layout_width(
                char.text,
                font,
                metrics,
                latin_metrics,
                font_for,
                style,
            )
        )
        for char in line.chars
    ]


def char_ink_widths_for_intervals(
    line: TimingLine,
    style: Style,
) -> list[int]:
    """Measure glyph ink widths for the leader-guard split weights.

    角色/导唱符路径退化为布局宽度——保底分支只关心「多 checkpoint leader +
    无时间戳后随」的普通歌词段，角色行极少命中且退化行为等于历史口径。
    """
    if line_has_role_labels(line):
        return char_widths_for_intervals(line, style)
    font = build_font(style)
    metrics = QFontMetrics(font)
    latin_font = build_latin_font(style)
    font_for = make_font_for(style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    return [
        (
            vector_glyph_width(char.vector_glyph, style)
            if char.vector_glyph is not None
            else char_ink_width(
                char.text,
                font,
                metrics,
                latin_metrics,
                font_for,
            )
        )
        for char in line.chars
    ]


def resolved_char_intervals_for_line(
    line: TimingLine,
    style: Style,
) -> list[tuple[int, int]]:
    """Resolve final Painter-compatible character timing intervals."""
    return resolve_char_intervals(
        line,
        style,
        char_widths_for_intervals,
        char_ink_widths_for=char_ink_widths_for_intervals,
    )


__all__ = [
    "char_ink_widths_for_intervals",
    "char_widths_for_intervals",
    "resolved_char_intervals_for_line",
]
