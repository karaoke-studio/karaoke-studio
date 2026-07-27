from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import stat
import subprocess
import sys
import textwrap
import uuid

import numpy as np
import pytest

from krok_helper.subtitle_render.models import (
    GuideSymbol,
    KaraokeColors,
    KaraokeColorState,
    LyricsLayout,
    LineAnimationOverride,
    PaintFill,
    RubyAnnotation,
    Style,
    SubtitleStyleScheme,
    TimingChar,
    TimingLine,
    TimingTrack,
    TimingTrackMeta,
    TrackPage,
    TrackPagePlan,
    TrackSection,
    TitleOverlay,
    default_title_scheme,
)
from krok_helper.subtitle_render.native_backend import (
    NativeRendererError,
    NativeRendererProcess,
    SharedFrameRingReader,
    _sidecar_environment,
    _sidecar_qt_bin_dir,
    _sidecar_subprocess_kwargs,
    default_native_renderer_path,
    resolve_native_renderer_path,
)
from krok_helper.subtitle_render.native_protocol import (
    RENDER_IR_SCHEMA,
    build_render_ir,
    gpu_unsupported_feature_labels,
    gpu_unsupported_features,
)

_NATIVE_PARITY_DIVERGED = pytest.mark.skipif(
    os.environ.get("KROK_SUBTITLE_NATIVE_PARITY_STRICT") != "1",
    reason=(
        "已知漂移：native CPU 渲染语义落后于 2026-07 的 Python painter 布局改动"
        "（N3 字体像素等），parity 恢复并入 GPU 计划（docs/字幕渲染-GPU后端逆向与实施计划.md §2.3）；"
        "设 KROK_SUBTITLE_NATIVE_PARITY_STRICT=1 可强制运行"
    ),
)


def test_build_render_ir_contains_screen_style_track_and_ruby():
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("君", 100, role_label="A", explicit_start=True),
                    TimingChar("へ", 300, pause_release_ms=450),
                ],
                end_ms=600,
                singer_label="主",
                singer_id=2,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="君",
                reading="きみ",
                reading_part_ms=[100, 250],
                reading_parts=["き", "", "み"],
                pos_start_ms=100,
                pos_end_ms=300,
            )
        ],
    )
    style = Style(font_size_px=64, fill_color="#123456")

    ir = build_render_ir(track, style, width=640, height=360, fps=30)

    assert ir["schema"] == RENDER_IR_SCHEMA
    assert ir["screen"] == {"width": 640, "height": 360, "fps": 30, "dpr": 1.0}
    assert ir["style"]["font_size_px"] == 64
    assert ir["style"]["fill_color"] == "#123456"
    assert ir["style"]["ruby_horizontal_gradient_with_main"] is True
    assert ir["track"]["lines"][0]["singer_id"] == 2
    assert ir["track"]["lines"][0]["chars"][0]["text"] == "君"
    assert ir["track"]["lines"][0]["chars"][0]["role_label"] == "A"
    assert ir["track"]["lines"][0]["chars"][0]["explicit_start"] is True
    assert ir["track"]["lines"][0]["chars"][0]["explicit_end"] is False
    assert ir["track"]["lines"][0]["chars"][1]["pause_release_ms"] == 450
    assert ir["track"]["rubies"][0]["reading"] == "きみ"
    assert ir["track"]["rubies"][0]["reading_part_ms"] == [100, 250]
    assert ir["track"]["rubies"][0]["reading_parts"] == ["き", "", "み"]
    assert ir["extra_tracks"] == []


def test_build_render_ir_preserves_extra_track_boundaries():
    primary = TimingTrack(lines=[TimingLine(chars=[TimingChar("主", 0)], end_ms=500)])
    extra = TimingTrack(lines=[TimingLine(chars=[TimingChar("副", 100)], end_ms=600)])

    ir = build_render_ir(
        primary,
        Style(),
        width=640,
        height=360,
        fps=60,
        extra_tracks=[extra],
    )

    assert ir["track"]["lines"][0]["chars"][0]["text"] == "主"
    assert ir["extra_tracks"][0]["lines"][0]["chars"][0]["text"] == "副"


def test_build_render_ir_resolves_title_metadata_and_windows():
    track = TimingTrack(
        meta=TimingTrackMeta(title="曲名", artist="歌手"),
        lines=[TimingLine(chars=[TimingChar("終", 3_000)], end_ms=4_000)],
    )
    style = Style(
        custom_style_schemes={},
        title_overlay=TitleOverlay(
            enabled=True,
            text_template="{title} / {artist}",
            layout_index=None,
            show_mode="head",
            head_offset_ms=100,
            duration_ms=1_500,
        ),
    )

    ir = build_render_ir(track, style, width=640, height=360, fps=60)

    assert ir["title"]["text"] == "曲名 / 歌手"
    assert ir["title"]["windows"] == [[100, 1_600, 300, 300]]


def test_build_render_ir_anchors_two_segment_title_tail_to_project_duration():
    track = TimingTrack(
        meta=TimingTrackMeta(title="曲名"),
        lines=[TimingLine(chars=[TimingChar("終", 3_000)], end_ms=4_000)],
    )
    style = Style(
        title_overlay=TitleOverlay(
            enabled=True,
            show_mode="head_tail",
            head_offset_ms=2_000,
            duration_ms=6_000,
            fade_in_ms=500,
            fade_out_ms=700,
            tail_offset_ms=3_000,
            tail_duration_ms=9_000,
            tail_fade_in_ms=1_100,
            tail_fade_out_ms=1_300,
        )
    )

    ir = build_render_ir(
        track,
        style,
        width=640,
        height=360,
        fps=60,
        duration_ms=60_000,
    )

    assert ir["title"]["windows"] == [
        [2_000, 8_000, 500, 700],
        [48_000, 57_000, 1_100, 1_300],
    ]


def test_build_render_ir_keeps_title_latin_metrics_out_of_global_lyrics_style():
    track = TimingTrack(
        meta=TimingTrackMeta(title="Title / 01"),
        lines=[TimingLine(chars=[TimingChar("終", 3_000)], end_ms=4_000)],
    )
    title_scheme = replace(
        default_title_scheme(),
        font_size_px=36,
        latin_font_size_px=28,
        font_weight=500,
        latin_font_weight=600,
    )
    style = Style(
        latin_font_size_px=140,
        latin_font_weight=900,
        custom_style_schemes={"标题": title_scheme},
        title_overlay=TitleOverlay(enabled=True),
    )

    title = build_render_ir(track, style, width=640, height=360, fps=60)["title"]

    assert title["font_size_px"] == 36
    assert title["latin_font_size_px"] == 28
    assert title["font_weight"] == 500
    assert title["latin_font_weight"] == 600


