"""Phase 1: Font Capabilities — 纯数据，回答「这个字体实际有什么」。

不包含任何渲染决策（选 face / 膨胀 / 合成等全部在 weight_resolver.py）。
数据来源：QFontDatabase（face 清单）+ QRawFont fvar 解析（轴信息）+ n3
别名目录（本地化族名规范化）。进程级缓存。

设计的唯一目的是给 Phase 2（weight_resolver.resolve）提供完整、准确
的字体能力描述，让 Phase 2 可以作为无副作用的纯函数独立测试。
"""

from __future__ import annotations

import struct
import threading
from dataclasses import dataclass, field

from PyQt6.QtGui import QFont, QFontDatabase, QFontInfo, QPainterPath, QPainterPathStroker, QRawFont
from PyQt6.QtCore import Qt


@dataclass(frozen=True)
class FontFace:
    """族内一个可用 face（静态实例）。"""

    weight: int
    style_name: str
    is_italic: bool


@dataclass(frozen=True)
class FontCapabilities:
    """一个字体族的完整能力描述（Phase 2 的唯一输入）。

    ``axis`` 非 None 且 ``axis_effective`` 为 True 时族是**真可变字体**
    （fvar 有 wght 轴且两端渲染确实不同）；否则按静态 face 处理（伪
    可变——fvar 有轴但两端渲染相同——自动降级为静态）。
    """

    family: str
    """规范 Qt 族名（经别名目录换算后）。"""

    faces: tuple[FontFace, ...]
    """静态 face 清单，按 (weight, is_italic, style_name) 升序。"""

    axis_min: float | None = None
    axis_max: float | None = None
    axis_default: float | None = None

    axis_effective: bool = False
    """wght 轴两端（min/max）渲染是否确实不同（伪可变自动 False）。"""

    @property
    def is_variable(self) -> bool:
        return self.axis_effective

    @property
    def face_weights(self) -> tuple[int, ...]:
        return tuple(face.weight for face in self.faces if not face.is_italic)


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

_CACHE: dict[str, FontCapabilities | None] = {}
_LOCK = threading.RLock()

_WGHT = b"wght"


def clear_capabilities_cache() -> None:
    """字体装卸后清空（调用方显式触发）。"""
    with _LOCK:
        _CACHE.clear()


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


def _axis_is_effective(family: str, mn: float, mx: float) -> bool:
    """wght 轴两端是否产生可观测差异。"""
    if mn >= mx:
        return False
    signatures = []
    for value in (mn, mx):
        font = QFont(family)
        font.setVariableAxis(QFont.Tag(_WGHT), float(value))
        if QFontInfo(font).family().casefold() != family.casefold():
            return False
        # 指纹：多字号 advance + 墨迹 bbox
        sig = []
        for size in (40, 41):
            font.setPixelSize(size)
            from PyQt6.QtGui import QFontMetrics

            metrics = QFontMetrics(font)
            sig.extend(
                metrics.horizontalAdvance(ch) for ch in "Ag0Wg指あそ爽永"
            )
            path = QPainterPath()
            path.addText(0.0, 0.0, font, "Ag0Wg指あそ爽永")
            rect = path.boundingRect()
            sig.extend(
                (round(rect.left() * 8), round(rect.top() * 8),
                 round(rect.right() * 8), round(rect.bottom() * 8))
            )
        signatures.append(tuple(sig))
    return signatures[0] != signatures[1]


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
        for style in QFontDatabase.styles(canonical):
            weight = int(QFontDatabase.weight(canonical, style))
            name = str(style)
            italic = bool(QFontDatabase.italic(canonical, style))
            if 1 <= weight <= 1000:
                faces.append(FontFace(weight, name, italic))
    except (RuntimeError, TypeError, ValueError):
        faces = []
    faces.sort(key=lambda f: (f.weight, f.is_italic, f.style_name))

    # 检测可变轴
    axis_min = axis_max = axis_default = None
    axis_effective = False
    if faces:
        try:
            raw_font = QRawFont.fromFont(QFont(canonical))
            table = bytes(raw_font.fontTable(b"fvar"))
            axes = _parse_fvar_axes(table) if table else {}
            wght = axes.get(_WGHT)
            if wght is not None:
                axis_min, axis_default, axis_max = wght
                axis_effective = _axis_is_effective(canonical, axis_min, axis_max)
        except (RuntimeError, TypeError, ValueError):
            pass

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
    "clear_capabilities_cache",
    "get_capabilities",
]
