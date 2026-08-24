"""Project-file lifecycle transactions independent from the Qt frontend."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from krok_helper.subtitle_render.project.resources import (
    find_missing_project_resources,
)
from krok_helper.subtitle_render.project.store import (
    ProjectFileRevision,
    backup_project_file,
    inspect_project_file,
    load_render_project,
    save_render_project,
)


@dataclass(frozen=True)
class LoadedSubtitleProject:
    """One consistent on-disk project snapshot ready for frontend adoption."""

    path: Path
    data: dict
    revision: ProjectFileRevision
    missing_resources: tuple[tuple[str, Path], ...]


class SubtitleProjectController:
    """Coordinate consistent reads and backup-protected project writes."""

    @staticmethod
    def inspect(path: Path) -> ProjectFileRevision:
        """Return the current disk revision for conflict detection."""
        return inspect_project_file(Path(path))

    @staticmethod
    def open(path: Path) -> LoadedSubtitleProject:
        """Read one project only when its disk revision stays stable."""
        path = Path(path)
        revision_before = inspect_project_file(path)
        data = load_render_project(path)
        revision_after = inspect_project_file(path)
        if revision_before != revision_after:
            raise OSError("项目文件在打开期间发生了变化，请重试")
        return LoadedSubtitleProject(
            path=path,
            data=data,
            revision=revision_after,
            missing_resources=tuple(find_missing_project_resources(data)),
        )

    @staticmethod
    def save(
        path: Path,
        data: dict,
        *,
        backup_root: Path,
        backup_count: int,
    ) -> ProjectFileRevision:
        """Back up the old revision, atomically save, and inspect the result."""
        path = Path(path)
        backup_project_file(
            path,
            Path(backup_root),
            max_count=int(backup_count),
        )
        save_render_project(path, data)
        return inspect_project_file(path)
