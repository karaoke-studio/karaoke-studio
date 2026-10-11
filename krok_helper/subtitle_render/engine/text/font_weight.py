"""Phase 3: Font Weight — 渲染胶水（顺应引擎，2026-10-07 拍板）。

架构：

  Phase 1  font_capabilities.get_capabilities(family) → FontCapabilities
           （纯数据：逐 face 清单 + 逐 face 轴信息，含别名规范化与缓存）

  Phase 2  weight_resolver.resolve_instance(capabilities, request)
           → ResolvedFontInstance（可变=轴值实例；静态=确定的 face +
             合成标注；斜体参与决策）

  Phase 3  本模块：把决策应用到 QFont（薄胶水），并把同一份决策交给调用方
           下发 native 侧（``instance_fields()``）。

**顺应引擎**：字重回到绝对字重，模拟加粗/倾斜交还 Qt/DirectWrite 引擎。
本模块不做任何「自己膨胀」——静态字体直接 ``setWeight(请求字重)`` 让引擎
匹配 + 合成；可变字体用 ``setVariableAxis`` 设真实轴值。

CPU 侧刻意保持「请求而不是钉死 face」：Qt 的引擎匹配就是本仓库 v4.2.x 画面
基线的定义（``QFontInfo`` 实测校准见 weight_resolver 模块注释），钉 styleName
反而会在 TTC/网络字体上改变选 face 结果。决策里的 face 名/字重是**可验证的
描述**：测试用 ``QFontInfo`` 交叉验证预测与引擎实际命中一致，native 侧则直接
执行该决策（不再自行就地匹配）。
"""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QPainterPath

from krok_helper.subtitle_render.engine.text.font_capabilities import (
    FontCapabilities,
    FontFace,
    canonical_family,
    capabilities_generation,
    clear_capabilities_cache,
    get_capabilities,
)
from krok_helper.subtitle_render.engine.text.weight_resolver import (
    FontRequest,
    ResolvedFontInstance,
    ResolvedWeight,
    _legacy_weight,
    resolve,
    resolve_instance,
)

_AXIS_TAG_WEIGHT = b"wght"


def resolve_font_instance(
    family: str,
    weight: int,
    italic: bool = False,
    stretch_pct: int = 100,
) -> ResolvedFontInstance:
    """(family, 字重, 斜体, 拉伸) → 字体实例决策（带字体库代际）。"""
    capabilities = get_capabilities(family)
    request = FontRequest(
        family=str(family),
        weight=int(weight),
        italic=bool(italic),
        stretch_pct=int(stretch_pct),
    )
    instance = resolve_instance(capabilities, request)
    return replace(instance, generation=capabilities_generation())


def instance_fields(instance: ResolvedFontInstance) -> dict[str, object]:
    """决策 → IR 载荷字段（native 侧按同一份数据执行，不再自行匹配）。"""
    return {
        "family": instance.family,
        "face_style": instance.face_style,
        "face_weight": int(instance.face_weight),
        "axis": instance.axis_value,
        "synthetic_bold": bool(instance.synthetic_bold),
        "synthetic_italic": bool(instance.synthetic_italic),
        "exact": bool(instance.exact),
        "reason": instance.reason,
        "variable": bool(instance.is_variable),
        "identity": instance.identity,
    }


def resolve_weight_plan(
    family: str, weight: int, italic: bool = False
) -> ResolvedWeight:
    """(family, 请求字重, 请求斜体) → 解析决策（旧口径，UI/协议在用）。"""
    capabilities = get_capabilities(family)
    instance = resolve_instance(
        capabilities,
        FontRequest(family=str(family), weight=int(weight), italic=bool(italic)),
    )
    return _legacy_weight(instance, capabilities)


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


def apply_font_instance(font: QFont, instance: ResolvedFontInstance) -> None:
    """把实例决策应用到 QFont（调用方已设 family/pixelSize）。

    可变字体：只设轴值，不调 setWeight——实测（Noto Sans SC@600，
    2026-10-08）setWeight 会把 Qt 匹配吸附到最近命名实例/静态 face
    并使轴值失效（@600 平局档吸附到 Bold 且非单调）；轴值本身即权威实例。
    静态字体：绝对字重交给引擎匹配 + 合成粗体/倾斜。
    """
    if instance.axis_value is not None:
        font.setVariableAxis(
            QFont.Tag(_AXIS_TAG_WEIGHT), float(instance.axis_value)
        )
        return
    font.setWeight(QFont.Weight(int(instance.requested.weight)))


def apply_weight_plan(font: QFont, plan: ResolvedWeight) -> None:
    """把解析决策应用到 QFont（旧接口，等价 apply_font_instance）。"""
    if plan.render_mode == "axis":
        font.setVariableAxis(
            QFont.Tag(_AXIS_TAG_WEIGHT), float(plan.axis_value or 400)
        )
        return
    font.setWeight(QFont.Weight(plan.requested_weight))


def build_weight_font(
    family: str, size_px: int, weight: int, italic: bool = False
) -> QFont:
    """按统一口径构造 QFont。"""
    font = QFont(family, max(int(size_px), 1))
    font.setPixelSize(max(int(size_px), 1))
    instance = resolve_font_instance(family, weight, italic=italic)
    apply_font_instance(font, instance)
    if italic:
        font.setItalic(True)
    return font


def clear_font_weight_cache() -> None:
    """字体装卸后清空进程级缓存（能力层 + 缺字回退层）。"""
    clear_capabilities_cache()
    try:  # 延迟导入：font_fallback 正向依赖本模块，模块级反向导入会成环。
        from krok_helper.subtitle_render.engine.text.font_fallback import (
            clear_font_fallback_cache,
        )

        clear_font_fallback_cache()
    except Exception:  # noqa: BLE001 — 缓存清理失败不影响字体装卸主流程
        pass


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
    "FontRequest",
    "ResolvedFontInstance",
    "face_inventory",
    "family_weight_axis",
    "physical_weight_styles",
    "FontFace",
    "ResolvedWeight",
    "apply_font_instance",
    "apply_weight_plan",
    "bucket_weight",
    "build_weight_font",
    "canonical_family",
    "clear_font_weight_cache",
    "get_capabilities",
    "instance_fields",
    "resolve",
    "resolve_font_instance",
    "resolve_weight_plan",
    "winding_glyph_path",
]
