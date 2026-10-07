"""统一「请求字重 → 字体实例」解析——Qt/CPU 与 Direct2D/GPU 的共同口径。

背景（2026-10 可变字体排障）：可变字体的命名实例字重往往不是标准整百
（如 Iwata UD Gothic VF 的 300/425/460/540/700/790/900）。此前 CPU 侧
用 ``clamp_weight`` 桶化后交给 Qt 自行匹配、GPU 侧把原始字重交给
DirectWrite ``GetFirstMatchingFont``，两条管线对同一 (family, weight)
会选出不同实例；Qt 对缺失字重还会落进 GDI 伪家族的合成粗体（'L Bold'），
导致两条后端的字形 / advance / 行宽全部分叉。

本模块是唯一的决策点，规则与 native 侧 ``d2d_font_fallback.cpp`` 的
unified weight resolution 逐条对应（改动任一侧必须同步另一侧）：

1. **可变字体**（fvar 含 wght 轴）：按 ``clamp(W, axis.min, axis.max)``
   渲染**真实轴值插值实例**（``QFont.setVariableAxis`` 优先级高于
   setWeight/setStyleName）。轴上限之上的请求在轴端点之上继续**膨胀
   放大**（见下）；轴下限之下钳制到端点并标「越界」。
2. **静态字体**（v7，2026-10-07 用户拍板：**比例严格按 v4.2.x 旧版
   一致，并按基 face 字重平移（转化）**——NK-B@700~800 的变化比例
   与 400 基础字体 @700~800 相同）：
   a. E = bucket_weight(W) 命中真实 face 字重 → 钉住该 face；
   b. E ∉ R 且 **E ≥ 600 且基 face（E 之下最重的真实 face）字重
      < 600** → 基 face + **固定一档轻度膨胀（字号×2%，即旧版
      引擎合成粗体的实测强度）**，600~900 渲染完全相同（旧版阶跃），
      UI 标「模拟」；
   c. 其余缺档（E<600，或基 face 已是粗体）→ 渲染基 face，无膨胀
      ——NK-B 这类粗体单 face 族任何字重都不变化，与旧版逐档一致；
   d. E 比族内最轻 face 还轻 → 渲染最轻 face。
3. **拿不到 face 元数据**（字体缺失 / headless 枚举为空）：退回旧行为
   （仅按桶化值 setWeight），不钉扎。

可变轴信息来自 ``QRawFont.fontTable('fvar')`` 的最小解析（tag/min/
default/max），进程级缓存；face 清单来自 ``QFontDatabase.styles``。
"""

from __future__ import annotations

import struct
import threading
from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtGui import (
    QFont,
    QFontDatabase,
    QFontInfo,
    QFontMetrics,
    QPainterPath,
    QPainterPathStroker,
    QRawFont,
)

_AXIS_TAG_WEIGHT = b"wght"

# v7 阶跃膨胀档：激活时 embolden_delta 取该值，膨胀宽度 = 字号×2%
# （v4.2.x 引擎合成粗体的实测强度：楷体 1.9%em、MS Gothic 2.3%em）。
_EMBOLDEN_TRIGGER_DELTA = 200
_EMBOLDEN_EM_RATIO = 0.02

# 与 metrics.clamp_weight / native 侧 weightBucket 同表的整百桶化。
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
    """把任意请求字重映射到标准整百（100..900），与 clamp_weight 同表。"""
    value = int(weight)
    for upper, bucket in _WEIGHT_BUCKETS:
        if value <= upper:
            return bucket
    return 900


@dataclass(frozen=True)
class WeightAxis:
    minimum: float
    default: float
    maximum: float


