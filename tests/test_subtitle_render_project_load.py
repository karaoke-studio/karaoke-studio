"""Focused contracts for typed subtitle project load planning."""

from __future__ import annotations

from pathlib import Path

from krok_helper.subtitle_render.project.load import ProjectLoadPlan


def test_project_load_plan_resolves_legacy_style_reference_height() -> None:
    plan = ProjectLoadPlan.from_data(
        {
            "style": {"font_size_px": 96, "title_overlay": None},
            "screen": {"width": 3840, "height": 2160, "fps": 120},
            "selected_scheme_key": "custom:瑞",
        }
    )

    assert plan.style.font_size_px == 96
    assert plan.style.font_reference_height == 2160
    assert plan.style.title_overlay is not None
    assert (plan.screen.width, plan.screen.height, plan.screen.fps) == (3840, 2160, 120)
    assert plan.selected_scheme_key == "custom:瑞"


def test_project_load_plan_preserves_explicit_reference_height() -> None:
    plan = ProjectLoadPlan.from_data(
        {
            "style": {
                "font_size_px": 72,
                "font_reference_height": 1080,
            },
            "screen": {"height": 2160},
        }
    )

    assert plan.style.font_reference_height == 1080
    assert plan.selected_scheme_key is None


def test_project_load_plan_parses_paths_and_track_payloads(tmp_path: Path) -> None:
    subtitle = tmp_path / "main.sug"
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.flac"
    background = {"kind": "image", "path": str(tmp_path / "background.png")}
    plan = ProjectLoadPlan.from_data(
        {
            "subtitle_path": str(subtitle),
            "video_path": str(video),
            "audio_path": str(audio),
            "background": background,
            "output": {"codec": "h264"},
            "line_breaks_before": ["none", "page"],
            "line_layout_indices": [0, 1],
            "char_role_labels": [["主唱"]],
            "project_role_names": ["主唱"],
        }
    )

    assert plan.subtitle_path == subtitle
    assert plan.fallback_video_path == video
    assert plan.audio_path == audio
    assert plan.background is background
    assert plan.output == {"codec": "h264"}
    assert plan.line_breaks_before == ["none", "page"]
    assert plan.line_layout_indices == [0, 1]
    assert plan.char_role_labels == [["主唱"]]
    assert plan.project_role_names == ["主唱"]


def test_project_load_plan_builds_detached_deferred_assets(tmp_path: Path) -> None:
    background = {"kind": "image", "path": str(tmp_path / "background.png")}
    extras = [{"name": "和声", "path": str(tmp_path / "chorus.lrc")}]
    roles = ["主唱", "和声"]
    audio = tmp_path / "missing-but-deferred.flac"
    plan = ProjectLoadPlan.from_data(
        {
            "background": background,
            "audio_path": str(audio),
            "extra_subtitle_sources": extras,
            "project_role_names": roles,
        }
    )

    loads = plan.deferred_assets()
    background["path"] = "changed"
    extras[0]["name"] = "changed"
    roles.append("changed")

    assert [load.kind for load in loads] == [
        "background",
        "audio",
        "extra_subtitle_sources",
    ]
    assert loads[0].payload["path"] != "changed"
    deferred_extras, deferred_roles = loads[2].payload
    assert deferred_extras[0]["name"] == "和声"
    assert deferred_roles == ["主唱", "和声"]


def test_project_load_plan_uses_existing_legacy_video_for_deferred_load(
    tmp_path: Path,
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    plan = ProjectLoadPlan.from_data({"video_path": str(video)})

    assert [(load.kind, load.payload) for load in plan.deferred_assets()] == [
        ("video", video)
    ]
