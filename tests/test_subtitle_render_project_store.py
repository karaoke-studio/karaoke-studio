"""A11 项目文件（.yurika）读写与 standalone 新建/保存/打开往返。"""

from __future__ import annotations

from copy import deepcopy
import json
import os
import logging
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QUrl, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu  # noqa: E402
from qfluentwidgets.components.widgets.menu import MenuAnimationType  # noqa: E402

from krok_helper.subtitle_render.frontend import main_window as mw  # noqa: E402
from krok_helper.subtitle_render.frontend import lyrics_list  # noqa: E402
from krok_helper.subtitle_render import models as subtitle_models  # noqa: E402
from krok_helper.subtitle_render import project_store as project_store_module  # noqa: E402
from krok_helper.subtitle_render.models import (  # noqa: E402
    BackgroundSource,
    LineAnimationOverride,
    Style,
    STYLE_APPEARANCE_FIELDS,
    StyleTimingConfig,
    StylePreset,
    SubtitleStyleScheme,
    TITLE_SCHEME_NAME,
    TimingChar,
    TimingLine,
    TimingTrack,
    TimingTrackMeta,
    TitleOverlay,
    style_from_dict,
    style_to_dict,
)
from krok_helper.subtitle_render.preferences import (  # noqa: E402
    APP_LOCAL_ONLY_OUTPUT_FIELDS,
    app_default_style_to_dict,
    load_app_style_preferences,
    merge_common_style_preferences,
    update_app_output_preferences,
)
from krok_helper.subtitle_render.project_store import (  # noqa: E402
    PROJECT_SCHEMA_VERSION,
    backup_project_file,
    background_payload,
    inspect_project_file,
    invalidate_recovery_project,
    load_render_project,
    save_discarded_project_backup,
    save_recovery_project,
    save_render_project,
    scan_recovery_projects,
)
from krok_helper.subtitle_render.session import (  # noqa: E402
    ExtraSubtitleSource,
    SubtitleProjectDocument,
    SubtitleProjectSession,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_window(qapp, monkeypatch):
    if not os.environ.get("KARAOKE_STUDIO_SETTINGS_DIR"):
        monkeypatch.setenv(
            "KARAOKE_STUDIO_SETTINGS_DIR",
            tempfile.mkdtemp(prefix="karaoke-studio-test-settings-"),
        )
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)
    monkeypatch.setattr(
        mw.SubtitleRenderWindow, "_resolve_ffprobe_path", lambda self: "ffprobe"
    )
    return mw.SubtitleRenderWindow(embedded=False)


def test_save_render_project_round_trip(tmp_path):
    path = tmp_path / "demo.yurika"
    data = {"style": {"font_size_px": 80}, "selected_scheme_key": "global"}
    save_render_project(path, data)
    assert path.is_file()
    loaded = load_render_project(path)
    assert loaded["schema_version"] == PROJECT_SCHEMA_VERSION
    assert loaded["style"]["font_size_px"] == 80


def test_project_session_owns_dirty_revision_and_generation_state(tmp_path):
    session = SubtitleProjectSession(
        path=tmp_path / "demo.yurika",
        save_error="old failure",
        disk_revision=object(),
        missing_resources=(("主字幕", tmp_path / "missing.lrc"),),
        unresolved_resource_labels={"主字幕"},
        missing_resource_source_data={"subtitle_path": "missing.lrc"},
    )

    assert session.set_dirty(True) is False
    assert session.dirty is True
    assert session.revision == 1
    assert session.save_error is None

    # Reasserting dirty preserves the old window behaviour: it does not create
    # a second content revision, while an actual mutation always does.
    assert session.set_dirty(True) is True
    assert session.revision == 1
    assert session.mark_dirty() == (True, False)
    assert session.revision == 2

    session.begin_generation()
    assert session.generation == 1
    assert session.revision == 0
    assert session.saved_revision == 0
    assert session.disk_revision is None
    assert session.missing_resources == ()
    assert session.unresolved_resource_labels == set()
    assert session.missing_resource_source_data is None


def test_project_session_snapshot_preserves_host_status_text(tmp_path):
    project = tmp_path / "song.yurika"
    missing = tmp_path / "missing.lrc"
    session = SubtitleProjectSession(
        path=project,
        dirty=True,
        missing_resources=(("主字幕", missing),),
    )

    state = session.snapshot(
        has_project=True,
        exporting=True,
        recovery_path=tmp_path / "recovery.yurika",
    )

    assert state.path == project
    assert state.status_text() == "song.yurika · 未保存 · 导出中 · 素材缺失 1 项"


def test_project_document_clears_sources_without_resetting_style(tmp_path):
    style = Style(font_size_px=144)
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar("歌", 1000)])])
    document = SubtitleProjectDocument(
        timing_track=track,
        extra_sources=[
            ExtraSubtitleSource(
                name="和声",
                path=tmp_path / "chorus.lrc",
                track=track,
            )
        ],
        subtitle_path=tmp_path / "main.lrc",
        video_path=tmp_path / "video.mp4",
        background_source=BackgroundSource(kind="video", path="video.mp4"),
        audio_path=tmp_path / "audio.wav",
        style=style,
        role_names=["主唱"],
    )

    document.clear_loaded_media()

    assert document.timing_track is None
    assert document.extra_sources == []
    assert document.subtitle_path is None
    assert document.video_path is None
    assert document.video_info is None
    assert document.background_source is None
    assert document.audio_path is None
    assert document.audio_info is None
    assert document.style is style
    assert document.role_names == []


def test_project_document_owns_track_lookup_and_replacement(tmp_path):
    primary = TimingTrack(lines=[TimingLine(chars=[TimingChar("主", 0)])])
    chorus = TimingTrack(lines=[TimingLine(chars=[TimingChar("和", 0)])])
    replacement = TimingTrack(lines=[TimingLine(chars=[TimingChar("新", 0)])])
    document = SubtitleProjectDocument(
        timing_track=primary,
        extra_sources=[
            ExtraSubtitleSource(
                name="和声",
                path=tmp_path / "chorus.lrc",
                track=chorus,
            )
        ],
    )

    assert document.tracks() == [primary, chorus]
    assert document.track_at(0) is primary
    assert document.track_at(1) is chorus
    assert document.track_at(2) is None
    assert document.replace_track(1, replacement) is True
    assert document.track_at(1) is replacement
    assert document.replace_track(2, primary) is False


def test_project_document_captures_track_mutation_snapshots():
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar("主", 0)])])
    document = SubtitleProjectDocument(timing_track=track)

    mutation = document.mutate_track(
        0,
        lambda target: setattr(target.lines[0], "layout_index", 2) or "applied",
    )

    assert mutation is not None
    assert mutation.changed is True
    assert mutation.result == "applied"
    assert mutation.before.lines[0].layout_index == 0
    assert mutation.after.lines[0].layout_index == 2
    track.lines[0].layout_index = 3
    assert mutation.after.lines[0].layout_index == 2


def test_project_document_reports_noop_and_rejects_missing_track():
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar("主", 0)])])
    document = SubtitleProjectDocument(timing_track=track)

    noop = document.mutate_track(0, lambda _target: False)

    assert noop is not None
    assert noop.changed is False
    assert noop.result is False
    assert document.mutate_track(1, lambda _target: True) is None


def test_project_document_captures_atomic_multi_track_mutation(tmp_path):
    primary = TimingTrack(lines=[TimingLine(chars=[TimingChar("主", 0)])])
    chorus = TimingTrack(lines=[TimingLine(chars=[TimingChar("和", 0)])])
    document = SubtitleProjectDocument(
        timing_track=primary,
        extra_sources=[
            ExtraSubtitleSource("和声", tmp_path / "chorus.lrc", chorus)
        ],
    )

    mutation = document.mutate_tracks(
        (0, 1),
        lambda tracks: [
            setattr(track.lines[0], "layout_index", index + 1)
            for index, track in enumerate(tracks)
        ],
    )

    assert mutation is not None
    assert mutation.changed is True
    assert mutation.track_indices == (0, 1)
    assert [track.lines[0].layout_index for track in mutation.before] == [0, 0]
    assert [track.lines[0].layout_index for track in mutation.after] == [1, 2]
    assert document.mutate_tracks((0, 2), lambda _tracks: None) is None


def test_project_document_builds_complete_ui_independent_snapshot(tmp_path):
    line = TimingLine(
        chars=[TimingChar("歌", 1000, role_label="主唱")],
        end_ms=2000,
        layout_index=2,
        break_before="page",
        display_start_override_ms=750,
        display_end_override_ms=2250,
        animation_override=LineAnimationOverride(
            entry_anim="slide_in",
            entry_duration_ms=450,
        ),
    )
    track = TimingTrack(lines=[line])
    document = SubtitleProjectDocument(
        timing_track=track,
        extra_sources=[
            ExtraSubtitleSource(
                name="和声",
                path=tmp_path / "chorus.lrc",
                track=track,
            )
        ],
        subtitle_path=tmp_path / "main.lrc",
        video_path=tmp_path / "video.mp4",
        background_source=BackgroundSource(
            kind="video",
            path=str(tmp_path / "video.mp4"),
            video_offset_ms=125,
        ),
        audio_path=tmp_path / "audio.wav",
        style=Style(font_size_px=96),
        role_names=["主唱", "和声"],
    )

    payload = document.to_project_data(
        screen={"width": 1920, "height": 1080, "fps": 60, "par": "1:1"},
        selected_scheme_key="global",
        output={"encoder_mode": "cpu", "crf": 18},
    )

    assert payload["subtitle_path"] == str(tmp_path / "main.lrc")
    assert payload["video_path"] == str(tmp_path / "video.mp4")
    assert payload["audio_path"] == str(tmp_path / "audio.wav")
    assert payload["background"]["video_offset_ms"] == 125
    assert payload["style"]["font_size_px"] == 96
    assert payload["line_layout_indices"] == [2]
    assert payload["line_breaks_before"] == ["page"]
    assert payload["char_role_labels"] == [["主唱"]]
    assert payload["line_display_overrides"] == [[750, 2250]]
    assert payload["line_animation_overrides"] == [
        {
            "entry_anim": "slide_in",
            "entry_duration_ms": 450,
            "exit_anim": "none",
            "exit_duration_ms": 300,
        }
    ]
    assert payload["project_role_names"] == ["主唱", "和声"]
    assert payload["extra_subtitle_sources"][0]["name"] == "和声"
    assert payload["extra_subtitle_sources"][0]["line_layout_indices"] == [2]
    assert payload["output"] == {"encoder_mode": "cpu", "crf": 18}


def test_project_document_preserves_future_extension_fields():
    document = SubtitleProjectDocument(style=Style(font_size_px=88))
    document.remember_project_data(
        {
            "schema_version": 99,
            "future_section": {"mode": "new"},
            "style": {"font_size_px": 12, "future_style": {"v": 1}},
            "screen": {"width": 320, "future_screen": True},
            "output": {"crf": 51, "future_output": "keep"},
            "background": {"kind": "solid", "future_background": 7},
        }
    )

    payload = document.to_project_data(
        screen={"width": 1920, "height": 1080, "fps": 60, "par": "1:1"},
        selected_scheme_key="global",
        output={"crf": 18},
    )

    assert "schema_version" not in payload
    assert payload["future_section"] == {"mode": "new"}
    assert payload["style"]["font_size_px"] == 88
    assert payload["style"]["future_style"] == {"v": 1}
    assert payload["screen"]["width"] == 1920
    assert payload["screen"]["future_screen"] is True
    assert payload["output"] == {"crf": 18, "future_output": "keep"}
    assert "background" not in payload


def test_project_document_preserves_nested_future_fields_by_object_identity(tmp_path):
    style = Style(
        font_size_px=88,
        layouts=[
            subtitle_models.LyricsLayout(name="A", layout_id="layout-a"),
            subtitle_models.LyricsLayout(name="B", layout_id="layout-b"),
        ],
        custom_style_schemes={
            "主唱": SubtitleStyleScheme(fill_color="#112233")
        },
        title_overlay=TitleOverlay(enabled=True),
    )
    page_plan = subtitle_models.TrackPagePlan(
        [subtitle_models.TrackSection([subtitle_models.TrackPage(1, "layout-a")])]
    )
    settings = subtitle_models.SubtitleLoadingSettings(rows_per_page=3)
    primary = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("主", 0)])],
        page_plan=page_plan,
        loading_settings_mode="custom",
        loading_settings=settings,
        loading_settings_snapshot=settings,
    )
    chorus_a = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("甲", 0)])],
        page_plan=deepcopy(page_plan),
    )
    chorus_b = TimingTrack(lines=[TimingLine(chars=[TimingChar("乙", 0)])])
    document = SubtitleProjectDocument(
        timing_track=primary,
        style=style,
        extra_sources=[
            ExtraSubtitleSource("甲", tmp_path / "a.lrc", chorus_a),
            ExtraSubtitleSource("乙", tmp_path / "b.lrc", chorus_b),
        ],
    )
    source = document.to_project_data(
        screen={"width": 1920, "height": 1080, "fps": 60, "par": "1:1"},
        selected_scheme_key="global",
        output={"crf": 18},
    )
    source["style"]["future_style"] = {"enabled": True}
    source["style"]["title_overlay"]["future_title"] = "keep"
    source["style"]["title_overlay"]["fill"]["future_fill"] = 7
    source["style"]["custom_style_schemes"]["主唱"]["future_scheme"] = {
        "mode": "new"
    }
    source_layouts = {
        item["layout_id"]: item for item in source["style"]["layouts"]
    }
    source_layouts["layout-a"]["future_layout"] = "A"
    source_layouts["layout-b"]["future_layout"] = "B"
    source["page_plan"]["future_plan"] = 1
    source["page_plan"]["sections"][0]["future_section"] = 2
    source["page_plan"]["sections"][0]["pages"][0]["future_page"] = 3
    source["loading_settings"]["future_loading"] = 4
    source["loading_settings_snapshot"]["future_snapshot"] = 5
    source["extra_subtitle_sources"][0]["future_source"] = "A"
    source["extra_subtitle_sources"][1]["future_source"] = "B"
    document.remember_project_data(source)

    document.style = replace(
        document.style,
        font_size_px=96,
        layouts=[document.style.layouts[1], document.style.layouts[0]],
    )
    document.extra_sources.reverse()
    payload = document.to_project_data(
        screen={"width": 1280, "height": 720, "fps": 60, "par": "1:1"},
        selected_scheme_key="global",
        output={"crf": 20},
    )

    assert payload["style"]["font_size_px"] == 96
    assert payload["style"]["future_style"] == {"enabled": True}
    assert payload["style"]["title_overlay"]["future_title"] == "keep"
    assert payload["style"]["title_overlay"]["fill"]["future_fill"] == 7
    assert payload["style"]["custom_style_schemes"]["主唱"]["future_scheme"] == {
        "mode": "new"
    }
    assert [item["future_layout"] for item in payload["style"]["layouts"]] == [
        "B",
        "A",
    ]
    assert payload["page_plan"]["future_plan"] == 1
    assert payload["page_plan"]["sections"][0]["future_section"] == 2
    assert payload["page_plan"]["sections"][0]["pages"][0]["future_page"] == 3
    assert payload["loading_settings"]["future_loading"] == 4
    assert payload["loading_settings_snapshot"]["future_snapshot"] == 5
    assert [
        item["future_source"] for item in payload["extra_subtitle_sources"]
    ] == ["B", "A"]

    document.style = replace(
        document.style,
        layouts=[],
        custom_style_schemes={},
    )
    document.extra_sources = []
    deleted = document.to_project_data(
        screen={}, selected_scheme_key="global", output={}
    )
    assert deleted["style"]["layouts"] == []
    assert deleted["style"]["custom_style_schemes"] == {}
    assert "extra_subtitle_sources" not in deleted


