"""Public layout diagnostics contracts consumed by the subtitle editor UI."""

from __future__ import annotations

from dataclasses import dataclass

from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass


@dataclass(frozen=True)
class LayoutTimingDiagnostic:
    """One user-facing explanation emitted by the timing/layout solver."""

    kind: str
    line_indices: tuple[int, ...]
    title: str
    summary: str
    detail: str


@dataclass(frozen=True)
class LayoutMarginWarning:
    """One line whose main-text box violates the configured viewport margin."""

    line_index: int
    text: str
    level: str
    left: int
    right: int


__all__ = [
    "LayoutMarginWarning",
    "LayoutTimingDiagnostic",
    "layout_pass",
]
