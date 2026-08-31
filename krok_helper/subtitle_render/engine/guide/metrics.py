"""Qt-backed bitmap/vector guide resource loading and layout measurements."""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock

from PyQt6.QtGui import QImage

from krok_helper.subtitle_render.engine.guide.semantics import guide_symbol_is_bitmap
from krok_helper.subtitle_render.engine.render.image_resource import image_file_signature
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import GuideSymbol


_BITMAP_GUIDE_CACHE_MAX = 64
_BITMAP_GUIDE_CACHE: OrderedDict[tuple[str, int, int], QImage] = OrderedDict()
_BITMAP_GUIDE_LOCK = Lock()


def bitmap_guide_image(path: str | None) -> QImage | None:
    """Load one premultiplied guide bitmap through the bounded shared cache."""
    if not path:
        return None
    signature = image_file_signature(path)
    if signature is None:
        return None
    with _BITMAP_GUIDE_LOCK:
        cached = _BITMAP_GUIDE_CACHE.get(signature)
        if cached is not None:
            _BITMAP_GUIDE_CACHE.move_to_end(signature)
            return cached
    image = QImage(signature[0])
    if image.isNull():
        return None
    image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    with _BITMAP_GUIDE_LOCK:
        existing = _BITMAP_GUIDE_CACHE.get(signature)
        if existing is not None:
            _BITMAP_GUIDE_CACHE.move_to_end(signature)
            return existing
        _BITMAP_GUIDE_CACHE[signature] = image
        while len(_BITMAP_GUIDE_CACHE) > _BITMAP_GUIDE_CACHE_MAX:
            _BITMAP_GUIDE_CACHE.popitem(last=False)
    return image


def bitmap_guide_content_size(
    symbol: GuideSymbol,
    style: Style,
) -> tuple[int, int]:
    """Resolve a guide bitmap's layout content size before outer margins.

    Sizing prefers the after-state image: the SHINTA ``@Emoji`` avatar pattern
    pairs a transparent spacer as the before image with the real picture as
    the after image, and the cell must follow the picture or the avatar
    collapses into a sliver a few pixels wide.  A before-only symbol (the
    colour-separation 1x1 placeholder) keeps sizing by its own image.
    """
    image = bitmap_guide_image(symbol.bitmap_after_path)
    if image is None:
        image = bitmap_guide_image(symbol.bitmap_before_path)
    if image is None or image.isNull():
        return 1, max(int(style.font_size_px), 1)
    if symbol.bitmap_fix_size:
        return max(int(image.width()), 1), max(int(image.height()), 1)
    target_h = max(
        max(int(style.font_size_px), 1)
        * max(int(symbol.bitmap_zoom_percent), 1)
        // 100,
        1,
    )
    target_w = max(
        int(round(image.width() * target_h / max(image.height(), 1))),
        1,
    )
    return target_w, target_h


def vector_glyph_width(symbol: GuideSymbol, style: Style) -> int:
    """Resolve the layout width of a vector or bitmap guide glyph.

    负余白（N3 分色用 ``MarginRight=-400`` 之类）故意把单元格压成**零宽**，
    让后续文字左移与头像重叠——这是合法用法。布局 advance 钳到 ≥0（负宽
    会让 char_x_ranges 反转、走字分段碎掉，且与 GPU 口径不一致）；图片矩形
    与走字墨心仍按内容矩形（``margin_left`` 偏移、允许溢出单元格）计算。
    """
    if guide_symbol_is_bitmap(symbol):
        content_w, _content_h = bitmap_guide_content_size(symbol, style)
        return max(
            content_w
            + int(symbol.bitmap_margin_left_px)
            + int(symbol.bitmap_margin_right_px),
            0,
        )
    return max(
        int(
            round(
                max(int(style.font_size_px), 1)
                * max(float(symbol.advance_width), 0.0)
                / max(int(symbol.units_per_em), 1)
            )
        ),
        1,
    )


__all__ = [
    "bitmap_guide_content_size",
    "bitmap_guide_image",
    "vector_glyph_width",
]
