"""Phase 2: Weight Resolver — 字重→渲染决策的纯函数。

规则（2026-10-07 用户拍板，替换 W3C CSS §5.2 语义）：

真可变字体（axis_effective）：
  轴内 [min, max] → 轴值插值（真实）；轴外 < min → snap 到 min。
静态单 face：
  无论 face 实际字重是多少，一律归一化为 **400 档**；W > 400 →
  从该 face 放大 Δ = W − 400；W ≤ 400 → 渲染 face 本身。
静态多 face：
  精确命中 → 直接使用；缺档 → 从**紧邻较小 face** 放大 Δ = W −
  base.weight；无更小 face → snap 到最小 face。
模拟放大：
  仅放大（只能从比请求小的基准放大）；比例与 v4.2.x 一致，以楷体
  为准（楷体 @600 即 Δ=200 → 约 2% em，系数 = Δ/10000）。

本模块无副作用、无 I/O、无缓存——给定相同输入永远返回相同输出。
"""

from __future__ import annotations

from dataclasses import dataclass

from krok_helper.subtitle_render.engine.text.font_capabilities import (
    FontCapabilities,
    FontFace,
)


@dataclass(frozen=True)
class ResolvedWeight:
    """一个 (capabilities, weight) 请求的渲染决策。"""

    render_mode: str
    """"axis" | "face" | "embolden" | "snap" | "missing"。"""

    base_face: FontFace | None = None
    """face / embolden / snap 模式的基准 face；axis / missing 为 None。"""

    axis_value: float | None = None
    """axis 模式的轴值；其他为 None。"""

    embolden_delta: int = 0
    """embolden 模式的放大字重差 Δ（其他为 0）。"""

    requested_weight: int = 400
    """原始请求字重。"""

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
        return self.render_mode == "embolden"

    @property
    def mark(self) -> str | None:
        """UI 下拉标注。"""
        if self.render_mode == "embolden":
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


def resolve(capabilities: FontCapabilities | None, weight: int) -> ResolvedWeight:
    """字重匹配（v4.2.x 比例 + 紧邻放大语义）。"""
    W = int(weight)

    if capabilities is None or not capabilities.faces:
        return ResolvedWeight(render_mode="missing", requested_weight=W)

    # ── 真可变字体 ──
    if capabilities.is_variable:
        assert capabilities.axis_min is not None and capabilities.axis_max is not None
        if capabilities.axis_min <= W <= capabilities.axis_max:
            return ResolvedWeight(
                render_mode="axis", axis_value=float(W),
                requested_weight=W, is_exact=True,
            )
        if W < capabilities.axis_min:
            return ResolvedWeight(
                render_mode="axis", axis_value=capabilities.axis_min,
                requested_weight=W, is_exact=False,
            )
        # W > max：从轴上限放大（连续模拟）
        return ResolvedWeight(
            render_mode="embolden",
            axis_value=capabilities.axis_max,
            requested_weight=W,
            embolden_delta=W - int(round(capabilities.axis_max)),
            is_exact=False,
        )

    upright = _upright(capabilities)
    weights = [f.weight for f in upright]

    # ── 单 face：归一化为 400 档（优先于精确命中）──
    # 用户规则：无论 face 实际字重是多少，一律视为 400 档；W > 400 →
    # 放大 Δ = W − 400；W ≤ 400 → 渲染 face 本身（W == 400 视为精确）。
    if len(upright) == 1:
        face = upright[0]
        if W > 400:
            return ResolvedWeight(
                render_mode="embolden", base_face=face,
                requested_weight=W, embolden_delta=W - 400, is_exact=False,
            )
        if W == 400:
            return ResolvedWeight(
                render_mode="face", base_face=face,
                requested_weight=W, is_exact=True,
            )
        # W < 400：无更小基准，放弃模拟取 snap（渲染 face 本身）。
        return ResolvedWeight(
            render_mode="snap", base_face=face,
            requested_weight=W, is_exact=False,
        )

    # ── 精确命中 ──
    for face in upright:
        if face.weight == W:
            return ResolvedWeight(
                render_mode="face", base_face=face,
                requested_weight=W, is_exact=True,
            )

    # ── 多 face：紧邻较小放大，无更小则 snap ──
    smaller = [f for f in upright if f.weight < W]
    if smaller:
        base = max(smaller, key=lambda f: f.weight)
        return ResolvedWeight(
            render_mode="embolden", base_face=base,
            requested_weight=W, embolden_delta=W - base.weight, is_exact=False,
        )
    # W 比所有 face 都小：snap 到最小 face
    base = min(upright, key=lambda f: f.weight)
    return ResolvedWeight(
        render_mode="snap", base_face=base, requested_weight=W, is_exact=False,
    )


__all__ = ["ResolvedWeight", "resolve"]
