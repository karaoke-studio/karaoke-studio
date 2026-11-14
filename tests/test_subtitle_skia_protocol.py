"""P04 product IR, shared-memory preview and export acceptance (opt-in native binary)."""
import copy
import os
import sys
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import numpy as np
import pytest
from PyQt6.QtWidgets import QApplication

from krok_helper.subtitle_render.domain.models import Style, TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess, SharedFrameRingReader
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess, SkiaSelection

_APPLICATION = None


@pytest.fixture(scope="module", autouse=True)
def application():
    global _APPLICATION
    _APPLICATION = QApplication.instance() or QApplication([])
    return _APPLICATION


def sample():
    style = Style(font_family="Arial" if sys.platform in {"win32", "darwin"} else "DejaVu Sans", font_family_latin=None,
                  font_size_px=48, stroke_width_px=0, stroke2_width_px=0, decoration_kind="none",
                  dual_line_layout=False, line_horizontal_layout="center", line_alignments=["center"],
                  smart_horizontal="none", entry_anim="none", exit_anim="none", karaoke_anim="none")
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar(c, 200 + i * 100) for i, c in enumerate("Skia P04")], end_ms=1200)])
    return track, style


@pytest.fixture
def supported_project(tmp_path):
    from krok_helper.subtitle_render.domain.models import style_to_dict
    from krok_helper.subtitle_render.project.controller import SubtitleProjectController
    from krok_helper.subtitle_render.project.load import ProjectLoadPlan, apply_track_project_data
    from krok_helper.subtitle_render.project.store import project_payload, save_render_project
    from krok_helper.subtitle_render.sources.loader import SubtitleSourceLoader
    lrc = tmp_path / "P04.lrc"
    lrc.write_text("".join(f"[00:00:{20 + i * 10:02}]" + c for i, c in enumerate("Skia P04")) + "[00:01:20]\n", encoding="utf-8")
    path = tmp_path / "P04.yurika"
    payload = project_payload(subtitle_path=lrc, video_path=None, audio_path=None,
                              style=style_to_dict(sample()[1]), screen={"width": 640, "height": 360, "fps": 60},
                              selected_scheme_key="", output={}, background={"kind": "solid", "color": "#183048"})
    save_render_project(path, payload)
    loaded = SubtitleProjectController.open(path)
    assert not loaded.missing_resources
    plan = ProjectLoadPlan.from_data(loaded.data)
    track = SubtitleSourceLoader.load_file(plan.subtitle_path)
    apply_track_project_data(track, plan.style, plan.source_data)
    return track, plan.style


@pytest.fixture
def binary(monkeypatch):
    path = os.environ.get("KROK_TEST_SKIA_SIDECAR")
    if not path:
        pytest.skip("Set KROK_TEST_SKIA_SIDECAR to the P04 executable")
    assert Path(path).is_file()
    monkeypatch.setenv("KROK_SUBTITLE_SKIA_RENDERER", path)
    return path


def selection(target="gpu", fallback="none"):
    return SkiaSelection(target, fallback, os.environ.get("KROK_SUBTITLE_SKIA_REQUIRE_HARDWARE", "1") != "0")


