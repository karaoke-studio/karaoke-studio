"""Phase 2: Weight/Instance Resolver — 字体解析决策（顺应引擎，2026-10-07 拍板）。

**顺应引擎**：字重回到绝对字重，模拟加粗/倾斜交还给 Qt/DirectWrite 引擎
（v4.2.x 语义）。本模块的职责是把「族 + 字重 + 斜体」解析成一份**明确的
字体实例决策**（``ResolvedFontInstance``），供渲染胶水与 native 侧共同执行：

1. 可变字体：逐 face 判轴（``FontCapabilities.variable_style``），轴内插值、
   轴外钳制到端点。
2. 静态字体：字重＋斜体联合匹配出**确定的 face**（真实斜体 face 优先，无
   斜体 face 时标注引擎合成倾斜），外加引擎就近匹配与合成粗体标注。
   **渲染不做任何"我们膨胀"**，引擎匹配 + 合成自然发生。

与旧版（只输出字重预测）的差别：

* 斜体参与决策（``synthetic_italic`` 标注「真实斜体 face」vs「引擎合成倾斜」）；
* 轴只在**带轴的那个 face** 上生效（族内静态/可变共存不再互相误伤）；
* ``exact`` / ``reason`` / ``axis_ineffective`` 如实标注请求是否被精确满足、
  回退原因，UI 与诊断据此显示「模拟 / 就近 / 越界」。

引擎匹配口径（QFontInfo/像素全档实测校准，2026-10-08 首测 + 2026-10-11 复核）：
  请求 W 精确命中 → 该 face；
  缺档 → 就近匹配（|face−W| 最小，**平局取更接近 Normal(400) 的 face**）；
  匹配 face < 600 且 W ≥ 600 → 引擎合成粗体（faux bold，粗 face 豁免）。
  实测锚点：Yu Gothic{300,400,500,700}@600 → 500+合成（平局落 400 侧），
  @350 → Regular(400)（**不是** Light，旧「平局取轻」口径在此选错 face），
  NK{400,700}@600 → 700（无平局就近），思源@600 → 500+合成，
  Segoe UI Semibold{600}@600/700 → 同 face 无合成（拉丁实测 152x113 恒定；
  早先「600 face 会合成」的观测实为 CJK 回退 face(400) 的合成）。
  斜体锚点（2026-10-11 QFontInfo 实测）：MS Gothic/Yu Gothic/Noto Sans SC
  等无斜体 face 的族 ⇒ QFontInfo.styleName 回报 'Oblique' 系合成名；族内
  有真实斜体 face（Segoe UI/Meiryo）⇒ 回报真实斜体 face。

本模块无副作用、无 I/O、无缓存。
"""

from __future__ import annotations

from dataclasses import dataclass

from krok_helper.subtitle_render.engine.text.font_capabilities import (
    FontCapabilities,
    FontFace,
)


@dataclass(frozen=True)
class FontRequest:
    """一次字体解析请求（族名 + 字重 + 斜体 + 横向拉伸）。"""

    family: str
    weight: int = 400
    italic: bool = False
    stretch_pct: int = 100