def test_build_render_ir_carries_painter_display_schedule():
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("A", 1_000)],
                end_ms=1_500,
                display_start_override_ms=200,
                display_end_override_ms=400,
            ),
            TimingLine(chars=[TimingChar("B", 2_000)], end_ms=2_500),
        ]
    )
    style = Style(dual_line_layout=True, line_lead_in_ms=900, line_tail_ms=700)

    ir = build_render_ir(track, style, width=640, height=360, fps=60)

    first = ir["track"]["lines"][0]
    assert first["lane"] == 0
    assert first["display_start_ms"] == 200
    # Dual-line protection never lets a manual window cut off the sung span.
    assert first["display_end_ms"] == 1_500
    assert ir["track"]["lines"][1]["lane"] == 1
    assert first["page_index"] == 0
    assert ir["track"]["lines"][1]["page_index"] == 0


def test_build_render_ir_carries_painter_page_groups_for_native_smart_horizon():
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("L", 1_000)], end_ms=1_400),
            TimingLine(chars=[TimingChar("R", 2_000)], end_ms=2_400),
            TimingLine(chars=[TimingChar("N", 3_000)], end_ms=3_400),
        ]
    )
    style = Style(
        layout_semantics="n3_1074",
        dual_line_layout=True,
        line_alignments=["left", "right"],
        smart_horizontal="equal_margins",
    )

    lines = build_render_ir(track, style, width=1920, height=1080, fps=60)["track"][
        "lines"
    ]

    assert [(line["page_index"], line["layout_lane"]) for line in lines] == [
        (0, 0),
        (0, 1),
        (2, 1),
    ]
    assert lines[0]["layout"]["smart_horizontal"] == "equal_margins"
    # 末页先保留作者布局中的 Bottom 行位；若与前页真实像素冲突，由共享
    # layout_offset_windows 整页避让，不再提前套用 N3 ForceBottom 上移一次。
    assert [line["page_line_count"] for line in lines] == [2, 2, 1]


def test_build_render_ir_carries_shared_cross_page_layout_offset_windows():
    lines = [
        TimingLine(
            chars=[TimingChar(text, start)],
            end_ms=start + 500,
            display_start_override_ms=0,
            display_end_override_ms=5_000,
        )
        for text, start in (("A", 1_000), ("B", 2_000), ("C", 3_000))
    ]
    track = TimingTrack(
        lines=lines,
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(2, "default"), TrackPage(1, "builtin-1")])]
        ),
    )

    ir_lines = build_render_ir(
        track, Style(), width=1280, height=720, fps=60
    )["track"]["lines"]

    assert ir_lines[0]["layout_offset_y"] == ir_lines[1]["layout_offset_y"] == 0
    assert ir_lines[2]["layout_offset_y"] == 0
    assert all(line["layout_offset_x"] == 0 for line in ir_lines)
    assert ir_lines[0]["layout_offset_windows"][0]["offset_y"] == 0
    assert ir_lines[1]["layout_offset_windows"][0]["offset_y"] == 0
    assert ir_lines[2]["layout_offset_windows"][0]["offset_y"] != 0
    assert all(
        window["start_ms"] == 0 and window["end_ms"] == 5_000
        for line in ir_lines
        for window in line["layout_offset_windows"]
    )

    legacy_lines = build_render_ir(
        track,
        Style(allow_inter_page_line_overlap=True),
        width=1280,
        height=720,
        fps=60,
    )["track"]["lines"]
    assert all(line["layout_offset_x"] == 0 for line in legacy_lines)
    assert all(line["layout_offset_y"] == 0 for line in legacy_lines)
    assert all(not line["layout_offset_windows"] for line in legacy_lines)


def test_build_render_ir_resolves_guide_symbols_with_painter_semantics():
    symbol = GuideSymbol(
        path_commands=(
            ("M", 100.0, 0.0),
            ("L", 500.0, -800.0),
            ("L", 900.0, 0.0),
            ("Z",),
        ),
        duration_ms=400,
        count=2,
        role_labels=("lead-a", "lead-b"),
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("A", 1_000)],
                end_ms=1_500,
                guide_symbol=symbol,
            )
        ]
    )

    line = build_render_ir(track, Style(), width=640, height=360, fps=60)[
        "track"
    ]["lines"][0]

    assert [ch["text"] for ch in line["chars"]] == ["\uFFFC", "\uFFFC", "A"]
    assert [ch["start_ms"] for ch in line["chars"]] == [200, 600, 1_000]
    assert [ch["role_label"] for ch in line["chars"][:2]] == [
        "lead-a",
        "lead-b",
    ]
    assert line["chars"][0]["vector_glyph"]["advance_width"] == 1_000.0
    assert line["resolved_intervals"][0][0] == 200
    assert len(line["resolved_intervals"]) == 3
    assert gpu_unsupported_features(track, Style()) == ()


def test_build_render_ir_serializes_bitmap_guide_symbol(tmp_path: Path):
    image_path = tmp_path / "lead.png"
    image_path.write_bytes(b"png")
    symbol = GuideSymbol(
        kind="bitmap",
        bitmap_before_path=str(image_path),
        bitmap_zoom_percent=120,
        bitmap_no_decor=True,
        bitmap_margin_right_px=7,
        bitmap_margin_bottom_px=20,
        duration_ms=400,
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("A", 1_000)],
                end_ms=1_500,
                guide_symbol=symbol,
            )
        ]
    )

    line = build_render_ir(track, Style(), width=640, height=360, fps=60)[
        "track"
    ]["lines"][0]
    bitmap = line["chars"][0]["bitmap_guide"]

    assert line["chars"][0]["text"] == "\uFFFC"
    assert bitmap["before_path"] == str(image_path)
    assert bitmap["zoom_percent"] == 120
    assert bitmap["no_decor"] is True
    assert bitmap["margin_right_px"] == 7
    assert bitmap["margin_bottom_px"] == 20
    assert bitmap["before_size"] == 3
    assert line["guide_anchor_bounds"] is None
    assert gpu_unsupported_features(track, Style()) == ()