@dataclass(frozen=True)
class FontWeightPlan:
    """(family, 请求字重) 的权威解析结果。

    ``axis_value`` 非 None 时走可变字体真实轴值渲染，其余字段仅静态
    路径有效；``mark`` 供 UI 标注：None=真实，"模拟"/"就近"/"越界"。
    """

    family: str
    requested_weight: int
    axis_value: float | None = None
    style_name: str | None = None
    base_weight: int = 0
    synthetic_bold: bool = False
    enum_weight: int = 0
    italic: bool = False
    # 连续膨胀的重量差（桶化值 − 基 face 字重），膨胀宽度 =
    # 字号 × Δ / 10000（锚点 Δ=200→2%em=旧版合成粗体实测强度）。
    embolden_delta: int = 0
    mark: str | None = None


_CANON_CACHE: dict[str, str] = {}
# 模拟放大旁路表：apply_weight_plan 按 QFont 签名登记膨胀量，
# embolden_glyph_path 据此取量（QFont 是值类型，签名相同的拷贝共享）。
_EMBOLDEN_BY_SIGNATURE: dict[tuple, int] = {}
_EMBOLDEN_SIGNATURE_MAX = 4096
_AXIS_CACHE: dict[str, dict[bytes, WeightAxis] | None] = {}
_FACE_CACHE: dict[str, tuple[tuple[int, str, bool], ...]] = {}
_PLAN_CACHE: dict[tuple[str, int, bool], FontWeightPlan] = {}
_LOCK = threading.Lock()


def clear_font_weight_cache() -> None:
    """字体安装/卸载后清空进程级缓存（调用方：宿主字体库刷新）。"""
    with _LOCK:
        _CANON_CACHE.clear()
        _EMBOLDEN_BY_SIGNATURE.clear()
        _AXIS_CACHE.clear()
        _FACE_CACHE.clear()
        _PLAN_CACHE.clear()


def canonical_family(family: str) -> str:
    """把存储/显示用的本地化族名换成 Qt 字体库拼写。

    工作台字体选择器列出的是 SUG 文件扫描的本地化名（日文/别名），
    渲染端经 n3 字体目录的别名表（DirectWrite 全本地化名清单）换算；
    字重解析必须走同一换算，否则 face 枚举落空、全档退兜底。换算失败
    或换算后仍枚举不到 face 时保留原名（调用方按兜底处理）。
    """
    key = str(family)
    with _LOCK:
        cached = _CANON_CACHE.get(key)
    if cached is not None:
        return cached
    canonical = key
    try:
        from krok_helper.subtitle_render.n3.font_catalog import (
            resolve_qt_font_family,
        )

        resolved = resolve_qt_font_family(key)
        if resolved and resolved != key:
            probe = QFont(resolved)
            if QFontInfo(probe).family().casefold() == resolved.casefold():
                canonical = resolved
    except Exception:
        canonical = key
    with _LOCK:
        _CANON_CACHE[key] = canonical
    return canonical


def _parse_fvar(raw: bytes) -> dict[bytes, WeightAxis] | None:
    if len(raw) < 16:
        return None
    major, _minor, axes_offset, _reserved, axis_count, axis_size = struct.unpack_from(
        ">6H", raw, 0
    )
    _instance_count, _instance_size = struct.unpack_from(">2H", raw, 12)
    if major != 1 or axis_size < 20 or axes_offset < 16:
        return None
    axes: dict[bytes, WeightAxis] = {}
    for index in range(axis_count):
        offset = axes_offset + index * axis_size
        if offset + 20 > len(raw):
            break
        tag = bytes(raw[offset : offset + 4])
        minimum, default, maximum = struct.unpack_from(">3l", raw, offset + 4)
        axes[tag] = WeightAxis(minimum / 65536.0, default / 65536.0, maximum / 65536.0)
    return axes


