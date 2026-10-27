"""User-command orchestration for native subtitle projects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


ChoicePrompt = Callable[..., int]
FilePicker = Callable[..., tuple[str, str]]
Command = Callable[[], Any]


def _is_network_location(path: Path) -> bool:
    """UNC 路径不做存在性探测：SMB 断开时 ``is_dir()`` 会卡住几十秒。"""

    return str(path).startswith(("\\\\", "//"))


def _safe_current_directory() -> Path:
    """启动目录被删除 / 所在盘符消失时 ``Path.cwd()`` 本身会抛 ``WinError 3``。"""

    try:
        return Path.cwd()
    except OSError:
        return Path.home()


def _existing_directory(directory: Path) -> Path:
    """离 ``directory`` 最近的现存目录，找不到时退回用户主目录。

    默认另存位置如果落在已删除的文件夹、已拔出的盘符或断开的网络共享上，
    系统会在写盘时抛 ``[WinError 3] 系统找不到指定的路径``——导出前的保存
    会因此在弹完「保存并导出」后中断，工程也建不出来。这里把起始目录退到
    还能写的位置。网络位置不探测（SMB 断开时卡住），保持原样交给对话框。
    """

    directory = Path(directory)
    if _is_network_location(directory):
        return directory
    for candidate in (directory, *directory.parents):
        if _is_network_location(candidate):
            return candidate
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return Path.home()


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
        """Return a suffixed path selected by the existing save-file dialog.

        起始位置逐级退回现存目录：工程原路径或素材所在目录被删除、盘符被
        拔除时不把对话框开在一个写不进去的位置（用户顺手一点「保存」就会
        撞上 ``[WinError 3] 系统找不到指定的路径``）。
        """
        if current_project_path is not None:
            current = Path(current_project_path)
            start = str(_existing_directory(current.parent) / current.name)
        else:
            media = subtitle_path or video_path
            if media is None:
                fallback = (
                    _safe_current_directory()
                    if current_directory is None
                    else Path(current_directory)
                )
                directory = _existing_directory(fallback)
                name = fallback.name or "未命名项目"
            else:
                media = Path(media)
                directory = _existing_directory(media.parent)
                name = media.stem
            start = str(directory / f"{name}{self.project_suffix}")
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
