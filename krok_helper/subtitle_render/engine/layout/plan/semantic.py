"""Public construction boundary for shared CPU/GPU subtitle semantics."""

from __future__ import annotations

from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.layout.plan.model import TrackLayoutPlan
from krok_helper.subtitle_render.engine.layout.plan.orchestrator import (
    LayoutPlanResolvers,
    resolve_track_layout_plan,
)
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingTrack


def build_track_layout_plan(
    track: TimingTrack,
    style: Style,
    resolvers: LayoutPlanResolvers,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> TrackLayoutPlan:
    """Build one immutable semantic plan through explicit backend ports."""

    return resolve_track_layout_plan(
        track,
        style,
        resolvers,
        logical_w=logical_w,
        logical_h=logical_h,
    )

__all__ = ["LayoutPlanResolvers", "build_track_layout_plan", "layout_pass"]
