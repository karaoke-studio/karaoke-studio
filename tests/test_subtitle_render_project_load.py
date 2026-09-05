"""Focused contracts for typed subtitle project load planning."""

from __future__ import annotations

from pathlib import Path

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.project.load import (
    ProjectLoadPlan,
    apply_track_project_data,
)


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
    # 旧工程 ``title_overlay: null`` 现在加载为默认一条（禁用）条目
    assert plan.style.title_overlays
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


def test_apply_track_project_data_restores_all_line_projections() -> None:
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("甲", 1000)]),
            TimingLine(chars=[TimingChar("乙", 2000)]),
        ]
    )

    result = apply_track_project_data(
        track,
        Style(),
        {
            "line_breaks_before": ["page", "invalid"],
            "line_layout_indices": [1, 99],
            "char_role_labels": [["主唱"], ["和声"]],
            "line_display_overrides": [[500, None], [None, 2600]],
            "line_animation_overrides": [
                {
                    "entry_anim": "fade",
                    "entry_duration_ms": 120,
                    "exit_anim": "slide_out",
                    "exit_duration_ms": 240,
                    "karaoke_anim": "utopia",
                },
                None,
            ],
        },
    )

    assert result.char_role_labels_changed is True
    assert result.guide_symbol_mismatches == ()
    assert [line.break_before for line in track.lines] == ["none", "page"]
    assert [line.layout_index for line in track.lines] == [1, 0]
    assert [line.chars[0].role_label for line in track.lines] == ["主唱", "和声"]
    assert track.lines[0].display_start_override_ms == 500
    assert track.lines[1].display_end_override_ms == 2600
    assert track.lines[0].animation_override is not None
    assert track.lines[0].animation_override.entry_anim == "fade"
    assert track.lines[1].animation_override is None
    assert track.page_plan is not None
    assert track.loading_settings_mode == "custom"


def test_apply_track_project_data_applies_wipe_reverse_overrides() -> None:
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("甲", 1000)], end_ms=2000),
            TimingLine(chars=[TimingChar("乙", 3000)], end_ms=4000),
            TimingLine(chars=[TimingChar("丙", 5000)], end_ms=6000),
        ]
    )

    apply_track_project_data(
        track,
        Style(),
        {"line_wipe_reverse_overrides": [True, False, "invalid"]},
    )

    # 手动覆盖回放时同时改写渲染消费的有效标记；非布尔项忽略
    assert track.lines[0].wipe_reverse is True
    assert track.lines[0].wipe_reverse_override is True
    assert track.lines[1].wipe_reverse is False
    assert track.lines[1].wipe_reverse_override is False
    assert track.lines[2].wipe_reverse is False
    assert track.lines[2].wipe_reverse_override is None


def test_wipe_reverse_overrides_round_trip_through_project_data() -> None:
    from krok_helper.subtitle_render.project.session import _track_project_data

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("甲", 1000)], end_ms=2000, wipe_reverse=True
            ),
            TimingLine(
                chars=[TimingChar("乙", 3000)], end_ms=4000, wipe_reverse=False
            ),
        ]
    )
    track.lines[0].wipe_reverse_override = True
    # 源逆序自动判定为反向、用户手动取消的行也保留覆盖值
    track.lines[1].wipe_reverse_override = False

    data = _track_project_data(track)
    assert data["line_wipe_reverse_overrides"] == [True, False]

    # 全部行为自动判定时不写项目字段
    auto = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("甲", 1000)], end_ms=2000, wipe_reverse=True)
        ]
    )
    assert _track_project_data(auto)["line_wipe_reverse_overrides"] is None

    # 重新解析出的顺序行回放覆盖后恢复手动反向
    restored = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("甲", 1000)], end_ms=2000)]
    )
    apply_track_project_data(restored, Style(), data)
    assert restored.lines[0].wipe_reverse is True
    assert restored.lines[0].wipe_reverse_override is True


