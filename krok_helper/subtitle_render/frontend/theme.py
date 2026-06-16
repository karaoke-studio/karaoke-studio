"""Lazy workbench theme helpers for the subtitle render frontend.

``krok_helper.theme_workbench`` imports the SUG theme singleton at module import
time, and that singleton expects a live ``QApplication``.  The subtitle render
package is also imported by tests and by the standalone ``__main__`` before the
window is constructed, so keep the import behind small runtime wrappers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PyQt6.QtWidgets import QApplication


@dataclass(frozen=True)
class _FallbackPalette:
    shell_bg: str = "#F4F7FB"
    text_primary: str = "#1f2937"
    text_secondary: str = "#667085"
    text_hint: str = "#64748B"
    card_bg: str = "#FFFFFF"
    card_border: str = "#E5EAF2"
    panel_bg: str = "#FFFFFF"
    preview_bg: str = "#F8FAFC"
    preview_border: str = "#DDE5EF"
    preview_selection_bg: str = "#FAD7DE"
    preview_selection_text: str = "#111827"
    table_row_hover: str = "#F8FAFC"
    title_text: str = "#1f2937"
    accent_primary: str = "#FF5A6F"
    secondary_button_bg: str = "#FFFFFF"
    secondary_button_border: str = "#D7DEE9"
    secondary_button_hover_bg: str = "#F8FAFC"
    secondary_button_hover_border: str = "#C6D0DE"
    secondary_button_pressed_bg: str = "#EEF2F7"


def _workbench_theme_module():
    if QApplication.instance() is None:
        return None
    try:
        from krok_helper import theme_workbench

        return theme_workbench
    except RuntimeError:
        return None


def palette():
    """Return the current workbench palette, or a light fallback in early imports."""
    module = _workbench_theme_module()
    if module is None:
        return _FallbackPalette()
    try:
        return module.palette()
    except RuntimeError:
        return _FallbackPalette()


def themed(widget, qss_factory: Callable[[], str]) -> None:
    """Apply a theme-aware stylesheet when possible, with a stable fallback."""
    module = _workbench_theme_module()
    if module is None:
        widget.setStyleSheet(qss_factory())
        return
    try:
        module.themed(widget, qss_factory)
    except RuntimeError:
        widget.setStyleSheet(qss_factory())
