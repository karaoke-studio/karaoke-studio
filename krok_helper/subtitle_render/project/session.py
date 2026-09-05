"""Project-session state independent from the Qt subtitle frontend."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from krok_helper.models import MediaInfo
from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.contracts import SubtitleProjectState
from krok_helper.subtitle_render.serialization.compat import merge_extensible_value
from krok_helper.subtitle_render.domain.timing import (
    GuideSymbol,
    TimingTrack,
    guide_symbol_has_visual,
)
from krok_helper.subtitle_render.domain.models import (
    Style,
    style_to_dict,
)
from krok_helper.subtitle_render.project.store import (
    background_payload,
    project_payload,
    split_project_paths,
)
from krok_helper.subtitle_render.serialization.timing import (
    guide_symbol_to_dict,
    line_animation_override_to_dict,
    subtitle_loading_settings_to_dict,
    track_page_plan_to_dict,
)


_PROJECT_OWNED_KEYS = frozenset(
    {
        "schema_version",
        "subtitle_path",
        "subtitle_sug_axis_singer_ids",
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
        "guide_symbol_table",
        "line_display_overrides",
        "line_animation_overrides",
        "line_wipe_reverse_overrides",
        "page_plan",
        "loading_settings_mode",
        "loading_settings",
        "loading_settings_snapshot",
        "extra_subtitle_sources",
        "project_role_names",
    }
)
_PROJECT_EXTENSIBLE_FIELDS = frozenset(
    {
        "style",
        "screen",
        "output",
        "background",
        "page_plan",
        "loading_settings",
        "loading_settings_snapshot",
        "line_guide_symbols",
        "line_inline_guide_symbols",
        "line_animation_overrides",
        "extra_subtitle_sources",
    }
)


@dataclass
class ExtraSubtitleSource:
    """One secondary lyrics source, such as an N3 chorus track."""

    name: str
    path: Path
    track: TimingTrack
    sug_axis_singer_ids: Optional[frozenset[str]] = None
    """SUG 分色分轴：该副源对应的分组歌手集合；``None`` = 普通整份源。"""

    source_baseline: Optional[TimingTrack] = None
    """该源上一次接受的解析基线（供分轴重载合并本地编辑；不持久化）。"""


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
    subtitle_axis_singer_ids: Optional[frozenset[str]] = None
    """主字幕槽位的 SUG 轴过滤（主分组的歌手集合）；``None`` = 未分轴。"""
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
        self.subtitle_axis_singer_ids = None
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
                **(
                    {
                        "sug_axis_singer_ids": sorted(source.sug_axis_singer_ids)
                    }
                    if source.sug_axis_singer_ids is not None
                    else {}
                ),
                **_track_project_data(source.track),
            }
            for source in self.extra_sources
        ] or None
        background = self.background_source
        payload = project_payload(
            subtitle_path=self.subtitle_path,
            subtitle_sug_axis_singer_ids=(
                sorted(self.subtitle_axis_singer_ids)
                if self.subtitle_axis_singer_ids is not None
                else None
            ),
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
                    image_fit=background.image_fit,
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
    for key in _PROJECT_EXTENSIBLE_FIELDS:
        current_value = current.get(key)
        source_value = source.get(key)
        if not isinstance(current_value, (dict, list)) or not isinstance(
            source_value, type(current_value)
        ):
            continue
        current[key] = merge_extensible_value(
            source_value,
            current_value,
            path=(key,),
        )
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
            "line_wipe_reverse_overrides": None,
            "page_plan": None,
            "loading_settings_mode": None,
            "loading_settings": None,
            "loading_settings_snapshot": None,
        }
    glyph_table, table_payload = _guide_symbol_dedup_table(track)
    data = {
        "line_layout_indices": [
            int(getattr(line, "layout_index", 0) or 0) for line in track.lines
        ],
        "line_breaks_before": [
            str(getattr(line, "break_before", "none")) for line in track.lines
        ],
        "char_role_labels": _char_role_rows(track),
        "line_guide_symbols": _guide_symbol_rows(track, glyph_table),
        "line_inline_guide_symbols": _inline_guide_symbol_rows(track, glyph_table),
        "line_display_overrides": _display_override_rows(track),
        "line_animation_overrides": _animation_override_rows(track),
        "line_wipe_reverse_overrides": _wipe_reverse_override_rows(track),
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
    if table_payload:
        data["guide_symbol_table"] = table_payload
    return data


def _char_role_rows(track: TimingTrack) -> Optional[list]:
    rows = [
        [char.role_label for char in line.chars]
        if any(char.role_label for char in line.chars)
        else None
        for line in track.lines
    ]
    return rows if any(row is not None for row in rows) else None


def _guide_symbol_dedup_table(
    track: TimingTrack,
) -> tuple[dict[GuideSymbol, str], dict[str, dict]]:
    """收集被 ≥2 个槽位引用的导唱符；它们只序列化一次，行数据存 ID。

    精细 SVG 的完整轮廓可达上万条命令，而典型用法是同一符号应用到几十上百
    行——按行内嵌会让 ``.yurika`` 与保存/加载一起膨胀。仅被引用一次的符号
    保持内嵌，旧版本（以及旧程序）读到的仍是完整行数据。
    """
    counts: dict[GuideSymbol, int] = {}
    for line in track.lines:
        if line.guide_symbol is not None:
            counts[line.guide_symbol] = counts.get(line.guide_symbol, 0) + 1
        for index, symbol in line.inline_guide_symbols.items():
            if 0 <= index < len(line.chars) and guide_symbol_has_visual(symbol):
                counts[symbol] = counts.get(symbol, 0) + 1
    table: dict[GuideSymbol, str] = {}
    payload: dict[str, dict] = {}
    for symbol, count in counts.items():
        if count >= 2:
            glyph_id = f"g{len(payload)}"
            table[symbol] = glyph_id
            payload[glyph_id] = guide_symbol_to_dict(symbol)
    return table, payload


def _guide_symbol_row(
    symbol: Optional[GuideSymbol], table: dict[GuideSymbol, str]
) -> object:
    glyph_id = table.get(symbol) if symbol is not None else None
    if glyph_id is not None:
        return glyph_id
    return guide_symbol_to_dict(symbol)


def _guide_symbol_rows(
    track: TimingTrack, table: dict[GuideSymbol, str]
) -> Optional[list]:
    rows = [_guide_symbol_row(line.guide_symbol, table) for line in track.lines]
    return rows if any(row is not None for row in rows) else None


def _inline_guide_symbol_rows(
    track: TimingTrack, table: dict[GuideSymbol, str]
) -> Optional[list]:
    rows = [
        {
            str(index): _guide_symbol_row(symbol, table)
            for index, symbol in sorted(line.inline_guide_symbols.items())
            if 0 <= index < len(line.chars) and guide_symbol_has_visual(symbol)
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


def _wipe_reverse_override_rows(track: TimingTrack) -> Optional[list]:
    rows = [line.wipe_reverse_override for line in track.lines]
    return rows if any(row is not None for row in rows) else None


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

    def record_save_inspection_failure(self, error: str) -> None:
        """Publish a pre-save disk inspection failure without starting a save."""
        self.saving = False
        self.save_error = str(error)

    def begin_save(self) -> int:
        """Enter saving state and return the content revision being persisted."""
        revision_at_save = int(self.revision)
        self.saving = True
        self.save_error = None
        return revision_at_save

    def fail_save(self, error: str) -> None:
        """Leave saving state while retaining dirty content and its failure."""
        self.saving = False
        self.save_error = str(error)

    def complete_save(
        self,
        *,
        path: Path,
        disk_revision: Any,
        saved_revision: int,
    ) -> None:
        """Adopt the successful on-disk identity without hiding dirty side effects."""
        self.path = Path(path)
        self.disk_revision = disk_revision
        self.saved_revision = int(saved_revision)
        self.saving = False
        self.save_error = None

    def begin_generation(self) -> None:
        """Invalidate state tied to the previously loaded project."""
        self.generation += 1
        self.revision = 0
        self.saved_revision = 0
        self.disk_revision = None
        self.missing_resources = ()
        self.unresolved_resource_labels = set()
        self.missing_resource_source_data = None

    def adopt_project_identity(
        self,
        *,
        path: Optional[Path],
        disk_revision: Any,
        missing_resources: tuple[tuple[str, Path], ...]
        | list[tuple[str, Path]] = (),
        source_data: Optional[dict] = None,
    ) -> None:
        """Adopt one loaded/imported/recovered project identity atomically."""
        self.path = Path(path) if path is not None else None
        self.disk_revision = disk_revision
        self.remember_missing_resources(
            missing_resources,
            source_data if isinstance(source_data, dict) else {},
        )

    def remember_missing_resources(
        self,
        missing: tuple[tuple[str, Path], ...] | list[tuple[str, Path]],
        source_data: dict,
    ) -> None:
        """Record unavailable paths without adopting them into loaded document state."""
        self.missing_resources = tuple(missing)
        self.unresolved_resource_labels = {label for label, _path in missing}
        self.missing_resource_source_data = (
            deepcopy(source_data) if self.missing_resources else None
        )

    def resolve_missing_resource_labels(self, labels: set[str]) -> bool:
        """Forget unavailable references explicitly replaced by the user."""
        if not labels:
            return False
        before = set(self.unresolved_resource_labels)
        self.unresolved_resource_labels = before - set(labels)
        if self.missing_resources:
            self.missing_resources = tuple(
                item for item in self.missing_resources if item[0] not in labels
            )
        if not self.unresolved_resource_labels:
            self.missing_resource_source_data = None
        return before != self.unresolved_resource_labels

    def merge_unresolved_resource_references(self, payload: dict) -> dict:
        """Keep skipped missing paths in project data until explicitly replaced."""
        source = self.missing_resource_source_data
        labels = self.unresolved_resource_labels
        if not isinstance(source, dict) or not labels:
            return payload
        merged = deepcopy(payload)
        source_paths = split_project_paths(source)
        if "主字幕" in labels and not merged.get("subtitle_path"):
            path = source_paths["subtitle_path"]
            merged["subtitle_path"] = str(path) if path is not None else None
        background_labels = {"背景视频", "背景图片", "背景图片序列"}
        if labels & background_labels and not merged.get("background"):
            source_background = source.get("background")
            if isinstance(source_background, dict):
                merged["background"] = deepcopy(source_background)
            elif source_paths["video_path"] is not None:
                merged["video_path"] = str(source_paths["video_path"])
        if "独立音频" in labels and not merged.get("audio_path"):
            path = source_paths["audio_path"]
            merged["audio_path"] = str(path) if path is not None else None
        source_extras = source.get("extra_subtitle_sources")
        if isinstance(source_extras, list):
            current_extras = (
                list(merged.get("extra_subtitle_sources"))
                if isinstance(merged.get("extra_subtitle_sources"), list)
                else []
            )
            current_paths = {
                str(item.get("path") or "")
                for item in current_extras
                if isinstance(item, dict)
            }
            for index, item in enumerate(source_extras, start=1):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip() or str(index)
                label = f"副字幕「{name}」"
                path_text = str(item.get("path") or "").strip()
                if label in labels and path_text and path_text not in current_paths:
                    current_extras.append(deepcopy(item))
                    current_paths.add(path_text)
            if current_extras:
                merged["extra_subtitle_sources"] = current_extras
        return merged

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