def test_guide_symbol_table_round_trips_through_project_data() -> None:
    """同一符号应用到多行时，.yurika 只存一份轮廓 + 行数据引用 ID。"""
    from krok_helper.subtitle_render.domain.timing import GuideSymbol
    from krok_helper.subtitle_render.project.session import _track_project_data

    symbol = GuideSymbol(
        path_commands=tuple(
            ("C", float(i), 1.0, float(i) + 1.0, 2.0, float(i) + 2.0, 3.0)
            for i in range(200)
        ),
        duration_ms=400,
        count=1,
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("歌", 1000)],
                guide_symbol=symbol,
            )
            for _row in range(20)
        ]
    )

    data = _track_project_data(track)
    rows = data["line_guide_symbols"]
    table = data["guide_symbol_table"]

    assert len(table) == 1
    glyph_id = next(iter(table))
    assert rows == [glyph_id] * 20
    assert len(table[glyph_id]["path_commands"]) == 200

    restored = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("歌", 1000)]) for _row in range(20)]
    )
    result = apply_track_project_data(restored, Style(), data)

    assert result.guide_symbol_mismatches == ()
    assert all(line.guide_symbol == symbol for line in restored.lines)


def test_single_use_guide_symbol_stays_inline_in_project_data() -> None:
    """只被引用一次的符号保持内嵌，兼容旧版本读取。"""
    from krok_helper.subtitle_render.domain.timing import GuideSymbol
    from krok_helper.subtitle_render.project.session import _track_project_data
    from krok_helper.subtitle_render.serialization.timing import guide_symbol_to_dict

    symbol = GuideSymbol(
        path_commands=(("M", 0.0, 0.0), ("L", 5.0, -5.0), ("Z",)),
        duration_ms=400,
    )
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar("歌", 1000)], guide_symbol=symbol)])

    data = _track_project_data(track)

    assert data["line_guide_symbols"] == [guide_symbol_to_dict(symbol)]
    assert "guide_symbol_table" not in data


def test_session_to_project_data_accepts_guide_symbol_table() -> None:
    """符号表键必须能穿过 ``project_payload``（恢复自动保存的崩溃路径）。

    回归：``_track_project_data`` 产出 ``guide_symbol_table`` 后经 ``**track_data``
    展开，而 ``project_payload`` 参数表没有该键时保存链路直接 TypeError。
    """
    from krok_helper.subtitle_render.domain.timing import GuideSymbol
    from krok_helper.subtitle_render.project.session import SubtitleProjectDocument

    symbol = GuideSymbol(
        path_commands=(("M", 0.0, 0.0), ("L", 5.0, -5.0), ("Z",)),
        duration_ms=400,
    )
    session = SubtitleProjectDocument()
    session.timing_track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("歌", 1000)], guide_symbol=symbol)
            for _row in range(3)
        ]
    )

    payload = session.to_project_data(
        screen={"width": 1920, "height": 1080, "fps": 60},
        selected_scheme_key="",
        output={"encoder_mode": "cpu"},
    )

    assert payload["line_guide_symbols"] == ["g0", "g0", "g0"]
    from krok_helper.subtitle_render.serialization.timing import guide_symbol_to_dict

    assert payload["guide_symbol_table"]["g0"] == guide_symbol_to_dict(symbol)


def test_anchored_guide_rows_report_mismatch_after_source_rewrap() -> None:
    """保存后源被换行重排：行号错位 + 锚点对不上时报 mismatch，不静默回放。"""
    from krok_helper.subtitle_render.domain.timing import GuideSymbol
    from krok_helper.subtitle_render.serialization.timing import guide_symbol_to_dict

    symbol = GuideSymbol(
        path_commands=(("M", 0.0, 0.0), ("L", 5.0, -5.0), ("Z",)),
        replacement_prefix=("h", "h"),
        replacement_anchor=("歌", "词"),
        count=2,
    )
    # 新源的第一行行首仍是 hh，但正文换成了别句。
    restored = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("h", 0),
                    TimingChar("h", 500),
                    TimingChar("别", 1000),
                    TimingChar("句", 1500),
                ],
                end_ms=2000,
            )
        ]
    )

    result = apply_track_project_data(
        restored,
        Style(),
        {"line_guide_symbols": [guide_symbol_to_dict(symbol)]},
    )

    assert result.guide_symbol_mismatches == (0,)
    assert restored.lines[0].guide_symbol is None
