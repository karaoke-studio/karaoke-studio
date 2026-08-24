"""User-command orchestration for native subtitle projects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


ChoicePrompt = Callable[..., int]
FilePicker = Callable[..., tuple[str, str]]
Command = Callable[[], Any]


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
        current_directory: Path,
        choose_file: FilePicker,
    ) -> Optional[Path]:
        """Return a suffixed path selected by the existing save-file dialog."""
        start = (
            str(current_project_path)
            if current_project_path is not None
            else str(
                (subtitle_path or video_path or Path(current_directory)).with_suffix("")
            )
            + self.project_suffix
        )
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
