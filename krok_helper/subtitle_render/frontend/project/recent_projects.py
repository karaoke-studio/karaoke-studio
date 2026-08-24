"""Recent-project persistence and menu orchestration for the subtitle frontend."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from qfluentwidgets import Action, FluentIcon as FIF, RoundMenu

from krok_helper.subtitle_render.recent_projects import RecentProjectPolicy
from krok_helper.subtitle_render.settings_store import SubtitleRenderSettingsStore


RebuildCallback = Callable[[], None]


class RecentProjectsController:
    """Own recent native-project state while exposing explicit UI callbacks."""

    def __init__(
        self,
        policy: RecentProjectPolicy,
        settings_store: SubtitleRenderSettingsStore,
        *,
        settings_key: str = "recent_projects",
    ) -> None:
        self._policy = policy
        self._settings_store = settings_store
        self._settings_key = str(settings_key)
        self._paths: list[str] = []

    @property
    def paths(self) -> list[str]:
        return self._paths

    @paths.setter
    def paths(self, value: list[str]) -> None:
        self._paths = [str(path) for path in value]

    def load(self) -> list[str]:
        """Load valid native projects and prune stale or duplicate entries."""
        data = self._settings_store.load()
        stored = data.get(self._settings_key, [])
        paths = self._policy.normalize(stored)
        if paths != stored:
            self.persist(paths)
        return paths

    def persist(self, paths: list[str]) -> None:
        """Persist only the recent-project field within the module namespace."""
        try:
            data = self._settings_store.load()
            data[self._settings_key] = list(paths)
            self._settings_store.save(data)
        except Exception:
            logging.getLogger(__name__).warning(
                "保存字幕渲染最近项目失败",
                exc_info=True,
            )

    def rebuild_menu(
        self,
        menu: RoundMenu,
        *,
        open_recent: Callable[[str], Any],
        clear_recent: Callable[..., Any],
    ) -> None:
        """Refresh only the recent-project submenu and keep its parents intact."""
        old_actions = list(menu.actions())
        menu.clear()
        for action in old_actions:
            action.deleteLater()

        if not self._paths:
            empty_action = Action("暂无最近打开的项目", menu)
            empty_action.setEnabled(False)
            menu.addAction(empty_action)
            return

        for file_path in self._paths:
            path = Path(file_path)
            action = Action(
                FIF.DOCUMENT,
                f"{path.name}  —  {path.parent}",
                menu,
            )
            action.setToolTip(file_path)
            action.triggered.connect(
                lambda checked=False, p=file_path: open_recent(p)
            )
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(
            Action(
                FIF.DELETE,
                "清除最近打开记录",
                menu,
                triggered=clear_recent,
            )
        )

    def set_paths(self, paths: list[str], *, rebuild: RebuildCallback) -> None:
        """Update paths and rebuild only when the normalized list changed."""
        normalized = [str(path) for path in paths]
        if normalized == self._paths:
            return
        self._paths = normalized
        rebuild()

    def record(self, path: Path | str, *, rebuild: RebuildCallback) -> None:
        """Move one successfully opened native project to the front."""
        paths = self._policy.record(self.load(), path)
        self.persist(paths)
        self.set_paths(paths, rebuild=rebuild)

    def clear(self, *, rebuild: RebuildCallback) -> None:
        self.persist([])
        self.set_paths([], rebuild=rebuild)

    def open(
        self,
        file_path: str,
        *,
        parent: Any,
        rebuild: RebuildCallback,
        show_warning: Callable[..., Any],
        open_project: Callable[[Path], Any],
    ) -> None:
        path = Path(file_path)
        if not path.is_file():
            self.set_paths(self.load(), rebuild=rebuild)
            show_warning(
                parent,
                "文件不存在",
                "最近打开的项目已被移动或删除。",
            )
            return
        open_project(path)
