"""Qt-independent controllers behind the subtitle property-panel facade."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Callable

from krok_helper.subtitle_render.models import (
    LYRICS_LAYOUT_FIELDS,
    LyricsLayout,
    Style,
    StylePreset,
    SubtitleStyleScheme,
    TITLE_SHOW_MODES,
    TitleOverlay,
    migrate_title_char_role_labels,
)


class RoleSchemeController:
    """Own the project role registry and ensure every role has a style scheme."""

    def __init__(self) -> None:
        self._names: list[str] = []

    @property
    def names(self) -> list[str]:
        return list(self._names)

    def replace(self, role_names: list[str]) -> None:
        """Replace roles using the panel's historical normalization semantics."""
        self._names = [str(name) for name in role_names if name]

    def merge(self, role_names: list[str]) -> None:
        """Append trimmed, non-empty roles without removing earlier entries."""
        seen = set(self._names)
        for value in role_names:
            name = str(value or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            self._names.append(name)

    def add(self, name: str) -> None:
        if name and name not in self._names:
            self._names.append(name)

    def rename(self, old: str, new: str) -> None:
        self._names = [new if name == old else name for name in self._names]

    def remove(self, name: str) -> None:
        self._names = [candidate for candidate in self._names if candidate != name]

    def ensure_style_schemes(
        self,
        style: Style,
        presets: dict[str, StylePreset],
        fallback: Callable[[int], SubtitleStyleScheme],
    ) -> tuple[Style, bool]:
        """Return a style containing one scheme for every registered role."""
        schemes = dict(style.custom_style_schemes)
        changed = False
        for index, name in enumerate(self._names):
            if name in schemes:
                continue
            matches = [preset for preset in presets.values() if preset.name == name]
            schemes[name] = (
                deepcopy(matches[0].scheme) if len(matches) == 1 else fallback(index)
            )
            changed = True
        if not changed:
            return style, False
        return replace(style, custom_style_schemes=schemes), True


class LayoutCatalogController:
    """Perform layout-catalog model edits without knowing about panel widgets."""

    @staticmethod
    def source(style: Style, index: int):
        return style if index <= 0 else style.layouts[index - 1]

    def resolved_values(self, style: Style, index: int) -> dict:
        source = self.source(style, index)
        values = {}
        for name in LYRICS_LAYOUT_FIELDS:
            value = getattr(source, name)
            if value is None:
                value = getattr(style, name)
            values[name] = deepcopy(value)
        return values

    @staticmethod
    def field_changes(style: Style, index: int, changes: dict) -> dict:
        if index <= 0:
            return dict(changes)
        layouts = list(style.layouts)
        layouts[index - 1] = replace(layouts[index - 1], **changes)
        return {"layouts": layouts}

    def add_changes(self, style: Style, source_index: int) -> tuple[dict, int]:
        values = self.resolved_values(style, source_index)
        existing = {layout.name for layout in style.layouts}
        number = len(style.layouts) + 1
        name = f"布局 {number}"
        while name in existing:
            number += 1
            name = f"布局 {number}"
        layouts = list(style.layouts) + [LyricsLayout(name=name, **values)]
        return {"layouts": layouts}, len(layouts)

    @staticmethod
    def rename_changes(style: Style, index: int, name: str) -> dict:
        layouts = list(style.layouts)
        layouts[index - 1] = replace(layouts[index - 1], name=name)
        return {"layouts": layouts}

    @staticmethod
    def delete_changes(style: Style, index: int) -> dict:
        layouts = list(style.layouts)
        del layouts[index - 1]
        changes: dict = {"layouts": layouts}
        title = style.title_overlay
        if title is not None and title.layout_index is not None:
            title_index = int(title.layout_index)
            if title_index == index:
                changes["title_overlay"] = replace(title, layout_index=0)
            elif title_index > index:
                changes["title_overlay"] = replace(
                    title,
                    layout_index=title_index - 1,
                )
        return changes


class TitleOverlayController:
    """Apply title-overlay edits while preserving per-character role labels."""

    @staticmethod
    def current(style: Style) -> TitleOverlay:
        return style.title_overlay if style.title_overlay is not None else TitleOverlay()

    def update(self, style: Style, changes: dict) -> Style:
        title = self.current(style)
        normalized = dict(changes)
        if "text_template" in normalized:
            new_text = str(normalized["text_template"])
            normalized["text_template"] = new_text
            normalized["char_role_labels"] = migrate_title_char_role_labels(
                title.text_template,
                title.char_role_labels,
                new_text,
            )
        if (
            "show_mode" in normalized
            and normalized["show_mode"] not in TITLE_SHOW_MODES
        ):
            normalized["show_mode"] = "whole"
        return replace(style, title_overlay=replace(title, **normalized))
