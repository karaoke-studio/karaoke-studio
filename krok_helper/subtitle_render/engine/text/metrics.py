"""Qt-backed font selection and N3-compatible text geometry measurements."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtGui import QFont, QFontMetrics, QPainterPath

from krok_helper.subtitle_render.engine.layout_context import _LAYOUT_PASS
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.n3_font_catalog import resolve_qt_font_family


FontSelector = Callable[[str], QFont]


def clamp_weight(weight: int) -> QFont.Weight:
    if weight <= 250:
        return QFont.Weight.Thin
    if weight <= 350:
        return QFont.Weight.Light
    if weight <= 450:
        return QFont.Weight.Normal
    if weight <= 550:
        return QFont.Weight.Medium
    if weight <= 650:
        return QFont.Weight.DemiBold
    if weight <= 750:
        return QFont.Weight.Bold
    if weight <= 850:
        return QFont.Weight.ExtraBold
    return QFont.Weight.Black


def build_font(style: Style) -> QFont:
    font = QFont(
        resolve_qt_font_family(style.font_family),
        max(style.font_size_px, 1),
    )
    font.setPixelSize(max(style.font_size_px, 1))
    font.setWeight(clamp_weight(style.font_weight))
    font.setItalic(style.italic)
    return font


def latin_font_size(style: Style) -> int:
    value = style.latin_font_size_px
    return int(value) if value is not None and int(value) > 0 else int(style.font_size_px)


def latin_font_weight(style: Style) -> int:
    value = style.latin_font_weight
    return int(value) if value is not None and int(value) > 0 else int(style.font_weight)


def is_n3_latin_text(text: str) -> bool:
    return bool(text) and all(
        "\u0020" <= char <= "\u007e" or "\u00c0" <= char <= "\u00ff"
        for char in text
    )


def is_emoji_text(text: str) -> bool:
    return any(
        0x1F000 <= ord(char) <= 0x1FAFF or 0x2600 <= ord(char) <= 0x27BF
        for char in text
    )


def _build_emoji_font(style: Style) -> QFont:
    font = QFont("Segoe UI Symbol", max(int(style.font_size_px), 1))
    font.setPixelSize(max(int(style.font_size_px), 1))
    font.setWeight(clamp_weight(style.font_weight))
    font.setItalic(style.italic)
    return font


def build_latin_font(style: Style) -> QFont:
    family = style.font_family_latin or style.font_family
    size = max(latin_font_size(style), 1)
    font = QFont(resolve_qt_font_family(family), size)
    font.setPixelSize(size)
    font.setWeight(clamp_weight(latin_font_weight(style)))
    font.setItalic(style.italic)
    return font


def make_font_for(
    style: Style,
    jp_font: QFont,
    latin_font: QFont,
) -> FontSelector:
    emoji_font = _build_emoji_font(style)
    same_text_fonts = _font_signature(latin_font) == _font_signature(jp_font)

    def font_for(text: str) -> QFont:
        if is_emoji_text(text):
            return emoji_font
        return latin_font if not same_text_fonts and is_n3_latin_text(text) else jp_font

    return font_for


def char_advance(
    text: str,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for: FontSelector | None,
) -> int:
    cache = getattr(_LAYOUT_PASS, "char_advances", None)
    if cache is None:
        if font_for is not None and is_emoji_text(text):
            return QFontMetrics(font_for(text)).horizontalAdvance(text)
        if font_for is not None and is_n3_latin_text(text):
            return latin_metrics.horizontalAdvance(text)
        return metrics.horizontalAdvance(text)
    use_emoji = font_for is not None and is_emoji_text(text)
    use_latin = font_for is not None and is_n3_latin_text(text)
    if use_emoji:
        emoji_font = font_for(text)
        source = QFontMetrics(emoji_font)
        source_key = ("emoji", _font_signature(emoji_font))
    else:
        source = latin_metrics if use_latin else metrics
        source_key = id(source)
    cache_key = (text, source_key)
    hit = cache.get(cache_key)
    if hit is None:
        hit = source.horizontalAdvance(text)
        cache[cache_key] = hit
        _LAYOUT_PASS.metrics.append(source)
    return hit


_CHAR_METRIC_CACHE: dict[tuple, tuple[int, float]] = {}
_CHAR_METRIC_CACHE_MAX = 16384


def clear_char_metric_cache() -> None:
    _CHAR_METRIC_CACHE.clear()


def _font_signature(font: QFont) -> tuple:
    return (font.family(), font.pixelSize(), int(font.weight()), font.italic())


def _char_metric_key(
    text: str,
    glyph_font: QFont,
    advance: int,
    style: Style,
) -> tuple:
    return (
        text,
        _font_signature(glyph_font),
        advance,
        bool(style.allow_biting),
        int(style.stroke_width_px),
        int(style.space_width_percent),
        int(style.font_size_px),
    )


def truncate_div(numerator: int, denominator: int) -> int:
    """Integer division truncated toward zero, matching C# arithmetic."""
    if denominator == 0:
        return 0
    sign = -1 if (numerator < 0) != (denominator < 0) else 1
    return sign * (abs(numerator) // abs(denominator))


def nicokara_layout_width(
    ink_width: int,
    advance: int,
    left_bearing: int,
    right_bearing: int,
    *,
    edge_size: int,
    allow_biting: bool,
) -> int:
    advance = max(int(advance), 1)
    left = int(left_bearing)
    right = int(right_bearing)
    if not allow_biting:
        left = max(left, 0)
        right = max(right, 0)
    body_width = truncate_div(
        max(int(ink_width), 0) * (left + advance + right),
        advance,
    )
    return max(body_width, 0) + max(int(edge_size), 0)


def nicokara_char_geometry_left_offset(
    ink_width: int,
    advance: int,
    left_bearing: int,
    *,
    allow_biting: bool,
) -> int:
    advance = max(int(advance), 1)
    left = int(left_bearing)
    if not allow_biting:
        left = max(left, 0)
    return truncate_div(max(int(ink_width), 0) * left, advance)


def _char_layout_metrics(
    text: str,
    font: QFont,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for: FontSelector | None,
    style: Style,
) -> tuple[int, float]:
    is_latin_glyph = font_for is not None and is_n3_latin_text(text)
    glyph_font = font_for(text) if font_for is not None else font
    glyph_metrics = (
        QFontMetrics(glyph_font)
        if is_emoji_text(text)
        else latin_metrics
        if is_latin_glyph
        else metrics
    )
    font_size = glyph_font.pixelSize()
    if font_size <= 0:
        font_size = max(
            latin_font_size(style) if is_latin_glyph else int(style.font_size_px),
            1,
        )
    space_percent = max(10, min(int(style.space_width_percent), 100))
    edge_size = max(int(style.stroke_width_px), 0)

    if text == " ":
        return font_size * space_percent // 100, 0.0

    advance = char_advance(text, metrics, latin_metrics, font_for)
    key = _char_metric_key(text, glyph_font, advance, style)
    cached = _CHAR_METRIC_CACHE.get(key)
    if cached is not None:
        return cached

    path = QPainterPath()
    if text:
        path.addText(0.0, 0.0, glyph_font, text)
    bounds = path.boundingRect()
    if bounds.isEmpty():
        body_width = font_size * space_percent * 25 // 100 // 10
        result = (max(body_width, 0) + edge_size, 0.0)
    else:
        try:
            width_left_bearing = glyph_metrics.leftBearing(text)
            width_right_bearing = glyph_metrics.rightBearing(text)
        except (TypeError, ValueError):
            width_left_bearing = int(bounds.left())
            width_right_bearing = int(advance - bounds.right())
        width = nicokara_layout_width(
            int(bounds.width()),
            advance,
            width_left_bearing,
            width_right_bearing,
            edge_size=edge_size,
            allow_biting=bool(style.allow_biting),
        )
        try:
            offset_left_bearing = glyph_metrics.leftBearing(text)
        except (TypeError, ValueError):
            offset_left_bearing = int(bounds.left())
        geometry_left = nicokara_char_geometry_left_offset(
            int(bounds.width()),
            advance,
            offset_left_bearing,
            allow_biting=bool(style.allow_biting),
        )
        offset = (
            -float(bounds.left())
            + float(geometry_left)
            + max(int(style.stroke_width_px), 0) / 2.0
        )
        result = (width, offset)

    if len(_CHAR_METRIC_CACHE) >= _CHAR_METRIC_CACHE_MAX:
        _CHAR_METRIC_CACHE.clear()
    _CHAR_METRIC_CACHE[key] = result
    return result


def char_path_left_offset(
    text: str,
    font: QFont,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for: FontSelector | None,
    style: Style,
) -> float:
    if not text or text.isspace():
        return 0.0
    return _char_layout_metrics(
        text,
        font,
        metrics,
        latin_metrics,
        font_for,
        style,
    )[1]


def char_layout_width(
    text: str,
    font: QFont,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for: FontSelector | None,
    style: Style,
) -> int:
    return _char_layout_metrics(
        text,
        font,
        metrics,
        latin_metrics,
        font_for,
        style,
    )[0]


def letter_spacing(style: Style) -> int:
    return int(style.letter_spacing_px)


def line_text_width(char_widths: list[int], style: Style) -> int:
    if not char_widths:
        return 0
    spacing = letter_spacing(style)
    if style.layout_semantics == "n3_1074":
        return max(
            0,
            sum(max(int(width) + spacing, 0) for width in char_widths[:-1])
            + int(char_widths[-1]),
        )
    return max(0, sum(char_widths) + spacing * (len(char_widths) - 1))


__all__ = [
    "FontSelector",
    "build_font",
    "build_latin_font",
    "char_advance",
    "char_layout_width",
    "char_path_left_offset",
    "clamp_weight",
    "clear_char_metric_cache",
    "is_emoji_text",
    "is_n3_latin_text",
    "latin_font_size",
    "latin_font_weight",
    "letter_spacing",
    "line_text_width",
    "make_font_for",
    "nicokara_char_geometry_left_offset",
    "nicokara_layout_width",
    "truncate_div",
]
