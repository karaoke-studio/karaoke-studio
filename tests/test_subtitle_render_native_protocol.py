from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import textwrap

import numpy as np
import pytest

from krok_helper.subtitle_render.engine.painter import (
    clear_before_layer_cache,
    _fill_clip_band,
    _glow_extent,
    _glow_radius,
    _layout_line,
    _layout_rubies,
    _ruby_after_clip_rect,
    _ruby_progress_ratio,
    _resolve_display_baselines,
    _resolve_sayatoo_line_layouts,
    _resolve_visible_content,
    _visual_stroke_extent,
    paint_frame,
)
from krok_helper.subtitle_render.models import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
    RubyAnnotation,
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.native_backend import (
    NativeRendererError,
    NativeRendererProcess,
    default_native_renderer_path,
    resolve_native_renderer_path,
)
from krok_helper.subtitle_render.native_protocol import RENDER_IR_SCHEMA, build_render_ir


def _native_after_clip_vertical_extent(style: Style) -> int:
    stroke_extent = _visual_stroke_extent(
        style.stroke_width_px,
        style.stroke2_width_px,
    )
    glow_extent = (
        _glow_extent(
            style.stroke_width_px,
            style.stroke2_width_px,
            _glow_radius(style, after=True),
        )
        if style.decoration_kind == "glow"
        else 0
    )
    shadow_extent = abs(style.shadow_offset_y) if style.decoration_kind == "shadow" else 0
    return max(stroke_extent, glow_extent, shadow_extent, 2) + 4


def _assert_native_after_clip_matches_layout(
    frame: dict,
    py_layout,
    style: Style,
    t_ms: int,
    *,
    assert_close,
) -> None:
    band = _fill_clip_band(py_layout.fill_segments, t_ms, py_layout.rtl)
    if band is None:
        fill_start = py_layout.x0 + py_layout.total_w if py_layout.rtl else py_layout.x0
        fill_end = fill_start
    else:
        fill_start, fill_end = band
    extent = _native_after_clip_vertical_extent(style)
    if py_layout.rtl:
        expected_left = fill_start
        expected_right = py_layout.x0 + py_layout.total_w
    else:
        expected_left = py_layout.x0
        expected_right = fill_end
    assert_close(frame["after_clip_left"], expected_left, f"clip_left@{t_ms}")
    assert_close(frame["after_clip_right"], expected_right, f"clip_right@{t_ms}")
    assert_close(
        frame["after_clip_top"],
        py_layout.baseline_y - py_layout.metrics.ascent() - extent,
        f"clip_top@{t_ms}",
    )
    assert_close(
        frame["after_clip_height"],
        py_layout.metrics.height() + extent * 2,
        f"clip_height@{t_ms}",
    )


def test_build_render_ir_contains_screen_style_track_and_ruby():
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("君", 100, role_label="A"),
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
                pos_start_ms=100,
                pos_end_ms=300,
            )
        ],
    )
    style = Style(font_size_px=64, fill_color="#123456")

    ir = build_render_ir(track, style, width=640, height=360, fps=30)

    assert ir["schema"] == RENDER_IR_SCHEMA
    assert ir["screen"] == {"width": 640, "height": 360, "fps": 30}
    assert ir["style"]["font_size_px"] == 64
    assert ir["style"]["fill_color"] == "#123456"
    assert ir["track"]["lines"][0]["singer_id"] == 2
    assert ir["track"]["lines"][0]["chars"][0]["text"] == "君"
    assert ir["track"]["lines"][0]["chars"][0]["role_label"] == "A"
    assert ir["track"]["lines"][0]["chars"][1]["pause_release_ms"] == 450
    assert ir["track"]["rubies"][0]["reading"] == "きみ"
    assert ir["track"]["rubies"][0]["reading_part_ms"] == [100, 250]


def test_build_render_ir_clamps_screen_values():
    ir = build_render_ir(TimingTrack(), Style(), width=0, height=-1, fps=0)
    assert ir["screen"] == {"width": 1, "height": 1, "fps": 1}


def test_default_native_renderer_path_uses_build_tree():
    root = Path("D:/repo")
    assert default_native_renderer_path(root) == root / "build" / "native-renderer" / "krok_subtitle_renderer.exe"


