"""Focused contracts for subtitle project crash-recovery policy."""

from __future__ import annotations

import hashlib
from pathlib import Path

from krok_helper.subtitle_render.project.recovery import ProjectRecoveryPolicy
from krok_helper.subtitle_render.project.store import (
    load_render_project,
    save_recovery_project,
    save_render_project,
)


def test_recovery_policy_builds_stable_project_paths(tmp_path: Path) -> None:
    policy = ProjectRecoveryPolicy(tmp_path / "recovery")
    project = tmp_path / "projects" / "song.yurika"
    identity = str(project.resolve()).encode("utf-8", errors="surrogatepass")
    suffix = hashlib.sha256(identity).hexdigest()[:12]

    assert policy.path_for(None) == tmp_path / "recovery" / "untitled.yurika.recovery"
    assert policy.path_for(project) == (
        tmp_path / "recovery" / f"song.yurika.{suffix}.recovery"
    )


def test_recovery_policy_snapshot_is_detached_and_keeps_session_identity(
    tmp_path: Path,
) -> None:
    source = {"style": {"font_size_px": 80}}
    project = tmp_path / "song.yurika"
    policy = ProjectRecoveryPolicy(
        tmp_path,
        snapshot_id_factory=lambda: 123456,
        timestamp_factory=lambda: 789.25,
    )

    snapshot = policy.snapshot(
        source,
        project_path=project,
        generation=4,
        revision=9,
    )
    source["style"]["font_size_px"] = 100

    assert snapshot.snapshot_id == 123456
    assert snapshot.generation == 4
    assert snapshot.revision == 9
    assert snapshot.payload == {
        "style": {"font_size_px": 80},
        "recovery": {
            "source_project_path": str(project),
            "created_at_unix": 789.25,
            "snapshot_id": 123456,
            "project_generation": 4,
            "project_revision": 9,
        },
    }


def test_recovery_policy_scan_removes_stale_and_preserves_actionable_entries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recovery"
    root.mkdir()
    source = tmp_path / "saved.yurika"
    save_render_project(source, {"style": {}})
    stale = root / "saved.yurika.stale.recovery"
    candidate = root / "untitled.yurika.recovery"
    invalid = root / "broken.yurika.recovery"
    save_recovery_project(
        stale,
        {
            "recovery": {
                "source_project_path": str(source),
                "created_at_unix": 1.0,
                "snapshot_id": 1,
            }
        },
    )
    save_recovery_project(
        candidate,
        {
            "recovery": {
                "source_project_path": None,
                "created_at_unix": 2.0,
                "snapshot_id": 2,
            }
        },
    )
    invalid.write_text("not json", encoding="utf-8")
    source.touch()

    scan = ProjectRecoveryPolicy(root).scan()

    assert [item.path for item in scan.candidates] == [candidate]
    assert scan.invalid_paths == (invalid,)
    assert scan.requires_attention is True
    assert not stale.exists()


def test_recovery_policy_cleanup_only_removes_matching_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "song.yurika.recovery"
    save_recovery_project(
        path,
        {
            "recovery": {
                "source_project_path": None,
                "created_at_unix": 1.0,
                "snapshot_id": 20,
            }
        },
    )

    ProjectRecoveryPolicy.cleanup_snapshot(path, 19)
    assert load_render_project(path)["recovery"]["snapshot_id"] == 20

    ProjectRecoveryPolicy.cleanup_snapshot(path, 20)
    assert not path.exists()
