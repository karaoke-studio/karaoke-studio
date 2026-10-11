"""Phase 1: Font Capabilities — 纯数据，回答「这个字体实际有什么」。

不包含任何渲染决策（选 face / 膨胀 / 合成等全部在 weight_resolver.py）。
数据来源：QFontDatabase（face 清单）+ 逐 face 的 QRawFont fvar 解析（轴信息）
+ n3 别名目录（本地化族名规范化）。进程级缓存。

设计要点（2026-10-11 逐 face 化）：

* **逐 face 判可变**。历史实现只探族默认 face 的 fvar，族内「静态文件 +
  可变文件」共存时会整体误判——设置轴值却渲染在静态 face 上（轴静默失效），
  或反过来把真可变字体当静态就近匹配。现在每个 face 单独钉 styleName 读
  fvar，只有真正带 wght 轴的 face 才标 ``is_variable``。
* ``axis_present`` 来自字体表（引擎/文件真值），``axis_effective`` 是渲染
  指纹的**辅助确认**（轴两端渲染确实不同）。二者分开记录：表说可变而渲染
  不变（§8.11 已知的伪可变/静态回退）时既不否认字体是可变字体，也不把
  权重请求交给一条死轴。
* 缓存键带字体库代际（``clear_capabilities_cache`` 递增），字体装卸后旧
  结果整体失效；运行时身份不落地为工程路径，只有 (族, style, 字重, 斜体,
  轴值) 这套可验证描述。
"""

from __future__ import annotations

import struct
import threading
from dataclasses import dataclass, field, replace

from PyQt6.QtGui import QFont, QFontDatabase, QFontInfo, QPainterPath, QPainterPathStroker, QRawFont
from PyQt6.QtCore import Qt


@dataclass(frozen=True)
class FontFace:
    """族内一个可用 face。

    ``is_variable`` 为 True 表示**这个 face 自己**带 wght 可变轴（fvar 真值），
    与族内其它静态 face 无关；``weight`` 是该 face 的静态标称字重（可变 face
    取命名实例/默认实例的字重）。
    """

    weight: int
    style_name: str
    is_italic: bool
    is_variable: bool = False
    axis_min: float | None = None
    axis_max: float | None = None
    axis_default: float | None = None
    axes: tuple[tuple[str, float, float, float], ...] = ()
    """该 face 的**完整轴清单** ``((tag, min, default, max), ...)``——wght 之外
    的 wdth / opsz / ital / slnt / 自定义轴都在内；wght 便捷字段由它派生。"""

    @property
    def has_weight_axis(self) -> bool:
        return (
            self.is_variable
            and self.axis_min is not None
            and self.axis_max is not None
            and self.axis_min < self.axis_max
        )

    def axis_range(self, tag: str) -> tuple[float, float, float] | None:
        """该 face 上某个轴的 (min, default, max)；无此轴返回 None。"""
        for axis_tag, minimum, default, maximum in self.axes:
            if axis_tag == tag:
                return (minimum, default, maximum)
        return None


@dataclass(frozen=True)
class FontCapabilities:
    """一个字体族的完整能力描述（Phase 2 的唯一输入）。

    ``axis_effective`` 为 True 时族内存在**真可变 face**（fvar 有 wght 轴、
    逐 face 定位且两端渲染确实不同）；``axis_present`` 只表示表里声明了轴。
    轴信息取自带轴的那个 face（``variable_style``）。
    """

    family: str
    """规范 Qt 族名（经别名目录换算后）。"""

    faces: tuple[FontFace, ...]
    """face 清单，按 (weight, is_italic, style_name) 升序。"""

    axis_min: float | None = None
    axis_max: float | None = None
    axis_default: float | None = None

    axis_effective: bool = False
    """wght 轴两端（min/max）渲染是否确实不同（伪可变自动 False）。"""

    axis_present: bool = False
    """字体表（fvar）是否声明了 wght 轴——不依赖渲染指纹的表级真值。"""

    variable_style: str | None = None
    """带 wght 轴的 face 的 style 名（``axis_*`` 取自该 face）。"""

    has_italic_face: bool = False
    """族内是否存在真实斜体/倾斜 face（False ⇒ 斜体请求走引擎合成/轴）。"""

    axes: tuple[tuple[str, float, float, float], ...] = ()
    """族级完整轴清单（取自带轴 face / 族级兜底探测），wght 之外的轴也在此。"""

    @property
    def is_variable(self) -> bool:
        return self.axis_effective

    @property
    def face_weights(self) -> tuple[int, ...]:
        return tuple(face.weight for face in self.faces if not face.is_italic)

    @property
    def variable_face(self) -> FontFace | None:
        if self.variable_style is None:
            return None
        for face in self.faces:
            if face.style_name == self.variable_style:
                return face
        return None


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

