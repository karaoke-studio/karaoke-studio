"""Tests for the P1.b subtitle layer compositor skeleton."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Hashable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, QRectF  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402

from krok_helper.subtitle_render.engine.layers import (  # noqa: E402
    BakedLayer,
    LayerAnimation,
    LayerCache,
    LayerCompositor,
    LayerContext,
    SCOPE_LINE,
)


@dataclass
class _SolidLayer:
    color: str
    point: QPointF
    z_index: int = 0
    key: Hashable = "solid"
    scope: str = SCOPE_LINE
    bake_count: int = 0

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> object:
        return self.point

    def static_key(self, ctx: LayerContext, layout: object) -> Hashable | None:
        return self.key

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        self.bake_count += 1
        image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(self.color))
        return BakedLayer(image)

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation(top_left=layout)

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        raise AssertionError("static layer should be baked")

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        y = int(self.point.y())
        return y, y + 8


class _WindowedLayer(_SolidLayer):
    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return [(100, 200)]


class _DynamicLayer(_SolidLayer):
    def static_key(self, ctx: LayerContext, layout: object) -> Hashable | None:
        return None

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        painter.fillRect(QRectF(layout.x(), layout.y(), 8.0, 8.0), QColor(self.color))


def _blank() -> QImage:
    image = QImage(32, 24, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#000000"))
    return image


def _pixel(image: QImage, x: int, y: int) -> str:
    return QColor(image.pixel(x, y)).name(QColor.NameFormat.HexRgb).upper()


def test_layer_compositor_bakes_once_and_reuses_cache():
    layer = _SolidLayer("#FF0000", QPointF(4.0, 5.0))
    compositor = LayerCompositor()
    ctx = LayerContext(t_ms=100, logical_w=32, logical_h=24)

    for _ in range(2):
        image = _blank()
        painter = QPainter(image)
        try:
            compositor.paint(painter, ctx, [layer])
        finally:
            painter.end()
        assert _pixel(image, 5, 6) == "#FF0000"

    assert layer.bake_count == 1
    assert len(compositor.cache) == 1


def test_layer_compositor_paints_in_z_order():
    red = _SolidLayer("#FF0000", QPointF(4.0, 5.0), z_index=0, key="red")
    blue = _SolidLayer("#0000FF", QPointF(4.0, 5.0), z_index=1, key="blue")
    image = _blank()
    painter = QPainter(image)
    try:
        LayerCompositor().paint(
            painter,
            LayerContext(t_ms=100, logical_w=32, logical_h=24),
            [blue, red],
        )
    finally:
        painter.end()

    assert _pixel(image, 5, 6) == "#0000FF"


def test_layer_compositor_respects_active_windows():
    layer = _WindowedLayer("#00FF00", QPointF(4.0, 5.0))
    compositor = LayerCompositor()

    image = _blank()
    painter = QPainter(image)
    try:
        compositor.paint(painter, LayerContext(t_ms=50, logical_w=32, logical_h=24), [layer])
    finally:
        painter.end()

    assert _pixel(image, 5, 6) == "#000000"
    assert layer.bake_count == 0


def test_layer_compositor_supports_dynamic_layers():
    layer = _DynamicLayer("#00FF00", QPointF(4.0, 5.0))
    image = _blank()
    painter = QPainter(image)
    try:
        LayerCompositor().paint(
            painter,
            LayerContext(t_ms=100, logical_w=32, logical_h=24),
            [layer],
        )
    finally:
        painter.end()

    assert _pixel(image, 5, 6) == "#00FF00"
    assert layer.bake_count == 0


def test_layer_cache_evicts_least_recently_used_item():
    cache = LayerCache(max_items=1)
    compositor = LayerCompositor(cache)
    ctx = LayerContext(t_ms=100, logical_w=32, logical_h=24)
    first = _SolidLayer("#FF0000", QPointF(0.0, 0.0), key="first")
    second = _SolidLayer("#0000FF", QPointF(0.0, 0.0), key="second")
    image = _blank()
    painter = QPainter(image)
    try:
        compositor.paint(painter, ctx, [first])
        compositor.paint(painter, ctx, [second])
        compositor.paint(painter, ctx, [first])
    finally:
        painter.end()

    assert first.bake_count == 2
    assert second.bake_count == 1
    assert len(cache) == 1


def test_layer_compositor_unions_vertical_bounds():
    low = _SolidLayer("#FF0000", QPointF(0.0, 10.0))
    high = _SolidLayer("#0000FF", QPointF(0.0, 2.0))

    assert LayerCompositor().vertical_bounds(
        LayerContext(t_ms=100, logical_w=32, logical_h=24),
        [low, high],
    ) == (2, 18)
