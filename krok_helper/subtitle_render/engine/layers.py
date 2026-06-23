"""Layer compositor primitives for subtitle rendering.

P1.b starts by making the layer contract explicit, while the existing painter
continues to own the concrete text/ruby/signal rendering.  Individual effects
can migrate onto this compositor one by one under pixel regression tests.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Hashable, Protocol

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QImage, QPainter, QTransform


LayerScope = str
SCOPE_GLYPH: LayerScope = "glyph"
SCOPE_LINE: LayerScope = "line"
SCOPE_GROUP: LayerScope = "group"
SCOPE_FRAME: LayerScope = "frame"


@dataclass(frozen=True)
class LayerContext:
    """Per-frame inputs shared by layer layout/animation/paint."""

    t_ms: int
    logical_w: int
    logical_h: int


@dataclass(frozen=True)
class BakedLayer:
    """A time-independent transparent bitmap produced by ``SubtitleLayer.bake``."""

    image: QImage
    offset: QPointF = field(default_factory=QPointF)


@dataclass(frozen=True)
class LayerAnimation:
    """Time-dependent compositing state for a baked layer."""

    top_left: QPointF = field(default_factory=QPointF)
    opacity: float = 1.0
    clip_rect: QRectF | None = None
    transform: QTransform | None = None


class SubtitleLayer(Protocol):
    """Effect layer contract for the P1.b compositor.

    ``layout`` must be pure geometry for the given context.  ``static_key`` being
    non-None means the compositor may cache ``bake`` and then apply only the
    per-frame ``animate`` state.  A None key marks the layer as dynamic and
    routes it to ``paint_dynamic``.
    """

    z_index: int
    scope: LayerScope

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        ...

    def layout(self, ctx: LayerContext) -> object:
        ...

    def static_key(self, ctx: LayerContext, layout: object) -> Hashable | None:
        ...

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        ...

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        ...

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        ...

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        ...


class LayerCache:
    """Small LRU cache for baked layer bitmaps."""

    def __init__(self, max_items: int = 128) -> None:
        self.max_items = max(max_items, 1)
        self._items: OrderedDict[Hashable, BakedLayer] = OrderedDict()

    def clear(self) -> None:
        self._items.clear()

    def get_or_build(self, key: Hashable, build) -> BakedLayer:
        cached = self._items.get(key)
        if cached is not None:
            self._items.move_to_end(key)
            return cached
        baked = build()
        self._items[key] = baked
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)
        return baked

    def __len__(self) -> int:
        return len(self._items)


class LayerCompositor:
    """Composes static and dynamic subtitle layers in z-order."""

    def __init__(self, cache: LayerCache | None = None) -> None:
        self.cache = cache if cache is not None else LayerCache()

    def paint(self, painter: QPainter, ctx: LayerContext, layers: list[SubtitleLayer]) -> None:
        self.paint_ordered(painter, ctx, sorted(layers, key=lambda item: item.z_index))

    def paint_ordered(
        self, painter: QPainter, ctx: LayerContext, layers: list[SubtitleLayer]
    ) -> None:
        """Paint layers that are already in desired z-order."""
        for layer in layers:
            if not _is_layer_active(layer, ctx):
                continue
            layout = layer.layout(ctx)
            key = layer.static_key(ctx, layout)
            if key is None:
                layer.paint_dynamic(painter, ctx, layout)
                continue
            cache_key = (type(layer), key)
            baked = self.cache.get_or_build(
                cache_key,
                lambda layer=layer, ctx=ctx, layout=layout, key=key: layer.bake(ctx, layout, key),
            )
            animation = layer.animate(ctx, layout)
            _paint_baked_layer(painter, baked, animation)

    def vertical_bounds(
        self, ctx: LayerContext, layers: list[SubtitleLayer]
    ) -> tuple[int, int] | None:
        bounds: list[tuple[int, int]] = []
        for layer in layers:
            if not _is_layer_active(layer, ctx):
                continue
            layout = layer.layout(ctx)
            layer_bounds = layer.vertical_bounds(ctx, layout)
            if layer_bounds is not None:
                bounds.append(layer_bounds)
        if not bounds:
            return None
        return min(top for top, _ in bounds), max(bottom for _, bottom in bounds)


def _is_layer_active(layer: SubtitleLayer, ctx: LayerContext) -> bool:
    windows = layer.active_window(ctx)
    return not windows or any(start <= ctx.t_ms <= end for start, end in windows)


def _paint_baked_layer(
    painter: QPainter,
    baked: BakedLayer,
    animation: LayerAnimation,
) -> None:
    opacity = max(0.0, min(float(animation.opacity), 1.0))
    if opacity <= 0.0 or baked.image.isNull():
        return
    top_left = animation.top_left + baked.offset
    painter.save()
    try:
        painter.setOpacity(painter.opacity() * opacity)
        if animation.clip_rect is not None:
            painter.setClipRect(animation.clip_rect)
        if animation.transform is not None:
            painter.setTransform(animation.transform, combine=True)
        painter.drawImage(top_left, baked.image)
    finally:
        painter.restore()
