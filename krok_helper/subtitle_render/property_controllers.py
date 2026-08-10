"""Qt-independent controllers behind the subtitle property-panel facade."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Callable

from krok_helper.subtitle_render.models import Style, StylePreset, SubtitleStyleScheme


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
