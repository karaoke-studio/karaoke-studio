"""Bounded ownership cache for shared CPU/GPU track layout plans."""

from __future__ import annotations

from collections import OrderedDict
import os
from typing import Hashable

from krok_helper.subtitle_render.engine.layout_plan import TrackLayoutPlan
from krok_helper.subtitle_render.timing import TimingTrack
from krok_helper.subtitle_render.models import Style


_TRACK_LAYOUT_PLAN_CACHE_MAX = 24
_TRACK_LAYOUT_PLAN_CACHE: OrderedDict[
    Hashable,
    tuple[TimingTrack, Style, TrackLayoutPlan],
] = OrderedDict()


def layout_cache_enabled() -> bool:
    """Return whether frame-independent subtitle layout caches are enabled."""
    return os.environ.get("KROK_SUBTITLE_LAYOUT_CACHE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def cached_track_layout_plan(key: Hashable) -> TrackLayoutPlan | None:
    """Return and promote a cached plan without exposing its owner tuple."""

    cached = _TRACK_LAYOUT_PLAN_CACHE.get(key)
    if cached is None:
        return None
    _TRACK_LAYOUT_PLAN_CACHE.move_to_end(key)
    return cached[2]


def store_track_layout_plan(
    key: Hashable,
    track: TimingTrack,
    style: Style,
    plan: TrackLayoutPlan,
) -> None:
    """Retain a plan and its mutable owners under the established LRU bound."""

    _TRACK_LAYOUT_PLAN_CACHE[key] = (track, style, plan)
    _TRACK_LAYOUT_PLAN_CACHE.move_to_end(key)
    while len(_TRACK_LAYOUT_PLAN_CACHE) > _TRACK_LAYOUT_PLAN_CACHE_MAX:
        _TRACK_LAYOUT_PLAN_CACHE.popitem(last=False)


def clear_track_layout_plan_cache() -> None:
    _TRACK_LAYOUT_PLAN_CACHE.clear()


__all__ = [
    "cached_track_layout_plan",
    "clear_track_layout_plan_cache",
    "layout_cache_enabled",
    "store_track_layout_plan",
]
