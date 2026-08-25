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
    RecoveryCandidate,
    load_render_project,
    save_recovery_project,
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


def test_project_controller_opens_recovery_with_formal_project_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "song.yurika"
    recovery = tmp_path / "song.recovery"
    missing = tmp_path / "missing.lrc"
    save_render_project(source, {"value": "formal"})
    save_recovery_project(
        recovery,
        {
            "subtitle_path": str(missing),
            "value": "recovered",
            "recovery": {"snapshot_id": 7},
        },
    )
    candidate = RecoveryCandidate(
        path=recovery,
        source_project_path=source,
        created_at_unix=1.0,
        snapshot_id=7,
    )

    loaded = SubtitleProjectController.open_recovery(candidate)

    assert loaded.data["value"] == "recovered"
    assert "recovery" not in loaded.data
    assert loaded.source_project_path == source
    assert loaded.source_disk_revision is not None
    assert loaded.source_disk_revision.exists is True
    assert loaded.missing_resources == (("主字幕", missing),)


def test_project_controller_recovery_tolerates_formal_revision_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    recovery = tmp_path / "song.recovery"
    save_recovery_project(
        recovery,
        {"value": "recovered", "recovery": {"snapshot_id": 8}},
    )
    candidate = RecoveryCandidate(
        path=recovery,
        source_project_path=tmp_path / "formal.yurika",
        created_at_unix=1.0,
        snapshot_id=8,
    )
    monkeypatch.setattr(
        controller_module,
        "inspect_project_file",
        lambda _path: (_ for _ in ()).throw(OSError("busy")),
    )

    loaded = SubtitleProjectController.open_recovery(candidate)

    assert loaded.source_disk_revision is None
    assert loaded.data["value"] == "recovered"


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
