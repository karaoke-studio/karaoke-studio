"""Tests for the P1.b subtitle layer compositor skeleton."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Hashable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, QRectF  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter, QTransform  # noqa: E402

from krok_helper.subtitle_render.engine.layers import (  # noqa: E402
    BakedLayer,
    LayerAnimation,
    LayerCache,
    LayerCompositor,
    LayerContext,
    SCOPE_GROUP,
    SCOPE_LINE,
    _paint_baked_layer,
    _pivoted_transform,
)


@dataclass
class _SolidLayer:
    color: str
    point: QPointF
    z_index: int = 0
    key: Hashable = "solid"
    scope: str = SCOPE_LINE
    scope_id: Hashable | None = None
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


def test_layer_compositor_groups_scope_boxes_by_scope():
    line = _SolidLayer("#FF0000", QPointF(0.0, 10.0), scope=SCOPE_LINE)
    group_a = _SolidLayer("#00FF00", QPointF(0.0, 2.0), scope=SCOPE_GROUP)
    group_b = _SolidLayer("#0000FF", QPointF(0.0, 18.0), scope=SCOPE_GROUP)

    boxes = LayerCompositor().scope_boxes(
        LayerContext(t_ms=100, logical_w=32, logical_h=48),
        [line, group_a, group_b],
    )

    assert [box.scope for box in boxes] == [SCOPE_LINE, SCOPE_GROUP]
    assert boxes[0].layer_count == 1
    assert boxes[1].layer_count == 2
    assert int(boxes[1].rect.top()) == 2
    assert int(boxes[1].rect.bottom()) == 26


def test_layer_compositor_keeps_scope_ids_separate():
    group_a_top = _SolidLayer(
        "#FF0000", QPointF(0.0, 2.0), scope=SCOPE_GROUP, scope_id="phrase-a"
    )
    group_a_bottom = _SolidLayer(
        "#00FF00", QPointF(0.0, 18.0), scope=SCOPE_GROUP, scope_id="phrase-a"
    )
    group_b = _SolidLayer(
        "#0000FF", QPointF(0.0, 10.0), scope=SCOPE_GROUP, scope_id="phrase-b"
    )
    ctx = LayerContext(t_ms=100, logical_w=32, logical_h=48)
    compositor = LayerCompositor()

    boxes = compositor.scope_boxes(ctx, [group_b, group_a_bottom, group_a_top])

    assert [(box.scope, box.scope_id) for box in boxes] == [
        (SCOPE_GROUP, "phrase-a"),
        (SCOPE_GROUP, "phrase-b"),
    ]
    assert boxes[0].layer_count == 2
    assert int(boxes[0].rect.top()) == 2
    assert int(boxes[0].rect.bottom()) == 26
    rect = compositor.scope_rect(ctx, [group_a_top, group_a_bottom], SCOPE_GROUP, "phrase-a")
    assert rect is not None
    assert int(rect.top()) == 2
    assert int(rect.bottom()) == 26


def test_pivoted_transform_keeps_origin_fixed():
    origin = QPointF(4.0, 4.0)
    pivoted = _pivoted_transform(QTransform().rotate(90.0), origin)

    mapped = pivoted.map(origin)
    assert round(mapped.x(), 6) == 4.0
    assert round(mapped.y(), 6) == 4.0

    moved = pivoted.map(QPointF(8.0, 4.0))  # offset from the pivot must move
    assert (round(moved.x(), 6), round(moved.y(), 6)) != (8.0, 4.0)


def test_pivoted_transform_none_origin_is_identity_passthrough():
    transform = QTransform().rotate(30.0).scale(2.0, 1.5)
    assert _pivoted_transform(transform, None) is transform


def test_paint_baked_layer_pivots_transform_about_origin():
    # Left half red, right half blue; 180° about the centre swaps the halves.
    baked_image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
    baked_image.fill(QColor("#0000FF"))
    red = QPainter(baked_image)
    try:
        red.fillRect(QRectF(0.0, 0.0, 4.0, 8.0), QColor("#FF0000"))
    finally:
        red.end()

    animation = LayerAnimation(
        transform=QTransform().rotate(180.0),
        transform_origin=QPointF(4.0, 4.0),
    )

    image = _blank()
    painter = QPainter(image)
    try:
        _paint_baked_layer(painter, BakedLayer(baked_image), animation)
    finally:
        painter.end()

    assert _pixel(image, 6, 4) == "#FF0000"  # right side now red
    assert _pixel(image, 2, 4) == "#0000FF"  # left side now blue


def test_layer_compositor_ignores_inactive_scope_boxes():
    layer = _WindowedLayer("#00FF00", QPointF(4.0, 5.0), scope=SCOPE_GROUP)

    assert LayerCompositor().scope_boxes(
        LayerContext(t_ms=50, logical_w=32, logical_h=24),
        [layer],
    ) == []