def test_app_style_preferences_do_not_leak_project_only_content():
    app_title_scheme = SubtitleStyleScheme(fill_color="#112233")
    app_title = TitleOverlay(enabled=True, text_template="应用默认标题")
    app_default = Style(
        font_size_px=80,
        line_lead_in_ms=1200,
        custom_style_schemes={TITLE_SCHEME_NAME: app_title_scheme},
        title_overlay=app_title,
    )
    project = Style(
        font_size_px=140,
        line_lead_in_ms=2600,
        custom_style_schemes={
            TITLE_SCHEME_NAME: SubtitleStyleScheme(fill_color="#445566"),
            "主唱": SubtitleStyleScheme(fill_color="#FF0000"),
        },
        singer_style_overrides={1: SubtitleStyleScheme(fill_color="#00FF00")},
        title_overlay=TitleOverlay(enabled=False, text_template="逐曲标题"),
    )

    merged = merge_common_style_preferences(app_default, project)

    assert merged.line_lead_in_ms == 2600
    assert merged.font_size_px == 80
    assert merged.custom_style_schemes == {TITLE_SCHEME_NAME: app_title_scheme}
    assert merged.singer_style_overrides == {}
    assert merged.title_overlay == app_title
    assert "title_overlay" not in app_default_style_to_dict(merged)


def test_app_style_preferences_load_title_habits_without_project_content():
    persisted = Style(
        custom_style_schemes={
            TITLE_SCHEME_NAME: SubtitleStyleScheme(fill_color="#112233"),
            "逐曲角色": SubtitleStyleScheme(fill_color="#FF0000"),
        },
        singer_style_overrides={1: SubtitleStyleScheme(fill_color="#00FF00")},
        title_overlay=TitleOverlay(enabled=False, text_template="不得继承"),
    )

    loaded = load_app_style_preferences(
        {
            "style": style_to_dict(persisted),
            "new_project_defaults": {
                "title_enabled": True,
                "title_layout_name": persisted.layouts[0].name,
                "title_fades": {
                    "fade_in_ms": -20,
                    "tail_fade_in_ms": None,
                },
                "layout_assignment": {"mode": "auto"},
            },
        }
    )

    title = loaded.style.title_overlay
    assert title is not None
    assert title.enabled is True
    assert title.layout_index == 1
    assert title.fade_in_ms == 0
    assert title.tail_fade_in_ms is None
    assert title.text_template == TitleOverlay().text_template
    assert set(loaded.style.custom_style_schemes) == {TITLE_SCHEME_NAME}
    assert loaded.style.singer_style_overrides == {}
    assert loaded.layout_assignment == {"mode": "auto"}
    assert loaded.changed is True


def test_app_output_preferences_are_separate_from_project_output_fields():
    output = update_app_output_preferences(
        {"future_key": "preserved"},
        gpu_preview_enabled=True,
        gpu_preview_default_version=2,
        preview_quality="balanced",
        gpu_export_enabled=False,
        gpu_export_default_version=1,
        directory_mode="custom",
        custom_directory="D:/exports",
        name_template="{source_name}",
        encoder_mode="nvenc",
        codec="hevc",
        preset="slow",
        crf=99,
        render_workers=7,
        allowed_render_workers=(0, 4, 8),
    )

    assert output["future_key"] == "preserved"
    assert output["gpu_preview_enabled"] is True
    assert output["gpu_export_enabled"] is False
    assert output["directory_mode"] == "custom"
    assert output["crf"] == 18
    assert output["render_workers"] == 0
    project_output = {
        "encoder_mode": "nvenc",
        "codec": "hevc",
        "preset": "slow",
        "crf": 18,
        "output_path": "song.mp4",
        "native_export_enabled": False,
    }
    assert APP_LOCAL_ONLY_OUTPUT_FIELDS.isdisjoint(project_output)


def test_style_timing_nested_view_preserves_flat_and_project_compatibility():
    style = Style(
        line_lead_in_ms=1800,
        section_gap_ms=4000,
        entry_anim="fade",
        exit_anim="slide_out",
    )

    timing = style.timing
    assert isinstance(timing, StyleTimingConfig)
    assert timing.line_lead_in_ms == style.line_lead_in_ms
    assert timing.entry_anim == style.entry_anim

    updated = style.with_timing(
        replace(timing, section_gap_ms=5200),
        entry_lead_ms=450,
    )
    assert updated.section_gap_ms == 5200
    assert updated.entry_lead_ms == 450
    assert updated.font_size_px == style.font_size_px

    payload = style_to_dict(updated)
    assert "timing" not in payload
    assert payload["section_gap_ms"] == 5200
    restored = style_from_dict(payload)
    assert restored.timing == updated.timing
    with pytest.raises(TypeError, match="unsupported timing field"):
        style.with_timing(font_size_px=123)


def test_style_default_layout_view_preserves_flat_project_compatibility():
    style = Style(
        line_y_position="top",
        line_y_margin_px=64,
        letter_spacing_px=7,
    )

    layout = style.default_layout
    assert isinstance(layout, subtitle_models.LyricsLayout)
    assert layout.layout_id == "default"
    assert layout.line_y_position == style.line_y_position
    assert layout.letter_spacing_px == style.letter_spacing_px

    updated = style.with_default_layout(
        replace(layout, line_y_margin_px=96, letter_spacing_px=11)
    )
    assert updated.line_y_margin_px == 96
    assert updated.letter_spacing_px == 11
    assert updated.font_size_px == style.font_size_px

    inherited = style.with_default_layout(
        subtitle_models.LyricsLayout(letter_spacing_px=None)
    )
    assert inherited.letter_spacing_px == 7
    payload = style_to_dict(updated)
    assert "default_layout" not in payload
    assert payload["line_y_margin_px"] == 96
    assert style_from_dict(payload).default_layout == updated.default_layout
    with pytest.raises(TypeError, match="unsupported layout field"):
        style.with_default_layout(font_size_px=123)


def test_style_appearance_view_uses_scheme_without_changing_project_schema():
    style = Style(
        font_family="测试字体",
        font_size_px=88,
        fill_color="#123456",
        ruby_font_size_px=40,
        font_family_latin="Latin Font",
    )

    appearance = style.appearance
    assert isinstance(appearance, SubtitleStyleScheme)
    assert appearance.font_family == style.font_family
    assert appearance.fill_color == style.fill_color
    assert "line_y_margin_px" not in STYLE_APPEARANCE_FIELDS

    updated = style.with_appearance(
        replace(
            appearance,
            font_size_px=96,
            fill_color="#654321",
            ruby_font_size_px=44,
        ),
        font_family_latin=None,
    )
    assert updated.font_size_px == 96
    assert updated.fill_color == "#654321"
    assert updated.ruby_font_size_px == 44
    assert updated.font_family_latin is None
    assert updated.line_y_margin_px == style.line_y_margin_px

    payload = style_to_dict(updated)
    assert "appearance" not in payload
    assert payload["fill_color"] == "#654321"
    restored = style_from_dict(payload)
    assert restored.font_size_px == updated.font_size_px
    assert restored.fill_color == updated.fill_color
    assert restored.ruby_font_size_px == updated.ruby_font_size_px
    with pytest.raises(TypeError, match="unsupported appearance field"):
        style.with_appearance(line_y_margin_px=123)


def test_inter_page_overlap_setting_defaults_off_and_round_trips():
    assert style_from_dict({}).allow_inter_page_line_overlap is False

    payload = style_to_dict(Style(allow_inter_page_line_overlap=True))

    assert payload["allow_inter_page_line_overlap"] is True
    assert style_from_dict(payload).allow_inter_page_line_overlap is True


def test_ruby_horizontal_gradient_alignment_setting_round_trips():
    style = Style(
        ruby_horizontal_gradient_with_main=False,
        custom_style_schemes={
            "主唱": SubtitleStyleScheme(
                ruby_horizontal_gradient_with_main=False
            )
        },
    )

    restored = style_from_dict(style_to_dict(style))

    assert restored.ruby_horizontal_gradient_with_main is False
    assert (
        restored.custom_style_schemes["主唱"].ruby_horizontal_gradient_with_main
        is False
    )
    assert style_from_dict({}).ruby_horizontal_gradient_with_main is True


def test_background_payload_round_trip(tmp_path):
    image = tmp_path / "background.png"
    payload = background_payload(
        kind="image", path=image, color="#102030", source_fps=24, video_offset_ms=125
    )
    path = tmp_path / "background.yurika"
    save_render_project(path, {"background": payload})

    assert load_render_project(path)["background"] == {
        "kind": "image",
        "path": str(image),
        "color": "#102030",
        "source_fps": 24,
        "sequence_start_number": 0,
        "video_offset_ms": 125,
    }


def test_load_render_project_rejects_bad_json(tmp_path):
    path = tmp_path / "bad.yurika"
    path.write_text("not json {", encoding="utf-8")
    with pytest.raises(ValueError):
        load_render_project(path)


def test_atomic_project_save_preserves_previous_file_on_replace_failure(
    monkeypatch, tmp_path
):
    path = tmp_path / "atomic.yurika"
    save_render_project(path, {"value": "old"})
    monkeypatch.setattr(
        project_store_module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        save_render_project(path, {"value": "new"})

    assert load_render_project(path)["value"] == "old"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_project_file_revision_detects_content_change_with_same_mtime(tmp_path):
    path = tmp_path / "revision.yurika"
    path.write_text("AAAA", encoding="utf-8")
    original = inspect_project_file(path)

    path.write_text("BBBB", encoding="utf-8")
    os.utime(path, ns=(original.mtime_ns, original.mtime_ns))
    changed = inspect_project_file(path)

    assert changed.mtime_ns == original.mtime_ns
    assert changed.size == original.size
    assert changed.sha256 != original.sha256


def test_manual_project_backups_rotate_to_configured_count(tmp_path):
    project = tmp_path / "song.yurika"
    backup_root = tmp_path / "backups"
    for version in range(4):
        save_render_project(project, {"version": version})
        backup_project_file(project, backup_root, max_count=2)

    backups = list(backup_root.rglob("*.manual-backup.yurika"))
    assert len(backups) == 2
    assert {load_render_project(path)["version"] for path in backups} == {2, 3}


def test_discarded_backup_is_labelled_with_source_and_retention(tmp_path):
    source = tmp_path / "song.yurika"
    backup = save_discarded_project_backup(
        tmp_path / "backups",
        {"value": "unsaved"},
        source_project_path=source,
        retention_days=7,
    )

    restored = load_render_project(backup)
    assert ".discarded-backup.yurika" in backup.name
    assert restored["value"] == "unsaved"
    assert restored["backup"]["kind"] == "discarded_changes"
    assert restored["backup"]["source_project_path"] == str(source)
    assert restored["backup"]["retention_days"] == 7


def test_discard_unsaved_creates_emergency_backup_before_clearing_dirty(
    qapp, monkeypatch, tmp_path
):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)
    win._project_path = tmp_path / "discard-me.yurika"
    win._style = Style(font_size_px=88)
    win._mark_project_dirty()

    win.discard_unsaved()

    backups = list((tmp_path / "settings" / "subtitle_render_backups").rglob(
        "*.discarded-backup.yurika"
    ))
    assert len(backups) == 1
    assert load_render_project(backups[0])["style"]["font_size_px"] == 88
    assert win.has_unsaved_changes() is False


def test_recovery_writer_rejects_an_older_snapshot(tmp_path):
    path = tmp_path / "song.yurika.recovery"
    newer = {
        "value": "newer",
        "recovery": {
            "created_at_unix": 2.0,
            "snapshot_id": 2,
            "source_project_path": None,
        },
    }
    older = {
        "value": "older",
        "recovery": {
            "created_at_unix": 1.0,
            "snapshot_id": 1,
            "source_project_path": None,
        },
    }

    assert save_recovery_project(path, newer) is True
    assert save_recovery_project(path, older) is False
    assert load_render_project(path)["value"] == "newer"


def test_recovery_invalidation_blocks_inflight_old_snapshot(tmp_path):
    path = tmp_path / "discarded.yurika.recovery"
    invalidate_recovery_project(path, snapshot_floor=10)

    written = save_recovery_project(
        path,
        {
            "recovery": {
                "created_at_unix": 1.0,
                "snapshot_id": 9,
                "source_project_path": None,
            }
        },
    )

    assert written is False
    assert not path.exists()