def family_weight_axis(family: str) -> WeightAxis | None:
    """返回族解析结果里**有效**的 wght 轴；静态 / 伪可变 / 失败返回 None。

    「伪可变」：字体带 fvar 的 wght 轴（有范围）但实际渲染不变——轴上
    没有真实变体数据（gvar/HVAR 缺失或恒等），或该字体在当前 Qt 环境
    无法应用轴值。检测口径：轴两端（min/max）构造的字体指纹完全一致 ⇒
    调字重不会产生任何视觉/宽度变化，按静态族处理（就近/模拟语义），
    避免把「恒定渲染」误判为真实可变而被标注为真实档。
    """
    key = str(family)
    with _LOCK:
        cached = _AXIS_CACHE.get(key, _MISSING)
    if cached is not _MISSING:
        return None if cached is None else cached.get(_AXIS_TAG_WEIGHT)
    try:
        raw_font = QRawFont.fromFont(QFont(canonical_family(key)))
        table = bytes(raw_font.fontTable(b"fvar"))
        axes = _parse_fvar(table) if table else None
    except (RuntimeError, TypeError, ValueError):
        axes = None
    axis = None if axes is None else axes.get(_AXIS_TAG_WEIGHT)
    if axis is not None:
        # 恒定轴（min==max，单字重的"可变"包装文件）与轴两端指纹一致的
        # 伪可变一律按静态处理——否则所有字重会被 clamp 到唯一值且 UI
        # 全档标"真实"（2026-10-07 用户报「全字重无效且无就近/模拟标注」
        # 的根因之一）。min==max 时两端指纹必相同，统一走指纹检测即可。
        if not _wght_axis_is_effective(canonical_family(key), axis):
            axes = None
    with _LOCK:
        _AXIS_CACHE[key] = axes
    return None if axes is None else axes.get(_AXIS_TAG_WEIGHT)


def _wght_axis_is_effective(family: str, axis: WeightAxis) -> bool:
    """wght 轴两端是否产生可观测差异（advance/墨迹指纹）。"""
    signatures = []
    family = canonical_family(family)
    for value in (axis.minimum, axis.maximum):
        font = QFont(family)
        font.setVariableAxis(QFont.Tag(_AXIS_TAG_WEIGHT), float(value))
        if QFontInfo(font).family().casefold() != family.casefold():
            # 族名被静默替换（本地化名/别名不在 Qt 字体库）——指纹毫
            # 无意义，按静态处理。
            return False
        signatures.append(_font_fingerprint(font))
    return signatures[0] != signatures[1]


class _Missing:
    pass


_MISSING = _Missing()


def face_inventory(family: str) -> tuple[tuple[int, str, bool], ...]:
    """族内全部真实 face 的 ``(字重, styleName, 是否斜体)``，按字重升序。

    同字重保留全部 face（ upright 与斜体是不同 face，去重会钉错）；
    headless / 字体缺失返回空 tuple。进程级缓存。
    """
    key = str(family)
    with _LOCK:
        cached = _FACE_CACHE.get(key)
    if cached is not None:
        return cached
    canonical = canonical_family(key)
    faces: list[tuple[int, str, bool]] = []
    try:
        for style in QFontDatabase.styles(canonical):
            weight = int(QFontDatabase.weight(canonical, style))
            name = str(style)
            italic = bool(QFontDatabase.italic(canonical, style))
            if 1 <= weight <= 1000:
                faces.append((weight, name, italic))
    except (RuntimeError, TypeError, ValueError):
        faces = []
    faces.sort(key=lambda entry: (entry[0], entry[2], entry[1]))
    inventory = tuple(faces)
    with _LOCK:
        _FACE_CACHE[key] = inventory
    return inventory


def physical_weight_styles(family: str) -> tuple[tuple[int, str], ...]:
    """族内直立 face 的 ``(字重, styleName)`` 清单，按字重升序。

    同字重既有直立又有斜体 face 时只保留直立（斜体 face 由解析器按
    italic 请求单独选择）；仅有斜体 face 的族退回斜体清单。
    """
    inventory = face_inventory(family)
    upright = tuple(
        (weight, name) for weight, name, italic in inventory if not italic
    )
    if upright:
        seen: dict[int, str] = {}
        for weight, name in upright:
            if weight not in seen or name < seen[weight]:
                seen[weight] = name
        return tuple(sorted(seen.items()))
    seen = {}
    for weight, name, _italic in inventory:
        if weight not in seen or name < seen[weight]:
            seen[weight] = name
    return tuple(sorted(seen.items()))


