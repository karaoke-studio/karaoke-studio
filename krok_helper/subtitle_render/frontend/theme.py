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
    text_disabled: str = "#94A3B8"
    card_bg: str = "#FFFFFF"
    card_border: str = "#E5EAF2"
    panel_bg: str = "#FFFFFF"
    input_bg: str = "#FFFFFF"
    input_border: str = "#D9DEE8"
    input_border_hover: str = "#B6C2D2"
    input_border_focus: str = "#D87886"
    input_hover_bg: str = "#FBFCFE"
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
    secondary_button_text: str = "#334155"
    progress_bg: str = "#ECEFF5"
    progress_chunk: str = "#FF5A6F"


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


def control_qss(scope: str = "") -> str:
    """Theme-aware QSS for plain Qt inputs/buttons used by this module."""
    p = palette()
    prefix = f"{scope} " if scope else ""
    return f"""
        {prefix}QLineEdit,
        {prefix}QComboBox,
        {prefix}QFontComboBox,
        {prefix}QSpinBox {{
            background: {p.input_bg};
            color: {p.text_primary};
            border: 1px solid {p.input_border};
            border-radius: 6px;
            padding: 0 8px;
            font-size: 9.5pt;
        }}
        {prefix}QLineEdit:hover,
        {prefix}QComboBox:hover,
        {prefix}QFontComboBox:hover,
        {prefix}QSpinBox:hover {{
            background: {p.input_hover_bg};
            border-color: {p.input_border_hover};
        }}
        {prefix}QLineEdit:focus,
        {prefix}QComboBox:focus,
        {prefix}QFontComboBox:focus,
        {prefix}QSpinBox:focus {{
            border-color: {p.input_border_focus};
        }}
        {prefix}QLineEdit:disabled,
        {prefix}QComboBox:disabled,
        {prefix}QFontComboBox:disabled,
        {prefix}QSpinBox:disabled {{
            color: {p.text_disabled};
            background: {p.secondary_button_pressed_bg};
            border-color: {p.card_border};
        }}
        {prefix}QPushButton {{
            background: {p.secondary_button_bg};
            color: {p.secondary_button_text};
            border: 1px solid {p.secondary_button_border};
            border-radius: 6px;
            padding: 0 12px;
            font-size: 9.5pt;
        }}
        {prefix}QPushButton:hover {{
            background: {p.secondary_button_hover_bg};
            border-color: {p.secondary_button_hover_border};
        }}
        {prefix}QPushButton:pressed {{
            background: {p.secondary_button_pressed_bg};
        }}
        {prefix}QPushButton:disabled {{
            color: {p.text_disabled};
            background: {p.secondary_button_pressed_bg};
            border-color: {p.card_border};
        }}
        {prefix}QProgressBar {{
            background: {p.progress_bg};
            color: {p.text_primary};
            border: 1px solid {p.card_border};
            border-radius: 6px;
            min-height: 12px;
            text-align: center;
        }}
        {prefix}QProgressBar::chunk {{
            background: {p.progress_chunk};
            border-radius: 5px;
        }}
    """