def test_recovery_scan_separates_valid_stale_and_invalid_files(tmp_path):
    root = tmp_path / "recovery"
    source = tmp_path / "saved.yurika"
    stale = root / "saved.yurika.stale.recovery"
    valid = root / "untitled.yurika.recovery"
    invalid = root / "broken.yurika.recovery"
    save_recovery_project(
        stale,
        {
            "recovery": {
                "created_at_unix": 1.0,
                "snapshot_id": 1,
                "source_project_path": str(source),
            }
        },
    )
    save_render_project(source, {"saved": True})
    save_recovery_project(
        valid,
        {
            "recovery": {
                "created_at_unix": 2.0,
                "snapshot_id": 2,
                "source_project_path": None,
            }
        },
    )
    invalid.write_text("broken", encoding="utf-8")

    candidates, invalid_files, stale_files = scan_recovery_projects(root)

    assert [candidate.path for candidate in candidates] == [valid]
    assert invalid_files == [invalid]
    assert stale_files == [stale]


def test_project_bar_present_in_both_modes(qapp, monkeypatch):
    # 项目命令栏与快捷键在 standalone 与嵌入模式下都提供。
    standalone = _make_window(qapp, monkeypatch)
    assert standalone._project_bar is not None
    assert hasattr(standalone, "_project_shortcuts")

    embedded = mw.SubtitleRenderWindow(embedded=True)
    assert embedded._project_bar is not None
    assert hasattr(embedded, "_project_shortcuts")


def test_recent_projects_refreshes_only_its_submenu(qapp, monkeypatch, tmp_path):
    project = tmp_path / "recent.yurika"
    save_render_project(project, {})
    win = _make_window(qapp, monkeypatch)
    file_menu = win._file_menu_btn.menu()
    recent_menu = win._recent_projects_menu
    project_bar = win._project_bar

    win._record_recent_project(project)

    assert win._file_menu_btn.menu() is file_menu
    assert win._recent_projects_menu is recent_menu
    assert win._project_bar is project_bar
    assert win._recent_project_paths == [str(project.absolute())]
    assert recent_menu.actions()[0].toolTip() == str(project.absolute())

    rebuilds = []
    monkeypatch.setattr(
        win,
        "_rebuild_recent_projects_menu",
        lambda: rebuilds.append("recent-menu"),
    )
    win._set_recent_projects(list(win._recent_project_paths))
    assert rebuilds == []
    win._set_recent_projects([])
    assert rebuilds == ["recent-menu"]


