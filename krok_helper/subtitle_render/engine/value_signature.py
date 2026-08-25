"""Stable value signatures for caches backed by mutable project models."""

from __future__ import annotations

from dataclasses import fields as dataclass_fields, is_dataclass
from typing import Hashable

from krok_helper.subtitle_render.domain.timing import GuideSymbol


_SIG_FIELD_NAMES_BY_TYPE: dict[type, tuple[str, ...]] = {}


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


__all__ = ["value_signature"]