@dataclass(frozen=True)
class ResolvedFontInstance:
    """一个字体请求的解析结果：可验证的字体实例描述。

    字段是**决策**，不是渲染调用：两个后端各自把它翻译成自己的 API
    （QFont / IDWriteFontFace），不得再自行就近匹配或自行判定可变性。

    ``family``/``face_style``/``face_weight`` 构成运行时身份——不用工程内的
    绝对路径（字体装机变化由 capabilities 代际失效覆盖）。
    """

    requested: FontRequest
    family: str
    """规范族名（别名换算后）。"""

    face_style: str | None = None
    """决策命中的 face style 名；None = 交引擎按默认 face 解析。"""

    face_weight: int = 400
    """决策命中的 face 字重（可变字体为轴值取整）。"""

    axis_value: float | None = None
    """需要施加的 wght 轴值；None = 不施加（静态 face）。"""

    synthetic_bold: bool = False
    """请求字重超出 face 能力，由引擎合成加粗。"""

    synthetic_italic: bool = False
    """族内无真实斜体 face，斜体请求由引擎合成倾斜。"""

    exact: bool = False
    """请求是否被精确满足（face 字重/轴值命中且无合成）。"""

    reason: str | None = None
    """"missing_family" | "nearest_face" | "synthetic_bold" | "axis_clamped"。"""

    axis_ineffective: bool = False
    """表里声明了 wght 轴但渲染无差异（伪可变）——已按静态规则解析。"""

    is_variable: bool = False
    """决策是否走了可变轴实例。"""

    generation: int = 0
    """解析时字体库代际（缓存失效用）。"""

    @property
    def identity(self) -> str:
        """可验证的实例身份（无路径）。"""
        axis = "" if self.axis_value is None else f"|axis={self.axis_value:g}"
        return (
            f"{self.family}|{self.face_style or '<default>'}"
            f"|w={self.face_weight}|italic={int(self.synthetic_italic or bool(self.requested.italic))}"
            f"{axis}"
        )

    @property
    def base_weight(self) -> int:
        return self.face_weight

    @property
    def style_name(self) -> str | None:
        return self.face_style

    @property
    def needs_synthetic(self) -> bool:
        return self.synthetic_bold

    @property
    def mark(self) -> str | None:
        if self.synthetic_bold:
            return "模拟"
        if self.reason == "nearest_face":
            return "就近"
        if self.reason == "axis_clamped":
            return "越界"
        return None

    @property
    def synthetic_bold_requested(self) -> bool:
        return self.synthetic_bold


def _upright(faces: list[FontFace]) -> list[FontFace]:
    upright = [f for f in faces if not f.is_italic]
    return upright or list(faces)


def _italic_faces(faces: list[FontFace]) -> list[FontFace]:
    return [f for f in faces if f.is_italic]


def _engine_face(pool: list[FontFace], weight: int) -> FontFace:
    """Qt/DirectWrite 引擎对缺档字重的就近匹配（2026-10-11 全档实测校准）。

    规则：|face − W| 最小；**平局取更接近 Normal(400) 的 face**（实测锚点：
    Yu Gothic{300,400,500,700}@350 → Regular(400) 而非 Light；@450、@600 平局
    同样落到 400 侧；Noto{100,300,…}@200 → Light(300) 而非 Thin；
    Yu Gothic UI@325 → Semilight(350)；Meiryo@550 → Regular）。「平局取轻」
    的旧口径在 400 以下档位会选错 face（350 请求渲染成 Light 200 档观感）。
    若仍完全并列（如 {300,500}@400）取更轻 face 兜底。
    """
    return min(
        pool,
        key=lambda f: (abs(f.weight - weight), abs(f.weight - 400), f.weight),
    )