def test_recent_projects_persist_prune_and_clear(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    valid = tmp_path / "valid.yurika"
    missing = tmp_path / "missing.yurika"
    foreign = tmp_path / "import.n3proj"
    save_render_project(valid, {})
    foreign.write_text("not a native project", encoding="utf-8")

    settings = mw.load_app_settings()
    settings.subtitle_render = {
        "recent_projects": [str(missing), str(foreign), str(valid), str(valid)]
    }
    mw.save_app_settings(settings, merge_module_namespaces=False)

    win = _make_window(qapp, monkeypatch)
    assert win._recent_project_paths == [str(valid.absolute())]
    assert mw.load_app_settings().subtitle_render["recent_projects"] == [
        str(valid.absolute())
    ]

    win._clear_recent_projects()
    assert win._recent_project_paths == []
    assert mw.load_app_settings().subtitle_render["recent_projects"] == []


def test_successful_open_records_recent_project(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    project = tmp_path / "opened.yurika"
    save_render_project(project, {})

    assert win._open_project_path(project, confirm_discard=False)
    assert win._recent_project_paths[0] == str(project.absolute())

    n3_project = tmp_path / "imported.n3proj"
    monkeypatch.setattr(
        win,
        "_import_n3_project_path",
        lambda path, confirm_discard=True: True,
    )
    assert win._open_project_path(n3_project, confirm_discard=False)
    assert str(n3_project.absolute()) not in win._recent_project_paths


def test_save_and_save_as_record_recent_project(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)

    # 新项目首次保存（保存/另存为共用 _write_project）即进入最近列表
    first = tmp_path / "song.yurika"
    assert win._write_project(first) is True
    assert win._recent_project_paths[0] == str(first.absolute())

    # 另存为新路径：新路径置顶，旧路径保留
    second = tmp_path / "song-renamed.yurika"
    assert win._write_project(second) is True
    assert win._recent_project_paths[0] == str(second.absolute())
    assert str(first.absolute()) in win._recent_project_paths

    # 保存失败（父路径是个文件）不得记录
    blocker = tmp_path / "blocker"
    blocker.write_text("占位", encoding="utf-8")
    broken = blocker / "broken.yurika"
    assert win._write_project(broken) is False
    assert str(broken.absolute()) not in win._recent_project_paths


def test_window_save_new_open_round_trip(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    default_font_size = win._app_default_style.font_size_px

    # 改样式 → 标脏
    win._style = Style(font_size_px=88, title_overlay=TitleOverlay(enabled=True))
    win._property_panel.set_style(win._style)
    win._export_crf_spin.setValue(23)
    win._export_native_check.setChecked(True)
    win._mark_project_dirty()
    assert win._project_dirty is True

    # 保存
    path = tmp_path / "song.yurika"
    assert win._write_project(path) is True
    assert win._project_dirty is False
    assert win._project_path == path

    # 新建重置为默认
    win._new_project()
    assert win._style.font_size_px == default_font_size
    assert win._project_path is None
    assert win._project_dirty is False

    # 打开恢复
    data = load_render_project(path)
    win._apply_project_data(data)
    assert win._style.font_size_px == 88
    assert win._style.title_overlay is not None and win._style.title_overlay.enabled
    assert win._export_crf_spin.value() == 23
    assert win._export_native_check.isChecked() is False
    # 加载过程中不应把项目标脏
    assert win._project_dirty is False


def test_window_open_edit_save_preserves_future_project_fields(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    future_style = style_to_dict(
        Style(
            font_size_px=87,
            layouts=[subtitle_models.LyricsLayout(name="未来布局", layout_id="future")],
            custom_style_schemes={
                "主唱": SubtitleStyleScheme(fill_color="#123456")
            },
            title_overlay=TitleOverlay(enabled=True),
        )
    )
    future_style["future_style"] = {"version": 2}
    future_style["title_overlay"]["future_title"] = True
    future_style["custom_style_schemes"]["主唱"]["future_scheme"] = 7
    future_style["layouts"][0]["future_layout"] = "keep"
    win._apply_project_data(
        {
            "schema_version": 99,
            "future_section": {"mode": "new"},
            "style": future_style,
            "screen": {
                "width": 1280,
                "height": 720,
                "fps": 60,
                "par": "1:1",
                "future_screen": "keep",
            },
            "output": {"crf": 21, "future_output": [1, 2]},
            "background": {
                "kind": "solid",
                "color": "#010203",
                "future_background": True,
            },
        }
    )
    win._style = replace(win._style, font_size_px=91)

    path = tmp_path / "future.yurika"
    assert win._write_project(path) is True
    saved = load_render_project(path)

    assert saved["schema_version"] == PROJECT_SCHEMA_VERSION
    assert saved["future_section"] == {"mode": "new"}
    assert saved["style"]["font_size_px"] == 91
    assert saved["style"]["future_style"] == {"version": 2}
    assert saved["style"]["title_overlay"]["future_title"] is True
    assert saved["style"]["custom_style_schemes"]["主唱"]["future_scheme"] == 7
    assert saved["style"]["layouts"][0]["future_layout"] == "keep"
    assert saved["screen"]["future_screen"] == "keep"
    assert saved["output"]["future_output"] == [1, 2]
    assert saved["background"]["future_background"] is True

    win._new_project()
    assert "future_section" not in win._current_project_data()


def test_new_project_reuses_last_export_settings_and_keeps_fixed_directory(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    fixed_dir = tmp_path / "fixed-exports"
    win._set_export_directory_settings(
        mw.EXPORT_DIR_CUSTOM,
        str(fixed_dir),
        persist=False,
    )
    win._export_name_edit.setText("旧项目输出")
    win._export_encoder_combo.setCurrentIndex(
        win._export_encoder_combo.findData(mw.ENCODER_NVENC)
    )
    win._export_codec_combo.setCurrentIndex(
        win._export_codec_combo.findData(mw.CODEC_HEVC)
    )
    win._export_preset_combo.setCurrentIndex(
        win._export_preset_combo.findData("slow")
    )
    win._export_crf_spin.setValue(27)
    win._export_render_workers_combo.setCurrentIndex(
        win._export_render_workers_combo.findData(16)
    )
    win._project_dirty = False

    win._new_project()

    assert win._export_dir_mode == mw.EXPORT_DIR_CUSTOM
    assert win._export_custom_dir == str(fixed_dir)
    assert win._export_dir_edit.text() == str(fixed_dir)
    assert win._export_name_edit.text() == "subtitle_render_yurika出力"
    assert win._export_auto_name == "subtitle_render_yurika出力"
    assert win._export_encoder_combo.currentData() == mw.ENCODER_NVENC
    assert win._export_codec_combo.currentData() == mw.CODEC_HEVC
    assert win._export_preset_combo.currentData() == "slow"
    assert win._export_crf_spin.value() == 27
    assert win._export_render_workers_combo.currentData() == 16
    assert (win._export_width_spin.value(), win._export_height_spin.value()) == (
        1920,
        1080,
    )
    assert win._export_fps_value() == 60
    assert win._project_dirty is False


def test_new_project_clears_old_source_video_export_directory(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    old_video = tmp_path / "old-project" / "source.mp4"
    win._video_path = old_video
    win._set_export_directory_settings(
        mw.EXPORT_DIR_SOURCE_VIDEO,
        "",
        persist=False,
    )
    assert win._export_dir_edit.text() == str(old_video.parent)
    win._project_dirty = False

    win._new_project()

    assert win._export_dir_mode == mw.EXPORT_DIR_SOURCE_VIDEO
    assert win._export_dir_edit.text() == str(Path.cwd())
    assert win._export_name_edit.text() == "subtitle_render_yurika出力"


def test_export_location_dialog_offers_source_and_custom_modes(qapp, tmp_path):
    dialog = mw._ExportLocationDialog(
        mw.EXPORT_DIR_SOURCE_VIDEO,
        "",
        tmp_path,
    )
    assert dialog.source_radio.text() == "保存在字幕视频所在目录"
    assert dialog.custom_radio.text() == "保存在指定目录"
    assert dialog.source_radio.isChecked()
    assert not dialog.directory_edit.isEnabled()

    dialog.custom_radio.setChecked(True)
    assert dialog.directory_edit.isEnabled()
    assert not dialog.ok_button.isEnabled()
    dialog.directory_edit.setText(str(tmp_path / "exports"))
    dialog._sync_controls()
    assert dialog.ok_button.isEnabled()
    assert dialog.selection() == (mw.EXPORT_DIR_CUSTOM, str(tmp_path / "exports"))


def test_export_location_preference_persists_and_overrides_project_path(
    qapp, monkeypatch, tmp_path
):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    custom_dir = tmp_path / "exports"
    win = _make_window(qapp, monkeypatch)
    assert (
        win._export_location_settings_button.toolTip()
        == "导出视频位置与默认文件名设置"
    )

    win._set_export_directory_settings(
        mw.EXPORT_DIR_CUSTOM,
        str(custom_dir),
        persist=True,
    )
    assert win._export_dir_edit.text() == str(custom_dir)
    win._apply_output_settings(
        {"output_path": str(tmp_path / "old-project" / "旧文件名.mp4")}
    )
    assert win._export_dir_edit.text() == str(custom_dir)
    assert win._export_name_edit.text() == "旧文件名"

    restored = _make_window(qapp, monkeypatch)
    assert restored._export_dir_mode == mw.EXPORT_DIR_CUSTOM
    assert restored._export_custom_dir == str(custom_dir)
    assert restored._export_dir_edit.text() == str(custom_dir)

    source_video = tmp_path / "video" / "background.mp4"
    restored._video_path = source_video
    restored._set_export_directory_settings(
        mw.EXPORT_DIR_SOURCE_VIDEO,
        str(custom_dir),
        persist=False,
    )
    assert restored._export_dir_edit.text() == str(source_video.parent)


def test_title_text_is_isolated_between_yurika_projects(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    first_path = tmp_path / "first.yurika"
    second_path = tmp_path / "second.yurika"

    first_style = replace(
        win._style,
        title_overlay=TitleOverlay(enabled=True, text_template="第一个项目"),
    )
    win._style = first_style
    win._property_panel.set_style(first_style)
    assert win._write_project(first_path) is True

    second_style = replace(
        win._style,
        title_overlay=TitleOverlay(enabled=True, text_template="第二个项目"),
    )
    win._style = second_style
    win._property_panel.set_style(second_style)
    assert win._write_project(second_path) is True

    win._apply_project_data(load_render_project(first_path))
    assert win._style.title_overlay is not None
    assert win._style.title_overlay.text_template == "第一个项目"

    win._apply_project_data(load_render_project(second_path))
    assert win._style.title_overlay is not None
    assert win._style.title_overlay.text_template == "第二个项目"


def test_open_project_clears_previous_media_before_applying_snapshot(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    win._timing_track = TimingTrack(lines=[TimingLine()])
    win._subtitle_path = tmp_path / "old.lrc"
    win._video_path = tmp_path / "old.mp4"
    win._audio_path = tmp_path / "old.wav"
    win._background_source = BackgroundSource(
        kind="video", path=str(tmp_path / "old.mp4")
    )
    win._extra_sources = [
        mw.ExtraSubtitleSource(
            name="旧副字幕",
            path=tmp_path / "old-extra.lrc",
            track=TimingTrack(lines=[TimingLine()]),
        )
    ]

    project_path = tmp_path / "clean.yurika"
    save_render_project(project_path, {"style": style_to_dict(Style())})
    monkeypatch.setattr(
        mw.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(project_path), ""),
    )

    win._open_project()

    assert win._timing_track is None
    assert win._subtitle_path is None
    assert win._video_path is None
    assert win._audio_path is None
    assert win._background_source is None
    assert win._extra_sources == []


@pytest.mark.parametrize(
    "panel_name",
    ["_lyrics_panel", "_preview_panel", "_video_settings_panel"],
)
def test_yurika_is_accepted_by_both_drop_regions(
    qapp, monkeypatch, tmp_path, panel_name
):
    win = _make_window(qapp, monkeypatch)
    project_path = tmp_path / "drop.yurika"
    save_render_project(project_path, {"style": style_to_dict(Style())})

    assert getattr(win, panel_name).accepts(project_path) is True


@pytest.mark.parametrize(
    "panel_name",
    ["_lyrics_panel", "_preview_panel", "_video_settings_panel"],
)
def test_n3proj_is_accepted_by_all_drop_regions(
    qapp, monkeypatch, tmp_path, panel_name
):
    win = _make_window(qapp, monkeypatch)
    project_path = tmp_path / "drop.n3proj"
    project_path.write_bytes(b"fake")

    assert getattr(win, panel_name).accepts(project_path) is True


@pytest.mark.parametrize(
    "panel_name",
    ["_lyrics_panel", "_preview_panel", "_video_settings_panel"],
)
def test_dropped_yurika_opens_complete_project_like_file_menu(
    qapp, monkeypatch, tmp_path, panel_name
):
    win = _make_window(qapp, monkeypatch)
    project_path = tmp_path / f"{panel_name}.yurika"
    save_render_project(
        project_path,
        {"style": style_to_dict(Style(font_size_px=91))},
    )

    getattr(win, panel_name).pathDropped.emit(project_path)

    assert win._project_path == project_path
    assert win._style.font_size_px == 91
    assert win._project_dirty is False


@pytest.mark.parametrize(
    "panel_name",
    ["_lyrics_panel", "_preview_panel", "_video_settings_panel"],
)
def test_dropped_n3proj_imports_complete_project_like_file_menu(
    qapp, monkeypatch, tmp_path, panel_name
):
    win = _make_window(qapp, monkeypatch)
    project_path = tmp_path / f"{panel_name}.n3proj"
    project_path.write_bytes(b"fake")

    class Result:
        project_data = {"style": style_to_dict(Style(font_size_px=91))}
        warnings = []

    loaded_paths: list[Path] = []

    def fake_load(path):
        loaded_paths.append(path)
        return Result()

    monkeypatch.setattr(mw, "load_n3proj", fake_load)
    monkeypatch.setattr(mw.InfoBar, "success", lambda **kwargs: None)

    getattr(win, panel_name).pathDropped.emit(project_path)

    assert loaded_paths == [project_path]
    assert win._project_path is None
    assert win._style.font_size_px == 91
    assert win._project_dirty is True


def test_dropped_yurika_respects_unsaved_changes_confirmation(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    current_path = tmp_path / "current.yurika"
    dropped_path = tmp_path / "dropped.yurika"
    save_render_project(
        dropped_path,
        {"style": style_to_dict(Style(font_size_px=91))},
    )
    win._project_path = current_path
    win._style = Style(font_size_px=77)
    win._project_dirty = True
    monkeypatch.setattr(win, "_confirm_discard_changes", lambda: False)

    win._lyrics_panel.pathDropped.emit(dropped_path)

    assert win._project_path == current_path
    assert win._style.font_size_px == 77
    assert win._project_dirty is True


def test_dropped_n3proj_respects_unsaved_changes_confirmation(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    project_path = tmp_path / "dropped.n3proj"
    project_path.write_bytes(b"fake")
    win._style = Style(font_size_px=77)
    win._project_dirty = True
    monkeypatch.setattr(win, "_confirm_discard_changes", lambda: False)
    load_calls: list[Path] = []
    monkeypatch.setattr(mw, "load_n3proj", lambda path: load_calls.append(path))

    win._lyrics_panel.pathDropped.emit(project_path)

    assert load_calls == []
    assert win._style.font_size_px == 77
    assert win._project_dirty is True


def test_open_project_summarizes_all_missing_resources_once(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    project_path = tmp_path / "missing-assets.yurika"
    main_subtitle = tmp_path / "missing-main.sug"
    background = tmp_path / "missing-background.mp4"
    audio = tmp_path / "missing-audio.flac"
    chorus = tmp_path / "missing-chorus.lrc"
    save_render_project(
        project_path,
        {
            "subtitle_path": str(main_subtitle),
            "audio_path": str(audio),
            "background": {
                "kind": "video",
                "path": str(background),
                "color": "#000000",
            },
            "extra_subtitle_sources": [
                {"name": "和声", "path": str(chorus)},
            ],
            "style": style_to_dict(Style()),
        },
    )
    monkeypatch.setattr(
        mw.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(project_path), ""),
    )
    warnings: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        mw,
        "fluent_warning",
        lambda *args, **kwargs: warnings.append((args, kwargs)),
    )

    win._open_project()

    assert len(warnings) == 1
    args, kwargs = warnings[0]
    assert args[1] == "项目已打开，但部分素材未找到"
    assert "主字幕" in args[2] and str(main_subtitle) in args[2]
    assert "背景视频" in args[2] and str(background) in args[2]
    assert "独立音频" in args[2] and str(audio) in args[2]
    assert "副字幕「和声」" in args[2] and str(chorus) in args[2]
    assert kwargs == {"copyable": True}


def test_missing_resource_scan_tolerates_invalid_image_sequence_metadata(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    pattern = tmp_path / "frame_%04d.png"

    missing = win._missing_project_resources(
        {
            "background": {
                "kind": "image_sequence",
                "path": str(pattern),
                "source_fps": "invalid",
                "sequence_start_number": "invalid",
            }
        }
    )

    assert missing == [("背景图片序列", pattern)]


@pytest.mark.parametrize(
    ("choice", "save_result", "expected", "save_calls"),
    [
        (0, True, True, 1),
        (0, False, False, 1),
        (1, True, True, 0),
        (2, True, False, 0),
        (-1, True, False, 0),
    ],
)
def test_unsaved_project_uses_fluent_save_discard_cancel_confirmation(
    qapp, monkeypatch, choice, save_result, expected, save_calls
):
    win = _make_window(qapp, monkeypatch)
    win._project_dirty = True
    captured: dict[str, object] = {}

    def fake_choice(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return choice

    calls: list[bool] = []
    monkeypatch.setattr(mw, "fluent_choice", fake_choice)
    monkeypatch.setattr(
        win, "_save_project", lambda: calls.append(True) or save_result
    )

    assert win._confirm_discard_changes() is expected
    assert len(calls) == save_calls
    assert captured["args"][3] == ["保存", "放弃", "取消"]
    assert captured["kwargs"] == {"default": 2}


def _capture_question(monkeypatch, answer: bool) -> dict:
    """把导出前的保存确认换成假的，记下文案与按钮。"""
    captured: dict = {}

    def fake_question(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return answer

    monkeypatch.setattr(mw, "fluent_question", fake_question)
    return captured


def test_export_does_not_ask_when_the_project_is_already_saved(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    win._project_path = tmp_path / "demo.yurika"
    win._project_dirty = False
    captured = _capture_question(monkeypatch, False)

    assert win._confirm_project_saved_before_export() is True
    assert captured == {}


@pytest.mark.parametrize("save_result", [True, False])
def test_export_saves_a_never_saved_project_first(qapp, monkeypatch, save_result):
    """从未保存过 → 弹窗只给「保存并导出」和「取消导出」两条路。"""
    win = _make_window(qapp, monkeypatch)
    win._project_path = None
    win._project_dirty = True
    captured = _capture_question(monkeypatch, True)
    calls: list[bool] = []
    monkeypatch.setattr(win, "_save_project", lambda: calls.append(True) or save_result)

    assert win._confirm_project_saved_before_export() is save_result
    assert calls == [True]
    assert ".yurika" in captured["args"][2]
    assert captured["kwargs"] == {
        "yes_text": "保存并导出",
        "no_text": "取消导出",
        "default_cancel": True,
    }


def test_export_asks_again_when_a_saved_project_has_new_changes(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    win._project_path = tmp_path / "demo.yurika"
    win._project_dirty = True
    captured = _capture_question(monkeypatch, True)
    monkeypatch.setattr(win, "_save_project", lambda: True)

    assert win._confirm_project_saved_before_export() is True
    assert "未保存的改动" in captured["args"][2]


def test_cancelling_the_save_prompt_cancels_the_export(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    win._project_path = None
    win._project_dirty = True
    _capture_question(monkeypatch, False)
    calls: list[bool] = []
    monkeypatch.setattr(win, "_save_project", lambda: calls.append(True) or True)

    assert win._confirm_project_saved_before_export() is False
    assert calls == []


def test_export_thread_never_starts_when_the_save_prompt_is_cancelled(qapp, monkeypatch):
    """光有确认还不够 —— 取消之后导出线程一定不能起来。"""
    win = _make_window(qapp, monkeypatch)
    monkeypatch.setattr(win, "_build_render_job", lambda: object())
    monkeypatch.setattr(win, "_confirm_project_saved_before_export", lambda: False)

    win._start_render_export()

    assert win._render_thread is None
    assert win._export_start_button.isEnabled()


def test_n3_import_warnings_use_copyable_fluent_dialog(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    monkeypatch.setattr(
        mw.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: ("example.n3proj", ""),
    )

    class Result:
        project_data = {}
        warnings = ["输出格式已改为 MP4", "歌词间隔使用默认布局"]

    monkeypatch.setattr(mw, "load_n3proj", lambda _path: Result())
    monkeypatch.setattr(win, "_clear_loaded_media", lambda: None)
    monkeypatch.setattr(win, "_apply_project_data", lambda _data: None)
    monkeypatch.setattr(win, "_refresh_project_title", lambda: None)
    captured: dict[str, object] = {}

    def capture(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(mw, "fluent_info", capture)

    win._import_n3_project()

    assert captured["args"][1] == "导入完成（部分设置需注意）"
    assert captured["args"][2] == (
        "已导入 N3 项目，以下内容请检查：\n\n"
        "• 输出格式已改为 MP4\n"
        "• 歌词间隔使用默认布局"
    )
    assert captured["kwargs"] == {"copyable": True}


def test_apply_project_data_does_not_mark_dirty(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    win._project_dirty = False
    win._apply_project_data({"style": {"font_size_px": 64}})
    assert win._project_dirty is False
    assert win._style.font_size_px == 64


def test_startup_scheme_restore_does_not_mark_project_dirty(
    qapp, monkeypatch, tmp_path
):
    """构造期按持久化偏好同步方案下拉框不得把空项目标脏。

    set_current_scheme_key 会触发 currentIndexChanged，与用户手动切换方案
    走同一条 schemeSelectionChanged 链；未受加载守卫保护时，启动 2 秒后
    自动保存就会写出 untitled 恢复快照，下次启动再弹"检测到未保存的
    项目"，形成每次启动都要放弃一次的死循环。
    """
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps({"subtitle_render": {"selected_scheme_key": "custom:标题"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(settings_dir))
    win = _make_window(qapp, monkeypatch)

    assert win._selected_scheme_key == "custom:标题"
    assert win.has_unsaved_changes() is False
    assert not win._auto_save_timer.isActive()

    # 用户手动切回全局方案仍应正常标脏。
    win._property_panel.set_current_scheme_key("global")
    assert win.has_unsaved_changes() is True
    win.close()


def test_public_project_state_tracks_dirty_save_and_discard(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    states = []
    win.projectStateChanged.connect(states.append)
    win._project_path = tmp_path / "song.yurika"

    win._mark_project_dirty()

    assert win.has_unsaved_changes() is True
    assert states[-1].display_name == "song.yurika"
    assert states[-1].dirty is True
    assert states[-1].status_text() == "song.yurika · 未保存"

    win.discard_unsaved()

    assert win.has_unsaved_changes() is False
    assert states[-1].dirty is False
    assert states[-1].status_text() == "song.yurika"


def test_screen_and_encoder_project_fields_mark_dirty(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)

    win._set_project_dirty(False)
    win._export_width_spin.setValue(win._export_width_spin.value() + 2)
    assert win.has_unsaved_changes() is True

    win._set_project_dirty(False)
    win._export_fps_combo.setCurrentIndex(
        (win._export_fps_combo.currentIndex() + 1) % win._export_fps_combo.count()
    )
    assert win.has_unsaved_changes() is True

    win._set_project_dirty(False)
    win._export_crf_spin.setValue(win._export_crf_spin.value() + 1)
    assert win.has_unsaved_changes() is True

    win._set_project_dirty(False)
    win._export_codec_combo.setCurrentIndex(
        (win._export_codec_combo.currentIndex() + 1) % win._export_codec_combo.count()
    )
    assert win.has_unsaved_changes() is True

    win._set_project_dirty(False)
    win._export_encoder_combo.setCurrentIndex(
        (win._export_encoder_combo.currentIndex() + 1)
        % win._export_encoder_combo.count()
    )
    assert win.has_unsaved_changes() is True

    win._set_project_dirty(False)
    win._export_preset_combo.setCurrentIndex(
        (win._export_preset_combo.currentIndex() + 1)
        % win._export_preset_combo.count()
    )
    assert win.has_unsaved_changes() is True

    win._set_project_dirty(False)
    win._export_name_edit.textEdited.emit("custom-output")
    assert win.has_unsaved_changes() is True


def test_force_exit_flush_writes_subtitle_recovery(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)
    win._project_path = tmp_path / "song.yurika"
    win._mark_project_dirty()

    win.flush_unsaved()

    state = win.project_state()
    assert state.recovery_path is not None
    assert state.recovery_path.is_file()
    recovered = load_render_project(state.recovery_path)
    assert recovered["recovery"]["source_project_path"] == str(win._project_path)
    assert recovered["recovery"]["snapshot_id"] > 0


def test_background_auto_save_uses_snapshot_and_keeps_project_dirty(
    qapp, monkeypatch, tmp_path
):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)
    win._project_path = tmp_path / "song.yurika"
    win._mark_project_dirty()
    assert win._auto_save_timer.isActive()
    win._auto_save_timer.stop()

    win._start_recovery_auto_save()
    thread = win._auto_save_thread
    assert thread is not None
    assert thread.wait(3000)
    qapp.processEvents()

    state = win.project_state()
    assert state.dirty is True
    assert state.recovery_path is not None
    recovered = load_render_project(state.recovery_path)
    assert recovered["recovery"]["project_revision"] == win._project_revision
    win.discard_unsaved()
    win.close()


def test_auto_save_configuration_persists(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)

    win._configure_auto_save(False, 7, backup_count=9, persist=True)
    restored = _make_window(qapp, monkeypatch)

    assert restored._auto_save_enabled is False
    assert restored._auto_save_interval_minutes == 7
    assert restored._project_backup_count == 9
    assert not restored._periodic_auto_save_timer.isActive()
    win.close()
    restored.close()


def test_crash_recovery_restores_dirty_project(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    recovery_root = tmp_path / "settings" / "subtitle_render_recovery"
    recovery_path = recovery_root / "untitled.yurika.recovery"
    save_recovery_project(
        recovery_path,
        {
            "style": style_to_dict(Style(font_size_px=93)),
            "recovery": {
                "created_at_unix": 2.0,
                "snapshot_id": 2,
                "source_project_path": None,
            },
        },
    )
    win = _make_window(qapp, monkeypatch)
    monkeypatch.setattr(mw, "fluent_choice", lambda *_args, **_kwargs: 0)

    assert win.has_pending_crash_recovery() is True
    assert win.check_crash_recovery(dialog_parent=win) is True

    assert win._style.font_size_px == 93
    assert win._project_path is None
    assert win.has_unsaved_changes() is True
    assert recovery_path.is_file()
    win.discard_unsaved()
    win.close()


def test_save_failure_keeps_dirty_and_reports_state(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    win._project_path = tmp_path / "song.yurika"
    win._mark_project_dirty()
    monkeypatch.setattr(
        mw,
        "save_render_project",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert win.trigger_save() is False

    state = win.project_state()
    assert state.dirty is True
    assert state.saving is False
    assert state.save_error == "disk full"
    assert state.status_text() == "song.yurika · 保存失败"


def test_save_failure_dialog_exposes_copyable_path_and_reason(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    path = tmp_path / "copyable-error.yurika"
    win._project_path = path
    win._mark_project_dirty()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mw,
        "save_render_project",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(
        mw,
        "fluent_error",
        lambda *args, **kwargs: captured.update(args=args, kwargs=kwargs),
    )

    assert win.trigger_save() is False

    assert str(path) in captured["args"][2]
    assert "disk full" in captured["args"][2]
    assert captured["kwargs"] == {"copyable": True}


def test_direct_n3_import_uses_video_resolution_and_rebases_style_without_scaling(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    video = tmp_path / "4k.mp4"
    video.write_bytes(b"fake")
    monkeypatch.setattr(
        mw.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(tmp_path / "demo.n3proj"), ""),
    )
    monkeypatch.setattr(
        mw.SubtitleRenderWindow,
        "_probe",
        lambda self, path, label: mw.MediaInfo(
            path=path,
            duration=10.0,
            video_streams=1,
            audio_streams=0,
            subtitle_streams=0,
            video_width=3840,
            video_height=2160,
            video_fps=60.0,
        ),
    )

    class Result:
        project_data = {
            "style": style_to_dict(
                Style(
                    font_size_px=100,
                    stroke_width_px=10,
                    line_gap_px=90,
                    font_reference_height=1080,
                    layout_reference_height=1080,
                    custom_style_schemes={
                        "瑞": SubtitleStyleScheme(
                            font_size_px=100,
                            stroke_width_px=10,
                        )
                    },
                )
            ),
            "screen": {"width": 1920, "height": 1080, "fps": 60, "par": "1:1"},
            "selected_scheme_key": "custom:瑞",
            "project_role_names": ["瑞"],
            "video_path": str(video),
            "background": {
                "kind": "video",
                "path": str(video),
                "color": "#000000",
                "source_fps": None,
                "sequence_start_number": 0,
                "video_offset_ms": 0,
            },
        }
        warnings = []

    monkeypatch.setattr(mw, "load_n3proj", lambda _path: Result())
    monkeypatch.setattr(mw.InfoBar, "success", lambda **kwargs: None)

    win._import_n3_project()

    assert (win._screen_settings.width, win._screen_settings.height) == (3840, 2160)
    assert (win._export_width_spin.value(), win._export_height_spin.value()) == (
        3840,
        2160,
    )
    assert win._preview_panel.canvas._output_width == 3840
    assert win._preview_panel.canvas._output_height == 2160
    assert win._style.font_reference_height == 2160
    assert win._style.font_size_px == 100
    assert win._style.stroke_width_px == 10
    assert win._style.layout_reference_height == 2160
    assert win._style.line_gap_px == 90
    assert win._style.custom_style_schemes["瑞"].font_size_px == 100
    win._property_panel.set_current_scheme_key("custom:瑞")
    assert win._property_panel.current_scheme_key() == "custom:瑞"
    assert win._property_panel._font_size_spin.value() == 100


def test_external_project_change_requires_explicit_conflict_choice(
    qapp, monkeypatch, tmp_path
):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    project = tmp_path / "conflict.yurika"
    save_render_project(project, {"value": "opened", "style": style_to_dict(Style())})
    win = _make_window(qapp, monkeypatch)
    assert win._open_project_path(project)
    win._mark_project_dirty()
    save_render_project(project, {"value": "external", "style": style_to_dict(Style())})
    choices: list[tuple] = []

    def cancel_conflict(*args, **kwargs):
        choices.append((args, kwargs))
        return 2

    monkeypatch.setattr(mw, "fluent_choice", cancel_conflict)

    assert win.trigger_save() is False
    assert load_render_project(project)["value"] == "external"
    assert choices[0][0][3] == ("覆盖", "另存为", "取消")
    assert choices[0][1] == {"default": 2}
    assert win.has_unsaved_changes() is True


def test_overwriting_external_change_keeps_that_revision_as_backup(
    qapp, monkeypatch, tmp_path
):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    project = tmp_path / "overwrite.yurika"
    save_render_project(project, {"value": "opened", "style": style_to_dict(Style())})
    win = _make_window(qapp, monkeypatch)
    assert win._open_project_path(project)
    win._style = Style(font_size_px=97)
    win._property_panel.set_style(win._style)
    win._mark_project_dirty()
    save_render_project(project, {"value": "external", "style": style_to_dict(Style())})
    monkeypatch.setattr(mw, "fluent_choice", lambda *_args, **_kwargs: 0)

    assert win.trigger_save() is True

    backups = list((tmp_path / "settings" / "subtitle_render_backups").rglob(
        "*.manual-backup.yurika"
    ))
    assert len(backups) == 1
    assert load_render_project(backups[0])["value"] == "external"
    assert load_render_project(project)["style"]["font_size_px"] == 97


def test_save_action_and_missing_asset_status_share_project_state(
    qapp, monkeypatch, tmp_path
):
    project = tmp_path / "missing-state.yurika"
    subtitle = tmp_path / "later.lrc"
    save_render_project(
        project,
        {"subtitle_path": str(subtitle), "style": style_to_dict(Style())},
    )
    win = _make_window(qapp, monkeypatch)
    monkeypatch.setattr(mw.InfoBar, "success", lambda **_kwargs: None)
    monkeypatch.setattr(mw.InfoBar, "info", lambda **_kwargs: None)

    assert win._open_project_path(project)
    state = win.project_state()
    assert state.dirty is False
    assert state.missing_resources == (("主字幕", subtitle),)
    assert state.status_text() == "missing-state.yurika · 素材缺失 1 项"
    assert win._save_project_action.isEnabled() is False

    subtitle.write_text("[00:00.00]test", encoding="utf-8")
    win._refresh_missing_resource_status()

    refreshed = win.project_state()
    assert refreshed.missing_resources == ()
    assert refreshed.dirty is False
    assert win._timing_track is None
    assert win._current_project_data()["subtitle_path"] == str(subtitle)

    win._mark_project_dirty()
    assert win._save_project_action.isEnabled() is True


def test_project_state_diagnostics_do_not_log_path_or_error_text(
    qapp, monkeypatch, tmp_path, caplog
):
    win = _make_window(qapp, monkeypatch)
    secret_name = "private-song-name.yurika"
    secret_error = "secret filesystem detail"
    win._project_path = tmp_path / secret_name
    win._project_dirty = True
    win._project_save_error = secret_error

    with caplog.at_level(logging.INFO, logger=mw.__name__):
        win._last_logged_project_state = None
        win._refresh_project_title()

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("字幕项目状态变化:")
    ]
    assert messages
    assert secret_name not in messages[-1]
    assert secret_error not in messages[-1]


def test_open_backup_directory_opens_shared_backup_root(
    qapp, monkeypatch, tmp_path
):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)
    win._project_path = tmp_path / "open-backups.yurika"
    opened: list[QUrl] = []
    monkeypatch.setattr(
        mw.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url) or True,
    )

    win._open_project_backup_directory()

    assert len(opened) == 1
    opened_path = Path(opened[0].toLocalFile())
    assert opened_path.is_dir()
    assert opened_path.name == "subtitle_render_backups"


def test_title_char_roles_round_trip_and_follow_text_edits():
    title = TitleOverlay(
        text_template="AB\nCD",
        char_role_labels=[["red", None], ["blue", "blue"]],
        tail_duration_ms=8_000,
        tail_fade_in_ms=900,
        tail_fade_out_ms=1_100,
    )
    restored = style_from_dict(style_to_dict(Style(title_overlay=title))).title_overlay
    assert restored is not None
    assert restored.char_role_labels == [["red", None], ["blue", "blue"]]
    assert restored.tail_duration_ms == 8_000
    assert restored.tail_fade_in_ms == 900
    assert restored.tail_fade_out_ms == 1_100

    migrated = subtitle_models.migrate_title_char_role_labels(
        "AB\nCD",
        restored.char_role_labels,
        "AXB\nD",
    )
    assert migrated == [["red", None, None], ["blue"]]


def test_title_row_role_is_independent_of_character_count():
    """整行角色与字符数无关：模板展开、改字后仍覆盖整行。"""
    template_labels = [["A"] * len("{title} / {artist}")]
    assert subtitle_models.normalize_title_char_role_labels(
        "曲名 / 歌手", template_labels
    ) == [["A"] * len("曲名 / 歌手")]

    # 逐字符混排的行仍与文字严格对位
    assert subtitle_models.normalize_title_char_role_labels(
        "甲乙丙", [["A", "B"]]
    ) == [["A", "B", None]]

    # 改标题文字时整行角色跟着整行走，新增字符不掉回标题默认
    assert subtitle_models.migrate_title_char_role_labels(
        "甲乙", [["A", "A"]], "甲乙丙"
    ) == [["A", "A", "A"]]


def test_lyrics_panel_title_mode_reuses_character_role_picker(qapp, monkeypatch):
    panel = lyrics_list.LyricsPanel()
    title = TitleOverlay(
        enabled=True,
        text_template="标题甲\n标题乙",
        char_role_labels=[[None, None, "A"], ["B", "B", None]],
    )
    panel.set_title(title)

    assert panel.table_widget.rowCount() == 2
    assert panel.table_widget.horizontalHeaderItem(lyrics_list.COL_LANE).text() == "行"
    assert panel.table_widget.isColumnHidden(lyrics_list.COL_EFFECT)
    assert panel.table_widget.item(0, lyrics_list.COL_LANE).text() == "1"
    assert panel.table_widget.item(0, lyrics_list.COL_CONTENT).text() == "标题甲"
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        lyrics_list._StableRoundMenu,
        "exec",
        lambda menu, *_args: captured.setdefault("menu", menu),
    )
    panel._show_role_picker(0)
    assert captured["menu"].actions()[0].text() == "标题默认"

    dialog = lyrics_list._CharRoleDialog(
        0,
        ["标"],
        [None],
        ["A"],
        _role_style(),
        default_role_text="标题默认",
        default_swatch_role="标题",
    )
    assert dialog._role_buttons[0].text() == "标题默认"


def test_title_context_menu_applies_role_scheme_and_layout(qapp, monkeypatch):
    panel = lyrics_list.LyricsPanel()
    panel.set_style(
        Style(
            layouts=[
                subtitle_models.LyricsLayout(name="标题左上"),
                subtitle_models.LyricsLayout(name="标题中央"),
            ]
        )
    )
    panel.set_role_options(["标题配色"])
    panel.set_title(
        TitleOverlay(
            enabled=True,
            text_template="标题甲\n标题乙",
            layout_index=2,
        )
    )
    for row in (0, 1):
        for column in range(panel.table_widget.columnCount()):
            panel.table_widget.item(row, column).setSelected(True)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        lyrics_list._StableRoundMenu,
        "exec",
        lambda menu, *_args: captured.setdefault("menu", menu),
    )
    role_changes: list[tuple[list[int], str]] = []
    layout_changes: list[tuple[list[int], int]] = []
    panel.roleChangeRequested.connect(
        lambda rows, name: role_changes.append((list(rows), name))
    )
    panel.layoutChangeRequested.connect(
        lambda rows, index: layout_changes.append((list(rows), index))
    )

    panel._show_context_menu(QPoint(4, 4))

    menu = captured["menu"]
    submenus = {submenu.title(): submenu for submenu in menu._subMenus}
    assert set(submenus) == {"应用角色方案", "应用布局"}
    role_actions = {
        action.text(): action for action in submenus["应用角色方案"].actions()
    }
    assert "标题默认" in role_actions
    role_actions["标题配色"].trigger()
    assert role_changes == [([0, 1], "标题配色")]

    layout_actions = {
        action.text(): action for action in submenus["应用布局"].actions()
    }
    assert layout_actions["标题左上"].icon().isNull()
    assert not layout_actions["标题中央"].icon().isNull()
    layout_actions["标题左上"].trigger()
    assert layout_changes == [([0, 1], 1)]


def test_title_context_actions_persist_role_scheme_and_layout(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    win._timing_track = _mixed_track()
    style = replace(
        _role_style(),
        layouts=[
            subtitle_models.LyricsLayout(name="标题左上"),
            subtitle_models.LyricsLayout(name="标题中央"),
        ],
        title_overlay=TitleOverlay(
            enabled=True,
            text_template="标题甲\n标题乙",
            layout_index=1,
        ),
    )
    win._style = style
    win._property_panel.set_style(style)
    win._title_source_active = True
    win._refresh_lyrics_panel_source()

    win._on_lyrics_roles_changed([0, 1], "A")
    title = win._style.title_overlay
    assert title is not None
    assert title.char_role_labels == [["A", "A", "A"], ["A", "A", "A"]]

    win._on_layout_change_requested([0, 1], 2)
    title = win._style.title_overlay
    assert title is not None
    assert title.layout_index == 2
    assert win._lyrics_panel._track is not None
    assert {line.layout_index for line in win._lyrics_panel._track.lines} == {2}


def test_role_cell_click_uses_fluent_menu_without_inline_editor(qapp, monkeypatch):
    panel = lyrics_list.LyricsPanel()
    panel.set_role_options(["京吹"])
    panel.set_track(
        TimingTrack(
            lines=[
                TimingLine(
                    chars=[TimingChar("甲", 1000, role_label="京吹")],
                    end_ms=2000,
                )
            ]
        )
    )
    item = panel.table_widget.item(0, lyrics_list.COL_ROLE)
    assert not item.flags() & Qt.ItemFlag.ItemIsEditable

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        lyrics_list._StableRoundMenu,
        "exec",
        lambda menu, *_args: captured.setdefault("menu", menu),
    )
    emitted: list[tuple[list[int], str]] = []
    panel.roleChangeRequested.connect(
        lambda rows, name: emitted.append((list(rows), name))
    )

    panel._on_cell_clicked(0, lyrics_list.COL_ROLE)

    menu = captured["menu"]
    target = next(action for action in menu.actions() if action.text() == "京吹")
    assert target.isChecked()
    target.trigger()
    assert emitted == [([0], "京吹")]


def test_title_source_is_last_and_title_text_syncs_from_tab(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)
    track = _mixed_track()
    win._timing_track = track
    win._extra_sources = [
        mw.ExtraSubtitleSource(name="副字幕", path=tmp_path / "extra.lrc", track=_mixed_track())
    ]
    title = TitleOverlay(
        enabled=True,
        text_template="AB",
        char_role_labels=[["A", "B"]],
    )
    win._style = Style(title_overlay=title)
    win._property_panel.set_style(win._style)
    win._refresh_source_ui()

    combo = win._lyrics_panel._source_combo
    assert [combo.itemText(i) for i in range(combo.count())] == ["主字幕", "副字幕", "标题"]

    win._on_source_selected(2)
    win._clear_undo_history()
    win._property_panel._update_title(text_template="AXB")
    assert win._style.title_overlay.char_role_labels == [["A", None, "B"]]
    assert win._lyrics_panel.table_widget.item(0, lyrics_list.COL_CONTENT).text() == "AXB"
    win._undo_edit()
    assert win._style.title_overlay.text_template == "AB"
    assert win._lyrics_panel.table_widget.item(0, lyrics_list.COL_CONTENT).text() == "AB"

    win._active_source_index = 1
    win._on_source_selected(2)
    win._property_panel._update_title(enabled=False)
    assert win._lyrics_panel.current_source_index() == 0


def test_title_roles_join_current_options_and_template_freezes_on_edit(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)
    win._timing_track = TimingTrack(
        meta=TimingTrackMeta(title="曲名", artist="歌手"),
        lines=_mixed_track().lines,
    )
    win._property_panel.set_roles(["A"])
    win._style = Style(
        title_overlay=TitleOverlay(
            enabled=True,
            text_template="{title} / {artist}",
            char_role_labels=[],
        )
    )
    win._property_panel.set_style(win._style)
    win._title_source_active = True
    win._refresh_source_ui()
    win._refresh_lyrics_panel_source()

    assert win._lyrics_panel.table_widget.item(0, lyrics_list.COL_CONTENT).text() == "曲名 / 歌手"

    title = replace(
        win._style.title_overlay,
        text_template="固定",
        char_role_labels=[["标题角色", None]],
    )
    win._style = replace(win._style, title_overlay=title)
    assert win._merged_role_options() == ["A", "标题角色"]

    win._style = replace(
        win._style,
        title_overlay=replace(title, text_template="{title} / {artist}", char_role_labels=[]),
    )
    win._freeze_title_template_for_character_edit()
    assert win._style.title_overlay.text_template == "曲名 / 歌手"


def test_title_role_keeps_metadata_template_dynamic(qapp, monkeypatch, tmp_path):
    """给标题整行套角色不再把 {title} / {artist} 固定成纯文字。"""
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)
    win._timing_track = TimingTrack(
        meta=TimingTrackMeta(title="曲名", artist="歌手"),
        lines=_mixed_track().lines,
    )
    win._property_panel.set_roles(["A"])
    win._style = Style(
        title_overlay=TitleOverlay(
            enabled=True,
            text_template="{title} / {artist}",
            char_role_labels=[],
        )
    )
    win._property_panel.set_style(win._style)
    win._title_source_active = True
    win._refresh_source_ui()
    win._refresh_lyrics_panel_source()

    win._on_lyrics_roles_changed([0], "A")

    title = win._style.title_overlay
    assert title.text_template == "{title} / {artist}"
    assert subtitle_models.normalize_title_char_role_labels(
        "曲名 / 歌手", title.char_role_labels
    ) == [["A"] * len("曲名 / 歌手")]
    assert win._lyrics_panel.table_widget.item(0, lyrics_list.COL_ROLE).text() == "A"

    # 元数据换歌后角色仍覆盖整行新文字
    win._timing_track = replace(
        win._timing_track,
        meta=TimingTrackMeta(title="另一首", artist="另一个歌手"),
    )
    win._refresh_lyrics_panel_source()
    assert subtitle_models.normalize_title_char_role_labels(
        "另一首 / 另一个歌手", title.char_role_labels
    ) == [["A"] * len("另一首 / 另一个歌手")]


def test_title_row_role_change_does_not_request_template_freeze(qapp):
    """整行角色不触发模板固定，逐字符编辑仍然触发。"""
    panel = lyrics_list.LyricsPanel()
    panel.set_role_options(["A"])
    panel.set_title(
        TitleOverlay(enabled=True, text_template="曲名 / 歌手", char_role_labels=[])
    )
    freezes: list[int] = []
    requests: list[tuple[list[int], str]] = []
    panel.titleEditRequested.connect(lambda: freezes.append(1))
    panel.roleChangeRequested.connect(
        lambda rows, name: requests.append((list(rows), name))
    )

    panel._request_role_change([0], "A")

    assert requests == [([0], "A")]
    assert freezes == []

    panel._edit_char_roles(-1)  # 逐字符编辑器仍要求先固定模板
    assert freezes == [1]


def test_new_project_clears_loaded_media(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    lrc = tmp_path / "demo.lrc"
    lrc.write_bytes(
        b"\xef\xbb\xbf" + "[00:01:00]a[00:01:50]b[00:02:00]\r\n\r\n@Title=Foo\r\n".encode("utf-8")
    )
    assert win.load_from_lrc(lrc) is not None
    assert win._lyrics_panel.is_populated()
    assert win._preview_panel.is_populated()

    win._project_dirty = False  # 避开放弃确认弹窗
    win._new_project()
    assert win._timing_track is None
    assert win._subtitle_path is None and win._video_path is None
    assert win._source_watch_states == {}
    assert not win._lyrics_panel.is_populated()
    assert not win._preview_panel.is_populated()


def test_project_role_payload_applies_before_missing_schemes_are_materialized(
    qapp, monkeypatch, tmp_path
):
    """N3 FontIndex=0 clears LRC markers before auto-role colors are created."""
    win = _make_window(qapp, monkeypatch)
    lrc = tmp_path / "roles.lrc"
    lrc.write_text(
        "【Aqua】[00:01:00]a[00:01:50]b[00:02:00]\n",
        encoding="utf-8-sig",
    )

    win._apply_project_data(
        {
            "subtitle_path": str(lrc),
            "style": style_to_dict(Style()),
            "char_role_labels": [[None, None]],
        }
    )

    assert [ch.role_label for ch in win._timing_track.lines[0].chars] == [None, None]
    assert "Aqua" not in win._style.custom_style_schemes


def test_schema_v1_project_is_migrated_to_movable_page_plan_in_memory(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    lrc = tmp_path / "legacy.lrc"
    lrc.write_text(
        "[00:01:00]1[00:01:50]\n"
        "[00:02:00]2[00:02:50]\n"
        "\n"
        "[00:03:00]3[00:03:50]\n"
        "[00:04:00]4[00:04:50]\n",
        encoding="utf-8-sig",
    )
    legacy = {
        "schema_version": 1,
        "subtitle_path": str(lrc),
        "style": style_to_dict(Style()),
        # Early schema-v1 snapshots had no line_breaks_before and relied on
        # the parser's historical two-line SeqLinesBreaker.  A larger layout
        # must not collapse those two old pages into one four-line page.
        "line_layout_indices": [4, 4, 0, 4, 4],
    }

    win._apply_project_data(legacy)

    assert "page_plan" not in legacy
    assert win._timing_track.page_plan is not None
    assert [
        page.line_count
        for section in win._timing_track.page_plan.sections
        for page in section.pages
    ] == [2, 2]
    assert [
        page.layout_id
        for section in win._timing_track.page_plan.sections
        for page in section.pages
    ] == ["builtin-4", "builtin-4"]
    assert any(
        row.kind == "page_marker"
        for row in win._lyrics_panel._presentation_rows
    )
    assert not any(
        row.kind == "source_blank"
        for row in win._lyrics_panel._presentation_rows
    )

    win._on_page_move_requested(0, 0, 1)

    assert [
        page.line_count
        for section in win._timing_track.page_plan.sections
        for page in section.pages
    ] == [3, 1]
    assert win._undo_stack[-1][0] == "track_snapshot"
    assert win._project_dirty is True

    win._undo_edit()
    assert [
        page.line_count
        for section in win._timing_track.page_plan.sections
        for page in section.pages
    ] == [2, 2]

    win._redo_edit()
    assert [
        page.line_count
        for section in win._timing_track.page_plan.sections
        for page in section.pages
    ] == [3, 1]


def test_loading_lyrics_applies_each_selected_ambiguous_role_preset(
    qapp, monkeypatch
):
    win = _make_window(qapp, monkeypatch)
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("初", 1000, role_label="初音"),
                    TimingChar("镜", 1500, role_label="镜音"),
                ],
                end_ms=2000,
            )
        ]
    )
    requested: list[list[str]] = []

    def choose(role_names):
        requested.append(list(role_names))
        return {
            "初音": SubtitleStyleScheme(fill_color="#112233"),
            "镜音": SubtitleStyleScheme(fill_color="#445566"),
        }

    monkeypatch.setattr(win._property_panel, "choose_role_presets_for_import", choose)

    win._apply_timing_track(track, None)

    assert requested == [["初音", "镜音"]]
    assert win._style.custom_style_schemes["初音"].fill_color == "#112233"
    assert win._style.custom_style_schemes["镜音"].fill_color == "#445566"


def test_preview_canvas_does_not_swallow_drops(qapp, monkeypatch):
    # 预览画布（QGraphicsView）默认会吞拖拽；必须关掉它，让拖拽冒泡到 DropPanel，
    # 这样预览被填充后仍能往播放区拖入新视频。
    win = _make_window(qapp, monkeypatch)
    canvas = win._preview_panel.canvas
    assert canvas.acceptDrops() is False
    if hasattr(canvas, "viewport"):
        assert canvas.viewport().acceptDrops() is False
    # 外层 DropPanel 仍接受拖拽
    assert win._preview_panel.acceptDrops() is True


def _write_demo_lrc(path, text="[00:01:00]あ[00:01:50]い[00:02:00]\r\n"):
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    return path


def test_dropped_subtitle_replaces_the_selected_source(qapp, monkeypatch, tmp_path):
    """拖入 / 「替换」按钮换掉的是当前选中那一轨，不再一律砸到主字幕。"""
    win = _make_window(qapp, monkeypatch)
    main_lrc = _write_demo_lrc(tmp_path / "main.lrc")
    chorus_lrc = _write_demo_lrc(
        tmp_path / "chorus.lrc", "[00:10:00]ラ[00:11:00]ラ[00:12:00]\r\n"
    )
    other_lrc = _write_demo_lrc(
        tmp_path / "other.lrc", "[00:20:00]ヨ[00:21:00]ソ[00:22:00]\r\n"
    )
    assert win.load_from_lrc(main_lrc) is not None

    from krok_helper.subtitle_render.frontend.main_window import ExtraSubtitleSource
    from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc

    win._extra_sources.append(
        ExtraSubtitleSource(
            name="chorus", path=chorus_lrc, track=load_nicokara_lrc(chorus_lrc)
        )
    )
    win._refresh_source_ui()

    # 选中副源 → 拖入只换副源
    win._on_source_selected(1)
    win._load_dropped_subtitle(other_lrc)
    assert win._extra_sources[0].path == other_lrc
    assert [c.text for c in win._extra_sources[0].track.lines[0].chars] == ["ヨ", "ソ"]
    # 名字还是旧文件名时跟着新文件走
    assert win._extra_sources[0].name == "other"
    assert win._subtitle_path == main_lrc
    assert [c.text for c in win._timing_track.lines[0].chars] == ["あ", "い"]

    # 选中主字幕 → 拖入换主字幕，副源不动
    win._on_source_selected(0)
    win._load_dropped_subtitle(chorus_lrc)
    assert win._subtitle_path == chorus_lrc
    assert [c.text for c in win._timing_track.lines[0].chars] == ["ラ", "ラ"]
    assert win._extra_sources[0].path == other_lrc


def test_replace_source_keeps_a_renamed_extra_source_name(qapp, monkeypatch, tmp_path):
    """用户改过的副源名字不该被新文件名覆盖。"""
    win = _make_window(qapp, monkeypatch)
    assert win.load_from_lrc(_write_demo_lrc(tmp_path / "main.lrc")) is not None
    chorus_lrc = _write_demo_lrc(
        tmp_path / "chorus.lrc", "[00:10:00]ラ[00:11:00]ラ[00:12:00]\r\n"
    )
    other_lrc = _write_demo_lrc(
        tmp_path / "other.lrc", "[00:20:00]ヨ[00:21:00]ソ[00:22:00]\r\n"
    )

    from krok_helper.subtitle_render.frontend.main_window import ExtraSubtitleSource
    from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc

    win._extra_sources.append(
        ExtraSubtitleSource(
            name="コーラス1", path=chorus_lrc, track=load_nicokara_lrc(chorus_lrc)
        )
    )
    win._replace_source_file(1, other_lrc)
    assert win._extra_sources[0].path == other_lrc
    assert win._extra_sources[0].name == "コーラス1"


def test_extra_subtitle_sources_round_trip(qapp, monkeypatch, tmp_path):
    """副字幕源（N3 多歌词文件）随 .yurika 保存/恢复：路径、每行布局、逐字角色。"""
    win = _make_window(qapp, monkeypatch)
    main_lrc = _write_demo_lrc(tmp_path / "main.lrc")
    chorus_lrc = _write_demo_lrc(tmp_path / "chorus.lrc", "[00:10:00]ラ[00:11:00]ラ[00:12:00]\r\n")
    assert win.load_from_lrc(main_lrc) is not None

    from krok_helper.subtitle_render.frontend.main_window import ExtraSubtitleSource
    from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc

    chorus_track = load_nicokara_lrc(chorus_lrc)
    chorus_track.lines[0].layout_index = 1
    chorus_track.lines[0].break_before = "paragraph"
    for ch in chorus_track.lines[0].chars:
        ch.role_label = "コーラス配色"
    win._extra_sources.append(
        ExtraSubtitleSource(name="コーラス1", path=chorus_lrc, track=chorus_track)
    )

    data = win._current_project_data()
    extras = data.get("extra_subtitle_sources")
    assert extras and extras[0]["name"] == "コーラス1"
    assert extras[0]["line_layout_indices"] == [1]
    assert extras[0]["line_breaks_before"] == ["paragraph"]
    assert extras[0]["char_role_labels"] == [["コーラス配色", "コーラス配色"]]

    # 恢复到新窗口（布局数量需覆盖 layout_index=1）
    from dataclasses import replace as dc_replace
    from krok_helper.subtitle_render.models import LyricsLayout, style_to_dict

    data["style"] = style_to_dict(
        dc_replace(win._style, layouts=[LyricsLayout(name="コーラス")])
    )
    win2 = _make_window(qapp, monkeypatch)
    win2._apply_project_data(data)
    assert len(win2._extra_sources) == 1
    restored = win2._extra_sources[0]
    assert restored.name == "コーラス1"
    assert restored.track.lines[0].layout_index == 1
    # A boundary before the first renderable line is normalized away; it has
    # no visible section/page effect and the v2 page plan remains authoritative.
    assert restored.track.lines[0].break_before == "none"
    assert [c.role_label for c in restored.track.lines[0].chars] == ["コーラス配色", "コーラス配色"]
    combo = win2._lyrics_panel._source_combo
    expected_sources = ["主字幕", "コーラス1"]
    if win2._style.title_overlay is not None and win2._style.title_overlay.enabled:
        expected_sources.append("标题")
    assert [combo.itemText(i) for i in range(combo.count())] == expected_sources
    assert win2._subtitle_source_key(main_lrc) in win2._source_watch_states
    assert win2._subtitle_source_key(chorus_lrc) in win2._source_watch_states


def test_open_project_defers_saved_media_and_extra_sources(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    main_lrc = _write_demo_lrc(tmp_path / "main.lrc")
    chorus_lrc = _write_demo_lrc(
        tmp_path / "chorus.lrc",
        "[00:10:00]B[00:11:00]B[00:12:00]\r\n",
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    project = tmp_path / "demo.yurika"
    save_render_project(
        project,
        {
            "subtitle_path": str(main_lrc),
            "background": {
                "kind": "video",
                "path": str(video),
                "color": "#000000",
                "source_fps": None,
                "sequence_start_number": 0,
                "video_offset_ms": 0,
            },
            "extra_subtitle_sources": [
                {"name": "chorus", "path": str(chorus_lrc)}
            ],
            "screen": {"width": 1920, "height": 1080, "fps": 60, "par": "1:1"},
            "style": style_to_dict(Style()),
        },
    )
    monkeypatch.setattr(
        win,
        "_probe",
        lambda path, label: mw.MediaInfo(
            path=path,
            duration=12.0,
            video_streams=1,
            audio_streams=1,
            subtitle_streams=0,
            video_width=1280,
            video_height=720,
            video_fps=60.0,
        ),
    )

    assert win._open_project_path(project, confirm_discard=False)
    assert win._timing_track is not None
    assert win._extra_sources == []
    assert win._video_path is None
    assert len(win._project_deferred_loads) == 2
    assert not win._project_dirty

    win._project_deferred_load_timer.stop()
    win._tracks_window_refresh_timer.stop()
    win._process_project_deferred_load()
    win._process_project_deferred_load()

    assert len(win._extra_sources) == 1
    assert win._video_path == video
    assert not win._project_dirty
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_line_animation_overrides_round_trip(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    lrc = _write_demo_lrc(
        tmp_path / "effects.lrc",
        "[00:01:00]あ[00:01:50]い[00:02:00]\r\n"
        "[00:03:00]う[00:03:50]え[00:04:00]\r\n",
    )
    assert win.load_from_lrc(lrc) is not None
    override = LineAnimationOverride(
        entry_anim="slide_in",
        entry_duration_ms=450,
        exit_anim="char_fade",
        exit_duration_ms=350,
    )
    win._timing_track.lines[1].animation_override = override

    data = win._current_project_data()
    assert data["line_animation_overrides"] == [
        None,
        {
            "entry_anim": "slide_in",
            "entry_duration_ms": 450,
            "exit_anim": "char_fade",
            "exit_duration_ms": 350,
        },
    ]

    restored = _make_window(qapp, monkeypatch)
    restored._apply_project_data(data)
    assert restored._timing_track is not None
    assert restored._timing_track.lines[0].animation_override is None
    assert restored._timing_track.lines[1].animation_override == override


def test_lyrics_combo_menu_disables_racy_popup_animation(qapp, monkeypatch):
    """菜单提前关闭时不得留下访问已删除 view 的动画回调。"""
    seen = {}

    def fake_exec(self, pos, ani=True, aniType=MenuAnimationType.DROP_DOWN):
        seen["animation"] = aniType

    monkeypatch.setattr(ComboBoxMenu, "exec", fake_exec)
    combo = lyrics_list._StableFluentComboBox()
    menu = combo._createComboMenu()
    menu.exec(QPoint(0, 0), aniType=MenuAnimationType.DROP_DOWN)

    assert seen["animation"] == MenuAnimationType.NONE


def test_lyrics_table_shows_global_and_overridden_line_effects(qapp):
    panel = lyrics_list.LyricsPanel()
    panel.set_style(Style(entry_anim="fade", exit_anim="fade"))
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("全", 1000)], end_ms=2000),
            TimingLine(
                chars=[TimingChar("別", 3000)],
                end_ms=4000,
                animation_override=LineAnimationOverride(
                    entry_anim="slide_in",
                    entry_duration_ms=450,
                    exit_anim="none",
                    exit_duration_ms=0,
                ),
            ),
        ]
    )

    panel.set_track(track)

    assert panel.table_widget.columnCount() == 5
    assert panel.table_widget.horizontalHeaderItem(lyrics_list.COL_EFFECT).text() == "特效"
    assert panel.table_widget.item(0, lyrics_list.COL_EFFECT).text() == "全局：淡入 / 淡出"
    assert panel.table_widget.item(1, lyrics_list.COL_EFFECT).text() == "滑入 / 无"


def _role_style() -> Style:
    schemes = dict(Style().custom_style_schemes)
    schemes["A"] = SubtitleStyleScheme(fill_color="#FF0000")
    schemes["B"] = SubtitleStyleScheme(fill_color="#0000FF")
    return Style(custom_style_schemes=schemes)


def _mixed_track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("あ", 1000),
                    TimingChar("い", 1500, role_label="A"),
                    TimingChar("う", 2000, role_label="A"),
                ],
                end_ms=3000,
            ),
            TimingLine(
                chars=[
                    TimingChar("え", 4000, role_label="A"),
                    TimingChar("お", 4500, role_label="A"),
                ],
                end_ms=5000,
            ),
        ]
    )


def test_char_role_chips_selection_and_apply(qapp):
    chips = lyrics_list._CharChipsView(["あ", "い", "う"], [None, "A", None], _role_style())
    chips.resize(400, 120)
    chips._selected = {0, 2}
    chips.apply_role("B")
    assert chips.labels() == ["B", "A", "B"]
    chips.select_all()
    chips.apply_role(None)
    assert chips.labels() == [None, None, None]


def test_char_role_dialog_returns_edited_labels(qapp):
    dialog = lyrics_list._CharRoleDialog(
        0, ["あ", "い"], [None, None], ["A", "B"], _role_style()
    )
    dialog._chips._selected = {1}
    dialog._chips.apply_role("A")
    assert dialog.char_labels() == [None, "A"]


def test_char_role_dialog_creates_role_with_fluent_input(qapp, monkeypatch):
    dialog = lyrics_list._CharRoleDialog(
        0, ["あ", "い"], [None, None], ["A"], _role_style()
    )
    dialog._chips._selected = {0}
    captured: dict[str, object] = {}

    def get_text(parent, title, label, **kwargs):
        captured.update(parent=parent, title=title, label=label, kwargs=kwargs)
        return "合唱", True

    monkeypatch.setattr(lyrics_list, "fluent_get_text", get_text)
    dialog._create_role()

    assert captured == {
        "parent": dialog,
        "title": "新建角色",
        "label": "角色名称",
        "kwargs": {},
    }
    assert "合唱" in {button.text() for button in dialog._role_buttons}
    assert dialog.char_labels() == ["合唱", None]


def test_char_role_dialog_identifies_same_color_schemes_by_name(qapp):
    schemes = dict(Style().custom_style_schemes)
    schemes["同色A"] = SubtitleStyleScheme(fill_color="#336699")
    schemes["同色B"] = SubtitleStyleScheme(fill_color="#336699")
    dialog = lyrics_list._CharRoleDialog(
        0,
        ["甲", "乙"],
        ["同色A", "同色B"],
        ["同色A", "同色B"],
        Style(custom_style_schemes=schemes),
    )
    buttons = {button.text(): button for button in dialog._role_buttons}

    dialog._chips._selected = {0}
    dialog._chips.selectionChanged.emit()
    assert dialog._roles_label.text() == "当前分配：同色A"
    assert buttons["同色A"].isChecked()
    assert not buttons["同色B"].isChecked()
    assert dialog._chips.role_tooltip_text(0) == "角色方案：同色A"

    dialog._chips._selected = {0, 1}
    dialog._chips.selectionChanged.emit()
    assert dialog._roles_label.text() == "当前分配：混合（2 种方案）"
    assert not buttons["同色A"].isChecked()
    assert not buttons["同色B"].isChecked()

    buttons["同色B"].click()
    assert dialog.char_labels() == ["同色B", "同色B"]
    assert dialog._roles_label.text() == "当前分配：同色B"
    assert not buttons["同色A"].isChecked()
    assert buttons["同色B"].isChecked()


def test_char_role_options_match_current_property_panel_roles(qapp, monkeypatch, tmp_path):
    """导入覆盖逐字标签后，不应把旧歌词标签留下的预设继续列为可分配角色。"""
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("甲", 1000, role_label="1"),
                    TimingChar("乙", 1500, role_label="裏声"),
                    TimingChar("丙", 2000, role_label="标准配色2"),
                ],
                end_ms=3000,
            )
        ]
    )
    win._timing_track = track
    win._property_panel.set_roles(track.role_options)

    # 模拟 N3 的 char_role_labels 覆盖：当前导航只剩实际可用的两个角色，
    # 但初次载入歌词时自动生成的旧预设仍留在 preset 库中。
    track.lines[0].chars[0].role_label = "1"
    track.lines[0].chars[1].role_label = "【裏声】"
    track.lines[0].chars[2].role_label = None
    win._property_panel.set_roles(track.role_options)

    assert win._merged_role_options() == ["1", "【裏声】"]


def test_imported_project_role_is_immediately_available_in_lyrics_panel(
    qapp, monkeypatch, tmp_path
):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("甲", 1000, role_label="原角色")], end_ms=2000)]
    )
    win._timing_track = track
    win._lyrics_panel.set_track(track)
    win._property_panel.set_roles(track.role_options)

    win._property_panel._import_preset_schemes(
        {
            "新导入配色": StylePreset(
                name="新导入配色",
                scheme=SubtitleStyleScheme(fill_color="#123456"),
            )
        }
    )

    assert win._merged_role_options() == ["原角色", "新导入配色"]
    assert win._lyrics_panel._role_options == ["原角色", "新导入配色"]
    assert "新导入配色" in win._style.custom_style_schemes


def test_unassigned_imported_project_role_round_trips_in_project_data(
    qapp, monkeypatch, tmp_path
):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)
    win._property_panel._import_preset_schemes(
        {
            "尚未分配": StylePreset(
                name="尚未分配",
                scheme=SubtitleStyleScheme(fill_color="#654321"),
            )
        }
    )

    assert win._project_document.role_names == ["尚未分配"]
    data = win._current_project_data()
    assert data["project_role_names"] == ["尚未分配"]

    restored = _make_window(qapp, monkeypatch)
    restored._apply_project_data(data)

    assert restored._property_panel.role_names == ["尚未分配"]
    assert restored._project_document.role_names == ["尚未分配"]
    assert restored._lyrics_panel._role_options == ["尚未分配"]
    assert restored._style.custom_style_schemes["尚未分配"].fill_color == "#654321"


def test_replacing_lrc_preserves_all_existing_project_roles(
    qapp, monkeypatch, tmp_path
):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)
    win._property_panel._add_custom_scheme("手动角色")

    tagged_lrc = tmp_path / "tagged.lrc"
    tagged_lrc.write_text(
        "【旧 LRC 角色】[00:01:00]甲[00:02:00]\n",
        encoding="utf-8-sig",
    )
    assert win.load_from_lrc(tagged_lrc) is not None
    assert win._property_panel.role_names == ["手动角色", "旧 LRC 角色"]

    plain_lrc = tmp_path / "plain.lrc"
    plain_lrc.write_text(
        "[00:01:00]乙[00:02:00]\n",
        encoding="utf-8-sig",
    )
    assert win.load_from_lrc(plain_lrc) is not None

    assert win._property_panel.role_names == ["手动角色", "旧 LRC 角色"]
    assert win._merged_role_options() == ["手动角色", "旧 LRC 角色"]
    assert {"手动角色", "旧 LRC 角色"} <= set(
        win._style.custom_style_schemes
    )

    data = win._current_project_data()
    assert data["project_role_names"] == ["手动角色", "旧 LRC 角色"]

    restored = _make_window(qapp, monkeypatch)
    restored._apply_project_data(data)

    assert restored._property_panel.role_names == ["手动角色", "旧 LRC 角色"]
    assert restored._lyrics_panel._role_options == ["手动角色", "旧 LRC 角色"]