def test_resolve_native_renderer_path_prefers_explicit_existing_path(tmp_path, monkeypatch):
    exe = tmp_path / "renderer.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_RENDERER", str(tmp_path / "missing.exe"))

    assert resolve_native_renderer_path(exe) == exe


def test_resolve_native_renderer_path_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("KROK_SUBTITLE_NATIVE_RENDERER", raising=False)
    assert resolve_native_renderer_path(root=tmp_path) is None


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
                    print(json.dumps({{"ok": True, "event": "frame_ready", "checksum": "fake"}}), flush=True)
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

    renderer.close()
    assert renderer.is_running is False


def test_native_renderer_process_times_out_when_sidecar_stalls(tmp_path):
    sidecar = _write_fake_sidecar(tmp_path, mode="hang_after_ready")
    renderer = NativeRendererProcess(sidecar, response_timeout_s=0.3, close_timeout_s=1.0)

    renderer.start()
    with pytest.raises(NativeRendererError, match="timed out"):
        renderer.configure(TimingTrack(), Style(), width=640, height=360, fps=60)

    renderer.close()
    assert renderer.is_running is False


def test_native_renderer_process_matches_python_layout_when_exe_exists(tmp_path, monkeypatch):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None

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
        ruby_font_size_px=20,
        line_lead_in_ms=0,
        stroke_width_px=10,
        stroke2_width_px=6,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(
                text=PaintFill(color="#FFFFFF"),
                stroke=PaintFill(color="#222222"),
                stroke2=PaintFill(color="#202020"),
            ),
            after=KaraokeColorState(
                text=PaintFill(color="#FF5A6F"),
                stroke=PaintFill(color="#222222"),
                stroke2=PaintFill(color="#303030"),
            ),
        ),
    )
    track_t_ms, display_style, display_lines, _signal_lines, _title_opacity = (
        _resolve_visible_content(track, 900, style)
    )
    baselines = _resolve_display_baselines(360, track, display_lines, display_style)
    line_layouts = _resolve_sayatoo_line_layouts(
        640,
        360,
        track,
        display_lines,
        baselines,
        track_t_ms,
        display_style,
    )
    display_line = display_lines[0]
    line_layout = line_layouts[display_line.lane]
    py_layout = _layout_line(
        track,
        display_line.line,
        display_style,
        640,
        360,
        baseline_y=line_layout.baseline_y,
        line_x=line_layout.text_x,
        lane=display_line.lane,
    )
    assert py_layout is not None

    def assert_close(actual, expected, label, tolerance=4.0):
        assert abs(float(actual) - float(expected)) <= tolerance, (label, actual, expected)

    with NativeRendererProcess(renderer_path, response_timeout_s=2.0, close_timeout_s=1.0) as renderer:
        renderer.configure(track, style, width=640, height=360, fps=60)
        frame0 = renderer.render_frame_png(0, tmp_path / "frame-000.png")
        frame200 = renderer.render_frame_png(200, tmp_path / "frame-200.png")
        frame900 = renderer.render_frame_png(900, tmp_path / "frame-900.png")
        frame1800 = renderer.render_frame_png(1800, tmp_path / "frame-1800.png")

    assert_close(frame900["line_x"], py_layout.x0, "line_x")
    assert_close(frame900["line_width"], py_layout.total_w, "line_width")
    assert_close(frame900["baseline_y"], py_layout.baseline_y, "baseline_y")
    _assert_native_after_clip_matches_layout(
        frame0, py_layout, style, 0, assert_close=assert_close
    )
    _assert_native_after_clip_matches_layout(
        frame200, py_layout, style, 200, assert_close=assert_close
    )
    _assert_native_after_clip_matches_layout(
        frame900, py_layout, style, 900, assert_close=assert_close
    )
    _assert_native_after_clip_matches_layout(
        frame1800, py_layout, style, 1800, assert_close=assert_close
    )