# 指纹探测：多字号 + advance + 墨迹包围盒。双字号是为了打断单字号的
# 取整碰撞（Yu Gothic UI 的 Semibold/Bold 在 62px 的 advance 完全相同），
# 拉丁大小写/数字 + 假名/汉字的混合串保证不同字重实例必然分离。
_FINGERPRINT_TEXT = "Ag0Wg指あそ爽永"
_FINGERPRINT_SIZES = (40, 41, 56)


def _font_fingerprint(font: QFont) -> tuple:
    signature: list = []
    for size in _FINGERPRINT_SIZES:
        font.setPixelSize(size)
        metrics = QFontMetrics(font)
        signature.extend(metrics.horizontalAdvance(ch) for ch in _FINGERPRINT_TEXT)
        path = QPainterPath()
        path.addText(0.0, 0.0, font, _FINGERPRINT_TEXT)
        rect = path.boundingRect()
        signature.extend(
            (round(rect.left() * 8), round(rect.top() * 8),
             round(rect.right() * 8), round(rect.bottom() * 8))
        )
    font.setPixelSize(_FINGERPRINT_SIZES[0])
    return tuple(signature)


def resolve_weight_plan(
    family: str, weight: int, italic: bool = False
) -> FontWeightPlan:
    """(family, 请求字重, 请求斜体) → 权威渲染计划。"""
    requested = int(weight)
    cache_key = (str(family), requested, bool(italic))
    with _LOCK:
        cached_plan = _PLAN_CACHE.get(cache_key)
    if cached_plan is not None:
        return cached_plan
    plan = _compute_weight_plan(family, requested, bool(italic))
    with _LOCK:
        _PLAN_CACHE[cache_key] = plan
    return plan


