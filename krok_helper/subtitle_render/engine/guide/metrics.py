"""Qt-backed bitmap/vector guide resource loading and layout measurements."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from PyQt6.QtGui import QImage, QImageReader

from krok_helper.subtitle_render.engine.guide.semantics import guide_symbol_is_bitmap
from krok_helper.subtitle_render.engine.render.image_resource import image_file_signature
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import GuideSymbol


_BITMAP_GUIDE_CACHE_MAX = 64
_BITMAP_GUIDE_CACHE: OrderedDict[tuple[str, int, int], QImage] = OrderedDict()
_BITMAP_GUIDE_LOCK = Lock()

# 动图导唱符（GIF）帧上限与最小帧时长。两条渲染后端（Python QPainter /
# D2D sidecar）必须保持完全一致的数值与选帧规则，否则预览与导出会来回跳帧。
GUIDE_ANIM_MAX_FRAMES = 60
GUIDE_ANIM_MIN_FRAME_MS = 10
_ANIMATED_GUIDE_CACHE_MAX = 16
_ANIMATED_GUIDE_CACHE: OrderedDict[
    tuple[str, int, int], "AnimatedGuideImage"
] = OrderedDict()
# 「确认不是动图」的签名集合（有序去重，防止静态图每次查帧都重跑 imageCount）。
_ANIMATED_GUIDE_MISSING: OrderedDict[tuple[str, int, int], None] = OrderedDict()
_ANIMATED_GUIDE_LOCK = Lock()


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


@dataclass(frozen=True)
class AnimatedGuideImage:
    """Composited frames and clamped delays of one animated guide bitmap.

    选帧规则（``frame_at``）与 sidecar 的 WIC 选帧逻辑是同一契约：帧延时
    钳到 ≥ ``GUIDE_ANIM_MIN_FRAME_MS``、取前 ``GUIDE_ANIM_MAX_FRAMES`` 帧、
    循环取模后在累积时长表上线性查找。
    """

    frames: tuple[QImage, ...]
    delays_ms: tuple[int, ...]

    @property
    def total_ms(self) -> int:
        return sum(self.delays_ms)

    def frame_at(self, anim_ms: int) -> int:
        if len(self.frames) <= 1:
            return 0
        total = self.total_ms
        if total <= 0:
            return 0
        position = max(int(anim_ms), 0) % total
        cumulative = 0
        for index, delay in enumerate(self.delays_ms):
            cumulative += delay
            if position < cumulative:
                return index
        return len(self.frames) - 1


@dataclass(frozen=True)
class GuideAnimationFrame:
    """One resolved guide image plus its cache identity.

    ``identity`` 由文件签名（路径 + mtime + size）与帧序号组成：下游的装饰
    剪影 / 发光缓存以它为 key，GIF 在磁盘上被替换后签名变化，陈旧剪影
    自动失效——旧的「每帧重算」实现没有缓存，所以这里必须带签名。
    """

    image: QImage
    index: int
    identity: tuple = ()


def _skip_gif_sub_blocks(data: bytes, offset: int) -> int:
    length = len(data)
    while offset < length:
        size = data[offset]
        offset += 1
        if size == 0:
            return offset
        offset += size
    return offset


def _gif_frame_delays_ms(path: str) -> list[int]:
    """Parse per-frame GIF89a Graphic Control Extension delays (ms, unclamped).

    QImageReader 能逐帧读出合成像素但完全不暴露帧延时，QMovie 又需要事件
    循环才吐帧；延时只能按 GIF 字节流自解析（每个图像描述符前最近的 GCE）。
    """
    delays: list[int] = []
    try:
        data = Path(path).read_bytes()
    except OSError:
        return delays
    if len(data) < 13 or data[:3] != b"GIF":
        return delays
    flags = data[10]
    offset = 13
    if flags & 0x80:
        offset += 3 << ((flags & 0x07) + 1)
    pending_delay: int | None = None
    length = len(data)
    while offset < length:
        block = data[offset]
        if block == 0x3B:  # trailer
            break
        if block == 0x21:  # extension introducer
            if offset + 2 > length:
                break
            label = data[offset + 1]
            offset += 2
            if (
                label == 0xF9
                and offset < length
                and data[offset] >= 4
                and offset + 5 <= length
            ):
                pending_delay = ((data[offset + 3] << 8) | data[offset + 2]) * 10
            offset = _skip_gif_sub_blocks(data, offset)
            continue
        if block == 0x2C:  # image descriptor
            delays.append(pending_delay or 0)
            pending_delay = None
            if offset + 10 > length:
                break
            local_flags = data[offset + 9]
            offset += 10
            if local_flags & 0x80:
                offset += 3 << ((local_flags & 0x07) + 1)
            if offset < length:
                offset += 1  # LZW minimum code size
                offset = _skip_gif_sub_blocks(data, offset)
            continue
        break
    return delays


def _decode_animated_guide(signature: tuple[str, int, int]) -> AnimatedGuideImage | None:
    reader = QImageReader(signature[0])
    if reader.imageCount() <= 1:
        return None
    delays_raw = _gif_frame_delays_ms(signature[0])
    frames: list[QImage] = []
    delays: list[int] = []
    while len(frames) < GUIDE_ANIM_MAX_FRAMES:
        image = reader.read()
        if image.isNull():
            break
        frames.append(image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied))
        raw = delays_raw[len(frames) - 1] if len(frames) <= len(delays_raw) else 0
        delays.append(max(int(raw), GUIDE_ANIM_MIN_FRAME_MS))
    if len(frames) <= 1:
        return None
    return AnimatedGuideImage(tuple(frames), tuple(delays))


def animated_bitmap_guide(path: str | None) -> AnimatedGuideImage | None:
    """Return the cached frame sequence for an animated guide bitmap."""
    if not path:
        return None
    signature = image_file_signature(path)
    if signature is None:
        return None
    with _ANIMATED_GUIDE_LOCK:
        cached = _ANIMATED_GUIDE_CACHE.get(signature)
        if cached is not None:
            _ANIMATED_GUIDE_CACHE.move_to_end(signature)
            return cached
        if signature in _ANIMATED_GUIDE_MISSING:
            return None
    animated = _decode_animated_guide(signature)
    with _ANIMATED_GUIDE_LOCK:
        if animated is None:
            _ANIMATED_GUIDE_MISSING[signature] = None
            while len(_ANIMATED_GUIDE_MISSING) > _ANIMATED_GUIDE_CACHE_MAX * 4:
                _ANIMATED_GUIDE_MISSING.popitem(last=False)
            return None
        existing = _ANIMATED_GUIDE_CACHE.get(signature)
        if existing is not None:
            _ANIMATED_GUIDE_CACHE.move_to_end(signature)
            return existing
        _ANIMATED_GUIDE_CACHE[signature] = animated
        while len(_ANIMATED_GUIDE_CACHE) > _ANIMATED_GUIDE_CACHE_MAX:
            _ANIMATED_GUIDE_CACHE.popitem(last=False)
    return animated


def bitmap_guide_frame_at(path: str | None, anim_ms: int | None) -> GuideAnimationFrame | None:
    """Resolve the guide image for one paint pass.

    ``anim_ms`` 是相对动画锚点（行的显示窗口起点）已过的毫秒数；``None``
    保持旧的静态首帧行为（布局度量、缩略图等非时间轴调用方）。
    """
    if not path:
        return None
    signature = image_file_signature(path)
    if signature is None:
        return None
    animated = animated_bitmap_guide(path)
    if animated is not None:
        index = animated.frame_at(0 if anim_ms is None else anim_ms)
        return GuideAnimationFrame(animated.frames[index], index, (signature, index))
    image = bitmap_guide_image(path)
    if image is None or image.isNull():
        return None
    return GuideAnimationFrame(image, 0, (signature, 0))


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
    "animated_bitmap_guide",
    "bitmap_guide_content_size",
    "bitmap_guide_frame_at",
    "bitmap_guide_image",
    "vector_glyph_width",
]
