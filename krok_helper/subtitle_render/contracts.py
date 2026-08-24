"""Public host-facing contracts for the subtitle-render module.

Keep this module free of Qt and frontend imports.  The workbench shell should
depend on these capabilities rather than on ``SubtitleRenderWindow`` details;
the concrete widget remains an implementation hidden behind the package
factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class SubtitleProjectState:
    """Immutable project status published across the host boundary."""

    display_name: str
    path: Path | None
    has_project: bool
    dirty: bool
    saving: bool
    save_error: str | None
    exporting: bool
    recovery_path: Path | None
    missing_resources: tuple[tuple[str, Path], ...] = ()

    def status_text(self) -> str | None:
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


@runtime_checkable
class SubtitleRenderSettingsProvider(Protocol):
    """Persistence boundary injected by the host application."""

    def load(self) -> dict:
        """Return the complete ``subtitle_render`` settings namespace."""
        ...

    def save(self, data: dict) -> None:
        """Persist the complete ``subtitle_render`` settings namespace."""
        ...


@runtime_checkable
class SubtitleRenderPage(Protocol):
    """Capabilities used by the workbench shell.

    Return values intentionally stay source-neutral here: the host only needs
    success/failure and never consumes the renderer's internal timing or media
    model objects.  Concrete callers inside ``subtitle_render`` retain their
    precise types.
    """

    def connect_project_state_changed(
        self, callback: Callable[[SubtitleProjectState], Any]
    ) -> None:
        """Subscribe the shell to project-state changes without exposing Qt."""
        ...

    def project_state(self) -> SubtitleProjectState:
        """Return the immutable project-state snapshot shown by the shell."""
        ...

    def has_unsaved_changes(self) -> bool:
        """Return whether closing requires save/discard coordination."""
        ...

    def trigger_save(self) -> bool:
        """Save the current project, returning whether it succeeded."""
        ...

    def discard_unsaved(self) -> None:
        """Acknowledge intentional discard and clean recovery state."""
        ...

    def is_busy(self) -> bool:
        """Return whether an export is still running."""
        ...

    def open_initial_project(self, project_path: Path | str) -> bool:
        """Open a project supplied by the shell at application startup."""
        ...

    def load_from_sug(self, path: Path) -> object | None:
        """Load a saved SUG project into the primary subtitle track."""
        ...

    def load_video(self, path: Path, info: object | None = None) -> object | None:
        """Load a video as the current background source.

        ``info`` carries a pre-probed MediaInfo so callers that probed in the
        background can skip the synchronous ffprobe here.
        """
        ...

    def load_audio(self, path: Path, info: object | None = None) -> object | None:
        """Load an independent audio source when the background permits it."""
        ...

    def load_media_async(self, path: Path, *, as_video: bool) -> None:
        """Probe media off the UI thread, then load it (workflow handoff)."""
        ...

    def flush_unsaved(self) -> None:
        """Synchronously flush crash-recovery data before forced exit."""
        ...

    def has_pending_crash_recovery(self) -> bool:
        """Return whether startup recovery requires user attention."""
        ...

    def check_crash_recovery(self, dialog_parent: Any = None) -> bool:
        """Run the existing recovery prompt flow."""
        ...
