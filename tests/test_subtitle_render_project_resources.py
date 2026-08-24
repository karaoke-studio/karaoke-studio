"""Focused contracts for project resource inspection."""

from __future__ import annotations

from pathlib import Path

from krok_helper.subtitle_render.project.resources import (
    find_missing_project_resources,
)


def test_project_resource_scan_preserves_asset_order_and_labels(tmp_path: Path) -> None:
    subtitle = tmp_path / "main.sug"
    background = tmp_path / "background.mp4"
    audio = tmp_path / "audio.flac"
    chorus = tmp_path / "chorus.lrc"

    missing = find_missing_project_resources(
        {
            "subtitle_path": str(subtitle),
            "audio_path": str(audio),
            "background": {"kind": "video", "path": str(background)},
            "extra_subtitle_sources": [{"name": "和声", "path": str(chorus)}],
        }
    )

    assert missing == [
        ("主字幕", subtitle),
        ("背景视频", background),
        ("独立音频", audio),
        ("副字幕「和声」", chorus),
    ]


def test_project_resource_scan_accepts_existing_assets_and_deduplicates_paths(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing.lrc"
    existing.write_text("lyrics", encoding="utf-8")
    shared_missing = tmp_path / "shared.lrc"

    missing = find_missing_project_resources(
        {
            "subtitle_path": str(existing),
            "audio_path": str(shared_missing),
            "extra_subtitle_sources": [
                {"name": "副歌", "path": str(shared_missing)},
            ],
        }
    )

    assert missing == [("独立音频", shared_missing)]


def test_project_resource_scan_checks_first_image_sequence_frame(tmp_path: Path) -> None:
    pattern = tmp_path / "frame_%04d.png"
    (tmp_path / "frame_0003.png").write_bytes(b"frame")

    present = find_missing_project_resources(
        {
            "background": {
                "kind": "image_sequence",
                "path": str(pattern),
                "sequence_start_number": 3,
            }
        }
    )
    invalid_metadata = find_missing_project_resources(
        {
            "background": {
                "kind": "image_sequence",
                "path": str(pattern),
                "sequence_start_number": "invalid",
            }
        }
    )

    assert present == []
    assert invalid_metadata == [("背景图片序列", pattern)]


def test_project_resource_scan_uses_legacy_video_path_without_background(
    tmp_path: Path,
) -> None:
    video = tmp_path / "legacy.mp4"

    assert find_missing_project_resources({"video_path": str(video)}) == [
        ("背景视频", video)
    ]
