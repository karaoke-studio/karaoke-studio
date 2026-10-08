"""Phase 2: Weight Resolver — 字体解析决策（顺应引擎，2026-10-07 拍板）。

**顺应引擎**：字重回到绝对字重，模拟加粗交还给 Qt/DirectWrite 引擎
（v4.2.x 语义）。本模块只负责：

1. 可变字体：识别轴值实例（轴内插值、轴外钳制到端点）。
2. 静态字体：标注引擎行为——精确命中 / 引擎合成粗体 / 引擎就近。
   **渲染不做任何"我们膨胀"**，引擎匹配 + 合成自然发生。

引擎匹配口径（2026-10-08 实测 QFontInfo/像素校准，供标注）：
  请求 W 精确命中 → 该 face；
  缺档 → 就近匹配（|face−W| 最小，**平局取更轻 face**）；
  匹配 face < 600 且 W ≥ 600 → 引擎合成粗体（faux bold，粗 face 不可再加粗）。
  实测锚点：Yu Gothic{300,400,500,700}@600 → 500+合成（平局取轻），
  NK{400,700}@600 → 700（无平局就近），思源{…,500,700,…}@600 → 500+合成。

本模块无副作用、无 I/O、无缓存。
"""

from __future__ import annotations

from dataclasses import dataclass

from krok_helper.subtitle_render.engine.text.font_capabilities import (
    FontCapabilities,
    FontFace,
)


@dataclass(frozen=True)
class ResolvedWeight:
    """一个 (capabilities, weight) 请求的解析决策。"""

    render_mode: str
    """"axis" | "face" | "engine_synthetic" | "snap" | "missing"。"""

    axis_value: float | None = None
    """axis 模式的轴值。"""

    base_face: FontFace | None = None
    """face / engine_synthetic / snap 模式的引擎匹配到的 face。"""

    requested_weight: int = 400
    """原始请求字重（绝对）。"""

    is_exact: bool = False
    """请求是否被精确满足。"""

    # ── 便捷属性 ──

    @property
    def base_weight(self) -> int:
        if self.base_face is not None:
            return self.base_face.weight
        if self.axis_value is not None:
            return int(round(self.axis_value))
        return 400

    @property
    def style_name(self) -> str | None:
        return self.base_face.style_name if self.base_face else None

    @property
    def needs_synthetic(self) -> bool:
        return self.render_mode == "engine_synthetic"

    @property
    def mark(self) -> str | None:
        if self.render_mode == "engine_synthetic":
            return "模拟"
        if self.render_mode == "snap":
            return "就近"
        if self.render_mode == "axis" and not self.is_exact:
            return "越界"
        return None

    @property
    def synthetic_bold(self) -> bool:
        return self.needs_synthetic

    @property
    def is_variable(self) -> bool:
        return self.render_mode == "axis"


def _upright(capabilities: FontCapabilities) -> list[FontFace]:
    upright = [f for f in capabilities.faces if not f.is_italic]
    return upright or list(capabilities.faces)


def _engine_face(upright: list[FontFace], weight: int) -> FontFace:
    """Qt/DirectWrite 引擎对缺档字重的就近匹配：|face−W| 最小，平局取轻。"""
    return min(upright, key=lambda f: (abs(f.weight - weight), f.weight))


def resolve(capabilities: FontCapabilities | None, weight: int) -> ResolvedWeight:
    """顺应引擎的字体解析（可变=轴值，静态=引擎匹配标注）。"""
    W = int(weight)

    if capabilities is None or not capabilities.faces:
        return ResolvedWeight(render_mode="missing", requested_weight=W)

    # ── 真可变字体：轴值实例 ──
    if capabilities.is_variable:
        assert capabilities.axis_min is not None and capabilities.axis_max is not None
        if capabilities.axis_min <= W <= capabilities.axis_max:
            return ResolvedWeight(
                render_mode="axis", axis_value=float(W),
                requested_weight=W, is_exact=True,
            )
        clamped = (
            capabilities.axis_min if W < capabilities.axis_min else capabilities.axis_max
        )
        return ResolvedWeight(
            render_mode="axis", axis_value=float(clamped),
            requested_weight=W, is_exact=False,
        )

    upright = _upright(capabilities)

    # ── 精确命中 ──
    for face in upright:
        if face.weight == W:
            return ResolvedWeight(
                render_mode="face", base_face=face,
                requested_weight=W, is_exact=True,
            )

    # ── 缺档：引擎就近匹配，标注「合成」或「就近」──
    matched = _engine_face(upright, W)
    if W >= 600 and matched.weight < 600:
        return ResolvedWeight(
            render_mode="engine_synthetic", base_face=matched, requested_weight=W,
        )
    return ResolvedWeight(
        render_mode="snap", base_face=matched, requested_weight=W,
    )


__all__ = ["ResolvedWeight", "resolve"]