def test_gpu_capability_gate_rejects_only_unimplemented_whole_scene_features():
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar("A", 0)], end_ms=500)])

    assert gpu_unsupported_features(track, Style()) == ()
    assert gpu_unsupported_features(
        track, Style(entry_anim="future_effect")
    ) == ("line_animation",)
    unknown_override = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("A", 0)],
                end_ms=500,
                animation_override=LineAnimationOverride(
                    entry_anim="future_effect"
                ),
            )
        ]
    )
    assert gpu_unsupported_features(
        unknown_override, Style()
    ) == ("line_animation_override",)
    assert gpu_unsupported_features(
        track, Style(vertical=True, decoration_kind="shadow")
    ) == ()
    assert gpu_unsupported_features(
        track, Style(vertical=True, decoration_kind="glow")
    ) == ()
    assert gpu_unsupported_features(track, Style(entry_anim="fade")) == ()
    assert gpu_unsupported_features(track, Style(entry_anim="char_fade")) == ()
    assert gpu_unsupported_features(track, Style(entry_anim="char_drip")) == ()
    assert gpu_unsupported_features(track, Style(entry_anim="spin_flip")) == ()
    assert gpu_unsupported_features(track, Style(entry_anim="utopia")) == ()
    assert gpu_unsupported_features(track, Style(karaoke_anim="utopia")) == ()
    assert gpu_unsupported_features(
        track, Style(karaoke_anim="future_effect")
    ) == ("karaoke_animation",)
    assert gpu_unsupported_feature_labels(
        ("bitmap_guide_symbol", "karaoke_animation")
    ) == ("图片导唱符 / N3 Emoji 头像", "未知走字特效")
    assert gpu_unsupported_features(track, Style(lit_enabled=True)) == ()
    assert gpu_unsupported_features(track, Style(right_to_left=True)) == ()
    assert gpu_unsupported_features(
        track,
        Style(
            line_horizontal_layout="per_row",
            row1_align="right",
            row1_offset_x=-45,
            row1_offset_y=18,
        ),
    ) == ()
    assert gpu_unsupported_features(
        track,
        Style(
            viewport_scale_pct=125,
            viewport_rotation_deg=-20,
            viewport_offset_x=30,
            viewport_offset_y=-15,
            viewport_align="bottom_right",
        ),
    ) == ()
    for lit_style in ("circle", "square", "rounded"):
        assert gpu_unsupported_features(
            track, Style(lit_enabled=True, lit_style=lit_style)
        ) == ()
    ruby_track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("漢", 0)], end_ms=500)],
        rubies=[
            RubyAnnotation(
                kanji="漢",
                reading="かん",
                pos_start_ms=0,
                pos_end_ms=500,
            )
        ],
    )
    assert gpu_unsupported_features(ruby_track, Style(entry_anim="utopia")) == ()
    assert gpu_unsupported_features(
        ruby_track, Style(right_to_left=True)
    ) == ()
    assert gpu_unsupported_features(ruby_track, Style(vertical=True)) == ()
    assert gpu_unsupported_features(
        ruby_track, Style(vertical=True, entry_anim="fade")
    ) == ()
    assert gpu_unsupported_features(
        ruby_track, Style(vertical=True, entry_anim="utopia")
    ) == ()
    assert gpu_unsupported_features(
        ruby_track, Style(right_to_left=True, entry_anim="slide_in")
    ) == ()
    assert gpu_unsupported_features(
        ruby_track, Style(right_to_left=True, entry_anim="spin_flip")
    ) == ()
    combo_track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("A", 0, role_label="role")], end_ms=500
            )
        ]
    )
    assert gpu_unsupported_features(
        combo_track,
        Style(
            vertical=True,
            right_to_left=True,
            lit_enabled=True,
            title_overlay=TitleOverlay(enabled=True, text_template="Title"),
        ),
    ) == ()
    span_track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar(
                        "W",
                        0,
                        source_span_start_ms=0,
                        source_span_end_ms=1_500,
                        source_span_count=3,
                        source_span_index=0,
                    ),
                    TimingChar(
                        " ",
                        500,
                        source_span_start_ms=0,
                        source_span_end_ms=1_500,
                        source_span_count=3,
                        source_span_index=1,
                    ),
                    TimingChar(
                        "M",
                        1_000,
                        source_span_start_ms=0,
                        source_span_end_ms=1_500,
                        source_span_count=3,
                        source_span_index=2,
                    ),
                ],
                end_ms=1_500,
            )
        ]
    )
    assert gpu_unsupported_features(span_track, Style()) == ()
    span_line = build_render_ir(
        span_track,
        Style(font_family="Times New Roman", font_family_latin="Times New Roman"),
        width=640,
        height=360,
        fps=60,
    )["track"]["lines"][0]
    assert span_line["resolved_intervals"][0][0] == 0
    assert span_line["resolved_intervals"][-1][1] == 1_500
    assert span_line["resolved_intervals"] != [
        [0, 500],
        [500, 1_000],
        [1_000, 1_500],
    ]

    layout_track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("L", 0)], end_ms=500, layout_index=1
            )
        ]
    )
    layout_style = Style(
        layouts=[
            LyricsLayout(
                line_y_position="top",
                line_y_margin_px=33,
                line_alignments=["right"],
                letter_spacing_px=9,
                ruby_gap_px=7,
            )
        ]
    )
    assert gpu_unsupported_features(layout_track, layout_style) == ()
    line_layout = build_render_ir(
        layout_track, layout_style, width=640, height=360, fps=60
    )["track"]["lines"][0]["layout"]
    assert line_layout["line_y_position"] == "top"
    assert line_layout["line_y_margin_px"] == 33
    assert line_layout["line_alignments"] == ["right"]
    assert line_layout["letter_spacing_px"] == 9
    assert line_layout["ruby_gap_px"] == 7


def test_build_render_ir_expands_display_window_for_volume_signal_lead_in():
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("A", 10_000)], end_ms=11_000)]
    )
    style = Style(
        dual_line_layout=False,
        line_lead_in_ms=500,
        lit_enabled=True,
        signals_duration_ms=4_000,
        lit_waiting_time_ms=500,
        lit_time_offset_ms=-250,
    )

    line = build_render_ir(track, style, width=640, height=360, fps=60)["track"][
        "lines"
    ][0]

    # Painter reserves duration + waiting - offset = 4750 ms for the complete
    # Sayatoo signal window (the waiting portion is part of the display lead).
    assert line["display_start_ms"] == 5_250


def test_build_render_ir_resolves_global_and_per_line_basic_animations():
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("A", 1_000)], end_ms=2_000),
            TimingLine(
                chars=[TimingChar("B", 2_500)],
                end_ms=3_500,
                animation_override=LineAnimationOverride(
                    entry_anim="slide_in",
                    entry_duration_ms=700,
                    exit_anim="rise",
                    exit_duration_ms=600,
                ),
            ),
        ]
    )
    style = Style(
        entry_anim="fade",
        entry_lead_ms=900,
        exit_anim="slide_out",
        exit_fade_ms=800,
    )

    lines = build_render_ir(track, style, width=640, height=360, fps=60)["track"][
        "lines"
    ]

    assert lines[0]["entry_anim"] == "fade"
    assert lines[0]["entry_duration_ms"] == 900
    assert lines[0]["exit_anim"] == "slide_out"
    assert lines[0]["exit_duration_ms"] == 800
    assert lines[1]["entry_anim"] == "slide_in"
    assert lines[1]["entry_duration_ms"] == 700
    assert lines[1]["exit_anim"] == "rise"
    assert lines[1]["exit_duration_ms"] == 600
    assert gpu_unsupported_features(track, style) == ()


def test_build_render_ir_uses_compressed_entry_and_exit_animation_windows():
    begins = [10_000, 10_500, 12_100, 12_500]
    ends = [12_000, 12_400, 13_500, 14_000]
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar(chr(65 + index), begin)], end_ms=ends[index])
            for index, begin in enumerate(begins)
        ],
        page_plan=TrackPagePlan(
            [TrackSection([TrackPage(2, "default"), TrackPage(2, "default")])]
        ),
    )
    style = Style(
        dual_line_layout=True,
        entry_anim="fade",
        entry_lead_ms=900,
        exit_anim="slide_out",
        exit_fade_ms=800,
    )

    lines = build_render_ir(track, style, width=640, height=360, fps=60)["track"][
        "lines"
    ]

    assert lines[0]["display_end_ms"] == ends[0]
    assert lines[0]["exit_duration_ms"] == 0
    # Pixel-gated compression changes only the A/C conflict pair.  C keeps its
    # complete configured 900 ms entry animation, but may only move as far as
    # D's original page-order boundary; the unresolved overlap is spatially
    # avoided instead of rewriting D.
    assert lines[2]["display_start_ms"] == begins[3] - 1_800
    assert lines[2]["entry_duration_ms"] == 900


