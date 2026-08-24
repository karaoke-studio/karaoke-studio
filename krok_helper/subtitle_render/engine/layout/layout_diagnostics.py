"""Public layout diagnostics consumed by the subtitle editor UI.

The editor needs resolved display windows and user-facing diagnostics, but it
must not depend on Painter's implementation module.  Direct aliases keep the
existing object identity, cache scope, monkeypatch behaviour, and results while
the semantic planner is extracted from the CPU renderer incrementally.
"""

from __future__ import annotations

from krok_helper.subtitle_render.engine.painter import (
    LayoutMarginWarning,
    LayoutTimingDiagnostic,
    check_layout_margins,
    display_windows_for_style,
    layout_pass,
    layout_timing_diagnostics_for_style,
)

__all__ = [
    "LayoutMarginWarning",
    "LayoutTimingDiagnostic",
    "check_layout_margins",
    "display_windows_for_style",
    "layout_pass",
    "layout_timing_diagnostics_for_style",
]
