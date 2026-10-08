"""Phase 3: Font Weight — 渲染胶水（消费 Phase 1 + Phase 2 的结果）。

架构（2026-10-07 重做，替换 v6~v7.2 的全部决策代码）：

  Phase 1  font_capabilities.get_capabilities(family) → FontCapabilities
           （纯数据：face 清单 + 轴信息，含别名规范化与缓存）

  Phase 2  weight_resolver.resolve(capabilities, weight) → ResolvedWeight
           （W3C CSS §5.2 匹配算法，纯函数）

  Phase 3  本模块：把 ResolvedWeight 转成 QFont / IR 字段 / 膨胀量
           （薄胶水，无决策逻辑）

本模块不再包含任何字体匹配/选择规则——所有分支在 weight_resolver.py
里，可独立穷举测试。
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QPainterPath, QPainterPathStroker

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

# 合成粗体的膨胀宽度比率（v4.2.x 引擎合成粗体实测强度）。



# ---------------------------------------------------------------------------
# 公共 API（兼容旧调用方的函数签名）
# ---------------------------------------------------------------------------


def resolve_weight_plan(
    family: str, weight: int, italic: bool = False
) -> ResolvedWeight:
    """(family, 请求字重, 请求斜体) → 渲染决策。

    兼容旧 ``FontWeightPlan`` 的调用方：返回 ``ResolvedWeight``，字段
    名不同但信息等价（``base_weight`` / ``style_name`` / ``render_mode``
    / ``axis_value``）。旧的 ``synthetic_bold`` → ``needs_synthetic``，
    ``embolden_delta`` → ``needs_synthetic``（阶跃语义）。
    """
    capabilities = get_capabilities(family)
    return resolve(capabilities, weight)


def embolden_width_px(font_size_px: int, delta: int) -> float:
    """模拟放大的膨胀描边宽 = 字号 × Δ / 10000。

    比例与 v4.2.x 一致、以楷体为准（楷体 @600 即 Δ=200 → 约 2% em）。
    """
    if delta <= 0 or font_size_px <= 0:
        return 0.0
    # 比例与 v4.2.x 楷体 faux bold 一致（实测固定 +2.17%em，见
    # probe_kaiti_ratio.py）：@700 即 Δ=300 → 字号×2.17%em。
    return float(font_size_px) * float(delta) / 13800.0


# ---------------------------------------------------------------------------
# 膨胀量传递（apply 时登记签名 → 绘制时查表）
# ---------------------------------------------------------------------------

_EMBOLDEN_BY_SIGNATURE: dict[tuple, int] = {}
_SIGNATURE_MAX = 4096
_LOCK = threading.Lock()


def font_signature(font: QFont) -> tuple:
    """QFont 的解析签名。"""
    axis_tag = QFont.Tag(b"wght")
    axis_value = (
        float(font.variableAxisValue(axis_tag))
        if font.isVariableAxisSet(axis_tag)
        else None
    )
    return (
        font.family(),
        font.pixelSize(),
        int(font.weight()),
        font.italic(),
        font.stretch(),
        font.styleName(),
        axis_value,
    )


def embolden_delta_of_font(font: QFont) -> int:
    """从 QFont 反查模拟放大的字重差 Δ（无放大返回 0）。"""
    with _LOCK:
        return int(_EMBOLDEN_BY_SIGNATURE.get(font_signature(font), 0))


def embolden_glyph_path(path: QPainterPath, font: QFont) -> QPainterPath:
    """模拟放大时对字形轮廓做圆形膨胀。"""
    # OpenType 轮廓规范要求 NonZero(Winding) 填充；Qt addText() 默认
    # OddEvenFill，可变字体 gvar 插值导致轮廓重叠时 OddEven 会在笔画
    # 交叉处产生空洞。静态字体两种规则结果相同，统一设 Winding 无副作用。
    path.setFillRule(Qt.FillRule.WindingFill)
    if path.isEmpty():
        return path
    delta = embolden_delta_of_font(font)
    if delta <= 0:
        return path
    width = embolden_width_px(font.pixelSize(), delta)
    if width <= 0.0:
        return path
    stroker = QPainterPathStroker()
    stroker.setWidth(width)
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return path.united(stroker.createStroke(path))


# ---------------------------------------------------------------------------
# QFont 构造
# ---------------------------------------------------------------------------


def apply_weight_plan(font: QFont, plan: ResolvedWeight) -> None:
    """把渲染决策应用到 QFont（调用方已设 family/pixelSize）。"""
    if plan.render_mode == "axis":
        font.setVariableAxis(
            QFont.Tag(_AXIS_TAG_WEIGHT), float(plan.axis_value or 400)
        )
        font.setWeight(QFont.Weight(int(plan.axis_value or 400)))
        _record_signature(font, 0)
        return

    if plan.render_mode == "embolden":
        # 钉住基准 face，weight 停在基准字重（不触发引擎合成），
        # 膨胀量经签名旁路表交给 embolden_glyph_path。
        if plan.base_face is not None:
            font.setStyleName(plan.base_face.style_name)
            font.setWeight(QFont.Weight(plan.base_face.weight))
        else:
            font.setWeight(QFont.Weight(plan.base_weight))
        _record_signature(font, plan.embolden_delta)
        return

    # face / snap / missing：渲染基准 face 或按请求值让 Qt 匹配器解析。
    if plan.base_face is not None:
        font.setStyleName(plan.base_face.style_name)
        font.setWeight(QFont.Weight(plan.base_face.weight))
    else:
        font.setWeight(QFont.Weight(bucket_weight(plan.requested_weight)))
    _record_signature(font, 0)


def _record_signature(font: QFont, delta: int) -> None:
    with _LOCK:
        if len(_EMBOLDEN_BY_SIGNATURE) >= _SIGNATURE_MAX:
            _EMBOLDEN_BY_SIGNATURE.clear()
        _EMBOLDEN_BY_SIGNATURE[font_signature(font)] = delta


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


# ---------------------------------------------------------------------------
# 缓存清理
# ---------------------------------------------------------------------------


def clear_font_weight_cache() -> None:
    """字体装卸后清空全部进程级缓存。"""
    clear_capabilities_cache()
    with _LOCK:
        _EMBOLDEN_BY_SIGNATURE.clear()


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


# ---------------------------------------------------------------------------
# 向下兼容（旧调用方逐步迁移到新 API）
# ---------------------------------------------------------------------------


def family_weight_axis(family: str):
    """兼容旧 API → get_capabilities(family).axis 信息。"""
    cap = get_capabilities(family)
    if cap is None or not cap.axis_effective:
        return None
    from krok_helper.subtitle_render.engine.text.font_capabilities import (
        FontCapabilities,
    )

    # 旧 API 返回 WeightAxis 对象，这里用简单命名空间替代
    class _Axis:
        pass

    axis = _Axis()
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
    "embolden_delta_of_font",
    "embolden_glyph_path",
    "embolden_width_px",
    "font_signature",
    "get_capabilities",
    "resolve",
    "resolve_weight_plan",
]
