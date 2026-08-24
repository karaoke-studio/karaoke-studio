"""Typed project-load planning independent from the subtitle frontend."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from krok_helper.subtitle_render.models import Style, TitleOverlay, style_from_dict
from krok_helper.subtitle_render.project_store import split_project_paths
from krok_helper.subtitle_render.screen_settings import (
    ScreenSettings,
    screen_settings_from_dict,
)


@dataclass(frozen=True)
class DeferredProjectAsset:
    """One immutable project asset request to be applied after the UI settles."""

    kind: str
    payload: Any


@dataclass(frozen=True)
class ProjectLoadPlan:
    """Parsed ``.yurika`` state with compatibility defaults resolved once."""

    source_data: dict
    style: Style
    screen: ScreenSettings
    selected_scheme_key: Optional[str]
    output: dict
    subtitle_path: Optional[Path]
    fallback_video_path: Optional[Path]
    audio_path: Optional[Path]
    background: Optional[dict]
    line_breaks_before: Any
    line_layout_indices: Any
    char_role_labels: Any
    line_guide_symbols: Any
    line_inline_guide_symbols: Any
    line_display_overrides: Any
    line_animation_overrides: Any
    extra_subtitle_sources: Any
    project_role_names: Any

    @classmethod
    def from_data(cls, data: dict) -> "ProjectLoadPlan":
        """Parse one project payload without loading files or touching widgets."""
        source = data if isinstance(data, dict) else {}
        style_payload = source.get("style")
        style = style_from_dict(style_payload)
        screen = screen_settings_from_dict(source.get("screen"))
        # Older projects stored resolved pixel sizes without a reference height.
        # Bind those values to the saved canvas before any later screen resize.
        if (
            not isinstance(style_payload, dict)
            or "font_reference_height" not in style_payload
        ):
            style = replace(
                style,
                font_reference_height=max(int(screen.height), 1),
            )
        if style.title_overlay is None:
            style = replace(style, title_overlay=TitleOverlay())

        key = source.get("selected_scheme_key")
        selected_scheme_key = key if isinstance(key, str) and key else None
        output = source.get("output")
        paths = split_project_paths(source)
        background = source.get("background")
        return cls(
            source_data=source,
            style=style,
            screen=screen,
            selected_scheme_key=selected_scheme_key,
            output=output if isinstance(output, dict) else {},
            subtitle_path=paths["subtitle_path"],
            fallback_video_path=paths["video_path"],
            audio_path=paths["audio_path"],
            background=background if isinstance(background, dict) else None,
            line_breaks_before=source.get("line_breaks_before"),
            line_layout_indices=source.get("line_layout_indices"),
            char_role_labels=source.get("char_role_labels"),
            line_guide_symbols=source.get("line_guide_symbols"),
            line_inline_guide_symbols=source.get("line_inline_guide_symbols"),
            line_display_overrides=source.get("line_display_overrides"),
            line_animation_overrides=source.get("line_animation_overrides"),
            extra_subtitle_sources=source.get("extra_subtitle_sources"),
            project_role_names=source.get("project_role_names"),
        )

    def deferred_assets(self) -> tuple[DeferredProjectAsset, ...]:
        """Build the existing ordered background/audio/secondary-source queue."""
        loads: list[DeferredProjectAsset] = []
        if self.background is not None:
            loads.append(DeferredProjectAsset("background", deepcopy(self.background)))
        elif (
            self.fallback_video_path is not None
            and self.fallback_video_path.is_file()
        ):
            loads.append(DeferredProjectAsset("video", self.fallback_video_path))
        if self.audio_path is not None:
            loads.append(DeferredProjectAsset("audio", self.audio_path))
        if (
            isinstance(self.extra_subtitle_sources, list)
            and self.extra_subtitle_sources
        ):
            loads.append(
                DeferredProjectAsset(
                    "extra_subtitle_sources",
                    (
                        deepcopy(self.extra_subtitle_sources),
                        deepcopy(self.project_role_names),
                    ),
                )
            )
        return tuple(loads)
