"""Phase 2: Weight Resolver — W3C CSS Fonts Level 4 §5.2 字体匹配算法。

纯函数：输入 FontCapabilities + 请求字重，输出渲染决策。
无副作用、无 I/O、无缓存——给定相同输入永远返回相同输出。

算法来源：https://www.w3.org/TR/css-fonts-4/#font-matching-algorithm

规则（翻译自 W3C spec）：

1. 可变字体（axis_effective）：请求在 [min, max] 内 → 轴值插值；
   超出范围 → 钳制到端点。
2. 静态字体精确命中 → 直接使用该 face。
3. 请求 ≤ 500 → 先从比请求低的 face 里选最重的（向下取）。
4. 请求 > 500 → 先从比请求高的 face 里选最轻的（向上取）。
5. 优先方向无 face → 换另一个方向取最近。
6. 最终选到的 face < 600 且请求 ≥ 600 → 合成粗体（synthetic）。

实测验证：这套规则自然产生与 v4.2.x Qt 引擎完全一致的选择——
  {400,700}@600 → >500 向上取 → Bold(700)，无合成
  {400}@600    → >500 向上无 → 向下取 Regular(400) + 合成
  {400,700}@500 → ≤500 向下取 → Regular(400)，无合成
  {700}@600   → >500 向上无 → 向下取 Bold(700)（exact），无合成
  {100}@400   → ≤500 向下取 → ExtraLight(100)，无合成（400 < 600）
  {100}@600   → >500 向上无 → 向下取 ExtraLight(100) + 合成
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
    """"axis" | "face" | "synthetic" | "missing"。"""

    base_face: FontFace | None = None
    """face / synthetic 模式的基 face；axis / missing 模式为 None。"""

    axis_value: float | None = None
    """axis 模式的轴值；其他模式为 None。"""

    requested_weight: int = 400
    """原始请求字重（用于 QFont.weight 属性与缓存签名）。"""

    is_exact: bool = False
    """请求是否被精确满足（exact face 或轴值 == 请求值）。"""

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
        return self.render_mode == "synthetic"


def resolve(capabilities: FontCapabilities | None, weight: int) -> ResolvedWeight:
    """W3C CSS §5.2 字体匹配算法。"""
    W = int(weight)

    if capabilities is None or not capabilities.faces:
        return ResolvedWeight(render_mode="missing", requested_weight=W)

    # ── Rule 1: 可变字体 ──
    if capabilities.is_variable:
        assert capabilities.axis_min is not None and capabilities.axis_max is not None
        if capabilities.axis_min <= W <= capabilities.axis_max:
            return ResolvedWeight(
                render_mode="axis",
                axis_value=float(W),
                requested_weight=W,
                is_exact=True,
            )
        clamped = capabilities.axis_min if W < capabilities.axis_min else capabilities.axis_max
        return ResolvedWeight(
            render_mode="axis",
            axis_value=float(clamped),
            requested_weight=W,
            is_exact=False,
        )

    # ── Rule 2: 静态精确命中 ──
    upright = [f for f in capabilities.faces if not f.is_italic] or list(capabilities.faces)
    for face in upright:
        if face.weight == W:
            return ResolvedWeight(
                render_mode="face", base_face=face,
                requested_weight=W, is_exact=True)

    # ── Rules 3-5: 方向偏好搜索 ──
    below = [f for f in upright if f.weight < W]
    above = [f for f in upright if f.weight > W]

    if W <= 500:
        # 先向下取（最重的低于请求的 face），再向上取
        if below:
            chosen = max(below, key=lambda f: f.weight)
        elif above:
            chosen = min(above, key=lambda f: f.weight)
        else:
            chosen = upright[0]
    else:
        # 先向上取（最轻的高于请求的 face），再向下取
        if above:
            chosen = min(above, key=lambda f: f.weight)
        elif below:
            chosen = max(below, key=lambda f: f.weight)
        else:
            chosen = upright[0]

    # ── Rule 6: 合成粗体触发 ──
    if W >= 600 and chosen.weight < 600:
        return ResolvedWeight(
            render_mode="synthetic",
            base_face=chosen,
            requested_weight=W,
            is_exact=False,
        )

    return ResolvedWeight(
        render_mode="face", base_face=chosen,
        requested_weight=W, is_exact=False)


__all__ = ["ResolvedWeight", "resolve"]