def test_lyrics_panel_requests_one_role_for_multiple_selected_rows(qapp):
    panel = lyrics_list.LyricsPanel()
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("甲", 1000)], end_ms=1500),
            TimingLine(chars=[TimingChar("乙", 2000)], end_ms=2500),
        ]
    )
    panel.set_track(track)
    emitted: list[tuple[list[int], str]] = []
    panel.roleChangeRequested.connect(
        lambda rows, name: emitted.append((list(rows), name))
    )

    panel._request_role_change([0, 1], "新导入配色")

    assert emitted == [([0, 1], "新导入配色")]


def test_selected_rows_context_menu_exposes_role_schemes(qapp, monkeypatch):
    panel = lyrics_list.LyricsPanel()
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("甲", 1000)], end_ms=1500),
            TimingLine(chars=[TimingChar("乙", 2000)], end_ms=2500),
        ]
    )
    panel.set_track(track)
    panel.set_role_options(["新导入配色"])
    for row in (0, 1):
        for column in range(panel.table_widget.columnCount()):
            panel.table_widget.item(row, column).setSelected(True)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        lyrics_list._StableRoundMenu,
        "exec",
        lambda menu, *_args: captured.setdefault("menu", menu),
    )
    emitted: list[tuple[list[int], str]] = []
    panel.roleChangeRequested.connect(
        lambda rows, name: emitted.append((list(rows), name))
    )

    panel._show_context_menu(QPoint(4, 4))

    menu = captured["menu"]
    role_menu = next(
        submenu for submenu in menu._subMenus
        if submenu.title() == "应用角色方案"
    )
    target = next(
        action for action in role_menu.actions() if action.text() == "新导入配色"
    )
    target.trigger()
    assert emitted == [([0, 1], "新导入配色")]


