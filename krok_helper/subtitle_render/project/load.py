"""Typed project-load planning independent from the subtitle frontend."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

from krok_helper.subtitle_render.domain.models import (
    Style,
    TitleOverlay,
    style_from_dict,
)
from krok_helper.subtitle_render.domain.timing import (
    GuideSymbol,
    SubtitleLoadingSettings,
    TimingTrack,
    guide_symbol_has_visual,
    guide_symbol_replacement_anchored,
)
from krok_helper.subtitle_render.engine.layout.page.plan import (
    build_legacy_page_plan,
    normalize_page_plan,
    project_page_plan_to_legacy_fields,
)
from krok_helper.subtitle_render.engine.timing.timeline import apply_n3_seq_line_breaks
from krok_helper.subtitle_render.project.store import split_project_paths
from krok_helper.subtitle_render.serialization.timing import (
    guide_symbol_from_dict,
    line_animation_override_from_dict,
    subtitle_loading_settings_from_dict,
    track_page_plan_from_dict,
)
from krok_helper.subtitle_render.settings.screen import (
    ScreenSettings,
    screen_settings_from_dict,
)


@dataclass(frozen=True)
class DeferredProjectAsset:
    """One immutable project asset request to be applied after the UI settles."""

    kind: str
    payload: Any


@dataclass(frozen=True)
class AppliedTrackProjectState:
    """Observable results from restoring persisted state onto one timing track."""

    char_role_labels_changed: bool
    guide_symbol_mismatches: tuple[int, ...]


def apply_track_project_data(
    track: TimingTrack,
    style: Style,
    payload: object,
) -> AppliedTrackProjectState:
    """Restore all persisted per-track fields without touching frontend state."""

    data = payload if isinstance(payload, dict) else {}
    _apply_line_breaks(track, data.get("line_breaks_before"))
    _apply_layout_indices(track, style, data.get("line_layout_indices"))
    _restore_page_state(track, style, data)
    roles_changed = _apply_char_role_labels(track, data.get("char_role_labels"))
    resolve_guide_row = _guide_symbol_row_resolver(data)
    guide_mismatches = _apply_guide_symbols(
        track,
        data.get("line_guide_symbols"),
        resolve_guide_row,
    )
    _apply_inline_guide_symbols(
        track,
        data.get("line_inline_guide_symbols"),
        resolve_guide_row,
    )
    _apply_display_overrides(track, data.get("line_display_overrides"))
    _apply_animation_overrides(track, data.get("line_animation_overrides"))
    return AppliedTrackProjectState(
        char_role_labels_changed=roles_changed,
        guide_symbol_mismatches=tuple(guide_mismatches),
    )


def _guide_symbol_row_resolver(data: dict) -> Callable[[object], object]:
    """解析 ``guide_symbol_table``：行数据里的字符串 ID 映射回符号字典。"""
    table = data.get("guide_symbol_table")
    if not isinstance(table, dict):
        return lambda value: value

    def resolve(value: object) -> object:
        if isinstance(value, str):
            return table.get(value)
        return value

    return resolve


def _apply_layout_indices(track: TimingTrack, style: Style, payload: object) -> None:
    if not isinstance(payload, list):
        return
    limit = len(style.layouts)
    for line, value in zip(track.lines, payload):
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        line.layout_index = index if 0 <= index <= limit else 0


def _apply_line_breaks(track: TimingTrack, payload: object) -> None:
    if not isinstance(payload, list):
        return
    for line, value in zip(track.lines, payload):
        kind = str(value)
        line.break_before = kind if kind in {"page", "paragraph"} else "none"


def _restore_page_state(track: TimingTrack, style: Style, data: dict) -> None:
    restored = track_page_plan_from_dict(data.get("page_plan"))
    if restored is None:
        saved_breaks = data.get("line_breaks_before")
        has_complete_legacy_breaks = (
            isinstance(saved_breaks, list) and len(saved_breaks) >= len(track.lines)
        )
        if not has_complete_legacy_breaks:
            # Schema-v1 files omitted explicit boundaries. The historical LRC
            # loader supplied N3's two-line boundaries, so replay that rule
            # before constructing the modern page plan.
            apply_n3_seq_line_breaks(track)
        track.page_plan = build_legacy_page_plan(
            track,
            style,
            section_gap_ms=max(int(style.section_gap_ms), 0),
        )
        track.loading_settings_mode = "custom"
        track.loading_settings = SubtitleLoadingSettings(
            time_gap_section_enabled=True,
            section_gap_ms=max(int(style.section_gap_ms), 0),
            blank_line_section_enabled=False,
            rows_per_page=2,
        )
        track.loading_settings_snapshot = track.loading_settings
    else:
        track.page_plan = normalize_page_plan(track, style, restored)
        mode = str(data.get("loading_settings_mode") or "global")
        track.loading_settings_mode = (
            mode if mode in {"global", "custom"} else "global"
        )
        track.loading_settings = (
            subtitle_loading_settings_from_dict(data.get("loading_settings"))
            if track.loading_settings_mode == "custom"
            else None
        )
        track.loading_settings_snapshot = subtitle_loading_settings_from_dict(
            data.get("loading_settings_snapshot")
        )
    project_page_plan_to_legacy_fields(track, style)


def _apply_char_role_labels(track: TimingTrack, payload: object) -> bool:
    if not isinstance(payload, list):
        return False
    changed = False
    for line, labels in zip(track.lines, payload):
        if not isinstance(labels, list):
            continue
        for char, label in zip(line.chars, labels):
            new_label = str(label) if label else None
            if char.role_label != new_label:
                char.role_label = new_label
                changed = True
    return changed


def _apply_guide_symbols(
    track: TimingTrack,
    payload: object,
    resolve_row: Callable[[object], object] = lambda value: value,
) -> list[int]:
    if not isinstance(payload, list):
        return []
    mismatches: list[int] = []
    for row, (line, value) in enumerate(zip(track.lines, payload)):
        symbol = guide_symbol_from_dict(resolve_row(value))
        if (
            symbol is not None
            and symbol.replacement_prefix
            and not guide_symbol_replacement_anchored(line, symbol)
        ):
            # 源文件在保存后被改过（换行重排）时行号即错位；行首前缀 + 可见
            # 文字锚点都对得上才回放，否则丢弃并上报，避免静默替换错歌词。
            line.guide_symbol = None
            mismatches.append(row)
            continue
        line.guide_symbol = symbol
    return mismatches


def _apply_inline_guide_symbols(
    track: TimingTrack,
    payload: object,
    resolve_row: Callable[[object], object] = lambda value: value,
) -> None:
    if not isinstance(payload, list):
        return
    for line, value in zip(track.lines, payload):
        symbols: dict[int, GuideSymbol] = {}
        if isinstance(value, dict):
            for raw_index, raw_symbol in value.items():
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    continue
                symbol = guide_symbol_from_dict(resolve_row(raw_symbol))
                if 0 <= index < len(line.chars) and guide_symbol_has_visual(symbol):
                    symbols[index] = symbol
        line.inline_guide_symbols = symbols


def _apply_display_overrides(track: TimingTrack, payload: object) -> None:
    if not isinstance(payload, list):
        return
    for line, row in zip(track.lines, payload):
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        start, end = row
        line.display_start_override_ms = (
            int(start) if isinstance(start, (int, float)) else None
        )
        line.display_end_override_ms = (
            int(end) if isinstance(end, (int, float)) else None
        )


def _schema_version(value: object) -> int:
    """Best-effort schema stamp; missing/non-numeric counts as legacy ``0``."""
    try:
        return max(int(value), 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _apply_animation_overrides(track: TimingTrack, payload: object) -> None:
    if not isinstance(payload, list):
        return
    for line, row in zip(track.lines, payload):
        line.animation_override = line_animation_override_from_dict(row)


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
    schema_version: int = 0
    line_breaks_before: Any = None
    line_layout_indices: Any = None
    char_role_labels: Any = None
    line_guide_symbols: Any = None
    line_inline_guide_symbols: Any = None
    line_display_overrides: Any = None
    line_animation_overrides: Any = None
    extra_subtitle_sources: Any = None
    project_role_names: Any = None

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
            schema_version=_schema_version(source.get("schema_version")),
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
