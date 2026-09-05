"""Stable registry for subtitle property-panel pages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PropertyPageDefinition:
    """Navigation identity and host builder contract for one property page."""

    route_key: str
    label: str
    builder: Callable[[Any, Callable[[], Any], Callable[[Any, Any], Any]], Any]

    @property
    def builder_name(self) -> str:
        return self.builder.__name__


def _build_role_page(host: Any, scroll_page: Callable, _section_pair: Callable) -> Any:
    scroll, layout = scroll_page()
    host._font_color_section = host._make_font_color_section()
    host._role_section = host._font_color_section
    layout.addWidget(host._font_color_section)
    layout.addStretch(1)
    return scroll


def _build_layout_page(
    host: Any,
    scroll_page: Callable,
    section_pair: Callable,
) -> Any:
    scroll, layout = scroll_page()
    layout.addWidget(host._make_row_structure_section())
    host._ruby_section = host._make_ruby_section()
    layout.addWidget(
        section_pair(host._ruby_section, host._make_vertical_layout_section())
    )
    viewport = host._make_viewport_section()
    viewport.set_expanded(False)
    layout.addWidget(viewport)
    layout.addStretch(1)
    return scroll


def _build_timing_page(host: Any, scroll_page: Callable, _section_pair: Callable) -> Any:
    scroll, layout = scroll_page()
    layout.addWidget(host._make_timing_section())
    layout.addStretch(1)
    return scroll


def _build_effects_page(host: Any, scroll_page: Callable, _section_pair: Callable) -> Any:
    scroll, layout = scroll_page()
    layout.addWidget(host._make_animation_section())
    layout.addWidget(host._make_lit_section())
    layout.addStretch(1)
    return scroll


def _build_title_page(host: Any, scroll_page: Callable, _section_pair: Callable) -> Any:
    scroll, layout = scroll_page()
    layout.addWidget(host._make_title_page())
    layout.addStretch(1)
    return scroll


def _build_background_page(
    host: Any,
    scroll_page: Callable,
    _section_pair: Callable,
) -> Any:
    scroll, layout = scroll_page()
    layout.addWidget(host._make_background_source_section())
    layout.addWidget(host._make_screen_size_section())
    layout.addStretch(1)
    return scroll


PROPERTY_PAGE_DEFINITIONS = (
    PropertyPageDefinition("font", "角色", _build_role_page),
    PropertyPageDefinition("layout", "布局", _build_layout_page),
    PropertyPageDefinition("timing", "时间", _build_timing_page),
    PropertyPageDefinition("effects", "特效", _build_effects_page),
    PropertyPageDefinition("title", "标题", _build_title_page),
    PropertyPageDefinition("background", "背景/音频", _build_background_page),
)

PROPERTY_PAGE_SPECS = tuple(
    (definition.route_key, definition.label)
    for definition in PROPERTY_PAGE_DEFINITIONS
)


def build_property_pages(
    host: Any,
    *,
    scroll_page_factory: Callable[[], Any],
    section_pair_factory: Callable[[Any, Any], Any],
) -> tuple[Any, ...]:
    """Build registered pages through the current host implementation boundary."""
    return tuple(
        definition.builder(host, scroll_page_factory, section_pair_factory)
        for definition in PROPERTY_PAGE_DEFINITIONS
    )


def property_page_index(route_key: str) -> int | None:
    """Resolve a navigation route without exposing registry traversal to callers."""
    for index, definition in enumerate(PROPERTY_PAGE_DEFINITIONS):
        if definition.route_key == route_key:
            return index
    return None