def test_selected_rows_context_menu_marks_applied_layouts(qapp, monkeypatch):
    panel = lyrics_list.LyricsPanel()
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("甲", 1000)],
                end_ms=1500,
                layout_index=1,
            ),
            TimingLine(
                chars=[TimingChar("乙", 2000)],
                end_ms=2500,
                layout_index=2,
            ),
        ]
    )
    panel.set_style(
        Style(
            layouts=[
                subtitle_models.LyricsLayout(name="下寄せ3行"),
                subtitle_models.LyricsLayout(name="上寄せ2行"),
            ]
        )
    )
    panel.set_track(track)
    for row in (0, 1):
        for column in range(panel.table_widget.columnCount()):
            panel.table_widget.item(row, column).setSelected(True)
    captured: list[object] = []
    monkeypatch.setattr(
        lyrics_list._StableRoundMenu,
        "exec",
        lambda menu, *_args: captured.append(menu),
    )
    emitted: list[tuple[list[int], int]] = []
    panel.layoutChangeRequested.connect(
        lambda rows, index: emitted.append((list(rows), index))
    )

    panel._show_context_menu(QPoint(4, 4))

    menu = captured[-1]
    layout_menu = next(
        submenu for submenu in menu._subMenus if submenu.title() == "应用布局"
    )
    actions = {action.text(): action for action in layout_menu.actions()}
    assert actions["2 行布局（默认）"].icon().isNull()
    assert not actions["下寄せ3行"].icon().isNull()
    assert not actions["上寄せ2行"].icon().isNull()

    actions["上寄せ2行"].trigger()
    assert emitted == [([0, 1], 2)]

    panel.table_widget.clearSelection()
    for column in range(panel.table_widget.columnCount()):
        panel.table_widget.item(1, column).setSelected(True)
    panel._show_context_menu(
        QPoint(4, panel.table_widget.rowViewportPosition(1) + 4)
    )
    layout_menu = next(
        submenu
        for submenu in captured[-1]._subMenus
        if submenu.title() == "应用布局"
    )
    actions = {action.text(): action for action in layout_menu.actions()}
    assert actions["2 行布局（默认）"].icon().isNull()
    assert actions["下寄せ3行"].icon().isNull()
    assert not actions["上寄せ2行"].icon().isNull()


