"""Focused contracts for subtitle project lifecycle transactions."""

from __future__ import annotations

import json
import shutil
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


def test_project_resources_follow_a_moved_directory_tree(tmp_path: Path) -> None:
    original = tmp_path / "原目录"
    original.mkdir()
    (original / "projects").mkdir()
    (original / "media").mkdir()
    for name in ("main.lrc", "chorus.sug", "video.mp4", "audio.flac"):
        (original / "media" / name).write_bytes(b"asset")
    project = original / "projects" / "song.yurika"
    data = {
        "subtitle_path": str(original / "media" / "main.lrc"),
        "video_path": str(original / "media" / "video.mp4"),
        "audio_path": str(original / "media" / "audio.flac"),
        "background": {"kind": "video", "path": str(original / "media" / "video.mp4")},
        "extra_subtitle_sources": [{"name": "和声", "path": str(original / "media" / "chorus.sug")}],
        "output": {"output_path": str(original / "output.mp4")},
    }
    save_render_project(project, data)
    moved = tmp_path / "移动后的目录"
    original.rename(moved)

    loaded = SubtitleProjectController.open(moved / "projects" / project.name)

    assert loaded.missing_resources == ()
    assert loaded.data["subtitle_path"] == str(moved / "media" / "main.lrc")
    assert loaded.data["video_path"] == str(moved / "media" / "video.mp4")
    assert loaded.data["audio_path"] == str(moved / "media" / "audio.flac")
    assert loaded.data["background"]["path"] == str(moved / "media" / "video.mp4")
    assert loaded.data["extra_subtitle_sources"][0]["path"] == str(moved / "media" / "chorus.sug")
    assert loaded.data["output"] == data["output"]
    assert json.loads((moved / "projects" / project.name).read_text(encoding="utf-8"))["subtitle_path"] == data["subtitle_path"]
    # Saving the relocated project updates its base for the next directory move.
    save_render_project(loaded.path, loaded.data)
    moved_again = tmp_path / "再次移动"
    moved.rename(moved_again)
    reloaded = SubtitleProjectController.open(moved_again / "projects" / project.name)
    assert reloaded.missing_resources == ()
    assert reloaded.data["subtitle_path"] == str(moved_again / "media" / "main.lrc")


def test_project_resources_keep_existing_absolute_paths_after_copy(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.mkdir()
    lyric = original / "main.lrc"
    lyric.write_bytes(b"original")
    save_render_project(original / "song.yurika", {"subtitle_path": str(lyric)})
    copied = tmp_path / "copied"
    shutil.copytree(original, copied)
    (copied / lyric.name).write_bytes(b"different")

    loaded = SubtitleProjectController.open(copied / "song.yurika")

    assert loaded.data["subtitle_path"] == str(lyric)


@pytest.mark.parametrize("kind", ["image", "image_sequence"])
def test_moved_project_resolves_background_images_and_sequences(tmp_path: Path, kind: str) -> None:
    original = tmp_path / "original"
    frames = original / "frames"
    frames.mkdir(parents=True)
    (frames / "frame_0003.png").write_bytes(b"frame")
    name = "frame_%04d.png" if kind == "image_sequence" else "frame_0003.png"
    save_render_project(original / "song.yurika", {
        "background": {"kind": kind, "path": str(frames / name), "sequence_start_number": 3},
    })
    moved = tmp_path / "moved"
    original.rename(moved)

    loaded = SubtitleProjectController.open(moved / "song.yurika")

    assert loaded.data["background"]["path"] == str(moved / "frames" / name)
    assert loaded.missing_resources == ()


@pytest.mark.parametrize("resource", ["main.lrc", "lyrics/main.lrc", r"lyrics\main.lrc"])
def test_legacy_project_resolves_relative_paths_from_project_not_cwd(
    tmp_path: Path, monkeypatch, resource: str,
) -> None:
    project_dir = tmp_path / "project"
    target = project_dir / resource.replace("\\", "/")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"project lyrics")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    competing = cwd / resource.replace("\\", "/")
    competing.parent.mkdir(parents=True, exist_ok=True)
    competing.write_bytes(b"wrong cwd lyrics")
    monkeypatch.chdir(cwd)
    project = project_dir / "song.yurika"
    project.write_text(json.dumps({"subtitle_path": resource}), encoding="utf-8")

    loaded = SubtitleProjectController.open(project)

    assert loaded.data["subtitle_path"] == str(target)
    assert loaded.missing_resources == ()


@pytest.mark.parametrize("old_path", ["/old/root/lyrics/main.lrc", r"Z:\old\root\lyrics\main.lrc"])
def test_legacy_project_relocates_missing_absolute_path_by_matching_suffix(
    tmp_path: Path, old_path: str,
) -> None:
    (tmp_path / "lyrics").mkdir()
    target = tmp_path / "lyrics" / "main.lrc"
    target.write_bytes(b"lyrics")
    (tmp_path / "main.lrc").write_bytes(b"wrong shallower match")
    project = tmp_path / "song.yurika"
    project.write_text(json.dumps({"subtitle_path": old_path}), encoding="utf-8")

    loaded = SubtitleProjectController.open(project)

    assert loaded.data["subtitle_path"] == str(target)
    assert loaded.missing_resources == ()


def test_moved_project_does_not_replace_missing_subfolder_asset_with_same_name(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.mkdir()
    save_render_project(original / "song.yurika", {"subtitle_path": str(original / "lyrics" / "main.lrc")})
    moved = tmp_path / "moved"
    original.rename(moved)
    (moved / "main.lrc").write_bytes(b"unrelated same-name lyrics")

    loaded = SubtitleProjectController.open(moved / "song.yurika")

    assert loaded.missing_resources == (("主字幕", original / "lyrics" / "main.lrc"),)


def test_project_saved_on_another_platform_keeps_sibling_asset_relationship(tmp_path: Path) -> None:
    project_dir = tmp_path / "projects"
    project_dir.mkdir()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    target = media_dir / "main.lrc"
    target.write_bytes(b"lyrics")
    project = project_dir / "song.yurika"
    project.write_text(json.dumps({
        "resource_base_dir": r"Z:\old\projects",
        "subtitle_path": r"Z:\old\media\main.lrc",
    }), encoding="utf-8")

    loaded = SubtitleProjectController.open(project)

    assert loaded.data["subtitle_path"] == str(target)
    assert loaded.missing_resources == ()


@pytest.mark.parametrize("source", ["song.yurika", "../project/song.yurika"])
def test_recovery_relative_resources_use_formal_project_directory(tmp_path: Path, source: str) -> None:
    directory = tmp_path / "project"
    directory.mkdir()
    lyric = directory / "main.lrc"
    lyric.write_bytes(b"lyrics")
    recovery = directory / "song.recovery"
    recovery.write_text(json.dumps({
        "subtitle_path": "main.lrc",
        "recovery": {"source_project_path": source},
    }), encoding="utf-8")

    assert load_render_project(recovery)["subtitle_path"] == str(lyric)
    assert load_render_project(recovery, resolve_resources=False)["subtitle_path"] == "main.lrc"


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