def test_native_renderer_matches_python_layout_for_lower_lane_when_two_lines_visible(
    tmp_path, monkeypatch
):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("U", 0),
                    TimingChar("p", 400),
                ],
                end_ms=1800,
            ),
            TimingLine(
                chars=[
                    TimingChar("D", 0),
                    TimingChar("n", 400),
                ],
                end_ms=1800,
            ),
        ],
    )
    style = Style(
        font_size_px=48,
        ruby_font_size_px=20,
        line_lead_in_ms=0,
        stroke_width_px=10,
        stroke2_width_px=6,
        line_y_margin_px=70,
        line_gap_px=60,
        upper_line_left_margin_px=50,
        lower_line_right_margin_px=50,
    )
    track_t_ms, display_style, display_lines, _signal_lines, _title_opacity = (
        _resolve_visible_content(track, 900, style)
    )
    assert [display_line.lane for display_line in display_lines[:2]] == [0, 1]

    baselines = _resolve_display_baselines(360, track, display_lines, display_style)
    line_layouts = _resolve_sayatoo_line_layouts(
        640,
        360,
        track,
        display_lines,
        baselines,
        track_t_ms,
        display_style,
    )
    expected_layouts = []
    for display_line in display_lines[:2]:
        line_layout = line_layouts[display_line.lane]
        py_layout = _layout_line(
            track,
            display_line.line,
            display_style,
            640,
            360,
            baseline_y=line_layout.baseline_y,
            line_x=line_layout.text_x,
            lane=display_line.lane,
        )
        assert py_layout is not None
        expected_layouts.append(py_layout)

    with NativeRendererProcess(renderer_path, response_timeout_s=2.0, close_timeout_s=1.0) as renderer:
        renderer.configure(track, style, width=640, height=360, fps=60)
        frame900 = renderer.render_frame_png(900, tmp_path / "two-line-frame-900.png")

    assert frame900["visible_lines"] == 2
    assert len(frame900["line_diagnostics"]) == 2
    for index, py_layout in enumerate(expected_layouts):
        native_line = frame900["line_diagnostics"][index]
        assert native_line["lane"] == index
        assert abs(float(native_line["line_x"]) - float(py_layout.x0)) <= 4.0
        assert abs(float(native_line["line_width"]) - float(py_layout.total_w)) <= 4.0
        assert abs(float(native_line["baseline_y"]) - float(py_layout.baseline_y)) <= 4.0


@pytest.mark.parametrize(
    ("decoration_kind", "glow_after_radius_px", "shadow_offset_y"),
    [
        ("glow", 14, 1),
        ("shadow", 10, 18),
    ],
)
def test_native_after_clip_vertical_extent_matches_painter_decoration_bounds(
    tmp_path,
    monkeypatch,
    decoration_kind,
    glow_after_radius_px,
    shadow_offset_y,
):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None

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
        ruby_font_size_px=20,
        line_lead_in_ms=0,
        stroke_width_px=4,
        stroke2_width_px=6,
        decoration_kind=decoration_kind,
        glow_before_radius_px=6,
        glow_after_radius_px=glow_after_radius_px,
        shadow_offset_y=shadow_offset_y,
    )
    track_t_ms, display_style, display_lines, _signal_lines, _title_opacity = (
        _resolve_visible_content(track, 900, style)
    )
    baselines = _resolve_display_baselines(360, track, display_lines, display_style)
    line_layouts = _resolve_sayatoo_line_layouts(
        640,
        360,
        track,
        display_lines,
        baselines,
        track_t_ms,
        display_style,
    )
    display_line = display_lines[0]
    line_layout = line_layouts[display_line.lane]
    py_layout = _layout_line(
        track,
        display_line.line,
        display_style,
        640,
        360,
        baseline_y=line_layout.baseline_y,
        line_x=line_layout.text_x,
        lane=display_line.lane,
    )
    assert py_layout is not None

    def assert_close(actual, expected, label, tolerance=4.0):
        assert abs(float(actual) - float(expected)) <= tolerance, (label, actual, expected)

    with NativeRendererProcess(renderer_path, response_timeout_s=2.0, close_timeout_s=1.0) as renderer:
        renderer.configure(track, style, width=640, height=360, fps=60)
        frame900 = renderer.render_frame_png(
            900,
            tmp_path / f"{decoration_kind}-clip-extent-900.png",
        )

    _assert_native_after_clip_matches_layout(
        frame900,
        py_layout,
        style,
        900,
        assert_close=assert_close,
    )