def _compute_weight_plan(
    family: str, requested: int, italic: bool
) -> FontWeightPlan:
    axis = family_weight_axis(family)
    if axis is not None:
        value = min(max(float(requested), axis.minimum), axis.maximum)
        embolden = 0
        mark: str | None = None
        if float(requested) > axis.maximum:
            # 轴上限之上继续连续膨胀（Δ=桶化值−轴上限，同公式）。
            embolden = max(
                bucket_weight(requested) - int(round(axis.maximum)), 0
            )
            mark = "模拟" if embolden > 0 else None
        elif float(requested) < axis.minimum:
            mark = "越界"
        return FontWeightPlan(
            family=family,
            requested_weight=requested,
            axis_value=value,
            base_weight=int(round(value)),
            enum_weight=bucket_weight(requested),
            italic=bool(italic),
            embolden_delta=embolden,
            mark=mark,
        )

    inventory = face_inventory(family)
    # 斜体请求优先斜体 face；族内没有斜体 face 时回退直立 face 并由
    # 调用方的 setItalic 走合成斜体（与旧解析行为一致）。
    selected = [face for face in inventory if face[2] == italic]
    if not selected and italic:
        selected = list(inventory)
    if not selected:
        # 字体缺失 / headless：保持旧行为（仅桶化 setWeight）。
        bucket = bucket_weight(requested)
        return FontWeightPlan(
            family=family,
            requested_weight=requested,
            base_weight=bucket,
            enum_weight=bucket,
            italic=bool(italic),
        )

    bucket = bucket_weight(requested)
    weights = [face_weight for face_weight, _name, _face_italic in selected]

    # v6（2026-10-07 用户拍板）：取消"就近"——凡比基 face 重的缺档一律
    # 模拟放大（基 face 取 E 之下最重的真实 face，Δ=E−基 face 字重，
    # 两后端按统一公式膨胀轮廓）；不再指纹跟随 Qt 的 plain 选择。
    if bucket in weights:
        base_weight, style_name, _face_italic = selected[weights.index(bucket)]
        return FontWeightPlan(
            family=family,
            requested_weight=requested,
            style_name=style_name,
            base_weight=base_weight,
            enum_weight=bucket,
            italic=bool(italic),
        )
    # v7 阶跃（2026-10-07 用户拍板，严格按 v4.2.x 比例并按基 face 平移）：
    # 旧版引擎合成触发 = 「请求 ≥600 且匹配 face 非粗（<600）」，强度
    # 固定一档（≈2% em），600~900 完全相同。基 face 取 E 之下最重真实
    # face（替代 Qt 匹配保证 CPU/GPU 一致）：楷体（基 400）@600~900 同
    # 一档轻度加粗；Yu Gothic@600 = Medium+2%；NK-B（基 700，已粗）
    # 任何字重都不变化——即「NK-B@700~800 的变化 = 400 基 @700~800
    # （都为 0）」。
    # v7.1 双向匹配 + 阶跃膨胀（2026-10-07 用户拍板）：
    # 严格复刻 v4.2.x 的 Qt 匹配器行为——实测规律是「<600 向下取，
    # ≥600 向上取 bold face」（NK {400,700}@600 旧版直接用 Bold，
    # 不是 Regular+合成）。没有向上的 face 时才落 floor+阶跃膨胀。
    #
    #   E < 600: floor（向下取最近 face），不做膨胀（旧版同样无变化）
    #   E ≥ 600: 先找 ≥E 的最轻 face（snap up，旧版匹配器行为）；
    #            没有 → floor + Δ≥200 触发阶跃膨胀（旧版引擎合成）
    #
    # NK {400,700}@600 → snap Bold(700)；MS Gothic {400}@600 → 无
    # face≥600，floor 400+Δ200 膨胀；NK-B {700}@600 → snap Bold(700)；
    # Yu Gothic UI {300..700}@600 → exact Semibold(600)。
    if bucket >= 600:
        ceilings = [value for value in weights if value >= bucket]
        if ceilings:
            snap = min(ceilings)
            snap_weight, snap_style, _snap_italic = selected[weights.index(snap)]
            return FontWeightPlan(
                family=family,
                requested_weight=requested,
                style_name=snap_style,
                base_weight=snap_weight,
                enum_weight=bucket,
                italic=bool(italic),
            )

    floors = [value for value in weights if value < bucket]
    if floors:
        base_weight = max(floors)
        base_weight, style_name, _face_italic = selected[weights.index(base_weight)]
        delta = bucket - base_weight
        return FontWeightPlan(
            family=family,
            requested_weight=requested,
            style_name=style_name,
            base_weight=base_weight,
            enum_weight=bucket,
            italic=bool(italic),
            embolden_delta=delta if delta >= _EMBOLDEN_TRIGGER_DELTA else 0,
            mark="模拟" if delta >= _EMBOLDEN_TRIGGER_DELTA else None,
        )
    # 比族内最轻 face 还轻：放大无法变轻，渲染最轻 face。
    base_weight = min(weights)
    base_weight, style_name, _face_italic = selected[weights.index(base_weight)]
    return FontWeightPlan(
        family=family,
        requested_weight=requested,
        style_name=style_name,
        base_weight=base_weight,
        enum_weight=bucket,
        italic=bool(italic),
    )


def embolden_width_px(font_size_px: int, delta: int) -> float:
    """阶跃膨胀的描边宽：Δ≥200（触发距离）→ 字号×2%（旧版引擎合成
    粗体实测强度），否则 0。两后端同一公式（native 侧 textRealizationFor）。"""
    if delta < _EMBOLDEN_TRIGGER_DELTA or font_size_px <= 0:
        return 0.0
    return float(font_size_px) * _EMBOLDEN_EM_RATIO


def font_signature(font: QFont) -> tuple:
    """QFont 的解析签名（与 metrics._font_signature 同字段）。"""
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
    """从构造好的 QFont 反查模拟放大的重量差（apply 时登记的旁路表）。

    不能从 ``(family, font.weight())`` 重推：v6 模拟档钉基 face、
    QFont.weight 停在基 face 字重上，与精确档无法区分——膨胀量只有在
    apply_weight_plan 构造时才确定，经签名旁路表带过来。
    """
    with _LOCK:
        return int(_EMBOLDEN_BY_SIGNATURE.get(font_signature(font), 0))


