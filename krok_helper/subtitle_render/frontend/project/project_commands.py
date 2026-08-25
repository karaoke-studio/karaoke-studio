"""User-command orchestration for native subtitle projects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


ChoicePrompt = Callable[..., int]
FilePicker = Callable[..., tuple[str, str]]
Command = Callable[[], Any]


class ProjectSaveAction(str, Enum):
    """Result of checking whether an existing project can be overwritten."""

    CONTINUE = "continue"
    SAVE_AS = "save_as"
    CANCEL = "cancel"
    INSPECTION_FAILED = "inspection_failed"


@dataclass(frozen=True)
class ProjectSavePreflight:
    """Explicit save decision returned to the window composition root."""

    action: ProjectSaveAction
    error: str = ""


@dataclass(frozen=True)
class ProjectCommandController:
    """Own project command prompts without knowing window or document internals."""

    project_filter: str
    project_suffix: str

    @staticmethod
    def confirm_discard(
        parent: Any,
        *,
        dirty: bool,
        choose: ChoicePrompt,
        save: Callable[[], bool],
        discard: Command,
    ) -> bool:
        """Resolve the existing save/discard/cancel flow before replacement."""
        if not dirty:
            return True
        choice = choose(
            parent,
            "未保存的改动",
            "当前项目有未保存的改动，是否先保存？",
            ["保存", "放弃", "取消"],
            default=2,
        )
        if choice not in (0, 1):
            return False
        if choice == 0:
            return bool(save())
        discard()
        return True

    def choose_open_path(
        self,
        parent: Any,
        *,
        current_project_path: Optional[Path],
        choose_file: FilePicker,
    ) -> Optional[Path]:
        """Return the project selected by the existing open-file dialog."""
        start_dir = (
            str(current_project_path.parent) if current_project_path is not None else ""
        )
        path_text, _selected_filter = choose_file(
            parent,
            "打开字幕渲染项目",
            start_dir,
            self.project_filter,
        )
        return Path(path_text) if path_text else None

    def choose_save_path(
        self,
        parent: Any,
        *,
        current_project_path: Optional[Path],
        subtitle_path: Optional[Path],
        video_path: Optional[Path],
        current_directory: Optional[Path] = None,
        choose_file: FilePicker,
    ) -> Optional[Path]:
        """Return a suffixed path selected by the existing save-file dialog."""
        if current_project_path is not None:
            start = str(current_project_path)
        else:
            source = subtitle_path or video_path
            if source is None:
                source = Path.cwd() if current_directory is None else current_directory
            start = str(Path(source).with_suffix("")) + self.project_suffix
        path_text, _selected_filter = choose_file(
            parent,
            "保存字幕渲染项目",
            start,
            self.project_filter,
        )
        if not path_text:
            return None
        if not path_text.endswith(self.project_suffix):
            path_text += self.project_suffix
        return Path(path_text)

    @staticmethod
    def preflight_save(
        parent: Any,
        *,
        path: Path,
        current_project_path: Optional[Path],
        known_disk_revision: Any,
        inspect: Callable[[Path], Any],
        choose: ChoicePrompt,
    ) -> ProjectSavePreflight:
        """Resolve external-modification conflicts before an existing save."""
        path = Path(path)
        if current_project_path is None or path != current_project_path:
            return ProjectSavePreflight(ProjectSaveAction.CONTINUE)
        try:
            disk_revision = inspect(path)
        except OSError as exc:
            return ProjectSavePreflight(
                ProjectSaveAction.INSPECTION_FAILED,
                error=str(exc),
            )
        if known_disk_revision is None or disk_revision == known_disk_revision:
            return ProjectSavePreflight(ProjectSaveAction.CONTINUE)

        choice = choose(
            parent,
            "项目文件已被外部修改",
            f"磁盘上的项目文件在打开或上次保存后发生了变化：\n"
            f"{path}\n\n直接覆盖可能丢失其他程序的修改。",
            ("覆盖", "另存为", "取消"),
            default=2,
        )
        if choice == 0:
            return ProjectSavePreflight(ProjectSaveAction.CONTINUE)
        if choice == 1:
            return ProjectSavePreflight(ProjectSaveAction.SAVE_AS)
        return ProjectSavePreflight(ProjectSaveAction.CANCEL)


__all__ = [
    "ProjectCommandController",
    "ProjectSaveAction",
    "ProjectSavePreflight",
]
