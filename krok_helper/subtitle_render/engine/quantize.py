"""量子化变换 / 缓存键工具（§9 B 档地基）。

动机（§9.2 / §9.3）：动画特效（utopia 缩放/旋转、spin skew）下，「烘焙位图 + 每帧
QTransform」会软化；正确做法（libass PR#285）是**把变换量子化成离散桶**当缓存键——
慢/连续动画里相邻帧落同一桶 → 复用同一份**从矢量重栅**的烘焙（锐利、零软化），只有
跨桶才重栅一次；残余的「子量子变换 + 平移」在合成时套用（极小、肉眼不可见）。

本模块只提供纯函数原语（不碰渲染热路径），供 B2 把 utopia body 接上量子化重栅缓存。

量子化口径：
- **只量子化仿射的线性部分**（``m11 m12 m21 m22`` = 缩放/旋转/skew）；**平移是免费残差**，
  不进键（平移不改变栅格内容、只改 blit 落点）。
- 线性步长 ``_QUANT_LINEAR_STEP`` 按「可接受位移误差 / 参考字形半径」定（D1，§9.7）：
  半径 R 内一点经量子化后的位移误差 ≲ |Δ线性系数| · R，故步长 = 误差预算 / R。
  默认保守（偏锐利、命中略低），是可调常量。
"""

from __future__ import annotations

from PyQt6.QtGui import QTransform

# ---- D1（§9.7）：量子化步长 / 可接受像素误差（默认保守，可调）----------------------
_QUANT_REF_EXTENT_PX = 96.0
"""参考字形半径（1080p ~100px 字的量级）；线性误差按它折算到像素。"""

_QUANT_ERROR_PX = 0.4
"""可接受的量子化位移误差（像素）；越小越锐利、缓存命中越低。"""

_QUANT_LINEAR_STEP = _QUANT_ERROR_PX / _QUANT_REF_EXTENT_PX
"""线性系数（m11..m22）的量子化步长 ≈ 0.0042。"""

_QUANT_BLUR_STEP_PX = 1.0
"""blur 半径量子化步长（像素）。"""

_QUANT_STROKE_STEP_PX = 1.0
"""描边宽度量子化步长（像素）。"""


LinearKey = tuple[int, int, int, int]


def quantize_linear_key(transform: QTransform) -> LinearKey:
    """量子化仿射线性部分为缓存键（整数桶）；平移不参与。

    相邻帧（线性部分相差 < 半步）落同一键 → 命中同一烘焙；跨桶才重栅。
    """
    step = _QUANT_LINEAR_STEP
    return (
        round(transform.m11() / step),
        round(transform.m12() / step),
        round(transform.m21() / step),
        round(transform.m22() / step),
    )


def quantized_linear_transform(transform: QTransform) -> QTransform:
    """把线性部分吸附到量子格点、平移清零的变换——bake 即按它从矢量重栅。

    返回的变换对同一桶内的所有输入**完全相同**（idempotent），保证缓存键 ↔ 烘焙一一对应。
    """
    step = _QUANT_LINEAR_STEP
    return QTransform(
        round(transform.m11() / step) * step,
        round(transform.m12() / step) * step,
        round(transform.m21() / step) * step,
        round(transform.m22() / step) * step,
        0.0,
        0.0,
    )


def residual_transform(actual: QTransform, quantized: QTransform) -> QTransform:
    """合成时套在量子化烘焙上的残差变换：``quantized · residual == actual``。

    （Qt 行向量约定 ``p' = p·M``：``residual = quantized⁻¹ · actual``。）含子量子线性
    残差 + 完整平移；极小，软化不可见。``quantized`` 不可逆时退回 ``actual``。
    """
    inverse, ok = quantized.inverted()
    if not ok:
        return QTransform(actual)
    return inverse * actual


def quantize_blur(radius: float) -> int:
    """blur 半径量子化为整数桶（步长单位）。"""
    return max(0, round(float(radius) / _QUANT_BLUR_STEP_PX))


def quantize_stroke(width: float) -> int:
    """描边宽度量子化为整数桶（步长单位）。"""
    return max(0, round(float(width) / _QUANT_STROKE_STEP_PX))