def test_build_render_ir_resolves_independent_karaoke_animation():
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("A", 1_000)], end_ms=2_000)]
    )

    inherited = build_render_ir(
        track, Style(entry_anim="utopia"), width=640, height=360, fps=60
    )["track"]["lines"][0]
    disabled = build_render_ir(
        track,
        Style(entry_anim="utopia", karaoke_anim="none"),
        width=640,
        height=360,
        fps=60,
    )["track"]["lines"][0]
    standalone = build_render_ir(
        track, Style(karaoke_anim="utopia"), width=640, height=360, fps=60
    )["track"]["lines"][0]

    assert inherited["karaoke_anim"] == "utopia"
    assert disabled["karaoke_anim"] == "none"
    assert standalone["karaoke_anim"] == "utopia"


def test_build_render_ir_clamps_screen_values():
    # dpr=0 视为未设置（与预览侧 `float(dpr or 1.0)` 语义一致），负值钳制到下限。
    ir = build_render_ir(TimingTrack(), Style(), width=0, height=-1, fps=0, dpr=0.0)
    assert ir["screen"] == {"width": 1, "height": 1, "fps": 1, "dpr": 1.0}

    negative_ir = build_render_ir(TimingTrack(), Style(), width=640, height=360, fps=60, dpr=-2.0)
    assert negative_ir["screen"]["dpr"] == 0.01

    default_ir = build_render_ir(TimingTrack(), Style(), width=640, height=360, fps=60)
    assert default_ir["screen"]["dpr"] == 1.0


def test_default_native_renderer_path_uses_build_tree():
    root = Path("D:/repo")
    assert default_native_renderer_path(root) == root / "build" / "native-renderer" / "krok_subtitle_renderer.exe"


def test_sidecar_subprocess_hides_console_on_windows_only():
    assert _sidecar_subprocess_kwargs("win32") == {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
    }
    assert _sidecar_subprocess_kwargs("linux") == {}


def test_resolve_native_renderer_path_prefers_explicit_existing_path(tmp_path, monkeypatch):
    exe = tmp_path / "renderer.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_RENDERER", str(tmp_path / "missing.exe"))

    assert resolve_native_renderer_path(exe) == exe


def test_resolve_native_renderer_path_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("KROK_SUBTITLE_NATIVE_RENDERER", raising=False)
    assert resolve_native_renderer_path(root=tmp_path) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows PyInstaller layout")
