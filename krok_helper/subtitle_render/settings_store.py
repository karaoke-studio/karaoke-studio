"""Persistence adapter for the subtitle-render settings namespace."""

from __future__ import annotations

from krok_helper.settings import load_app_settings, save_app_settings
from krok_helper.subtitle_render.contracts import SubtitleRenderSettingsProvider


class SubtitleRenderSettingsStore:
    """Read and replace the module namespace through one stable boundary."""

    def __init__(
        self,
        provider: SubtitleRenderSettingsProvider | None = None,
    ) -> None:
        self._provider = provider

    def load(self) -> dict:
        if self._provider is not None and hasattr(self._provider, "load"):
            loaded = self._provider.load()
        else:
            loaded = load_app_settings().subtitle_render
        return dict(loaded) if isinstance(loaded, dict) else {}

    def save(self, data: dict) -> None:
        if self._provider is not None and hasattr(self._provider, "save"):
            self._provider.save(data)
            return
        settings = load_app_settings()
        settings.subtitle_render = data
        save_app_settings(settings, merge_module_namespaces=False)
