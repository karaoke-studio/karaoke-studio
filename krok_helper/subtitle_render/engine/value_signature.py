"""Stable value signatures for caches backed by mutable project models."""

from __future__ import annotations

from dataclasses import fields as dataclass_fields, is_dataclass
from typing import Hashable

from krok_helper.subtitle_render.domain.timing import GuideSymbol


_SIG_FIELD_NAMES_BY_TYPE: dict[type, tuple[str, ...]] = {}
_SIG_EXCLUDED_NAMES_BY_TYPE: dict[type, tuple[str, ...]] = {}

_LYRIC_LAYOUT_EXCLUDED_STYLE_FIELDS = frozenset({
    "title_overlays",
    "hidden_builtin_layout_ids",
})
"""纯标题字段：只喂标题层，与歌词行的排版/分页/布局计划无关。

把它们从「歌词布局」类缓存的样式签名里剔除后，编辑标题属性不再连带
作废全部歌词布局缓存（整轨重排的根因之一）。剔除清单必须保守：
任何可能影响歌词布局的字段都不能进来。
"""


def value_signature(value) -> Hashable:
    """Recursively describe the current value without using object identity."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # GuideSymbol is the only frozen model carrying a potentially very large
    # immutable tuple (the complete SVG outline). Reusing the value as the key
    # avoids recursively copying every path command on every cache lookup.
    if isinstance(value, GuideSymbol):
        return value
    if isinstance(value, (list, tuple)):
        return tuple(value_signature(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            (key, value_signature(item))
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        )
    if is_dataclass(value) and not isinstance(value, type):
        value_type = type(value)
        names = _SIG_FIELD_NAMES_BY_TYPE.get(value_type)
        if names is None:
            names = tuple(field.name for field in dataclass_fields(value))
            _SIG_FIELD_NAMES_BY_TYPE[value_type] = names
        return (value_type.__name__,) + tuple(
            value_signature(getattr(value, name)) for name in names
        )
    return repr(value)


def lyric_layout_style_signature(style) -> Hashable:
    """Style signature restricted to fields that can affect lyric layout.

    与 :func:`value_signature` 的区别：剔除 ``_LYRIC_LAYOUT_EXCLUDED_STYLE_FIELDS``
    列出的纯标题字段。用于歌词行布局 / 显示行解析 / 页偏移 / 布局计划等
    缓存的 key——标题属性编辑不再整份作废这些缓存。
    局部复用永远以本签名为准（签名不匹配即回退重建），调用方传入的
    「只改了标题」只是性能提示，不是正确性依据。
    """
    value_type = type(style)
    names = _SIG_EXCLUDED_NAMES_BY_TYPE.get(value_type)
    if names is None:
        names = tuple(
            field.name
            for field in dataclass_fields(style)
            if field.name not in _LYRIC_LAYOUT_EXCLUDED_STYLE_FIELDS
        )
        _SIG_EXCLUDED_NAMES_BY_TYPE[value_type] = names
    return (value_type.__name__,) + tuple(
        value_signature(getattr(style, name)) for name in names
    )


__all__ = [
    "lyric_layout_style_signature",
    "value_signature",
]
