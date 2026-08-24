"""Focused contracts for subtitle project lifecycle transactions."""

from __future__ import annotations

from pathlib import Path

import pytest

from krok_helper.subtitle_render.project import controller as controller_module
from krok_helper.subtitle_render.project.controller import (
    SubtitleProjectController,
)
from krok_helper.subtitle_render.project.store import (
    ProjectFileRevision,
    load_render_project,
    save_render_project,
)


def test_project_controller_opens_one_consistent_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "song.yurika"
    missing = tmp_path / "missing.lrc"
    save_render_project(path, {"subtitle_path": str(missing), "style": {}})

    loaded = SubtitleProjectController.open(path)

    assert loaded.path == path
    assert loaded.data["subtitle_path"] == str(missing)
    assert loaded.revision.exists is True
    assert loaded.missing_resources == (("主字幕", missing),)


def test_project_controller_rejects_a_snapshot_changed_during_open(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "song.yurika"
    save_render_project(path, {"style": {}})
    revisions = iter(
        (
            ProjectFileRevision(True, 1, 10, "before"),
            ProjectFileRevision(True, 2, 10, "after"),
        )
    )
    monkeypatch.setattr(
        controller_module,
        "inspect_project_file",
        lambda _path: next(revisions),
    )

    with pytest.raises(OSError, match="项目文件在打开期间发生了变化"):
        SubtitleProjectController.open(path)


def test_project_controller_saves_with_a_rotated_manual_backup(tmp_path: Path) -> None:
    path = tmp_path / "song.yurika"
    backup_root = tmp_path / "backups"
    save_render_project(path, {"value": "old"})

    revision = SubtitleProjectController.save(
        path,
        {"value": "new"},
        backup_root=backup_root,
        backup_count=1,
    )

    assert revision.exists is True
    assert load_render_project(path)["value"] == "new"
    backups = list(backup_root.rglob("*.manual-backup.yurika"))
    assert len(backups) == 1
    assert load_render_project(backups[0])["value"] == "old"