def test_native_ruby_diagnostics_match_python_horizontal_layout_and_timing(
    tmp_path,
    monkeypatch,
):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None

    line = TimingLine(
        chars=[
            TimingChar("A", 0),
            TimingChar("B", 1000),
            TimingChar("C", 2000),
        ],
        end_ms=3000,
    )
    ruby = RubyAnnotation(
        kanji="AB",
        reading="xy",
        reading_part_ms=[1000],
        pos_start_ms=0,
        pos_end_ms=2000,
    )
    track = TimingTrack(lines=[line], rubies=[ruby])
    style = Style(
        font_size_px=48,
        ruby_font_size_px=20,
        ruby_gap_px=8,
        line_lead_in_ms=0,
        stroke_width_px=4,
        stroke2_width_px=2,
        line_y_position="center",
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(
                text=PaintFill(color="#FFFFFF"),
                stroke=PaintFill(color="#222222"),
                stroke2=PaintFill(color="#202020"),
            ),
            after=KaraokeColorState(
                text=PaintFill(color="#FF5A6F"),
                stroke=PaintFill(color="#222222"),
                stroke2=PaintFill(color="#303030"),
            ),
        ),
    )

    track_t_ms, display_style, display_lines, _signal_lines, _title_opacity = (
        _resolve_visible_content(track, 1500, style)
    )
    baselines = _resolve_display_baselines(360, track, display_lines, display_style)
    line_layouts = _resolve_sayatoo_line_layouts(
        640,
        360,
        track,
        display_lines,
        baselines,
        track_t_ms,
        display_style,
    )
    display_line = display_lines[0]
    line_layout = line_layouts[display_line.lane]
    py_layout = _layout_line(
        track,
        display_line.line,
        display_style,
        640,
        360,
        baseline_y=line_layout.baseline_y,
        line_x=line_layout.text_x,
        lane=display_line.lane,
    )
    assert py_layout is not None
    assert py_layout.ruby_metrics is not None
    ruby_layouts = _layout_rubies(
        py_layout.ruby_metrics,
        display_line.line,
        py_layout.intervals,
        py_layout.char_x_ranges,
        py_layout.baseline_y,
        track.rubies,
        display_style,
        main_ascent_px=py_layout.metrics.ascent(),
    )
    assert len(ruby_layouts) == 1
    expected = ruby_layouts[0]

    def assert_close(actual, expected_value, label, tolerance=4.0):
        assert abs(float(actual) - float(expected_value)) <= tolerance, (
            label,
            actual,
            expected_value,
        )

    with NativeRendererProcess(renderer_path, response_timeout_s=2.0, close_timeout_s=1.0) as renderer:
        renderer.configure(track, style, width=640, height=360, fps=60)
        frame500 = renderer.render_frame_png(500, tmp_path / "ruby-frame-500.png")
        frame1500 = renderer.render_frame_png(1500, tmp_path / "ruby-frame-1500.png")

    assert len(frame1500["ruby_diagnostics"]) == 1
    for frame, t_ms in [(frame500, 500), (frame1500, 1500)]:
        native_ruby = frame["ruby_diagnostics"][0]
        expected_ratio = _ruby_progress_ratio(expected.ruby, t_ms)
        expected_clip = _ruby_after_clip_rect(
            expected,
            py_layout.ruby_metrics,
            display_style,
            py_layout.rtl,
            expected_ratio,
        )
        assert native_ruby["kanji"] == "AB"
        assert native_ruby["reading"] == "xy"
        assert native_ruby["indices"] == [0, 1]
        assert_close(native_ruby["x"], expected.x, f"x@{t_ms}")
        assert_close(native_ruby["baseline_y"], expected.baseline_y, f"baseline@{t_ms}")
        assert_close(native_ruby["target_width"], expected.target_width, f"target_width@{t_ms}")
        assert_close(native_ruby["reading_width"], expected.reading_width, f"reading_width@{t_ms}")
        assert_close(native_ruby["progress"], expected_ratio, f"progress@{t_ms}", tolerance=0.01)
        assert_close(native_ruby["after_clip_left"], expected_clip.left(), f"clip_left@{t_ms}")
        assert_close(native_ruby["after_clip_right"], expected_clip.right(), f"clip_right@{t_ms}")
        assert_close(native_ruby["after_clip_top"], expected_clip.top(), f"clip_top@{t_ms}")
        assert_close(native_ruby["after_clip_height"], expected_clip.height(), f"clip_height@{t_ms}")


