"""Application-wide recent colors, saved through the preset-library store."""

from __future__ import annotations

import re

from .store import SubtitleRenderSettingsStore


RECENT_COLOR_LIMIT = 20
_SETTINGS_KEY = "recent_colors"


def _normalize_color(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    if not re.fullmatch(r"#[0-9A-F]{6}(?:[0-9A-F]{2})?", value):
        return None
    return "#FF" + value[1:] if len(value) == 7 else value


def _normalize_history(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    colors: list[str] = []
    seen: set[str] = set()
    for row in value:
        color = _normalize_color(row)
        if color is None or color in seen:
            continue
        seen.add(color)
        colors.append(color)
        if len(colors) == RECENT_COLOR_LIMIT:
            break
    return colors


class RecentColorStore:
    """Apply each addition/deletion to fresh settings, never a UI snapshot.

    Like the preset library, keep unrelated and future fields in the loaded
    payload and use SubtitleRenderSettingsStore for the final atomic write.
    Stale dialogs can therefore delete their chosen RGBA without restoring
    entries another dialog already removed or losing entries it added.
    """

    def __init__(self, settings: SubtitleRenderSettingsStore | None = None) -> None:
        self._settings = settings if settings is not None else SubtitleRenderSettingsStore()

    def load(self) -> list[str]:
        return _normalize_history(self._settings.load().get(_SETTINGS_KEY))

    def remember(self, value: str) -> None:
        color = _normalize_color(value)
        if color is None:
            return
        data = self._settings.load()
        previous = data.get(_SETTINGS_KEY)
        colors = [color] + [row for row in _normalize_history(previous) if row != color]
        colors = colors[:RECENT_COLOR_LIMIT]
        if colors != previous:
            data[_SETTINGS_KEY] = colors
            self._settings.save(data)

    def remove(self, value: str) -> None:
        color = _normalize_color(value)
        if color is None:
            return
        data = self._settings.load()
        colors = _normalize_history(data.get(_SETTINGS_KEY))
        if color not in colors:
            return
        data[_SETTINGS_KEY] = [row for row in colors if row != color]
        self._settings.save(data)

    def clear(self) -> None:
        data = self._settings.load()
        if data.get(_SETTINGS_KEY, []) != []:
            data[_SETTINGS_KEY] = []
            self._settings.save(data)
