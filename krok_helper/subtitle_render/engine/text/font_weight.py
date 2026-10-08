"""Phase 3: Font Weight — 渲染胶水（顺应引擎，2026-10-07 拍板）。

架构：

  Phase 1  font_capabilities.get_capabilities(family) → FontCapabilities
           （纯数据：face 清单 + 轴信息，含别名规范化与缓存）

  Phase 2  weight_resolver.resolve(capabilities, weight) → ResolvedWeight
           （可变=轴值，静态=引擎匹配标注）

  Phase 3  本模块：把 ResolvedWeight 转成 QFont（薄胶水）。

**顺应引擎**：字重回到绝对字重，模拟加粗交还 Qt/DirectWrite 引擎。
本模块不再做任何「自己膨胀」——静态字体直接 ``setWeight(请求字重)``
让引擎匹配 + 合成粗体；可变字体用 ``setVariableAxis`` 设真实轴值。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QPainterPath

from krok_helper.subtitle_render.engine.text.font_capabilities import (
    FontCapabilities,
    FontFace,
    canonical_family,
    clear_capabilities_cache,
    get_capabilities,
)
from krok_helper.subtitle_render.engine.text.weight_resolver import (
    ResolvedWeight,
    resolve,
)

_AXIS_TAG_WEIGHT = b"wght"


def resolve_weight_plan(
    family: str, weight: int, italic: bool = False
) -> ResolvedWeight:
    """(family, 请求字重, 请求斜体) → 解析决策。"""
    capabilities = get_capabilities(family)
    return resolve(capabilities, weight)


def winding_glyph_path(path: QPainterPath, _font: QFont | None = None) -> QPainterPath:
    """字形轮廓统一用 NonZero(Winding) 填充。

    OpenType 轮廓规范要求 NonZero 填充；Qt ``addText()`` 默认
    OddEvenFill，可变字体 gvar 插值导致轮廓重叠时 OddEven 会在笔画
    交叉处产生空洞。静态字体两种规则结果相同，统一设 Winding 无副作用。
    第二参仅为兼容旧「我们膨胀」调用点，顺应引擎后忽略。
    """
    path.setFillRule(Qt.FillRule.WindingFill)
    return path


# 兼容旧调用名（曾用于「我们膨胀」，顺应引擎后仅保留 WindingFill 职责）。
embolden_glyph_path = winding_glyph_path


def apply_weight_plan(font: QFont, plan: ResolvedWeight) -> None:
    """把解析决策应用到 QFont（调用方已设 family/pixelSize）。"""
    if plan.render_mode == "axis":
        font.setVariableAxis(
            QFont.Tag(_AXIS_TAG_WEIGHT), float(plan.axis_value or 400)
        )
        font.setWeight(QFont.Weight(int(plan.axis_value or 400)))
        return
    # 静态字体：绝对字重交给引擎匹配 + 合成粗体。
    font.setWeight(QFont.Weight(plan.requested_weight))


def build_weight_font(
    family: str, size_px: int, weight: int, italic: bool = False
) -> QFont:
    """按统一口径构造 QFont。"""
    font = QFont(family, max(int(size_px), 1))
    font.setPixelSize(max(int(size_px), 1))
    plan = resolve_weight_plan(family, weight, italic=False)
    apply_weight_plan(font, plan)
    if italic:
        font.setItalic(True)
    return font


def clear_font_weight_cache() -> None:
    """字体装卸后清空进程级缓存。"""
    clear_capabilities_cache()


# ---------------------------------------------------------------------------
# 向下兼容（UI 层展示用，不参与决策）
# ---------------------------------------------------------------------------

_WEIGHT_BUCKETS: tuple[tuple[int, int], ...] = (
    (250, 100),
    (350, 300),
    (450, 400),
    (550, 500),
    (650, 600),
    (750, 700),
    (850, 800),
    (1000, 900),
)


def bucket_weight(weight: int) -> int:
    """把任意请求字重映射到标准整百（UI 展示用）。"""
    value = int(weight)
    for upper, bucket in _WEIGHT_BUCKETS:
        if value <= upper:
            return bucket
    return 900


def family_weight_axis(family: str):
    """兼容旧 API → get_capabilities(family).axis 信息。"""
    cap = get_capabilities(family)
    if cap is None or not cap.axis_effective:
        return None
    axis = type("_Axis", (), {})()
    axis.minimum = cap.axis_min
    axis.maximum = cap.axis_max
    axis.default = cap.axis_default
    return axis


def face_inventory(family: str):
    """兼容旧 API → get_capabilities(family).faces。"""
    cap = get_capabilities(family)
    return cap.faces if cap else ()


def physical_weight_styles(family: str):
    """兼容旧 API → capabilities 的直立 face (weight, style) 清单。"""
    cap = get_capabilities(family)
    if not cap:
        return ()
    seen = {}
    for face in cap.faces:
        if not face.is_italic and (
            face.weight not in seen or face.style_name < seen[face.weight]
        ):
            seen[face.weight] = face.style_name
    return tuple(sorted(seen.items()))


__all__ = [
    "FontCapabilities",
    "face_inventory",
    "family_weight_axis",
    "physical_weight_styles",
    "FontFace",
    "ResolvedWeight",
    "apply_weight_plan",
    "bucket_weight",
    "build_weight_font",
    "canonical_family",
    "clear_font_weight_cache",
    "get_capabilities",
    "resolve",
    "resolve_weight_plan",
    "winding_glyph_path",
]