def embolden_glyph_path(path: QPainterPath, font: QFont) -> QPainterPath:
    """按统一口径把字形轮廓圆形膨胀（粗上加粗）。

    在 addText 之后、进入任何绘制/度量之前应用——填充、描边、走字、
    墨迹盒等全部下游管线自动消费膨胀后的轮廓，CPU/GPU 一致由构造保证。
    """
    # OpenType 轮廓规范要求 NonZero(Winding) 填充；Qt addText() 默认
    # OddEvenFill，可变字体 gvar 插值导致轮廓重叠时 OddEven 会在笔画
    # 交叉处产生空洞（2026-10-07 用户报「单个字内两笔画相交处镂空」）。
    # 静态字体两种规则结果相同，统一设 Winding 无副作用。
    path.setFillRule(Qt.FillRule.WindingFill)
    if path.isEmpty():
        return path
    width = embolden_width_px(font.pixelSize(), embolden_delta_of_font(font))
    if width <= 0.0:
        return path
    stroker = QPainterPathStroker()
    stroker.setWidth(width)
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return path.united(stroker.createStroke(path))


def apply_weight_plan(font: QFont, plan: FontWeightPlan) -> None:
    """把权威解析结果应用到 QFont（调用方已设 family/pixelSize）。

    v6 构造口径：

    - **精确档 / 拿不到元数据**：plain ``setWeight(桶化值)``（与最初
      旧行为一致，QFont.weight 保留请求值供缺字 fallback 字体跟随）。
    - **模拟放大档**（embolden_delta>0）：``setStyleName`` 钉住基 face
      + ``setWeight(基 face 字重)``——weight 与 face 自身字重相等，
      匹配器无劫持压力、Qt 也不触发合成粗体（避免与我们的轮廓膨胀
      叠加成双重加粗）；主 face 由钉扎保证，膨胀量经签名旁路表
      ``_EMBOLDEN_BY_SIGNATURE`` 交给 embolden_glyph_path。
    - **可变字体**：plain setWeight 之上叠加 wght 轴值（优先级高于
      setWeight，仅作用于轴字体本身）；轴上限之上的请求轴值停在
      上限、膨胀量同样走旁路表。
    """
    font.setItalic(bool(plan.italic))
    if plan.embolden_delta > 0 and plan.style_name is not None:
        font.setStyleName(plan.style_name)
        font.setWeight(QFont.Weight(plan.base_weight))
    else:
        font.setWeight(QFont.Weight(plan.enum_weight))
        if plan.axis_value is not None:
            font.setVariableAxis(
                QFont.Tag(_AXIS_TAG_WEIGHT), float(plan.axis_value)
            )
    if plan.embolden_delta > 0:
        with _LOCK:
            if len(_EMBOLDEN_BY_SIGNATURE) >= _EMBOLDEN_SIGNATURE_MAX:
                _EMBOLDEN_BY_SIGNATURE.clear()
            _EMBOLDEN_BY_SIGNATURE[font_signature(font)] = int(
                plan.embolden_delta
            )


def build_weight_font(
    family: str, size_px: int, weight: int, italic: bool = False
) -> QFont:
    """按统一口径构造 QFont（family 已是 resolve_qt_font_family 的结果）。"""
    font = QFont(family, max(int(size_px), 1))
    font.setPixelSize(max(int(size_px), 1))
    apply_weight_plan(
        font, resolve_weight_plan(family, weight, italic=bool(italic))
    )
    return font


__all__ = [
    "FontWeightPlan",
    "WeightAxis",
    "apply_weight_plan",
    "bucket_weight",
    "build_weight_font",
    "canonical_family",
    "clear_font_weight_cache",
    "embolden_delta_of_font",
    "embolden_glyph_path",
    "embolden_width_px",
    "face_inventory",
    "family_weight_axis",
    "physical_weight_styles",
    "resolve_weight_plan",
]
