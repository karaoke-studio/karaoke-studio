"""Tests for the §9 B-tier quantisation primitives (engine/quantize.py)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF  # noqa: E402
from PyQt6.QtGui import QTransform  # noqa: E402

from krok_helper.subtitle_render.engine import quantize as q  # noqa: E402


def _scale_rotate(sx: float, sy: float, deg: float, dx: float = 0.0, dy: float = 0.0) -> QTransform:
    t = QTransform()
    t.translate(dx, dy)
    t.rotate(deg)
    t.scale(sx, sy)
    return t


def test_quantize_linear_key_ignores_translation():
    base = _scale_rotate(1.2, 1.2, 7.0, dx=0.0, dy=0.0)
    moved = _scale_rotate(1.2, 1.2, 7.0, dx=137.0, dy=-42.0)
    assert q.quantize_linear_key(base) == q.quantize_linear_key(moved)


def test_quantize_linear_key_merges_subquantum_neighbours():
    step = q._QUANT_LINEAR_STEP
    a = QTransform(1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    # 在半步内扰动 → 同桶。
    b = QTransform(1.0 + step * 0.3, 0.0, 0.0, 1.0 - step * 0.3, 0.0, 0.0)
    assert q.quantize_linear_key(a) == q.quantize_linear_key(b)


def test_quantize_linear_key_separates_across_bucket():
    step = q._QUANT_LINEAR_STEP
    a = QTransform(1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    c = QTransform(1.0 + step * 1.5, 0.0, 0.0, 1.0, 0.0, 0.0)
    assert q.quantize_linear_key(a) != q.quantize_linear_key(c)


def test_quantized_linear_transform_is_idempotent():
    t = _scale_rotate(1.27, 1.13, 11.0, dx=50.0, dy=9.0)
    once = q.quantized_linear_transform(t)
    twice = q.quantized_linear_transform(once)
    assert q.quantize_linear_key(once) == q.quantize_linear_key(t)
    for getter in ("m11", "m12", "m21", "m22"):
        assert getattr(once, getter)() == getattr(twice, getter)()
    # 平移被清零（由残差承担）。
    assert once.dx() == 0.0 and once.dy() == 0.0


def test_residual_reconstructs_actual_transform():
    actual = _scale_rotate(1.3, 1.15, 17.0, dx=200.0, dy=-30.0)
    quant = q.quantized_linear_transform(actual)
    residual = q.residual_transform(actual, quant)
    combined = quant * residual  # 行向量：先 quant 后 residual == actual
    p = QPointF(73.0, -19.0)
    a = actual.map(p)
    c = combined.map(p)
    assert abs(a.x() - c.x()) < 1e-6
    assert abs(a.y() - c.y()) < 1e-6


def test_quantisation_error_is_bounded_within_reference_extent():
    # 量子化只动线性部分；参考半径内一点的线性位移误差应 ≲ 误差预算。
    actual = _scale_rotate(1.21, 1.08, 9.0)
    quant = q.quantized_linear_transform(actual)
    r = q._QUANT_REF_EXTENT_PX
    budget = q._QUANT_ERROR_PX
    for p in (QPointF(r, 0.0), QPointF(0.0, r), QPointF(r, r)):
        # 线性映射（去掉平移，quant 平移本就为 0）。
        a = QPointF(actual.m11() * p.x() + actual.m21() * p.y(),
                    actual.m12() * p.x() + actual.m22() * p.y())
        c = QPointF(quant.m11() * p.x() + quant.m21() * p.y(),
                    quant.m12() * p.x() + quant.m22() * p.y())
        # 每轴两个系数各偏 ≤ step/2 → 误差 ≤ R·step = budget（(r,r) 处放宽到 2×）。
        tol = budget * (2.0 if p.x() and p.y() else 1.0) + 1e-6
        assert abs(a.x() - c.x()) <= tol
        assert abs(a.y() - c.y()) <= tol


def test_quantize_blur_and_stroke_round_to_steps():
    assert q.quantize_blur(0.0) == 0
    assert q.quantize_blur(2.4) == 2
    assert q.quantize_blur(2.6) == 3
    assert q.quantize_blur(-5.0) == 0
    assert q.quantize_stroke(8.5) == 8 or q.quantize_stroke(8.5) == 9  # banker's rounding tolerant
    assert q.quantize_stroke(0.0) == 0