def resolve_instance(
    capabilities: FontCapabilities | None, request: FontRequest
) -> ResolvedFontInstance:
    """(capabilities, 请求) → 明确的字体实例决策（纯函数）。"""
    weight = int(request.weight)
    family = capabilities.family if capabilities is not None else str(request.family)

    if capabilities is None or not capabilities.faces:
        return ResolvedFontInstance(
            requested=request,
            family=family,
            face_style=None,
            face_weight=weight,
            exact=False,
            reason="missing_family",
        )

    faces = list(capabilities.faces)

    # ── 斜体联合匹配：真实斜体 face 优先，缺则标注引擎合成倾斜 ──
    if request.italic:
        real_italic = _italic_faces(faces)
        if real_italic:
            pool = real_italic
            synthetic_italic = False
        else:
            pool = _upright(faces)
            # 全族只有斜体 face 时该 face 本身即正确斜体，不算合成。
            synthetic_italic = any(not f.is_italic for f in faces)
    else:
        pool = _upright(faces)
        synthetic_italic = False

    # ── 真可变 font：只在该 face 参与本次请求（斜体/直立集合内）时走轴实例 ──
    # 逐 face 能力（variable_style 非空）时要求带轴 face 落在本次请求的
    # face 池里；只有族级轴信息（手搓 capabilities / 旧调用方）时按族级
    # 判断，保持旧契约。
    variable_face = capabilities.variable_face
    axis_applies = capabilities.is_variable and (
        variable_face is None or variable_face in pool
    )
    if axis_applies:
        assert capabilities.axis_min is not None and capabilities.axis_max is not None
        if capabilities.axis_min <= weight <= capabilities.axis_max:
            axis_value = float(weight)
            exact = True
            reason = None
        else:
            axis_value = float(
                capabilities.axis_min
                if weight < capabilities.axis_min
                else capabilities.axis_max
            )
            exact = False
            reason = "axis_clamped"
        return ResolvedFontInstance(
            requested=request,
            family=family,
            face_style=variable_face.style_name if variable_face else None,
            face_weight=int(round(axis_value)),
            axis_value=axis_value,
            synthetic_bold=False,
            synthetic_italic=synthetic_italic,
            exact=exact,
            reason=reason,
            axis_ineffective=False,
            is_variable=True,
        )

    # ── 静态：引擎就近匹配 + 合成标注 ──
    # 合成条件（实测）：请求≥600 且匹配 face<600（600/700 face 均豁免）。
    matched = _engine_face(pool, weight)
    synthetic_bold = weight >= 600 and matched.weight < 600
    if synthetic_bold:
        reason = "synthetic_bold"
    elif matched.weight == weight:
        reason = None
    else:
        reason = "nearest_face"
    return ResolvedFontInstance(
        requested=request,
        family=family,
        face_style=matched.style_name,
        face_weight=matched.weight,
        axis_value=None,
        synthetic_bold=synthetic_bold,
        synthetic_italic=synthetic_italic,
        exact=(matched.weight == weight and not synthetic_bold),
        reason=reason,
        axis_ineffective=bool(
            capabilities.axis_present and not capabilities.axis_effective
        ),
    )


# ---------------------------------------------------------------------------
# 兼容层：旧 ResolvedWeight（Phase 2 初版只解析字重）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedWeight:
    """一个 (capabilities, weight) 请求的解析决策（旧接口，UI/协议在用）。"""

    render_mode: str
    """"axis" | "face" | "engine_synthetic" | "snap" | "missing"。"""

    axis_value: float | None = None
    base_face: FontFace | None = None
    requested_weight: int = 400
    is_exact: bool = False
    instance: ResolvedFontInstance | None = None
    """同一决策的完整实例（含斜体/合成/身份）；旧字段由它派生。"""

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


def _legacy_weight(
    instance: ResolvedFontInstance, capabilities: FontCapabilities | None
) -> ResolvedWeight:
    weight = int(instance.requested.weight)
    if instance.reason == "missing_family":
        return ResolvedWeight(
            render_mode="missing", requested_weight=weight, instance=instance
        )
    base_face = None
    if capabilities is not None and instance.face_style is not None:
        for face in capabilities.faces:
            if face.style_name == instance.face_style:
                base_face = face
                break
    if instance.is_variable:
        return ResolvedWeight(
            render_mode="axis",
            axis_value=instance.axis_value,
            base_face=base_face,
            requested_weight=weight,
            is_exact=instance.exact,
            instance=instance,
        )
    if instance.synthetic_bold:
        render_mode = "engine_synthetic"
    elif instance.exact:
        render_mode = "face"
    else:
        render_mode = "snap"
    return ResolvedWeight(
        render_mode=render_mode,
        base_face=base_face,
        requested_weight=weight,
        is_exact=instance.exact,
        instance=instance,
    )


def resolve(capabilities: FontCapabilities | None, weight: int) -> ResolvedWeight:
    """顺应引擎的字体解析（可变=轴值，静态=引擎匹配标注）。

    旧签名保留：只传字重，斜体按未请求处理。新代码请直接用
    :func:`resolve_instance`（斜体参与决策）。
    """
    family = capabilities.family if capabilities is not None else ""
    instance = resolve_instance(
        capabilities, FontRequest(family=family, weight=int(weight))
    )
    return _legacy_weight(instance, capabilities)


__all__ = [
    "FontRequest",
    "ResolvedFontInstance",
    "ResolvedWeight",
    "resolve",
    "resolve_instance",
]
