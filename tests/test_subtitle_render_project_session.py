"""Focused lifecycle contracts for subtitle project session state."""

from __future__ import annotations

from pathlib import Path

from krok_helper.subtitle_render.project.session import SubtitleProjectSession


def test_project_session_remembers_and_resolves_missing_resources(
    tmp_path: Path,
) -> None:
    subtitle = tmp_path / "missing.sug"
    audio = tmp_path / "missing.flac"
    source = {"subtitle_path": str(subtitle), "audio_path": str(audio)}
    session = SubtitleProjectSession()

    session.remember_missing_resources(
        [("主字幕", subtitle), ("独立音频", audio)],
        source,
    )
    source["subtitle_path"] = "changed"

    assert session.missing_resources == (("主字幕", subtitle), ("独立音频", audio))
    assert session.unresolved_resource_labels == {"主字幕", "独立音频"}
    assert session.missing_resource_source_data == {
        "subtitle_path": str(subtitle),
        "audio_path": str(audio),
    }

    assert session.resolve_missing_resource_labels({"主字幕"}) is True
    assert session.missing_resources == (("独立音频", audio),)
    assert session.missing_resource_source_data is not None
    assert session.resolve_missing_resource_labels({"独立音频"}) is True
    assert session.missing_resources == ()
    assert session.missing_resource_source_data is None


def test_project_session_merges_only_still_unresolved_references(
    tmp_path: Path,
) -> None:
    subtitle = tmp_path / "main.sug"
    background = tmp_path / "background.png"
    audio = tmp_path / "audio.flac"
    chorus = tmp_path / "chorus.lrc"
    existing = tmp_path / "existing.lrc"
    source = {
        "subtitle_path": str(subtitle),
        "audio_path": str(audio),
        "background": {"kind": "image", "path": str(background)},
        "extra_subtitle_sources": [
            {"name": "和声", "path": str(chorus), "future": {"value": 1}},
        ],
    }
    session = SubtitleProjectSession()
    session.remember_missing_resources(
        [
            ("主字幕", subtitle),
            ("背景图片", background),
            ("独立音频", audio),
            ("副字幕「和声」", chorus),
        ],
        source,
    )

    merged = session.merge_unresolved_resource_references(
        {"extra_subtitle_sources": [{"name": "现有", "path": str(existing)}]}
    )

    assert merged["subtitle_path"] == str(subtitle)
    assert merged["audio_path"] == str(audio)
    assert merged["background"] == source["background"]
    assert merged["extra_subtitle_sources"] == [
        {"name": "现有", "path": str(existing)},
        {"name": "和声", "path": str(chorus), "future": {"value": 1}},
    ]


def test_project_session_merge_returns_original_when_nothing_is_unresolved() -> None:
    payload = {"style": {}}

    assert SubtitleProjectSession().merge_unresolved_resource_references(payload) is payload


def test_project_session_owns_save_state_transitions(tmp_path: Path) -> None:
    original = tmp_path / "original.yurika"
    saved = tmp_path / "saved.yurika"
    disk_revision = object()
    session = SubtitleProjectSession(
        path=original,
        dirty=True,
        revision=7,
        save_error="old error",
    )

    revision_at_save = session.begin_save()

    assert revision_at_save == 7
    assert session.saving is True
    assert session.save_error is None
    assert session.dirty is True

    session.fail_save("disk full")
    assert session.saving is False
    assert session.save_error == "disk full"
    assert session.path == original
    assert session.revision == 7
    assert session.dirty is True

    assert session.begin_save() == 7
    session.complete_save(
        path=saved,
        disk_revision=disk_revision,
        saved_revision=7,
    )
    assert session.path == saved
    assert session.disk_revision is disk_revision
    assert session.saved_revision == 7
    assert session.saving is False
    assert session.save_error is None
    assert session.dirty is True


def test_project_session_records_pre_save_inspection_failure() -> None:
    session = SubtitleProjectSession(dirty=True, revision=3, saving=True)

    session.record_save_inspection_failure("cannot inspect")

    assert session.saving is False
    assert session.save_error == "cannot inspect"
    assert session.dirty is True
    assert session.revision == 3


def test_project_session_adopts_named_and_unnamed_project_identity(
    tmp_path: Path,
) -> None:
    project = tmp_path / "song.yurika"
    missing = tmp_path / "missing.sug"
    disk_revision = object()
    session = SubtitleProjectSession(path=tmp_path / "old.yurika")

    session.adopt_project_identity(
        path=project,
        disk_revision=disk_revision,
        missing_resources=[("主字幕", missing)],
        source_data={"subtitle_path": str(missing)},
    )

    assert session.path == project
    assert session.disk_revision is disk_revision
    assert session.missing_resources == (("主字幕", missing),)
    assert session.unresolved_resource_labels == {"主字幕"}

    session.adopt_project_identity(path=None, disk_revision=None)

    assert session.path is None
    assert session.disk_revision is None
    assert session.missing_resources == ()
    assert session.unresolved_resource_labels == set()
    assert session.missing_resource_source_data is None