def test_native_ruby_changes_rendered_frame_when_exe_exists(tmp_path, monkeypatch):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QImage

    line = TimingLine(
        chars=[
            TimingChar("A", 0),
            TimingChar("B", 1000),
            TimingChar("C", 2000),
        ],
        end_ms=3000,
    )
    ruby = RubyAnnotation(
        kanji="AB",
        reading="xy",
        reading_part_ms=[1000],
        pos_start_ms=0,
        pos_end_ms=2000,
    )
    style = Style(
        font_size_px=64,
        ruby_font_size_px=30,
        ruby_gap_px=10,
        line_lead_in_ms=0,
        line_y_position="center",
        stroke_width_px=4,
        stroke2_width_px=2,
    )
    plain_output = tmp_path / "native-plain-without-ruby.png"
    ruby_output = tmp_path / "native-with-ruby.png"

    with NativeRendererProcess(renderer_path, response_timeout_s=2.0, close_timeout_s=1.0) as renderer:
        renderer.configure(TimingTrack(lines=[line]), style, width=640, height=360, fps=60)
        renderer.render_frame_png(1500, plain_output)

        renderer.configure(TimingTrack(lines=[line], rubies=[ruby]), style, width=640, height=360, fps=60)
        renderer.render_frame_png(1500, ruby_output)

    plain = QImage(str(plain_output)).convertToFormat(QImage.Format.Format_RGBA8888)
    with_ruby = QImage(str(ruby_output)).convertToFormat(QImage.Format.Format_RGBA8888)
    assert plain.size() == with_ruby.size()
    diff_pixels = 0
    for y in range(plain.height()):
        for x in range(plain.width()):
            if plain.pixelColor(x, y).rgba() != with_ruby.pixelColor(x, y).rgba():
                diff_pixels += 1
    assert diff_pixels > 100


def test_native_ruby_color_controls_rendered_ruby_pixels(tmp_path, monkeypatch):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QImage

    line = TimingLine(
        chars=[
            TimingChar("A", 0),
            TimingChar("B", 1000),
        ],
        end_ms=2000,
    )
    ruby = RubyAnnotation(
        kanji="AB",
        reading="xy",
        reading_part_ms=[1000],
        pos_start_ms=0,
        pos_end_ms=2000,
    )
    base_style = dict(
        font_size_px=64,
        ruby_font_size_px=32,
        ruby_gap_px=10,
        line_lead_in_ms=0,
        line_y_position="center",
        stroke_width_px=0,
        stroke2_width_px=0,
        base_color="#FFFFFF",
        fill_color="#FFFFFF",
        stroke_color="#00000000",
        shadow_color="#00000000",
    )
    red_style = Style(**base_style, ruby_color="#FF0000")
    green_style = Style(**base_style, ruby_color="#00FF00")
    track = TimingTrack(lines=[line], rubies=[ruby])
    red_output = tmp_path / "native-ruby-red.png"
    green_output = tmp_path / "native-ruby-green.png"

    with NativeRendererProcess(renderer_path, response_timeout_s=2.0, close_timeout_s=1.0) as renderer:
        renderer.configure(track, red_style, width=640, height=360, fps=60)
        renderer.render_frame_png(2000, red_output)

        renderer.configure(track, green_style, width=640, height=360, fps=60)
        renderer.render_frame_png(2000, green_output)

    red = QImage(str(red_output)).convertToFormat(QImage.Format.Format_RGBA8888)
    green = QImage(str(green_output)).convertToFormat(QImage.Format.Format_RGBA8888)
    assert red.size() == green.size()
    diff_pixels = 0
    for y in range(red.height()):
        for x in range(red.width()):
            if red.pixelColor(x, y).rgba() != green.pixelColor(x, y).rgba():
                diff_pixels += 1
    assert diff_pixels > 100


