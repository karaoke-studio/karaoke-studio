"""Public construction boundary for shared CPU/GPU subtitle semantics.

Render IR consumers depend on this module rather than on the CPU Painter.  The
current aliases deliberately preserve the established planner implementation,
cache identity, and layout-pass scope while that implementation is extracted
behind this boundary in later behavior-preserving stages.
"""

from __future__ import annotations

from krok_helper.subtitle_render.engine.painter import (
    build_track_layout_plan,
    layout_pass,
)

__all__ = ["build_track_layout_plan", "layout_pass"]
