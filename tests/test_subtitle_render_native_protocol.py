from __future__ import annotations

from pathlib import Path

from krok_helper.subtitle_render.models import (
    RubyAnnotation,
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.native_backend import (
    default_native_renderer_path,
    resolve_native_renderer_path,
)
from krok_helper.subtitle_render.native_protocol import RENDER_IR_SCHEMA, build_render_ir


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