_CACHE: dict[str, FontCapabilities | None] = {}
_LOCK = threading.RLock()

# 字体库代际：clear_capabilities_cache() 递增。字体装卸（应用字体注册、
# 系统字体变化后的显式失效）必须让旧代际的能力/实例缓存整体作废。
_GENERATION = 0

_WGHT = b"wght"


def capabilities_generation() -> int:
    """当前字体库代际（缓存键的一部分）。"""
    with _LOCK:
        return _GENERATION


def clear_capabilities_cache() -> None:
    """字体装卸后清空（调用方显式触发）。"""
    global _GENERATION
    with _LOCK:
        _CACHE.clear()
        _GENERATION += 1


# ---------------------------------------------------------------------------
# 族名规范化
# ---------------------------------------------------------------------------


def canonical_family(family: str) -> str:
    """把存储/显示用族名换成规范 Qt 族名。

    工作台字体选择器列出 SUG 文件扫描的本地化名（日文/别名）；渲染
    端经 n3 字体目录的 DirectWrite 别名表换算。换算后仍枚举不到
    face 时保留原名（调用方按缺字体处理）。
    """
    key = str(family)
    with _LOCK:
        cached = _CACHE.get("__canon__" + key, _MISSING)
    if cached is not _MISSING:
        return cached  # type: ignore[return-value]
    result = key
    try:
        from krok_helper.subtitle_render.n3.font_catalog import (
            resolve_qt_font_family,
        )

        resolved = resolve_qt_font_family(key)
        if resolved and resolved != key:
            probe = QFont(resolved)
            if QFontInfo(probe).family().casefold() == resolved.casefold():
                result = resolved
    except Exception:
        result = key
    with _LOCK:
        _CACHE["__canon__" + key] = result
    return result


class _Missing:
    pass


_MISSING = _Missing()


# ---------------------------------------------------------------------------
# fvar 解析
# ---------------------------------------------------------------------------


def _parse_fvar_axes(raw: bytes) -> dict[bytes, tuple[float, float, float]]:
    """解析 fvar 表的 (tag → min, default, max)。"""
    if len(raw) < 16:
        return {}
    major, _minor, axes_offset, _reserved, axis_count, axis_size = struct.unpack_from(">6H", raw, 0)
    if major != 1 or axis_size < 20 or axes_offset < 16:
        return {}
    axes: dict[bytes, tuple[float, float, float]] = {}
    for index in range(axis_count):
        offset = axes_offset + index * axis_size
        if offset + 20 > len(raw):
            break
        tag = bytes(raw[offset : offset + 4])
        mn, df, mx = struct.unpack_from(">3l", raw, offset + 4)
        axes[tag] = (mn / 65536.0, df / 65536.0, mx / 65536.0)
    return axes


def _face_axes(
    family: str, style_name: str | None
) -> dict[str, tuple[float, float, float]] | None:
    """读**指定 face** 的完整轴表 ``{tag: (min, default, max)}``；取不到返回 None。

    styleName 钉住具体 face（Qt 的 styleName 匹配在权重不冲突时稳定命中），
    再用 QRawFont 读该 face 的 fvar。解析到的 face 与请求 style 不一致时
    按「取不到」处理，避免把别的 face 的轴信息当成这个 face 的。
    """
    try:
        probe = QFont(family)
        if style_name:
            probe.setStyleName(style_name)
        raw_font = QRawFont.fromFont(probe)
        if style_name:
            resolved_style = raw_font.styleName()
            if resolved_style and resolved_style.casefold() != style_name.casefold():
                return None
        table = bytes(raw_font.fontTable(b"fvar"))
    except (RuntimeError, TypeError, ValueError):
        return None
    if not table:
        return None
    axes = _parse_fvar_axes(table)
    if not axes:
        return None
    return {tag.decode("latin-1"): value for tag, value in axes.items()}


def _face_weight_axis(
    family: str, style_name: str | None
) -> tuple[float, float, float] | None:
    """``_face_axes`` 的 wght 便捷视图（兼容旧调用方）。"""
    axes = _face_axes(family, style_name)
    return None if axes is None else axes.get("wght")


