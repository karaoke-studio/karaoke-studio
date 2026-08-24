"""Temporary adapter from layout diagnostics consumers to Painter geometry."""

from krok_helper.subtitle_render.engine.painter import (
    check_layout_margins,
    display_windows_for_style,
    layout_timing_diagnostics_for_style,
)

__all__ = [
    "check_layout_margins",
    "display_windows_for_style",
    "layout_timing_diagnostics_for_style",
]
