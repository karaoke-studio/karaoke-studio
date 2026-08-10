"""Project-session state independent from the Qt subtitle frontend."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from krok_helper.models import MediaInfo
from krok_helper.subtitle_render.models import BackgroundSource, Style, TimingTrack


@dataclass
class ExtraSubtitleSource:
    """One secondary lyrics source, such as an N3 chorus track."""

    name: str
    path: Path
    track: TimingTrack


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
