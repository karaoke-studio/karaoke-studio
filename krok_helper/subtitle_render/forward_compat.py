"""Schema-aware merging for forward-compatible subtitle data."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional


AUTHORITATIVE_MAPPING_PATHS = frozenset(
    {
        ("style", "custom_style_schemes"),
        ("style", "singer_style_overrides"),
        ("style", "default_layout_by_row_count"),
    }
)


def merge_extensible_value(
    source: Any,
    current: Any,
    *,
    path: tuple[str, ...],
) -> Any:
    """Merge unknown nested fields without reviving removed user-owned entries."""

    if isinstance(source, dict) and isinstance(current, dict):
        if path in AUTHORITATIVE_MAPPING_PATHS:
            merged: dict[Any, Any] = {}
        else:
            merged = deepcopy(source)
        for key, current_value in current.items():
            source_value = source.get(key)
            if isinstance(current_value, (dict, list)) and isinstance(
                source_value, type(current_value)
            ):
                merged[key] = merge_extensible_value(
                    source_value,
                    current_value,
                    path=(*path, str(key)),
                )
            else:
                merged[key] = deepcopy(current_value)
        return merged

    if isinstance(source, list) and isinstance(current, list):
        used_source_indices: set[int] = set()
        merged_items: list[Any] = []
        for index, current_value in enumerate(current):
            source_index = _matching_source_list_index(
                source,
                current_value,
                path=path,
                fallback=index,
                used=used_source_indices,
            )
            if source_index is None:
                merged_items.append(deepcopy(current_value))
                continue
            used_source_indices.add(source_index)
            source_value = source[source_index]
            if isinstance(current_value, (dict, list)) and isinstance(
                source_value, type(current_value)
            ):
                merged_items.append(
                    merge_extensible_value(
                        source_value,
                        current_value,
                        path=(*path, "[]"),
                    )
                )
            else:
                merged_items.append(deepcopy(current_value))
        return merged_items

    return deepcopy(current)


def _matching_source_list_index(
    source: list[Any],
    current_value: Any,
    *,
    path: tuple[str, ...],
    fallback: int,
    used: set[int],
) -> Optional[int]:
    identity_key = None
    if path == ("style", "layouts"):
        identity_key = "layout_id"
    elif path == ("extra_subtitle_sources",):
        identity_key = "path"
    elif path == ("style_presets",):
        identity_key = "id"
    if identity_key and isinstance(current_value, dict):
        identity = str(current_value.get(identity_key) or "").strip()
        if identity:
            for index, source_value in enumerate(source):
                if index in used or not isinstance(source_value, dict):
                    continue
                if str(source_value.get(identity_key) or "").strip() == identity:
                    return index
    if 0 <= fallback < len(source) and fallback not in used:
        return fallback
    return None