_AXIS_PROBE_TEXT = "Ag0Wg指あそ爽永"


def _axis_cheap_signature(
    family: str, style_name: str | None, value: float, size: int
) -> tuple | None:
    """轴值 → 廉价指纹（advance + 1/8 像素取整墨迹盒）；族不可解析返回 None。

    只作快速路径：实测它对相邻轴档大量漏判（Noto Sans SC / Iwata /
    Segoe UI Variable / Bahnschrift 各有 300–600 个相邻档「宽度与包围盒相同、
    像素确有差异」——轴可能只改内部笔画粗细），因此它判「不同」即生效、
    判「相同」还要像素指纹定谳。
    """
    font = QFont(family)
    if style_name:
        font.setStyleName(style_name)
    font.setVariableAxis(QFont.Tag(_WGHT), float(value))
    if QFontInfo(font).family().casefold() != family.casefold():
        return None
    from PyQt6.QtGui import QFontMetrics

    font.setPixelSize(size)
    metrics = QFontMetrics(font)
    cheap: list = list(
        metrics.horizontalAdvance(ch) for ch in _AXIS_PROBE_TEXT
    )
    path = QPainterPath()
    path.addText(0.0, 0.0, font, _AXIS_PROBE_TEXT)
    rect = path.boundingRect()
    cheap.extend(
        (round(rect.left() * 8), round(rect.top() * 8),
         round(rect.right() * 8), round(rect.bottom() * 8))
    )
    return tuple(cheap)


def _axis_raster_signature(
    family: str, style_name: str | None, value: float, size: int
) -> bytes | None:
    """轴值 → 像素指纹（样本文字在固定画布上的光栅化字节）。"""
    font = QFont(family)
    if style_name:
        font.setStyleName(style_name)
    font.setVariableAxis(QFont.Tag(_WGHT), float(value))
    font.setPixelSize(size)
    return _raster_probe(font)


def _axis_render_signature(
    family: str, style_name: str | None, value: float, size: int
) -> tuple[tuple | None, bytes | None]:
    """轴值 → （廉价指纹，像素指纹）组合视图（测试/诊断用）。"""
    return (
        _axis_cheap_signature(family, style_name, value, size),
        _axis_raster_signature(family, style_name, value, size),
    )



