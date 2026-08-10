"""Project-session state independent from the Qt subtitle frontend."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from krok_helper.models import MediaInfo
from krok_helper.subtitle_render.models import (
    BackgroundSource,
    Style,
    TimingTrack,
    guide_symbol_to_dict,
    line_animation_override_to_dict,
    style_to_dict,
    subtitle_loading_settings_to_dict,
    track_page_plan_to_dict,
)
from krok_helper.subtitle_render.project_store import background_payload, project_payload


_PROJECT_OWNED_KEYS = frozenset(
    {
        "schema_version",
        "subtitle_path",
        "video_path",
        "audio_path",
        "style",
        "screen",
        "selected_scheme_key",
        "output",
        "background",
        "line_layout_indices",
        "line_breaks_before",
        "char_role_labels",
        "line_guide_symbols",
        "line_inline_guide_symbols",
        "line_display_overrides",
        "line_animation_overrides",
        "page_plan",
        "loading_settings_mode",
        "loading_settings",
        "loading_settings_snapshot",
        "extra_subtitle_sources",
        "project_role_names",
    }
)
_PROJECT_EXTENSIBLE_MAPPINGS = ("style", "screen", "output", "background")


@dataclass
class ExtraSubtitleSource:
    """One secondary lyrics source, such as an N3 chorus track."""

    name: str
    path: Path
    track: TimingTrack


@dataclass(frozen=True)
class SubtitleTrackMutation:
    """One document-owned track mutation with stable undo snapshots."""

    track_index: int
    before: TimingTrack
    after: TimingTrack
    result: Any = None

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass(frozen=True)
class SubtitleTracksMutation:
    """One atomic mutation spanning multiple document-owned tracks."""

    track_indices: tuple[int, ...]
    before: tuple[TimingTrack, ...]
    after: tuple[TimingTrack, ...]
    result: Any = None

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass
class SubtitleProjectDocument:
    """Mutable project content shared by UI, preview, and export adapters."""

    timing_track: Optional[TimingTrack] = None
    extra_sources: list[ExtraSubtitleSource] = field(default_factory=list)
    subtitle_path: Optional[Path] = None
    video_path: Optional[Path] = None
    video_info: Optional[MediaInfo] = None
    background_source: Optional[BackgroundSource] = None
    audio_path: Optional[Path] = None
    audio_info: Optional[MediaInfo] = None
    style: Style = field(default_factory=Style)
    role_names: list[str] = field(default_factory=list)
    preserved_project_data: dict[str, Any] = field(default_factory=dict, repr=False)

    def tracks(self) -> list[TimingTrack]:
        """Return primary and secondary timing tracks in their UI source order."""
        tracks = [] if self.timing_track is None else [self.timing_track]
        tracks.extend(source.track for source in self.extra_sources)
        return tracks

    def track_at(self, index: int) -> Optional[TimingTrack]:
        """Return one timing track by UI source index (primary is index zero)."""
        if index == 0:
            return self.timing_track
        if 1 <= index <= len(self.extra_sources):
            return self.extra_sources[index - 1].track
        return None

    def replace_track(self, index: int, track: TimingTrack) -> bool:
        """Replace one timing track while preserving its source identity."""
        if index == 0:
            self.timing_track = track
            return True
        if 1 <= index <= len(self.extra_sources):
            self.extra_sources[index - 1].track = track
            return True
        return False

    def mutate_track(
        self,
        index: int,
        operation: Callable[[TimingTrack], Any],
    ) -> Optional[SubtitleTrackMutation]:
        """Run one in-place track operation and capture undo-safe snapshots."""
        track = self.track_at(index)
        if track is None:
            return None
        before = deepcopy(track)
        result = operation(track)
        return SubtitleTrackMutation(
            track_index=int(index),
            before=before,
            after=deepcopy(track),
            result=result,
        )

    def mutate_tracks(
        self,
        indices: tuple[int, ...],
        operation: Callable[[tuple[TimingTrack, ...]], Any],
    ) -> Optional[SubtitleTracksMutation]:
        """Run one atomic multi-track operation with stable undo snapshots."""
        tracks: list[TimingTrack] = []
        for index in indices:
            track = self.track_at(index)
            if track is None:
                return None
            tracks.append(track)
        selected = tuple(tracks)
        before = tuple(deepcopy(track) for track in selected)
        result = operation(selected)
        return SubtitleTracksMutation(
            track_indices=tuple(int(index) for index in indices),
            before=before,
            after=tuple(deepcopy(track) for track in selected),
            result=result,
        )

    def clear_loaded_media(self) -> None:
        """Clear source material while preserving current project style."""
        self.timing_track = None
        self.extra_sources = []
        self.subtitle_path = None
        self.video_path = None
        self.video_info = None
        self.background_source = None
        self.audio_path = None
        self.audio_info = None
        self.role_names = []
        self.preserved_project_data = {}

    def remember_project_data(self, data: dict) -> None:
        """Keep forward-compatible fields that this version does not own."""

        self.preserved_project_data = deepcopy(data) if isinstance(data, dict) else {}

    def to_project_data(
        self,
        *,
        screen: dict,
        selected_scheme_key: str,
        output: dict,
    ) -> dict:
        """Serialize project-owned content with UI settings supplied as plain data."""
        independent_audio = (
            self.audio_path
            if self.audio_path is not None and self.audio_path != self.video_path
            else None
        )
        track_data = _track_project_data(self.timing_track)
        extra_subtitle_sources = [
            {
                "name": source.name,
                "path": str(source.path),
                **_track_project_data(source.track),
            }
            for source in self.extra_sources
        ] or None
        background = self.background_source
        payload = project_payload(
            subtitle_path=self.subtitle_path,
            video_path=self.video_path,
            audio_path=independent_audio,
            background=(
                background_payload(
                    kind=background.kind,
                    path=Path(background.path) if background.path else None,
                    color=background.color,
                    source_fps=background.source_fps,
                    sequence_start_number=background.sequence_start_number,
                    video_offset_ms=background.video_offset_ms,
                )
                if background is not None
                else None
            ),
            style=style_to_dict(self.style),
            screen=dict(screen),
            selected_scheme_key=selected_scheme_key,
            extra_subtitle_sources=extra_subtitle_sources,
            project_role_names=list(self.role_names),
            output=dict(output),
            **track_data,
        )
        return _merge_preserved_project_data(self.preserved_project_data, payload)


def _merge_preserved_project_data(source: dict, current: dict) -> dict:
    """Overlay current owned values while retaining unknown future fields."""

    merged = {
        key: deepcopy(value)
        for key, value in source.items()
        if key not in _PROJECT_OWNED_KEYS
    }
    for key in _PROJECT_EXTENSIBLE_MAPPINGS:
        current_value = current.get(key)
        source_value = source.get(key)
        if not isinstance(current_value, dict) or not isinstance(source_value, dict):
            continue
        nested = deepcopy(source_value)
        nested.update(current_value)
        current[key] = nested
    merged.update(current)
    return merged


def _track_project_data(track: Optional[TimingTrack]) -> dict:
    """Return the stable ``.yurika`` projection of one timing track."""
    if track is None:
        return {
            "line_layout_indices": None,
            "line_breaks_before": None,
            "char_role_labels": None,
            "line_guide_symbols": None,
            "line_inline_guide_symbols": None,
            "line_display_overrides": None,
            "line_animation_overrides": None,
            "page_plan": None,
            "loading_settings_mode": None,
            "loading_settings": None,
            "loading_settings_snapshot": None,
        }
    return {
        "line_layout_indices": [
            int(getattr(line, "layout_index", 0) or 0) for line in track.lines
        ],
        "line_breaks_before": [
            str(getattr(line, "break_before", "none")) for line in track.lines
        ],
        "char_role_labels": _char_role_rows(track),
        "line_guide_symbols": _guide_symbol_rows(track),
        "line_inline_guide_symbols": _inline_guide_symbol_rows(track),
        "line_display_overrides": _display_override_rows(track),
        "line_animation_overrides": _animation_override_rows(track),
        "page_plan": track_page_plan_to_dict(track.page_plan),
        "loading_settings_mode": track.loading_settings_mode,
        "loading_settings": (
            subtitle_loading_settings_to_dict(track.loading_settings)
            if track.loading_settings is not None
            else None
        ),
        "loading_settings_snapshot": subtitle_loading_settings_to_dict(
            track.loading_settings_snapshot
        ),
    }


def _char_role_rows(track: TimingTrack) -> Optional[list]:
    rows = [
        [char.role_label for char in line.chars]
        if any(char.role_label for char in line.chars)
        else None
        for line in track.lines
    ]
    return rows if any(row is not None for row in rows) else None


def _guide_symbol_rows(track: TimingTrack) -> Optional[list]:
    rows = [guide_symbol_to_dict(line.guide_symbol) for line in track.lines]
    return rows if any(row is not None for row in rows) else None


def _inline_guide_symbol_rows(track: TimingTrack) -> Optional[list]:
    rows = [
        {
            str(index): guide_symbol_to_dict(symbol)
            for index, symbol in sorted(line.inline_guide_symbols.items())
            if 0 <= index < len(line.chars) and symbol.path_commands
        }
        or None
        for line in track.lines
    ]
    return rows if any(row is not None for row in rows) else None


def _display_override_rows(track: TimingTrack) -> Optional[list]:
    rows = [
        [line.display_start_override_ms, line.display_end_override_ms]
        if line.display_start_override_ms is not None
        or line.display_end_override_ms is not None
        else None
        for line in track.lines
    ]
    return rows if any(row is not None for row in rows) else None


def _animation_override_rows(track: TimingTrack) -> Optional[list]:
    rows = [
        line_animation_override_to_dict(line.animation_override) for line in track.lines
    ]
    return rows if any(row is not None for row in rows) else None


@dataclass(frozen=True)
class SubtitleProjectState:
    """Immutable project status consumed by the workbench shell."""

    display_name: str
    path: Optional[Path]
    has_project: bool
    dirty: bool
    saving: bool
    save_error: Optional[str]
    exporting: bool
    recovery_path: Optional[Path]
    missing_resources: tuple[tuple[str, Path], ...] = ()

    def status_text(self) -> Optional[str]:
        if not self.has_project:
            return None
        states: list[str] = []
        if self.saving:
            states.append("正在保存")
        elif self.save_error:
            states.append("保存失败")
        elif self.dirty:
            states.append("未保存")
        if self.exporting:
            states.append("导出中")
        if self.missing_resources:
            states.append(f"素材缺失 {len(self.missing_resources)} 项")
        return f"{self.display_name} · {' · '.join(states)}" if states else self.display_name


@dataclass
class SubtitleProjectSession:
    """Single owner for mutable project lifecycle and recovery identity."""

    path: Optional[Path] = None
    dirty: bool = False
    saving: bool = False
    save_error: Optional[str] = None
    generation: int = 0
    revision: int = 0
    saved_revision: int = 0
    disk_revision: Any = None
    missing_resources: tuple[tuple[str, Path], ...] = ()
    unresolved_resource_labels: set[str] = field(default_factory=set)
    missing_resource_source_data: Optional[dict] = None

    def set_dirty(self, dirty: bool) -> bool:
        """Set dirty state and return its previous value."""
        was_dirty = self.dirty
        self.dirty = bool(dirty)
        if dirty and not was_dirty:
            self.revision += 1
        if dirty or not self.saving:
            self.save_error = None
        return was_dirty

    def mark_dirty(self) -> tuple[bool, bool]:
        """Record a mutation and return ``(was_dirty, had_save_error)``."""
        was_dirty = self.dirty
        had_save_error = self.save_error is not None
        self.revision += 1
        self.dirty = True
        self.save_error = None
        return was_dirty, had_save_error

    def begin_generation(self) -> None:
        """Invalidate state tied to the previously loaded project."""
        self.generation += 1
        self.revision = 0
        self.saved_revision = 0
        self.disk_revision = None
        self.missing_resources = ()
        self.unresolved_resource_labels = set()
        self.missing_resource_source_data = None

    def snapshot(
        self,
        *,
        has_project: bool,
        exporting: bool,
        recovery_path: Optional[Path],
    ) -> SubtitleProjectState:
        """Build the immutable state published to the host shell."""
        path = self.path
        return SubtitleProjectState(
            display_name=path.name if path is not None else "未命名项目",
            path=path,
            has_project=bool(has_project),
            dirty=bool(self.dirty),
            saving=bool(self.saving),
            save_error=self.save_error,
            exporting=bool(exporting),
            recovery_path=recovery_path,
            missing_resources=self.missing_resources,
        )