def collect(process, times, generation=1, pixel_format="rgba8888"):
    process._send({"cmd": "render_range", "t_ms": times, "generation": generation, "threads": 1,
                   "ring_slots": len(times), "shm_key": "krok-p04-test-" + uuid.uuid4().hex,
                   "pixel_format": pixel_format})
    process._expect_ok(process._read_until_event("range_started"))
    frames = []
    events = []
    while True:
        event = process.read_event()
        assert event["ok"], event
        if event.get("generation") != generation:
            continue
        if event["event"] == "frame_ready":
            with SharedFrameRingReader.from_event(event) as reader:
                slot = reader.read_frame(event)
                frames.append(np.frombuffer(slot.payload, np.uint8).reshape(slot.height, slot.stride // 4, 4)[:, :slot.width].copy())
                stale = dict(event, generation=generation + 1)
                with pytest.raises(NativeRendererError):
                    reader.read_frame(stale)
            events.append(event)
        elif event["event"] == "range_done":
            return frames, events


def test_selection_gpu_off_means_skia_raster(monkeypatch):
    monkeypatch.setenv("KROK_SUBTITLE_SKIA_TARGET", "gpu")
    assert SkiaSelection.from_environment(gpu_enabled=False).target == "raster"
    with pytest.raises(ValueError):
        SkiaSelection("qpainter")


def test_old_sidecar_is_rejected_before_selection_or_configure(monkeypatch, tmp_path):
    exe = tmp_path / "old-sidecar"
    exe.touch()
    monkeypatch.setattr(NativeRendererProcess, "start", lambda self: {"ok": True, "event": "ready", "schema": 3})
    sent = []
    monkeypatch.setattr(SkiaRendererProcess, "_send", lambda self, payload: sent.append(payload))
    monkeypatch.setattr(SkiaRendererProcess, "abort", lambda self, **kwargs: None)
    with pytest.raises(NativeRendererError, match="握手"):
        SkiaRendererProcess(exe).start()
    assert not sent


@pytest.mark.parametrize("target", ["gpu", "raster"])
def test_product_ir_ring_wipe_seek_and_transparency(binary, target):
    track, style = sample()
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        assert process.skia_info["execution_target"] == target
        configured = process.configure(track, style, width=641, height=360, fps=60, duration_ms=1600)
        assert configured["backend_info"]["profile"] == "p04_ascii_solid_wipe"
        frames, events = collect(process, [100, 550, 1250, 550])
        assert all(f.shape == (360, 641, 4) for f in frames)
        assert np.array_equal(frames[1], frames[3]), "random seek must be deterministic"
        assert not np.array_equal(frames[0], frames[1])
        assert not np.array_equal(frames[1], frames[2])
        assert frames[1][:50, :, 3].max() == 0
        assert frames[1][..., 3].max() > 200
        assert all(e["origin"] == "top_left" and e["alpha_type"] == "straight" and e["stride"] == 641 * 4 for e in events)


def test_same_ir_gpu_and_skia_raster(binary):
    track, style = sample()
    output = []
    for target in ("gpu", "raster"):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(track, style, width=640, height=360, fps=60, duration_ms=1600)
            frames, _ = collect(process, [550, 1250])
            output.append(np.stack(frames).astype(float))
    # Compare premultiplied colors: RGB under alpha=0 is not visible evidence.
    for pixels in output:
        pixels[..., :3] *= pixels[..., 3:] / 255
    delta = np.abs(output[0] - output[1])
    assert delta.mean() < 0.15
    assert np.mean(np.max(delta, axis=-1) > 16) < 0.003


@pytest.mark.parametrize("target", ["gpu", "raster"])
def test_bgra_premultiplied_and_rgba_straight_agree(binary, target):
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir
    from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(*sample(), width=641, height=360, fps=60, duration_ms=1600)
        ir = build_render_ir(*sample(), width=641, height=360, fps=60, duration_ms=1600)
        apply_resolved_font_faces(ir)
        ir["style"]["base_color"] = "#80FF8040"
        ir["style"]["fill_color"] = "#400080FF"
        process._send({"cmd": "configure", "ir": ir})
        process._expect_ok(process._read_until_event("configured"))
        rgba, _ = collect(process, [550])
        bgra, events = collect(process, [550], generation=2, pixel_format="bgra8888_premultiplied")
        expected = rgba[0][..., [2, 1, 0, 3]].astype(np.uint16)
        expected[..., :3] = (expected[..., :3] * expected[..., 3:] + 127) // 255
        assert np.array_equal(bgra[0], expected.astype(np.uint8))
        assert 0 < int(rgba[0][..., 3].max()) <= 128
        assert events[0]["alpha_type"] == "premultiplied"


def test_missing_gpu_loader_raster_fallback_is_explicit(binary, monkeypatch, tmp_path):
    if os.sys.platform == "darwin":
        pytest.skip("Vulkan loader failure injection")
    monkeypatch.setenv("KROK_SKIA_VULKAN_LIBRARY", str(tmp_path / "missing-vulkan"))
    with pytest.raises(NativeRendererError, match="Vulkan loader"):
        with SkiaRendererProcess(binary, selection=selection("gpu", "none")):
            pass
    with SkiaRendererProcess(binary, selection=selection("gpu", "raster")) as process:
        assert process.skia_info["execution_target"] == "raster"
        assert not process.skia_info["hardware"]
        assert "Vulkan loader" in process.skia_info["fallback_reason"]
        process.configure(*sample(), width=640, height=360, fps=60, duration_ms=1600)
        frames, _ = collect(process, [550])
        assert frames[0][..., 3].max() > 0


def test_cpu_only_sidecar_rejects_gpu_and_reports_raster_fallback(binary, monkeypatch, tmp_path):
    directory = Path(binary).parent
    cpu = directory.parent / (directory.name.rsplit("-", 1)[0] + "-raster") / Path(binary).name
    if not cpu.is_file():
        pytest.skip("Build --cpu-only --qt-root to verify the CPU-only executable")
    monkeypatch.setenv("KROK_SKIA_VULKAN_LIBRARY", str(tmp_path / "absent"))
    with SkiaRendererProcess(cpu, selection=selection("raster")) as process:
        assert process.skia_info["execution_target"] == "raster"
        process.configure(*sample(), width=640, height=360, fps=60, duration_ms=1600)
        frames, _ = collect(process, [550])
        assert frames[0][..., 3].max() > 0
    with pytest.raises(NativeRendererError, match="GPU"):
        with SkiaRendererProcess(cpu, selection=selection("gpu")):
            pass
    with SkiaRendererProcess(cpu, selection=selection("gpu", "raster")) as process:
        assert process.skia_info["execution_target"] == "raster"
        assert process.skia_info["fallback_reason"]


@pytest.mark.parametrize("target", ["gpu", "raster"])
def test_full_hd_shared_frame_does_not_depend_on_legacy_sysv_size_limit(binary, target):
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(*sample(), width=1920, height=1080, fps=60, duration_ms=1600)
        frames, events = collect(process, [550])
        assert frames[0].shape == (1080, 1920, 4)
        assert frames[0][..., 3].max() > 0
        if os.name != "nt":
            assert events[0]["shm_key"].startswith("posix:")


@pytest.mark.parametrize("mutation", ["schema", "required", "dimensions", "effect", "text", "font", "missing_guide"])
def test_unsupported_ir_fails_without_replacing_live_scene(binary, mutation):
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir
    from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
    track, style = sample()
    with SkiaRendererProcess(binary, selection=selection("raster")) as process:
        process.configure(track, style, width=640, height=360, fps=60, duration_ms=1600)
        before, _ = collect(process, [550])
        ir = build_render_ir(track, style, width=640, height=360, fps=60, duration_ms=1600)
        apply_resolved_font_faces(ir)
        ir = copy.deepcopy(ir)
        if mutation == "schema": ir["schema"] = 999
        elif mutation == "required": ir["required_features"] = ["future-shaping"]
        elif mutation == "dimensions": ir["screen"]["width"] = "640"
        elif mutation == "effect": ir["style"]["stroke_width_px"] = 7
        elif mutation == "text": ir["track"]["lines"][0]["chars"][0]["text"] = "字"
        elif mutation == "font": ir["style"]["font_resolved"]["resource_path"] = "missing.ttf"
        elif mutation == "missing_guide": ir["track"]["lines"][0]["chars"][0]["vector_glyph_id"] = "unretained-glyph"
        process._send({"cmd": "configure", "ir": ir})
        with pytest.raises(NativeRendererError):
            process._expect_ok(process._read_until_event("configured"))
        after, _ = collect(process, [550], generation=2)
        assert np.array_equal(before[0], after[0])


def test_cancel_and_reconfigure_remain_usable(binary):
    with SkiaRendererProcess(binary, selection=selection()) as process:
        process.configure(*sample(), width=640, height=360, fps=60, duration_ms=1600)
        process.start_render_range(list(range(0, 1200, 10)), generation=1, threads=1,
                                   shm_key="krok-p04-cancel-" + uuid.uuid4().hex, ring_slots=120)
        process.send_cancel_generation(1)
        cancelled = False
        while True:
            event = process.read_event()
            assert event["ok"], event
            if event["event"] == "generation_cancelled": cancelled = True
            elif cancelled and event["event"] == "frame_ready": pytest.fail("frame published after cancel acknowledgement")
            elif event["event"] == "range_done": break
        assert cancelled
        process.configure(*sample(), width=320, height=180, fps=60, duration_ms=1600)
        frames, _ = collect(process, [550], generation=2)
        assert frames[0].shape == (180, 320, 4)


@pytest.mark.parametrize("gpu", [True, False])
def test_actual_g5_preview_uses_skia_even_when_gpu_off(binary, monkeypatch, supported_project, gpu):
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.skia_async import SkiaAsyncSubtitleRenderer
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_BACKEND", "skia")
    monkeypatch.setenv("KROK_SUBTITLE_GPU_PREVIEW", "1" if gpu else "0")
    monkeypatch.setattr(pg, "paint_frame_to_painter", lambda *a, **k: pytest.fail("subtitle QPainter was called"))
    canvas = pg.PreviewGraphicsView()
    frames, errors = [], []
    try:
        assert isinstance(canvas._async_renderer, SkiaAsyncSubtitleRenderer)
        assert canvas._async_renderer.selection.target == ("gpu" if gpu else "raster")
        canvas._async_renderer.frame_ready.connect(lambda image, t: frames.append((image, t)))
        canvas.gpuFallback.connect(errors.append)
        canvas.resize(640, 360)
        canvas.show()
        canvas.set_output_size(640, 360)
        canvas.set_track(supported_project[0])
        canvas.set_style(supported_project[1])
        canvas.set_time(550)
        deadline = time.monotonic() + 30
        while (not frames or frames[-1][1] != 550 or canvas._subtitle_item._async_image is None) and not errors and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        assert not errors, errors
        assert frames and frames[-1][1] == 550 and not frames[-1][0].isNull()
        assert canvas._subtitle_item._async_image is not None
        assert "Skia" in canvas.render_backend_label()
        canvas.set_gpu_preview_enabled(not gpu)
        assert canvas._async_renderer.selection.target == ("raster" if gpu else "gpu")
    finally:
        canvas._stop_async_renderer()
        canvas.close()
        canvas.deleteLater()


@pytest.mark.parametrize("gpu", [True, False])
def test_product_export_short_mp4_uses_skia(binary, monkeypatch, tmp_path, supported_project, gpu):
    from krok_helper.subtitle_render.engine import renderer
    from krok_helper.subtitle_render.engine.export.render_job import RenderJob
    from krok_helper.subtitle_render.domain.background import BackgroundSource
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        import imageio_ffmpeg
        tools = tmp_path / "ffmpeg-bin"
        tools.mkdir()
        ffmpeg = str(tools / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
        shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), ffmpeg)
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_BACKEND", "skia")
    monkeypatch.setattr(renderer, "paint_frame", lambda *a, **k: pytest.fail("subtitle QPainter export was called"))
    monkeypatch.setattr(renderer, "paint_frame_to_painter", lambda *a, **k: pytest.fail("subtitle QPainter export was called"))
    track, style = supported_project
    job = RenderJob(track, style, tmp_path / "unused", tmp_path / "p04.mp4", width=640, height=360,
                    background_source=BackgroundSource(kind="solid", color="#183048"), fps=60,
                    duration_ms=1500, include_audio=False, gpu_export_enabled=gpu, preset="ultrafast")
    logs = []
    result = renderer.render_subtitle_video(job, ffmpeg_dir=Path(ffmpeg).parent, logger=logs.append)
    assert any("Skia 字幕后端" in line and ("GPU" if gpu else "Raster CPU") in line for line in logs)
    assert result.is_file() and result.stat().st_size > 1000
    decoded = subprocess.run([ffmpeg, "-v", "error", "-i", str(result), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, timeout=30)
    assert decoded.returncode == 0, decoded.stderr
    frames = np.frombuffer(decoded.stdout, np.uint8).reshape(-1, 360, 640, 3)
    assert len(frames) == 90
    assert np.mean(frames[33].max(axis=-1) > 150) > 0.003