def test_native_ruby_karaoke_colors_override_main_karaoke_colors(tmp_path, monkeypatch):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QImage

    def fill(color: str) -> PaintFill:
        return PaintFill(color=color)

    line = TimingLine(
        chars=[
            TimingChar("A", 0),
            TimingChar("B", 1000),
        ],
        end_ms=2000,
    )
    ruby = RubyAnnotation(
        kanji="AB",
        reading="xy",
        reading_part_ms=[1000],
        pos_start_ms=0,
        pos_end_ms=2000,
    )
    main_colors = KaraokeColors(
        before=KaraokeColorState(text=fill("#FFFFFF"), stroke=fill("#00000000"), stroke2=fill("#00000000")),
        after=KaraokeColorState(text=fill("#FFFFFF"), stroke=fill("#00000000"), stroke2=fill("#00000000")),
    )
    ruby_red = KaraokeColors(
        before=KaraokeColorState(text=fill("#FF0000"), stroke=fill("#00000000"), stroke2=fill("#00000000")),
        after=KaraokeColorState(text=fill("#FF0000"), stroke=fill("#00000000"), stroke2=fill("#00000000")),
    )
    ruby_green = KaraokeColors(
        before=KaraokeColorState(text=fill("#00FF00"), stroke=fill("#00000000"), stroke2=fill("#00000000")),
        after=KaraokeColorState(text=fill("#00FF00"), stroke=fill("#00000000"), stroke2=fill("#00000000")),
    )
    base_style = dict(
        font_size_px=64,
        ruby_font_size_px=32,
        ruby_gap_px=10,
        line_lead_in_ms=0,
        line_y_position="center",
        stroke_width_px=0,
        stroke2_width_px=0,
        karaoke_colors=main_colors,
    )
    red_style = Style(**base_style, ruby_karaoke_colors=ruby_red)
    green_style = Style(**base_style, ruby_karaoke_colors=ruby_green)
    track = TimingTrack(lines=[line], rubies=[ruby])
    red_output = tmp_path / "native-ruby-matrix-red.png"
    green_output = tmp_path / "native-ruby-matrix-green.png"

    with NativeRendererProcess(renderer_path, response_timeout_s=2.0, close_timeout_s=1.0) as renderer:
        renderer.configure(track, red_style, width=640, height=360, fps=60)
        renderer.render_frame_png(2000, red_output)

        renderer.configure(track, green_style, width=640, height=360, fps=60)
        renderer.render_frame_png(2000, green_output)

    red = QImage(str(red_output)).convertToFormat(QImage.Format.Format_RGBA8888)
    green = QImage(str(green_output)).convertToFormat(QImage.Format.Format_RGBA8888)
    assert red.size() == green.size()
    diff_pixels = 0
    for y in range(red.height()):
        for x in range(red.width()):
            if red.pixelColor(x, y).rgba() != green.pixelColor(x, y).rgba():
                diff_pixels += 1
    assert diff_pixels > 100


def test_native_renderer_after_stroke2_missing_does_not_inherit_before_stroke2(
    tmp_path, monkeypatch
):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QImage

    def fill(color: str) -> PaintFill:
        return PaintFill(color=color)

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
        font_size_px=72,
        line_lead_in_ms=0,
        stroke_width_px=8,
        stroke2_width_px=18,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(
                text=fill("#FFFFFF"),
                stroke=fill("#222222"),
                stroke2=fill("#00FF00"),
            ),
            after=KaraokeColorState(
                text=fill("#FF5A6F"),
                stroke=fill("#222222"),
                stroke2=fill("#000000"),
            ),
        ),
    )
    ir = build_render_ir(track, style, width=640, height=360, fps=60)
    del ir["style"]["karaoke_colors"]["after"]["stroke2"]

    explicit_output = tmp_path / "explicit-after-stroke2.png"
    missing_output = tmp_path / "missing-after-stroke2.png"
    explicit_ir = build_render_ir(track, style, width=640, height=360, fps=60)
    with NativeRendererProcess(renderer_path, response_timeout_s=2.0, close_timeout_s=1.0) as renderer:
        renderer._send({"cmd": "configure", "ir": explicit_ir})
        renderer._expect_ok(renderer._read_response())
        renderer.render_frame_png(1800, explicit_output)

        renderer._send({"cmd": "configure", "ir": ir})
        renderer._expect_ok(renderer._read_response())
        renderer.render_frame_png(1800, missing_output)

    explicit = QImage(str(explicit_output)).convertToFormat(QImage.Format.Format_RGBA8888)
    missing = QImage(str(missing_output)).convertToFormat(QImage.Format.Format_RGBA8888)
    assert explicit.size() == missing.size()
    for y in range(explicit.height()):
        for x in range(explicit.width()):
            assert explicit.pixelColor(x, y).rgba() == missing.pixelColor(x, y).rgba()


