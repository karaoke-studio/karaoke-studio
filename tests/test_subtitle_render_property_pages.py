"""Contracts for the property-panel page registry."""

from __future__ import annotations

from krok_helper.subtitle_render.frontend.property_pages import (
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

    class Host:
        pass

    host = Host()
    for definition in PROPERTY_PAGE_DEFINITIONS:
        setattr(
            host,
            definition.builder_name,
            lambda name=definition.builder_name: calls.append(name) or name,
        )

    pages = build_property_pages(host)

    assert pages == tuple(item.builder_name for item in PROPERTY_PAGE_DEFINITIONS)
    assert calls == list(pages)


def test_property_page_registry_resolves_routes_without_leaking_traversal() -> None:
    assert property_page_index("font") == 0
    assert property_page_index("background") == 5
    assert property_page_index("missing") is None
