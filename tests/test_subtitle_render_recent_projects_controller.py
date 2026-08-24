"""Focused contracts for recent-project frontend orchestration."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402
import pytest  # noqa: E402
from qfluentwidgets import RoundMenu  # noqa: E402

from krok_helper.subtitle_render.frontend.recent_projects import (  # noqa: E402
    RecentProjectsController,
)
from krok_helper.subtitle_render.recent_projects import RecentProjectPolicy  # noqa: E402


class _MemoryStore:
    def __init__(self, data: dict) -> None:
        self.data = dict(data)
        self.saved: list[dict] = []

    def load(self) -> dict:
        return dict(self.data)

    def save(self, data: dict) -> None:
        self.data = dict(data)
        self.saved.append(dict(data))


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _controller(store: _MemoryStore) -> RecentProjectsController:
    return RecentProjectsController(
        RecentProjectPolicy(project_suffix=".yurika", limit=10),
        store,  # type: ignore[arg-type]
    )


def test_recent_projects_controller_prunes_and_persists_on_load(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid.yurika"
    valid.write_text("{}", encoding="utf-8")
    store = _MemoryStore(
        {"recent_projects": [str(tmp_path / "missing.yurika"), str(valid), str(valid)]}
    )

    paths = _controller(store).load()

    assert paths == [str(valid.absolute())]
    assert store.saved[-1]["recent_projects"] == paths


def test_recent_projects_controller_rebuilds_only_after_state_changes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "song.yurika"
    project.write_text("{}", encoding="utf-8")
    controller = _controller(_MemoryStore({}))
    rebuilds: list[None] = []

    controller.set_paths([str(project)], rebuild=lambda: rebuilds.append(None))
    controller.set_paths([str(project)], rebuild=lambda: rebuilds.append(None))

    assert controller.paths == [str(project)]
    assert rebuilds == [None]


def test_recent_projects_controller_builds_existing_menu_contract(
    qapp,
    tmp_path: Path,
) -> None:
    project = tmp_path / "song.yurika"
    project.write_text("{}", encoding="utf-8")
    controller = _controller(_MemoryStore({}))
    controller.paths = [str(project)]
    menu = RoundMenu()
    opened: list[str] = []

    controller.rebuild_menu(
        menu,
        open_recent=opened.append,
        clear_recent=lambda: None,
    )

    action = menu.actions()[0]
    assert action.text() == f"{project.name}  —  {project.parent}"
    assert action.toolTip() == str(project)
    action.trigger()
    assert opened == [str(project)]


def test_recent_projects_controller_warns_and_prunes_a_missing_selection(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.yurika"
    store = _MemoryStore({"recent_projects": [str(missing)]})
    controller = _controller(store)
    controller.paths = [str(missing)]
    warnings: list[tuple] = []
    rebuilds: list[None] = []

    controller.open(
        str(missing),
        parent="parent",
        rebuild=lambda: rebuilds.append(None),
        show_warning=lambda *args: warnings.append(args),
        open_project=lambda _path: pytest.fail("missing path must not open"),
    )

    assert controller.paths == []
    assert rebuilds == [None]
    assert warnings == [
        ("parent", "文件不存在", "最近打开的项目已被移动或删除。")
    ]
