"""Painter-backed adapter for the shared semantic layout-plan contract."""

from __future__ import annotations

from krok_helper.subtitle_render.engine.layout.plan.model import TrackLayoutPlan
from krok_helper.subtitle_render.engine.layout.plan.semantic import (
    LayoutPlanResolvers,
    build_track_layout_plan as build_semantic_layout_plan,
)
from krok_helper.subtitle_render.engine.painter import (
    display_lines_for_style,
    resolved_page_offset_windows_for_style,
)
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingTrack


_PAINTER_LAYOUT_RESOLVERS = LayoutPlanResolvers(
    display_lines=display_lines_for_style,
    page_offset_windows=resolved_page_offset_windows_for_style,
)


def build_track_layout_plan(
    track: TimingTrack,
    style: Style,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
    use_cache: bool = True,
) -> TrackLayoutPlan:
    """Build the shared plan using Painter's current geometry backend.

    ``use_cache=False`` 强制全量重排；``True`` 时按签名复用（见
    :func:`resolve_track_layout_plan`）。
    """

    return build_semantic_layout_plan(
        track,
        style,
        _PAINTER_LAYOUT_RESOLVERS,
        logical_w=logical_w,
        logical_h=logical_h,
        use_cache=use_cache,
    )


__all__ = ["build_track_layout_plan"]