@pytest.mark.parametrize("t_ms", [0, 900, 1800])
def test_native_plain_horizontal_pixels_stay_within_bounded_diff(
    tmp_path, monkeypatch, t_ms
):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None

    def fill(color: str) -> PaintFill:
        return PaintFill(color=color)

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
        stroke_width_px=10,
        stroke2_width_px=6,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(
                text=fill("#FFFFFF"),
                stroke=fill("#222222"),
                stroke2=fill("#202020"),
            ),
            after=KaraokeColorState(
                text=fill("#FF5A6F"),
                stroke=fill("#222222"),
                stroke2=fill("#303030"),
            ),
        ),
    )

    python_image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
    python_image.fill(0)
    clear_before_layer_cache()
    paint_frame(python_image, track, t_ms, style)

    native_output = tmp_path / f"native-plain-horizontal-{t_ms}.png"
    with NativeRendererProcess(renderer_path, response_timeout_s=2.0, close_timeout_s=1.0) as renderer:
        renderer.configure(track, style, width=640, height=360, fps=60)
        renderer.render_frame_png(t_ms, native_output)

    def image_rows(image: QImage) -> np.ndarray:
        converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
        height, width = converted.height(), converted.width()
        bytes_per_line = converted.bytesPerLine()
        bits = converted.constBits()
        bits.setsize(converted.sizeInBytes())
        rows = np.frombuffer(bits, dtype=np.uint8, count=bytes_per_line * height).reshape(
            height, bytes_per_line
        )
        return rows[:, : width * 4].copy()

    diff = np.abs(
        image_rows(python_image).astype(int)
        - image_rows(QImage(str(native_output))).astype(int)
    )
    assert diff.mean() < 10.0
    assert int((diff > 8).sum()) < 50_000


def test_native_two_line_horizontal_pixels_stay_within_bounded_diff(
    tmp_path, monkeypatch
):
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("U", 0),
                    TimingChar("p", 400),
                ],
                end_ms=1800,
            ),
            TimingLine(
                chars=[
                    TimingChar("D", 0),
                    TimingChar("n", 400),
                ],
                end_ms=1800,
            ),
        ],
    )
    style = Style(
        font_size_px=48,
        line_lead_in_ms=0,
        stroke_width_px=10,
        stroke2_width_px=6,
        line_y_margin_px=70,
        line_gap_px=60,
        upper_line_left_margin_px=50,
        lower_line_right_margin_px=50,
    )

    python_image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
    python_image.fill(0)
    clear_before_layer_cache()
    paint_frame(python_image, track, 900, style)

    native_output = tmp_path / "native-two-line-horizontal-900.png"
    with NativeRendererProcess(renderer_path, response_timeout_s=2.0, close_timeout_s=1.0) as renderer:
        renderer.configure(track, style, width=640, height=360, fps=60)
        renderer.render_frame_png(900, native_output)

    def image_rows(image: QImage) -> np.ndarray:
        converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
        height, width = converted.height(), converted.width()
        bytes_per_line = converted.bytesPerLine()
        bits = converted.constBits()
        bits.setsize(converted.sizeInBytes())
        rows = np.frombuffer(bits, dtype=np.uint8, count=bytes_per_line * height).reshape(
            height, bytes_per_line
        )
        return rows[:, : width * 4].copy()

    diff = np.abs(
        image_rows(python_image).astype(int)
        - image_rows(QImage(str(native_output))).astype(int)
    )
    assert diff.mean() < 10.0
    assert int((diff > 8).sum()) < 50_000