def _raster_probe(font: QFont) -> bytes | None:
    """样本文字在固定画布上的光栅化字节（轴值像素指纹）。"""
    from PyQt6.QtGui import QImage, QPainter

    try:
        image = QImage(260, 96, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            painter.setFont(font)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            painter.drawText(4, 72, _AXIS_PROBE_TEXT)
        finally:
            painter.end()
        return bytes(image.constBits().asstring(image.sizeInBytes()))
    except (RuntimeError, TypeError, ValueError):
        return None


def _axis_is_effective(
    family: str, mn: float, mx: float, style_name: str | None = None
) -> bool:
    """wght 轴两端是否产生可观测差异（辅助确认，不作为唯一判据）。

    表里声明了轴但两端渲染完全相同（伪可变、或 Qt 把轴静默忽略）时，
    仅凭渲染指纹会误判为「静态」；因此调用方以 ``axis_present`` 记录
    表级事实，本函数只回答「这条轴值不值得交给渲染」。

    判定顺序：**廉价指纹优先短路**（advance/bbox 在 min/中/max 任一档不同
    即生效，不付光栅成本）→ 三档全同才上**像素指纹**逐字节比较，专治
    「轴只改内部笔画、外轮廓与字宽不变」的漏判（2026-10-11 外部评估指出的风险）。
    """
    if mn >= mx:
        return False
    values = (float(mn), float((mn + mx) / 2.0), float(mx))
    # 快速路径：advance/bbox 在 min/中/max 任一档不同即生效——可变字体的轴
    # 实例创建是百毫秒级成本（Segoe UI Variable 实测 3 档光栅 ~736ms），
    # 能短路就不付光栅。
    cheap = [
        _axis_cheap_signature(family, style_name, value, 40) for value in values
    ]
    if any(signature is None for signature in cheap):
        return False
    if len(set(cheap)) > 1:
        return True
    # 廉价指纹三档全同：像素指纹逐字节定谳（治「轴只改内部笔画」的漏判）。
    rasters = [
        _axis_raster_signature(family, style_name, value, 40) for value in values
    ]
    if any(raster is None for raster in rasters):
        return False
    return len(set(rasters)) > 1


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def get_capabilities(family: str) -> FontCapabilities | None:
    """获取一个族的完整能力描述；字体不存在返回 None。"""
    raw = str(family)
    with _LOCK:
        cached = _CACHE.get(raw, _MISSING)
    if cached is not _MISSING:
        return cached  # None 或 FontCapabilities

    canonical = canonical_family(raw)

    # 枚举 face
    faces: list[FontFace] = []
    try:
        styles = list(QFontDatabase.styles(canonical))
    except (RuntimeError, TypeError, ValueError):
        styles = []
    for style in styles:
        try:
            weight = int(QFontDatabase.weight(canonical, style))
            italic = bool(QFontDatabase.italic(canonical, style))
        except (RuntimeError, TypeError, ValueError):
            continue
        if not (1 <= weight <= 1000):
            continue
        face_axes = _face_axes(canonical, style)
        weight_axis = None if face_axes is None else face_axes.get("wght")
        faces.append(
            FontFace(
                weight,
                str(style),
                italic,
                is_variable=weight_axis is not None,
                axis_min=weight_axis[0] if weight_axis else None,
                axis_max=weight_axis[2] if weight_axis else None,
                axis_default=weight_axis[1] if weight_axis else None,
                axes=tuple(
                    (tag, values[0], values[1], values[2])
                    for tag, values in sorted((face_axes or {}).items())
                ),
            )
        )
    faces.sort(key=lambda f: (f.weight, f.is_italic, f.style_name))

    # 轴信息取自带轴 face；带轴 face 有多个时取字重最接近常规的那一个。
    variable_faces = [face for face in faces if face.has_weight_axis]
    variable_style = None
    axis_min = axis_max = axis_default = None
    axis_present = bool(variable_faces)
    axis_effective = False
    family_axis_inventory: tuple[tuple[str, float, float, float], ...] = ()
    if variable_faces:
        variable_face = min(
            variable_faces, key=lambda f: (abs(f.weight - 400), f.weight)
        )
        variable_style = variable_face.style_name
        axis_min = variable_face.axis_min
        axis_max = variable_face.axis_max
        axis_default = variable_face.axis_default
    elif faces:
        # 逐 face 钉不住 styleName 时退回族级探测：Qt 的 style 名可能带光学
        # 尺寸/条件前缀（Segoe UI Variable 的 'Small'/'Text'/'Display' 钉回去
        # 解析成 'Regular'），逐 face 校验会一律判否——族默认 face 仍读得到
        # fvar。族级只在逐 face 全部失败时兜底；「族内静态+可变共存」仍以
        # 逐 face 结果为准（那是本次修正的主目标）。
        family_axes = _face_axes(canonical, None) or {}
        family_axis = family_axes.get("wght")
        if family_axis is not None and family_axis[0] < family_axis[2]:
            axis_present = True
            axis_min, axis_default, axis_max = family_axis
        if family_axes:
            family_axis_inventory = tuple(
                (tag, values[0], values[1], values[2])
                for tag, values in sorted(family_axes.items())
            )
            # 逐 face 探测失败（styleName 钉不住）时，把族级轴清单挂到这些
            # face 上：决策层要按 face 拿「全轴最终值表」，空清单会让两侧
            # 引擎各自用默认实例（实测 Segoe UI Variable 差 ~9% 墨量）。
            faces = [replace(face, axes=family_axis_inventory) for face in faces]
        else:
            family_axis_inventory = ()
    if axis_present and axis_min is not None and axis_max is not None:
        axis_effective = _axis_is_effective(
            canonical, axis_min, axis_max, variable_style
        )

    if not faces:
        result = None
    else:
        result = FontCapabilities(
            family=canonical,
            faces=tuple(faces),
            axis_min=axis_min,
            axis_max=axis_max,
            axis_default=axis_default,
            axis_effective=axis_effective,
            axis_present=axis_present,
            variable_style=variable_style,
            has_italic_face=any(face.is_italic for face in faces),
            axes=(
                variable_face.axes
                if variable_faces
                else family_axis_inventory
            ),
        )

    with _LOCK:
        _CACHE[raw] = result
        if result is not None and canonical != raw:
            _CACHE.setdefault(canonical, result)
    return result


__all__ = [
    "FontCapabilities",
    "FontFace",
    "canonical_family",
    "capabilities_generation",
    "clear_capabilities_cache",
    "get_capabilities",
]