def test_batch_line_roles_apply_and_undo_as_one_command(
    qapp, monkeypatch, tmp_path
):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("甲", 1000, role_label="A")], end_ms=1500
            ),
            TimingLine(
                chars=[TimingChar("乙", 2000, role_label="B")], end_ms=2500
            ),
        ]
    )
    win._timing_track = track
    win._lyrics_panel.set_track(track)
    win._property_panel.set_roles(["A", "B", "新导入配色"])
    win._clear_undo_history()

    win._on_lyrics_roles_changed([0, 1], "新导入配色")

    assert [[ch.role_label for ch in line.chars] for line in track.lines] == [
        ["新导入配色"],
        ["新导入配色"],
    ]
    assert [
        win._lyrics_panel.table_widget.item(row, lyrics_list.COL_ROLE).text()
        for row in (0, 1)
    ] == ["新导入配色", "新导入配色"]
    assert [
        win._lyrics_panel.table_widget.item(
            row, lyrics_list.COL_ROLE
        ).data(Qt.ItemDataRole.UserRole)
        for row in (0, 1)
    ] == ["新导入配色", "新导入配色"]
    assert len(win._undo_stack) == 1
    assert win._undo_stack[-1][0] == "char_roles_batch"

    win._undo_edit()
    assert [[ch.role_label for ch in line.chars] for line in track.lines] == [
        ["A"],
        ["B"],
    ]
    assert [
        win._lyrics_panel.table_widget.item(row, lyrics_list.COL_ROLE).text()
        for row in (0, 1)
    ] == ["A", "B"]
    win._redo_edit()
    assert [[ch.role_label for ch in line.chars] for line in track.lines] == [
        ["新导入配色"],
        ["新导入配色"],
    ]
    assert [
        win._lyrics_panel.table_widget.item(row, lyrics_list.COL_ROLE).text()
        for row in (0, 1)
    ] == ["新导入配色", "新导入配色"]


