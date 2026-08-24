"""Contracts for the property-panel page registry."""

from __future__ import annotations

from krok_helper.subtitle_render.frontend.properties.property_pages import (
    PROPERTY_PAGE_DEFINITIONS,
    PROPERTY_PAGE_SPECS,
    build_property_pages,
    property_page_index,
)


def test_property_page_registry_preserves_navigation_order_and_labels() -> None:
    assert PROPERTY_PAGE_SPECS == (
        ("font", "角色"),
        ("layout", "布局"),
        ("timing", "时间"),
        ("effects", "特效"),
        ("title", "标题"),
        ("background", "背景/音频"),
    )
    assert len({item.route_key for item in PROPERTY_PAGE_DEFINITIONS}) == len(
        PROPERTY_PAGE_DEFINITIONS
    )


def test_property_page_registry_builds_each_host_page_once_in_order() -> None:
    calls: list[str] = []

    class Layout:
        def addWidget(self, widget) -> None:
            calls.append(f"widget:{widget}")

        def addStretch(self, stretch: int) -> None:
            calls.append(f"stretch:{stretch}")

    class Viewport:
        def set_expanded(self, expanded: bool) -> None:
            calls.append(f"viewport_expanded:{expanded}")

        def __str__(self) -> str:
            return "_make_viewport_section"

    class Host:
        def __getattr__(self, name: str):
            if name == "_make_viewport_section":
                return lambda: calls.append(name) or Viewport()
            if name.startswith("_make_"):
                return lambda: calls.append(name) or name
            raise AttributeError(name)

    host = Host()
    page_number = 0

    def scroll_page():
        nonlocal page_number
        page_number += 1
        return f"page:{page_number}", Layout()

    def section_pair(left, right):
        calls.append(f"pair:{left}:{right}")
        return f"pair:{left}:{right}"

    pages = build_property_pages(
        host,
        scroll_page_factory=scroll_page,
        section_pair_factory=section_pair,
    )

    assert pages == tuple(f"page:{index}" for index in range(1, 7))
    assert calls.count("stretch:1") == 6
    assert calls.index("_make_animation_section") < calls.index("_make_lit_section")
    assert calls.index("_make_title_text_section") < calls.index(
        "_make_title_style_section"
    ) < calls.index("_make_title_time_section")
    assert calls.index("_make_background_source_section") < calls.index(
        "_make_screen_size_section"
    )
    assert "viewport_expanded:False" in calls
    assert host._role_section == host._font_color_section
    assert host._ruby_section == "_make_ruby_section"


def test_property_page_registry_resolves_routes_without_leaking_traversal() -> None:
    assert property_page_index("font") == 0
    assert property_page_index("background") == 5
    assert property_page_index("missing") is None
