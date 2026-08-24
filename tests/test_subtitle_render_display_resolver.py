from __future__ import annotations

from krok_helper.subtitle_render.engine.layout.display_resolver import (
    DisplayResolutionPorts,
    resolve_display_lines,
)


def test_display_resolver_skips_collision_discovery_when_overlap_is_allowed() -> None:
    events: list[object] = []

    def compute(**kwargs):
        events.append(("compute", kwargs))
        return ["ideal"]

    ports = DisplayResolutionPorts(
        compute=compute,
        resolve_timing=lambda items, enforce: (
            events.append(("timing", tuple(items), enforce)) or ["resolved"]
        ),
        collision_pairs=lambda _items: (_ for _ in ()).throw(
            AssertionError("collision discovery must be skipped")
        ),
        secondary_collision_pairs=lambda _items: (_ for _ in ()).throw(
            AssertionError("secondary discovery must be skipped")
        ),
        fill_section_time=lambda items: items,
        apply_animation_guard=lambda items, _enforce: items,
    )

    resolved = resolve_display_lines(
        avoid_collisions=False,
        auto_fill_section_time=False,
        ports=ports,
    )

    assert resolved == ["resolved"]
    assert events == [
        (
            "compute",
            {
                "adjust_same_position": False,
                "dynamic_single_page_reflow": True,
                "independent_line_entry": True,
            },
        ),
        ("timing", ("ideal",), False),
    ]


def test_display_resolver_preserves_collision_pass_order() -> None:
    events: list[object] = []
    computed = iter((["ideal"], ["forced"], ["squeezed"], ["secondary"]))
    discovered = iter((((0, 1),), ((1, 2),)))

    def compute(**kwargs):
        events.append(("compute", kwargs))
        return list(next(computed))

    def resolve_timing(items, enforce):
        events.append(("timing", tuple(items), enforce))
        return list(items)

    ports = DisplayResolutionPorts(
        compute=compute,
        resolve_timing=resolve_timing,
        collision_pairs=lambda items: (
            events.append(("pairs", tuple(items))) or next(discovered)
        ),
        secondary_collision_pairs=lambda items: (
            events.append(("secondary_pairs", tuple(items))) or ((2, 3),)
        ),
        fill_section_time=lambda items: items,
        apply_animation_guard=lambda items, _enforce: items,
    )

    resolved = resolve_display_lines(
        avoid_collisions=True,
        auto_fill_section_time=False,
        ports=ports,
    )

    assert resolved == ["secondary"]
    compute_events = [event for event in events if event[0] == "compute"]
    assert compute_events[1][1]["force_bottom_pairs"] == ((0, 1),)
    assert compute_events[2][1]["squeeze_pairs"] == ((1, 2),)
    assert compute_events[3][1]["squeeze_pairs"] == ((1, 2), (2, 3))
    assert [event for event in events if event[0] == "timing"] == [
        ("timing", ("ideal",), True),
        ("timing", ("forced",), True),
        ("timing", ("squeezed",), True),
        ("timing", ("secondary",), True),
    ]


def test_display_resolver_refinalizes_timing_after_section_fill() -> None:
    events: list[object] = []
    ports = DisplayResolutionPorts(
        compute=lambda **_kwargs: ["ideal"],
        resolve_timing=lambda items, _enforce: list(items),
        collision_pairs=lambda _items: (),
        secondary_collision_pairs=lambda _items: (),
        fill_section_time=lambda items: (
            events.append(("fill", tuple(items))) or ["filled"]
        ),
        apply_animation_guard=lambda items, enforce: (
            events.append(("guard", tuple(items), enforce)) or ["guarded"]
        ),
    )

    resolved = resolve_display_lines(
        avoid_collisions=False,
        auto_fill_section_time=True,
        ports=ports,
    )

    assert resolved == ["guarded"]
    assert events == [
        ("fill", ("ideal",)),
        ("guard", ("filled",), False),
    ]
