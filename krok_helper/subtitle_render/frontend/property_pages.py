"""Stable registry for subtitle property-panel pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PropertyPageDefinition:
    """Navigation identity and host builder contract for one property page."""

    route_key: str
    label: str
    builder_name: str


PROPERTY_PAGE_DEFINITIONS = (
    PropertyPageDefinition("font", "角色", "_make_subtitle_page"),
    PropertyPageDefinition("layout", "布局", "_make_basic_page"),
    PropertyPageDefinition("timing", "时间", "_make_timing_page"),
    PropertyPageDefinition("effects", "特效", "_make_effects_page"),
    PropertyPageDefinition("title", "标题", "_make_title_page"),
    PropertyPageDefinition("background", "背景/音频", "_make_background_page"),
)

PROPERTY_PAGE_SPECS = tuple(
    (definition.route_key, definition.label)
    for definition in PROPERTY_PAGE_DEFINITIONS
)


def build_property_pages(host: Any) -> tuple[Any, ...]:
    """Build registered pages through the current host implementation boundary."""
    return tuple(
        getattr(host, definition.builder_name)()
        for definition in PROPERTY_PAGE_DEFINITIONS
    )


def property_page_index(route_key: str) -> int | None:
    """Resolve a navigation route without exposing registry traversal to callers."""
    for index, definition in enumerate(PROPERTY_PAGE_DEFINITIONS):
        if definition.route_key == route_key:
            return index
    return None