def test_lyrics_table_marks_mixed_role_rows(qapp):
    panel = lyrics_list.LyricsPanel()
    panel.set_style(_role_style())
    panel.set_track(_mixed_track())

    assert panel.table_widget.item(0, lyrics_list.COL_ROLE).text() == "混合"
    assert panel.table_widget.item(1, lyrics_list.COL_ROLE).text() == "A"


def test_line_role_overwrite_on_mixed_row_asks_confirmation(qapp, monkeypatch):
    panel = lyrics_list.LyricsPanel()
    panel.set_style(_role_style())
    panel.set_track(_mixed_track())
    emitted: list[tuple[int, str]] = []
    panel.roleChanged.connect(lambda row, name: emitted.append((row, name)))

    monkeypatch.setattr(lyrics_list, "fluent_question", lambda *a, **k: False)
    panel.table_widget.item(0, lyrics_list.COL_ROLE).setData(
        Qt.ItemDataRole.UserRole, "B"
    )
    assert emitted == []
    # 取消后角色列还原混合显示
    assert panel.table_widget.item(0, lyrics_list.COL_ROLE).text() == "混合"

    monkeypatch.setattr(lyrics_list, "fluent_question", lambda *a, **k: True)
    panel.table_widget.item(0, lyrics_list.COL_ROLE).setData(
        Qt.ItemDataRole.UserRole, "A"
    )
    assert emitted == [(0, "A")]
    # 非混合行（单一角色）不弹确认
    panel.table_widget.item(1, lyrics_list.COL_ROLE).setData(
        Qt.ItemDataRole.UserRole, "B"
    )
    assert emitted[-1] == (1, "B")


def test_char_roles_write_back_with_undo_redo(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    win = _make_window(qapp, monkeypatch)
    track = _mixed_track()
    win._timing_track = track
    win._lyrics_panel.set_track(track)
    win._property_panel.set_style(_role_style())
    win._style = win._property_panel.subtitle_style
    win._clear_undo_history()

    win._on_lyrics_char_roles_changed(0, ["B", None, "A"])
    assert [ch.role_label for ch in track.lines[0].chars] == ["B", None, "A"]
    assert win._undo_stack and win._undo_stack[-1][0] == "char_roles"

    win._undo_edit()
    assert [ch.role_label for ch in track.lines[0].chars] == [None, "A", "A"]
    win._redo_edit()
    assert [ch.role_label for ch in track.lines[0].chars] == ["B", None, "A"]

    # 行级下拉整行覆盖同样可撤销
    win._on_lyrics_role_changed(1, "B")
    assert [ch.role_label for ch in track.lines[1].chars] == ["B", "B"]
    win._undo_edit()
    assert [ch.role_label for ch in track.lines[1].chars] == ["A", "A"]


def test_lyrics_table_uses_measured_fixed_columns_and_elastic_content(qapp):
    panel = lyrics_list.LyricsPanel()
    panel.resize(900, 480)
    panel.show()
    panel.set_style(Style(entry_anim="fade", exit_anim="fade"))
    panel.set_track(
        TimingTrack(
            lines=[
                TimingLine(
                    chars=[TimingChar("一段用于测量的歌词内容", 1000)],
                    end_ms=2000,
                )
            ]
        )
    )
    qapp.processEvents()

    header = panel.table_widget.horizontalHeader()
    assert header.sectionResizeMode(lyrics_list.COL_LANE) == (
        lyrics_list.QHeaderView.ResizeMode.ResizeToContents
    )
    assert header.sectionResizeMode(lyrics_list.COL_ROLE) == (
        lyrics_list.QHeaderView.ResizeMode.Interactive
    )
    assert header.sectionResizeMode(lyrics_list.COL_EFFECT) == (
        lyrics_list.QHeaderView.ResizeMode.Interactive
    )
    assert header.sectionResizeMode(lyrics_list.COL_CONTENT) == (
        lyrics_list.QHeaderView.ResizeMode.Stretch
    )

    content_before = header.sectionSize(lyrics_list.COL_CONTENT)
    role_before = header.sectionSize(lyrics_list.COL_ROLE)
    delta = max(panel.fontMetrics().horizontalAdvance("宽"), 1)
    header.resizeSection(lyrics_list.COL_ROLE, role_before + delta)
    qapp.processEvents()

    assert header.sectionSize(lyrics_list.COL_ROLE) == role_before + delta
    assert header.sectionSize(lyrics_list.COL_CONTENT) == content_before - delta
    assert header.length() == panel.table_widget.viewport().width()

    role_after_drag = header.sectionSize(lyrics_list.COL_ROLE)
    effect_after_drag = header.sectionSize(lyrics_list.COL_EFFECT)
    content_after_drag = header.sectionSize(lyrics_list.COL_CONTENT)
    viewport_before_resize = panel.table_widget.viewport().width()
    panel.resize(panel.width() + delta * 3, panel.height())
    qapp.processEvents()
    viewport_growth = panel.table_widget.viewport().width() - viewport_before_resize

    assert header.sectionSize(lyrics_list.COL_ROLE) == role_after_drag
    assert header.sectionSize(lyrics_list.COL_EFFECT) == effect_after_drag
    assert header.sectionSize(lyrics_list.COL_CONTENT) == (
        content_after_drag + viewport_growth
    )
    panel.close()
    panel.deleteLater()
    qapp.processEvents()


def test_display_window_edit_defers_track_window_refresh(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("A", 1000)], end_ms=2000)]
    )
    win._timing_track = track

    preview_updates: list[TimingTrack] = []
    scheduled: list[bool] = []
    immediate_refreshes: list[bool] = []
    monkeypatch.setattr(win._preview_panel, "set_track", preview_updates.append)
    monkeypatch.setattr(
        win,
        "_schedule_tracks_view_window_refresh",
        lambda: scheduled.append(True),
    )
    monkeypatch.setattr(
        win,
        "_refresh_tracks_view_windows",
        lambda: immediate_refreshes.append(True),
    )

    win._refresh_after_display_edit(0)

    assert preview_updates == [track]
    assert scheduled == [True]
    assert immediate_refreshes == []
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_lyrics_table_effect_column_uses_measured_semantic_minimum(qapp):
    panel = lyrics_list.LyricsPanel()
    panel.resize(900, 480)
    panel.show()
    panel.set_style(Style(entry_anim="fade", exit_anim="fade"))
    panel.set_track(
        TimingTrack(
            lines=[TimingLine(chars=[TimingChar("歌词", 1000)], end_ms=2000)]
        )
    )
    qapp.processEvents()

    header = panel.table_widget.horizontalHeader()
    measured_minimum = panel._effect_minimum_width()
    header.resizeSection(lyrics_list.COL_EFFECT, 1)
    qapp.processEvents()

    assert measured_minimum > header.sectionSizeHint(lyrics_list.COL_EFFECT)
    assert header.sectionSize(lyrics_list.COL_EFFECT) == measured_minimum
    panel.close()
    panel.deleteLater()
    qapp.processEvents()


def test_lyrics_table_clamps_columns_to_viewport_and_exposes_minimum_width(qapp):
    """列宽双向钳制：拖不出 viewport、面板缩窄自动回收、最小宽传给 splitter。"""
    panel = lyrics_list.LyricsPanel()
    panel.resize(900, 480)
    panel.show()
    panel.set_style(Style(entry_anim="fade", exit_anim="fade"))
    panel.set_track(
        TimingTrack(
            lines=[
                TimingLine(
                    chars=[TimingChar("一段用于测量的歌词内容", 1000)],
                    end_ms=2000,
                )
            ]
        )
    )
    qapp.processEvents()

    table = panel.table_widget
    header = table.horizontalHeader()
    # 横向滚动被禁用：列宽预算保证总宽 == viewport，不存在"滚出去"的内容
    assert table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    # 各列语义下限之和成为表格最小宽，并经布局上传为面板的最小尺寸
    assert table.minimumWidth() > 0
    assert panel.minimumSizeHint().width() >= table.minimumWidth()

    # 特效列拖到再宽，内容列也最多缩到语义下限，总宽不超 viewport
    content_min = panel._content_minimum_width()
    header.resizeSection(lyrics_list.COL_EFFECT, 10_000)
    qapp.processEvents()
    assert header.length() == table.viewport().width()
    assert header.sectionSize(lyrics_list.COL_CONTENT) >= content_min

    # 面板缩窄后回收特效 / 角色列宽度，内容列仍保住下限
    panel.resize(table.minimumWidth() + 40, panel.height())
    qapp.processEvents()
    assert header.length() == table.viewport().width()
    assert header.sectionSize(lyrics_list.COL_CONTENT) >= content_min
    panel.close()
    panel.deleteLater()
    qapp.processEvents()