def test_sidecar_environment_uses_frozen_pyqt_runtime(tmp_path, monkeypatch):
    package_root = tmp_path / "Karaoke Studio"
    qt_bin = package_root / "_internal" / "PyQt6" / "Qt6" / "bin"
    plugin_root = qt_bin.parent / "plugins"
    qt_bin.mkdir(parents=True)
    (plugin_root / "platforms").mkdir(parents=True)
    (qt_bin / "Qt6Core.dll").write_bytes(b"")
    (plugin_root / "platforms" / "qwindows.dll").write_bytes(b"")
    app_exe = package_root / "Karaoke Studio.exe"
    sidecar_exe = package_root / "krok_subtitle_renderer.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app_exe))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setenv("PATH", "existing")

    assert _sidecar_qt_bin_dir(sidecar_exe) == qt_bin
    env = _sidecar_environment(sidecar_exe)
    assert env is not None
    assert env["PATH"] == f"{qt_bin}{os.pathsep}existing"
    assert env["QT_PLUGIN_PATH"] == str(plugin_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows native build layout")
def test_sidecar_environment_uses_loaded_qt_runtime_version(tmp_path, monkeypatch):
    from PyQt6 import QtCore

    sidecar_exe = tmp_path / "renderer.exe"
    sidecar_exe.write_bytes(b"")
    qt_bin = (
        tmp_path
        / "krok-helper"
        / "qt"
        / "6.11.1"
        / "msvc2022_64"
        / "bin"
    )
    qt_bin.mkdir(parents=True)
    (qt_bin / "Qt6Core.dll").write_bytes(b"")
    monkeypatch.delenv("KROK_SUBTITLE_NATIVE_RENDERER", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(QtCore, "qVersion", lambda: "6.11.1")

    assert _sidecar_qt_bin_dir(sidecar_exe) == qt_bin


def _write_fake_sidecar(tmp_path: Path, *, mode: str = "normal") -> Path:
    script = tmp_path / "fake_sidecar.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json
            import sys
            import time

            mode = {mode!r}

            sys.stdout.write("qt debug noise before ready\\n")
            sys.stdout.flush()
            for i in range(160):
                sys.stderr.write("qt warning %03d %s\\n" % (i, "x" * 1024))
            sys.stderr.flush()
            print(json.dumps({{"ok": True, "event": "ready", "schema": 1}}), flush=True)

            for raw in sys.stdin:
                request = json.loads(raw)
                command = request.get("cmd")
                if mode == "hang_after_ready":
                    time.sleep(30)
                if command == "configure":
                    print(json.dumps({{"ok": True, "event": "configured"}}), flush=True)
                elif command == "render_frame":
                    print(json.dumps({{"ok": True, "event": "frame_ready", "checksum": "fake", "render_ms": 1.25}}), flush=True)
                elif command == "render_frame_stats":
                    print(json.dumps({{"ok": True, "event": "frame_stats", "checksum": "fake", "render_ms": 1.25}}), flush=True)
                elif command == "render_range_stats":
                    frames = [
                        {{"t_ms": t_ms, "render_ms": 1.5, "checksum": "fake", "visible_lines": 1}}
                        for t_ms in request.get("t_ms", [])
                    ]
                    print(json.dumps({{"ok": True, "event": "range_stats", "frames": len(frames), "threads": request.get("threads", 1), "elapsed_ms": 3.0, "frame_stats": frames}}), flush=True)
                elif command == "render_range":
                    generation = request.get("generation", 0)
                    frames = request.get("t_ms", [])
                    shm_key = request.get("shm_key", "fake-shm")
                    ring_slots = request.get("ring_slots", 3)
                    print(json.dumps({{"ok": True, "event": "range_started", "generation": generation, "frames": len(frames), "threads": request.get("threads", 1), "shm_key": shm_key, "ring_slots": ring_slots, "width": 640, "height": 360}}), flush=True)
                    for index, t_ms in enumerate(frames):
                        print(json.dumps({{"ok": True, "event": "frame_ready", "generation": generation, "frame_index": index, "t_ms": t_ms, "render_ms": 1.5, "checksum": "fake", "payload": "shared_memory", "shm_key": shm_key, "slot_index": index % ring_slots, "slot_count": ring_slots, "slot_offset": 0, "slot_bytes": 64, "header_bytes": 64, "payload_offset": 64, "payload_bytes": 0, "width": 640, "height": 360, "stride": 2560, "pixel_format": "rgba8888"}}), flush=True)
                    print(json.dumps({{"ok": True, "event": "range_done", "generation": generation, "frames": len(frames), "frames_done": len(frames), "frames_emitted": len(frames), "cancelled": False}}), flush=True)
                elif command == "cancel_generation":
                    print(json.dumps({{"ok": True, "event": "generation_cancelled", "generation": request.get("generation", 0)}}), flush=True)
                elif command == "shutdown":
                    print(json.dumps({{"ok": True, "event": "shutdown"}}), flush=True)
                    break
                else:
                    print(json.dumps({{"ok": False, "event": "error", "error": "bad command"}}), flush=True)
            """
        ),
        encoding="utf-8",
    )

    if os.name == "nt":
        launcher = tmp_path / "fake_sidecar.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return launcher

    launcher = tmp_path / "fake_sidecar"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    return launcher


def test_native_renderer_process_round_trips_with_noisy_sidecar(tmp_path):
    sidecar = _write_fake_sidecar(tmp_path)
    renderer = NativeRendererProcess(sidecar, response_timeout_s=2.0, close_timeout_s=1.0)

    ready = renderer.start()
    assert ready["event"] == "ready"
    assert renderer.configure(TimingTrack(), Style(), width=640, height=360, fps=60)["event"] == "configured"
    assert renderer.render_frame_png(900, tmp_path / "frame.png")["event"] == "frame_ready"
    stats = renderer.render_frame_stats(900)
    assert stats["event"] == "frame_stats"
    assert stats["render_ms"] == 1.25
    range_stats = renderer.render_range_stats([900, 917], threads=2)
    assert range_stats["event"] == "range_stats"
    assert range_stats["threads"] == 2
    assert len(range_stats["frame_stats"]) == 2
    started = renderer.start_render_range([900, 917], generation=7, threads=2, shm_key="fake-shm", ring_slots=2)
    assert started["event"] == "range_started"
    assert started["generation"] == 7
    assert started["shm_key"] == "fake-shm"
    first_frame = renderer.read_event()
    assert first_frame["event"] == "frame_ready"
    assert first_frame["payload"] == "shared_memory"
    assert first_frame["slot_index"] == 0
    second_frame = renderer.read_event()
    assert second_frame["event"] == "frame_ready"
    assert second_frame["slot_index"] == 1
    assert renderer.read_event()["event"] == "range_done"
    assert renderer.cancel_generation(7)["event"] == "generation_cancelled"

    renderer.close()
    assert renderer.is_running is False


def test_native_renderer_process_can_send_cancel_without_consuming_response(tmp_path):
    sidecar = _write_fake_sidecar(tmp_path)

    with NativeRendererProcess(sidecar, response_timeout_s=2.0, close_timeout_s=1.0) as renderer:
        renderer.send_cancel_generation(11)

        event = renderer.read_event()

    assert event["event"] == "generation_cancelled"
    assert event["generation"] == 11


def test_native_render_range_shared_memory_reader_reads_slot_when_exe_exists(monkeypatch):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("K", 0),
                    TimingChar("a", 400),
                    TimingChar("r", 800),
                    TimingChar("a", 1200),
                ],
                end_ms=1800,
            )
        ],
    )
    style = Style(
        font_size_px=48,
        line_lead_in_ms=0,
        line_tail_ms=300,
        stroke_width_px=4,
        stroke2_width_px=0,
        line_y_position="center",
    )
    generation = 41
    shm_key = f"krok-test-shm-reader-{os.getpid()}-{uuid.uuid4().hex}"

    with NativeRendererProcess(renderer_path, response_timeout_s=3.0, close_timeout_s=1.0) as renderer:
        renderer.configure(track, style, width=320, height=180, fps=60)
        started = renderer.start_render_range(
            [900],
            generation=generation,
            threads=2,
            shm_key=shm_key,
            ring_slots=2,
        )
        assert started["event"] == "range_started"
        assert started["shm_key"] == shm_key

        frame = renderer.read_event()
        assert frame["event"] == "frame_ready"
        assert frame["payload"] == "shared_memory"
        with SharedFrameRingReader.from_event(frame) as reader:
            slot = reader.read_frame(frame)
            image = slot.to_qimage()

        assert slot.shm_key == shm_key
        assert slot.generation == generation
        assert slot.frame_index == 0
        assert slot.t_ms == 900
        assert slot.width == 320
        assert slot.height == 180
        assert slot.stride >= slot.width * 4
        assert slot.pixel_format == "rgba8888"
        assert len(slot.payload) == int(frame["payload_bytes"])
        rows = np.frombuffer(slot.payload, dtype=np.uint8).reshape(slot.height, slot.stride)
        assert int(rows[:, 3 : slot.width * 4 : 4].max()) > 0
        assert image.width() == slot.width
        assert image.height() == slot.height
        assert renderer.read_event()["event"] == "range_done"


def test_native_render_range_respects_preview_dpr_when_exe_exists(monkeypatch):
    """dpr 缩放：布局在逻辑坐标系、光栅化画布按 dpr 收缩（4K 预览按显示分辨率渲染）。"""
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("K", 0), TimingChar("a", 400)],
                end_ms=1200,
            )
        ],
    )
    style = Style(
        font_size_px=48,
        line_lead_in_ms=0,
        line_tail_ms=300,
        stroke_width_px=4,
        stroke2_width_px=0,
        line_y_position="center",
    )
    shm_key = f"krok-test-shm-dpr-{os.getpid()}-{uuid.uuid4().hex}"

    with NativeRendererProcess(renderer_path, response_timeout_s=3.0, close_timeout_s=1.0) as renderer:
        configured = renderer.configure(track, style, width=640, height=360, fps=60, dpr=0.5)
        assert configured["width"] == 640
        assert configured["height"] == 360
        assert configured["dpr"] == 0.5
        assert configured["physical_width"] == 320
        assert configured["physical_height"] == 180

        started = renderer.start_render_range(
            [600],
            generation=7,
            threads=1,
            shm_key=shm_key,
            ring_slots=2,
        )
        assert started["event"] == "range_started"

        frame = renderer.read_event()
        assert frame["event"] == "frame_ready"
        with SharedFrameRingReader.from_event(frame) as reader:
            slot = reader.read_frame(frame)
        # 帧按物理分辨率交付，且缩放后画面仍有内容（字幕没有画出画布外）。
        assert slot.width == 320
        assert slot.height == 180
        rows = np.frombuffer(slot.payload, dtype=np.uint8).reshape(slot.height, slot.stride)
        assert int(rows[:, 3 : slot.width * 4 : 4].max()) > 0
        assert renderer.read_event()["event"] == "range_done"


def test_native_gpu_preview_layout_is_scale_invariant_when_exe_exists(
    qapp,
    monkeypatch,
):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    body_text = "こういって繋いでいられたなら"
    body_track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(char, index * 100) for index, char in enumerate(body_text)],
                end_ms=3_000,
            )
        ]
    )
    ruby_track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("存", 0), TimingChar("在", 1_000)],
                end_ms=2_000,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="存在",
                reading="そんざい",
                reading_part_ms=[500, 1_000, 1_500],
                pos_start_ms=0,
                pos_end_ms=2_000,
            )
        ],
    )
    style = Style(
        font_family="Yu Gothic",
        font_size_px=100,
        letter_spacing_px=7,
        ruby_font_size_px=80,
        ruby_stroke_width_px=8,
        ruby_gap_px=4,
        line_lead_in_ms=0,
        stroke_width_px=15,
        stroke2_enabled=False,
        decoration_kind="none",
    )

    def alpha_bounds(renderer, track: TimingTrack, dpr: float, generation: int):
        renderer.configure_gpu(
            track,
            style,
            width=1_920,
            height=1_080,
            fps=60,
            dpr=dpr,
            force_warp=True,
            realization_enabled=False,
        )
        event = renderer.render_gpu_frame(
            1_200,
            force_warp=True,
            generation=generation,
            shm_key=f"krok-layout-dpr-{os.getpid()}-{uuid.uuid4().hex}",
            include_checksum=False,
            readback_bands=False,
        )
        with SharedFrameRingReader.from_event(event) as reader:
            slot = reader.read_frame(event)
        rows = np.frombuffer(slot.payload, dtype=np.uint8).reshape(
            slot.height, slot.stride
        )
        alpha = rows[:, 3 : slot.width * 4 : 4]
        y, x = np.where(alpha > 0)
        assert x.size > 0 and y.size > 0
        return float(x.min()), float(x.max() + 1)

    with NativeRendererProcess(
        renderer_path, response_timeout_s=5.0, close_timeout_s=1.0
    ) as renderer:
        generation = 0
        for track in (body_track, ruby_track):
            generation += 1
            export_left, export_right = alpha_bounds(renderer, track, 1.0, generation)
            for dpr in (0.25, 0.5):
                generation += 1
                preview_left, preview_right = alpha_bounds(
                    renderer, track, dpr, generation
                )
                # Antialias coverage may add or remove one target pixel, but
                # layout must no longer accumulate one rounding error per glyph.
                assert preview_left == pytest.approx(export_left * dpr, abs=1.0)
                assert preview_right == pytest.approx(export_right * dpr, abs=1.0)


def test_native_gpu_title_uses_title_latin_size_and_reconfigures_when_exe_exists(
    monkeypatch,
):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        meta=TimingTrackMeta(title="A/A"),
        lines=[TimingLine(chars=[TimingChar("終", 5_000)], end_ms=6_000)],
    )

    def title_style(size: int) -> Style:
        scheme = replace(
            default_title_scheme(),
            font_family="Arial",
            font_family_latin="Arial",
            font_size_px=size,
            latin_font_size_px=size,
            font_weight=400,
            latin_font_weight=400,
            stroke_width_px=0,
            latin_stroke_width_px=0,
            stroke2_enabled=False,
            decoration_kind="none",
        )
        return Style(
            font_family="Arial",
            font_family_latin="Arial",
            font_size_px=40,
            latin_font_size_px=120,
            line_lead_in_ms=0,
            stroke_width_px=0,
            stroke2_width_px=0,
            decoration_kind="none",
            custom_style_schemes={"标题": scheme},
            title_overlay=TitleOverlay(
                enabled=True,
                text_template="{title}",
                layout_index=None,
                offset_x=10,
                offset_y=10,
                fade_in_ms=0,
                fade_out_ms=0,
            ),
        )

    def alpha_height(slot) -> int:
        rows = np.frombuffer(slot.payload, dtype=np.uint8).reshape(
            slot.height, slot.stride
        )
        alpha = rows[:, 3 : slot.width * 4 : 4]
        y, _ = np.where(alpha > 0)
        assert y.size > 0
        return int(y.max() - y.min() + 1)

    with NativeRendererProcess(
        renderer_path, response_timeout_s=5.0, close_timeout_s=1.0
    ) as renderer:
        heights = []
        for generation, size in enumerate((20, 48), start=1):
            renderer.configure_gpu(
                track,
                title_style(size),
                width=320,
                height=180,
                fps=60,
                force_warp=True,
            )
            event = renderer.render_gpu_frame(
                1_000,
                force_warp=True,
                generation=generation,
                shm_key=f"krok-title-size-{os.getpid()}-{uuid.uuid4().hex}",
                readback_bands=False,
            )
            with SharedFrameRingReader.from_event(event) as reader:
                heights.append(alpha_height(reader.read_frame(event)))

    small_height, large_height = heights
    assert small_height < 40
    assert large_height > small_height * 1.5


def test_native_gpu_title_uses_project_timeline_and_independent_segment_fades(
    monkeypatch,
):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    # ``max_alpha`` 扫的是整帧，所以歌词行在采样时刻必须不在屏上，否则它的不透明
    # 像素会盖掉标题的淡入值。这一句实际演唱时刻 = 5000 + meta 2000 + style 3000
    # = 10_000，显示窗口 [10_000 − PreTime, 11_000 + PostTime] = [8200, 12_000]，
    # 与下面三个采样点（500 / 5500 / 7500）都不重叠。
    track = TimingTrack(
        meta=TimingTrackMeta(title="TITLE", offset_ms=2_000),
        lines=[TimingLine(chars=[TimingChar("終", 5_000)], end_ms=6_000)],
    )
    scheme = replace(
        default_title_scheme(),
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=52,
        latin_font_size_px=52,
        font_weight=400,
        latin_font_weight=400,
        stroke_width_px=0,
        latin_stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
    )
    style = Style(
        timing_offset_ms=3_000,
        custom_style_schemes={"标题": scheme},
        title_overlay=TitleOverlay(
            enabled=True,
            text_template="{title}",
            layout_index=None,
            show_mode="head_tail",
            duration_ms=2_000,
            fade_in_ms=1_000,
            fade_out_ms=0,
            tail_offset_ms=1_000,
            tail_duration_ms=2_000,
            tail_fade_in_ms=2_000,
            tail_fade_out_ms=0,
        ),
    )

    def max_alpha(renderer: NativeRendererProcess, t_ms: int, generation: int) -> int:
        event = renderer.render_gpu_frame(
            t_ms,
            force_warp=True,
            generation=generation,
            shm_key=f"krok-title-fades-{os.getpid()}-{uuid.uuid4().hex}",
            readback_bands=False,
        )
        with SharedFrameRingReader.from_event(event) as reader:
            slot = reader.read_frame(event)
        rows = np.frombuffer(slot.payload, dtype=np.uint8).reshape(
            slot.height, slot.stride
        )
        return int(rows[:, 3 : slot.width * 4 : 4].max())

    with NativeRendererProcess(
        renderer_path, response_timeout_s=5.0, close_timeout_s=1.0
    ) as renderer:
        renderer.configure_gpu(
            track,
            style,
            width=320,
            height=180,
            fps=60,
            force_warp=True,
            duration_ms=10_000,
        )
        head_half = max_alpha(renderer, 500, 1)
        tail_quarter = max_alpha(renderer, 7_500, 2)
        outside = max_alpha(renderer, 5_500, 3)

    # The title is not shifted by the 3s global + 2s track lyric offsets.
    assert head_half > 0
    # Tail fade-in is 2s, so at +0.5s it is visibly dimmer than the opening
    # segment at +0.5s of its 1s fade-in.
    assert 0 < tail_quarter < head_half * 0.7
    assert outside == 0


def test_native_renderer_process_times_out_when_sidecar_stalls(tmp_path):
    sidecar = _write_fake_sidecar(tmp_path, mode="hang_after_ready")
    renderer = NativeRendererProcess(sidecar, response_timeout_s=0.3, close_timeout_s=1.0)

    renderer.start()
    with pytest.raises(NativeRendererError, match="timed out"):
        renderer.configure(TimingTrack(), Style(), width=640, height=360, fps=60)

    renderer.close()
    assert renderer.is_running is False


def test_native_text_layer_cache_reuses_static_main_and_ruby_layers(tmp_path, monkeypatch):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("真", 0),
                    TimingChar("実", 700),
                    TimingChar(" ", 1200),
                    TimingChar("物", 1500),
                    TimingChar("憂", 2200),
                ],
                end_ms=3200,
            )
        ],
        rubies=[
            RubyAnnotation("真実", "しんじつ", pos_start_ms=0, pos_end_ms=2),
            RubyAnnotation("物憂", "ものう", pos_start_ms=3, pos_end_ms=5),
        ],
    )
    style = Style(
        font_family="Arial",
        font_size_px=72,
        ruby_font_size_px=24,
        line_lead_in_ms=0,
        line_y_position="center",
        line_horizontal_layout="center",
        stroke_width_px=6,
        stroke2_width_px=2,
        decoration_kind="shadow",
        shadow_offset_x=2,
        shadow_offset_y=3,
    )

    with NativeRendererProcess(renderer_path, response_timeout_s=2.0, close_timeout_s=1.0) as renderer:
        renderer.configure(track, style, width=960, height=540, fps=60)
        first = renderer.render_frame_stats(1800)
        second = renderer.render_frame_stats(1800)

    assert first["checksum"] == second["checksum"]
    assert "text_layer_cache_hits" in first
    assert first["text_layer_cache_misses"] > 0
    assert second["text_layer_cache_hits"] > first["text_layer_cache_hits"]
    assert second["text_layer_cache_misses"] == first["text_layer_cache_misses"]
    assert second["text_layer_cache_size"] >= first["text_layer_cache_size"] > 0
    assert "layout_cache_hits" in first
    assert first["layout_cache_misses"] > 0
    assert second["layout_cache_hits"] > first["layout_cache_hits"]
    assert second["layout_cache_misses"] == first["layout_cache_misses"]


def test_native_text_layer_cache_reuses_inline_role_runs(tmp_path, monkeypatch):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    def fill(color: str) -> PaintFill:
        return PaintFill(color=color)

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("A", 0, role_label="lead"),
                    TimingChar("B", 600, role_label="lead"),
                    TimingChar("C", 1200, role_label="back"),
                    TimingChar("D", 1800, role_label="back"),
                ],
                end_ms=2600,
            )
        ]
    )
    style = Style(
        font_family="Arial",
        font_size_px=56,
        line_lead_in_ms=0,
        line_y_position="center",
        line_horizontal_layout="center",
        stroke_width_px=5,
        stroke2_width_px=1,
        decoration_kind="shadow",
        shadow_offset_x=2,
        shadow_offset_y=2,
        custom_style_schemes={
            "lead": SubtitleStyleScheme(
                font_family="Arial",
                font_size_px=70,
                karaoke_colors=KaraokeColors(
                    before=KaraokeColorState(text=fill("#EFFFFF")),
                    after=KaraokeColorState(text=fill("#FF3366")),
                ),
            ),
            "back": SubtitleStyleScheme(
                font_family="Arial",
                font_size_px=50,
                karaoke_colors=KaraokeColors(
                    before=KaraokeColorState(text=fill("#DDE8FF")),
                    after=KaraokeColorState(text=fill("#55AAFF")),
                ),
            ),
        },
    )

    with NativeRendererProcess(renderer_path, response_timeout_s=2.0, close_timeout_s=1.0) as renderer:
        renderer.configure(track, style, width=960, height=540, fps=60)
        first = renderer.render_frame_stats(1500)
        second = renderer.render_frame_stats(1500)

    assert first["checksum"] == second["checksum"]
    assert first["text_layer_cache_misses"] > 0
    assert second["text_layer_cache_hits"] > first["text_layer_cache_hits"]
    assert second["text_layer_cache_misses"] == first["text_layer_cache_misses"]


def test_native_gpu_role_ruby_decoration_follows_role_main_not_global():
    """GPU: a role's ruby with no explicit decoration follows the ROLE's main.

    The global main decoration is ``shadow`` while role ``A``'s main is
    ``glow``. Rendering role ``A`` three ways on the GPU sidecar pins the
    resolution order the painter oracle already uses:

    * ``ruby_decoration_kind`` unset      -> must equal the role's own main (glow)
    * ``ruby_decoration_kind="shadow"``   -> the global main, must differ

    The sidecar used to bake the ruby decoration from the *global* main while
    parsing the base style, so a role that overrode only its main left the ruby
    on the global decoration: the unset frame came out byte-identical to the
    global-shadow frame instead of the role-glow one.
    """
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None

    def fill(color: str) -> PaintFill:
        return PaintFill(color=color)

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("A", 0, role_label="A"),
                    TimingChar("B", 800, role_label="A"),
                    TimingChar("C", 1600, role_label="A"),
                ],
                end_ms=2400,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="AB",
                reading="xy",
                reading_part_ms=[800],
                pos_start_ms=0,
                pos_end_ms=1600,
            )
        ],
    )
    main_colors = KaraokeColors(
        before=KaraokeColorState(
            text=fill("#FFFFFF"), stroke=fill("#111111"),
            stroke2=fill("#00000000"), shadow=fill("#3CE0FF"),
        ),
        after=KaraokeColorState(
            text=fill("#FF5A6F"), stroke=fill("#111111"),
            stroke2=fill("#00000000"), shadow=fill("#FFD54A"),
        ),
    )
    ruby_colors = KaraokeColors(
        before=KaraokeColorState(
            text=fill("#00FF88"), stroke=fill("#111111"),
            stroke2=fill("#00000000"), shadow=fill("#3CE0FF"),
        ),
        after=KaraokeColorState(
            text=fill("#FFCC00"), stroke=fill("#111111"),
            stroke2=fill("#00000000"), shadow=fill("#FFD54A"),
        ),
    )

    def style_with_ruby_decoration(ruby_decoration):
        scheme = SubtitleStyleScheme(
            decoration_kind="glow",
            glow_before_radius_px=6,
            glow_after_radius_px=6,
            ruby_glow_before_radius_px=18,
            ruby_glow_after_radius_px=18,
            karaoke_colors=main_colors,
            ruby_karaoke_colors=ruby_colors,
            ruby_decoration_kind=ruby_decoration,
        )
        return Style(
            font_family="Arial",
            font_size_px=64,
            ruby_font_size_px=56,
            ruby_gap_px=8,
            line_lead_in_ms=700,
            line_tail_ms=1200,
            line_y_position="center",
            line_horizontal_layout="center",
            stroke_width_px=2,
            stroke2_width_px=0,
            # Global main deliberately differs from the role's main.
            decoration_kind="shadow",
            shadow_offset_x=22,
            shadow_offset_y=22,
            entry_anim="none",
            exit_anim="none",
            karaoke_colors=main_colors,
            custom_style_schemes={"A": scheme},
        )

    frames: dict[str, np.ndarray] = {}
    with NativeRendererProcess(
        renderer_path, response_timeout_s=20.0, close_timeout_s=1.0
    ) as renderer:
        for generation, (label, decoration) in enumerate(
            (("unset", None), ("role_glow", "glow"), ("global_shadow", "shadow")),
            start=1,
        ):
            renderer.configure_gpu(
                track,
                style_with_ruby_decoration(decoration),
                width=640,
                height=360,
                fps=60,
                dpr=1.0,
                force_warp=True,
                realization_enabled=False,
            )
            event = renderer.render_gpu_frame(
                900,
                force_warp=True,
                generation=generation,
                shm_key=f"krok-ruby-deco-{os.getpid()}-{uuid.uuid4().hex}",
                include_checksum=False,
                readback_bands=False,
            )
            with SharedFrameRingReader.from_event(event) as reader:
                slot = reader.read_frame(event)
            rows = np.frombuffer(slot.payload, dtype=np.uint8).reshape(
                slot.height, slot.stride
            )
            frames[label] = (
                rows[:, : slot.width * 4]
                .reshape(slot.height, slot.width, 4)
                .astype(int)
                .copy()
            )

    assert frames["unset"][..., 3].max() > 0  # the ruby really rendered
    # An unset ruby decoration resolves to the role's own main decoration...
    assert np.array_equal(frames["unset"], frames["role_glow"])
    # ...and must not fall back to the global main decoration.
    assert not np.array_equal(frames["unset"], frames["global_shadow"])


def test_native_gpu_unset_ruby_stroke2_follows_main_flag_not_saved_width():
    """GPU: ``ruby_stroke2_enabled=None`` follows the main text's flag.

    ``.n3proj`` files that omit ``UseEdge2`` but still store ``EdgeSize2``
    import as ``ruby_stroke2_enabled=None`` + ``ruby_stroke2_width_px=3`` on
    top of a main text with ``stroke2_enabled=False``.  The sidecar collapsed
    the flag and the width into one optional width, so a saved ruby width
    switched stroke2 back on by itself and the ruby gained a second outline the
    main text did not have.  The CPU painter settles the flag first, so only
    the GPU backend was affected.

    Both directions are pinned: unset must be byte-identical to the main text's
    own state, and clearly different from the opposite one.
    """
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None

    def fill(color: str) -> PaintFill:
        return PaintFill(color=color)

    def colors() -> KaraokeColors:
        # A saturated stroke2 makes any second outline unmistakable.
        state = KaraokeColorState(
            text=fill("#FFFFFF"),
            stroke=fill("#111111"),
            stroke2=fill("#FF0000"),
            shadow=fill("#00000000"),
        )
        return KaraokeColors(before=state, after=state)

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("桜", 0), TimingChar("花", 800)], end_ms=1600
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="桜花",
                reading="おうか",
                reading_part_ms=[400, 800],
                pos_start_ms=0,
                pos_end_ms=1600,
            )
        ],
    )

    def style(*, main_enabled: bool, ruby_enabled: bool | None) -> Style:
        return Style(
            font_family="Arial",
            font_size_px=64,
            ruby_font_size_px=45,
            ruby_gap_px=8,
            line_lead_in_ms=0,
            line_tail_ms=0,
            line_y_position="center",
            line_horizontal_layout="center",
            stroke_width_px=5,
            stroke2_enabled=main_enabled,
            stroke2_width_px=5,
            ruby_stroke_width_px=4,
            ruby_stroke2_enabled=ruby_enabled,
            # The width N3 saved as EdgeSize2 must not enable stroke2 on its own.
            ruby_stroke2_width_px=3,
            decoration_kind="none",
            entry_anim="none",
            exit_anim="none",
            karaoke_colors=colors(),
            ruby_karaoke_colors=colors(),
        )

    cases = {
        "main_off_unset": dict(main_enabled=False, ruby_enabled=None),
        "main_off_explicit_off": dict(main_enabled=False, ruby_enabled=False),
        "main_on_unset": dict(main_enabled=True, ruby_enabled=None),
        "main_on_explicit_on": dict(main_enabled=True, ruby_enabled=True),
        # The opposite direction: an explicitly enabled ruby must keep drawing
        # even when the main text has stroke2 switched off.
        "main_off_explicit_on": dict(main_enabled=False, ruby_enabled=True),
    }
    frames: dict[str, np.ndarray] = {}
    with NativeRendererProcess(
        renderer_path, response_timeout_s=25.0, close_timeout_s=1.0
    ) as renderer:
        for generation, (label, kwargs) in enumerate(cases.items(), start=1):
            renderer.configure_gpu(
                track,
                style(**kwargs),
                width=640,
                height=360,
                fps=60,
                dpr=1.0,
                force_warp=True,
                realization_enabled=False,
            )
            event = renderer.render_gpu_frame(
                800,
                force_warp=True,
                generation=generation,
                shm_key=f"krok-ruby-stroke2-{os.getpid()}-{uuid.uuid4().hex}",
                include_checksum=False,
                readback_bands=False,
            )
            with SharedFrameRingReader.from_event(event) as reader:
                slot = reader.read_frame(event)
            rows = np.frombuffer(slot.payload, dtype=np.uint8).reshape(
                slot.height, slot.stride
            )
            frames[label] = (
                rows[:, : slot.width * 4]
                .reshape(slot.height, slot.width, 4)
                .astype(int)
                .copy()
            )

    def stroke2_pixels(frame: np.ndarray) -> int:
        # BGRA: the red stroke2 is the only saturated-red source in the frame.
        return int(
            (
                (frame[..., 2] > 180)
                & (frame[..., 1] < 80)
                & (frame[..., 0] < 80)
                & (frame[..., 3] > 128)
            ).sum()
        )

    assert frames["main_off_unset"][..., 3].max() > 0  # the ruby really rendered
    # Main text off -> an unset ruby draws no stroke2 at all...
    assert stroke2_pixels(frames["main_off_unset"]) == 0
    assert np.array_equal(frames["main_off_unset"], frames["main_off_explicit_off"])
    # ...and main text on -> the same unset ruby follows it back on.
    assert stroke2_pixels(frames["main_on_unset"]) > 0
    assert np.array_equal(frames["main_on_unset"], frames["main_on_explicit_on"])
    # The flag gates the width, never the other way round: explicitly enabling
    # the ruby draws its own width even while the main text stays off.
    assert stroke2_pixels(frames["main_off_explicit_on"]) > 0
