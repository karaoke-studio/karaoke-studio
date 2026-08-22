"""TransportBar 播放控制测试。

QMediaPlayer 的真实音频播放在 CI 不稳定，所以这里聚焦：

- play / pause / toggle_play 切按钮文字与播放状态
- 无音频时的 QTimer 视觉 tick 路径（直接调 ``_on_tick`` 模拟）
- ``set_audio_source`` 把 QMediaPlayer 切到音频路径
- ``timeChanged`` 信号在播放 / 拖动 / set_time 三个来源都能触发
- 抑制反馈环（_suppress_seek）：模拟 player.positionChanged 不会回写到 player
"""

from __future__ import annotations

import math
import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QUrl  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt6.QtMultimedia import QMediaPlayer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend import preview_view as pv  # noqa: E402
from krok_helper.subtitle_render.frontend.preview_view import (  # noqa: E402
    PreviewCanvas,
    TransportBar,
)


def _release_media_objects(app: QApplication) -> None:
    """确定性地销毁测试遗留的 QMediaPlayer/QAudioOutput（趁 QApplication 还活着）。

    各测试懒创建的 ``QMediaPlayer`` + ``QAudioOutput`` 若一直泄漏到解释器退出，
    Python GC 与 PyQt6 多媒体后端 C++ 析构的顺序竞争会段错误（Python 3.14 退出期尤甚）。
    在 app 仍存活时显式 stop + 解绑 source/output + deleteLater，可避免该竞争。
    """
    for widget in list(app.topLevelWidgets()):
        for attr in ("_player", "_video_player"):
            player = getattr(widget, attr, None)
            if isinstance(player, QMediaPlayer):
                try:
                    player.stop()
                    player.setSource(QUrl())
                    player.setAudioOutput(None)
                    player.setVideoOutput(None)
                except (RuntimeError, TypeError):
                    pass
        widget.close()
        widget.deleteLater()
    app.processEvents()


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    _release_media_objects(app)


def _bar(qapp) -> TransportBar:
    bar = TransportBar()
    bar.set_duration(60_000)
    return bar


def test_native_renderer_process_owner_centralizes_lazy_restart_and_close():
    from krok_helper.subtitle_render.native_backend import NativeRendererProcessOwner

    events: list[str] = []

    class FakeProcess:
        def __init__(self, *, marker):
            events.append(f"create:{marker}")

        def start(self):
            events.append("start")

        def close(self):
            events.append("close")

    owner = NativeRendererProcessOwner(FakeProcess, marker="preview")
    first = owner.ensure()

    assert owner.ensure() is first
    second = owner.restart()
    assert second is not first

    owner.close()
    owner.close()
    assert owner.process is None
    assert events == [
        "create:preview",
        "start",
        "close",
        "create:preview",
        "start",
        "close",
    ]


def test_native_renderer_process_owner_cleans_failed_start():
    from krok_helper.subtitle_render.native_backend import NativeRendererProcessOwner

    events: list[str] = []

    class FailingProcess:
        def start(self):
            events.append("start")
            raise RuntimeError("boom")

        def close(self):
            events.append("close")

    owner = NativeRendererProcessOwner(FailingProcess)

    with pytest.raises(RuntimeError, match="boom"):
        owner.ensure()

    assert owner.process is None
    assert events == ["start", "close"]


def test_native_renderer_process_owner_replaces_exited_process():
    from krok_helper.subtitle_render.native_backend import NativeRendererProcessOwner

    instances = []

    class FakeProcess:
        def __init__(self):
            self.is_running = False
            self.closed = False
            instances.append(self)

        def start(self):
            self.is_running = True

        def close(self):
            self.closed = True
            self.is_running = False

    owner = NativeRendererProcessOwner(FakeProcess)
    exited = owner.ensure()
    exited.is_running = False

    replacement = owner.ensure()

    assert replacement is not exited
    assert exited.closed is True
    assert replacement.is_running is True
    assert len(instances) == 2
    owner.close()


def test_native_renderer_process_owner_serializes_concurrent_start():
    from krok_helper.subtitle_render.native_backend import NativeRendererProcessOwner

    instances = []

    class SlowProcess:
        def __init__(self):
            self.is_running = False
            instances.append(self)

        def start(self):
            time.sleep(0.02)
            self.is_running = True

        def close(self):
            self.is_running = False

    owner = NativeRendererProcessOwner(SlowProcess)
    results = []

    threads = [
        threading.Thread(target=lambda: results.append(owner.ensure()))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1.0)

    assert all(not thread.is_alive() for thread in threads)
    assert len(instances) == 1
    assert results == [instances[0]] * 4
    owner.close()


def test_preview_surfaces_do_not_draw_frame_border(qapp):
    canvas = PreviewCanvas()
    try:
        assert "border: 0" in canvas.styleSheet()
    finally:
        canvas.close()
        canvas.deleteLater()

    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    graphics = PreviewGraphicsView()
    try:
        assert "border: 0" in graphics.styleSheet()
        # contain 语义：视频项与输出画布严格对齐（四周露出纯黑 letterbox，
        # 对齐导出 pad black），不再使用 cover 时代的负偏移 overscan。
        assert graphics._video_item.pos().x() == 0
        assert graphics._video_item.pos().y() == 0
        assert graphics._video_item.size().width() == graphics._output_w
        assert graphics._video_item.size().height() == graphics._output_h
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_video_source_uses_qt_playback_proxy(qapp, monkeypatch, tmp_path):
    from krok_helper.subtitle_render.frontend import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    graphics = PreviewGraphicsView()
    source = tmp_path / "source.mp4"
    proxy = tmp_path / "proxy.mp4"
    source.write_bytes(b"placeholder")
    proxy.write_bytes(b"proxy")
    monkeypatch.setattr(pg, "qt_playback_source", lambda path: proxy)
    seen = {}

    class FakePlayer:
        def pause(self):
            seen["paused"] = True

        def setSource(self, url):
            seen["source"] = url.toLocalFile()

        def setPosition(self, ms):
            seen["position"] = ms

        def play(self):
            seen["played"] = True

    try:
        graphics._video_player = FakePlayer()
        graphics.set_video_source(source)

        assert Path(seen["source"]) == proxy
        assert seen["position"] == 0
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_propagates_quality_to_shared_player(qapp):
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    seen: list[str] = []

    class FakeController:
        def set_video_output(self, _output):
            pass

        def set_preview_quality(self, quality):
            seen.append(quality)

    graphics = PreviewGraphicsView()
    try:
        graphics.use_external_player(FakeController())
        graphics.set_preview_quality("low")

        assert seen == ["high", "low"]
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_async_preview_target_size_uses_device_pixel_ratio():
    from krok_helper.subtitle_render.frontend.preview_async import preview_render_target_size

    assert preview_render_target_size(1920, 1080, 1.25) == (2400, 1350, 1.25)
    assert preview_render_target_size(0, 0, 0) == (1, 1, 1.0)
    assert preview_render_target_size(1, 1, -1.0) == (1, 1, 0.01)


def test_preview_quality_caps_lower_tiers_and_preserves_full_quality():
    from krok_helper.subtitle_render.frontend.preview_async import (
        normalize_preview_quality,
        preview_quality_render_scale,
    )

    assert preview_quality_render_scale(0.8, "low") == 0.25
    assert preview_quality_render_scale(0.4, "medium") == 0.4
    assert preview_quality_render_scale(1.5, "high") == 1.5
    assert normalize_preview_quality("unknown") == "high"


def test_transport_preview_quality_defaults_and_emits(qapp):
    bar = _bar(qapp)
    seen: list[str] = []
    bar.previewQualityChanged.connect(seen.append)

    assert bar._preview_quality_label.text() == "预览质量"
    assert bar.preview_quality() == "high"

    bar._preview_quality_combo.setCurrentIndex(
        bar._preview_quality_combo.findData("low")
    )

    assert bar.preview_quality() == "low"
    assert seen == ["low"]
    assert "540p" in bar._preview_quality_combo.toolTip()
    assert "不影响视频导出" in bar._preview_quality_combo.toolTip()


def test_native_preview_lookahead_timestamps_only_expand_while_playing():
    from krok_helper.subtitle_render.frontend.preview_async import native_preview_timestamps

    assert native_preview_timestamps(1_000, playing=False, fps=60, lookahead_frames=4) == [1_000]
    assert native_preview_timestamps(1_000, playing=True, fps=60, lookahead_frames=4) == [
        1_000,
        1_017,
        1_033,
        1_050,
        1_067,
    ]
    assert native_preview_timestamps(
        1_000,
        playing=True,
        fps=60,
        lookahead_frames=4,
        include_current=False,
    ) == [1_017, 1_033, 1_050, 1_067]
    assert native_preview_timestamps(
        1_000,
        playing=False,
        fps=60,
        lookahead_frames=4,
        include_current=False,
    ) == []


def test_gpu_preview_wide_stroke_keeps_hardware_backend(qapp, monkeypatch):
    from dataclasses import replace

    from krok_helper.subtitle_render.frontend.preview_async import (
        GpuAsyncSubtitleRenderer,
    )
    from krok_helper.subtitle_render.models import Style, TimingTrack

    monkeypatch.setenv("KROK_SUBTITLE_GPU_FORCE_WARP", "0")
    renderer = GpuAsyncSubtitleRenderer(320, 180)
    try:
        wide = replace(Style(), stroke_width_px=14, latin_stroke_width_px=14)
        renderer.set_state(TimingTrack(), wide)
        assert renderer._force_warp is False
        assert renderer.stats_snapshot()["warp_selected"] == 0
    finally:
        renderer.stop()


def test_gpu_preview_explicit_warp_request_is_preserved(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview_async import (
        GpuAsyncSubtitleRenderer,
    )
    from krok_helper.subtitle_render.models import Style, TimingTrack

    monkeypatch.setenv("KROK_SUBTITLE_GPU_FORCE_WARP", "1")
    renderer = GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_state(TimingTrack(), Style())
        assert renderer._force_warp is True
        assert renderer.stats_snapshot()["warp_selected"] == 1
    finally:
        renderer.stop()


def test_native_preview_frame_cache_detaches_and_evicts_oldest():
    from krok_helper.subtitle_render.frontend.preview_async import NativePreviewFrameCache

    cache = NativePreviewFrameCache(max_frames=2)
    first = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
    second = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
    third = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)

    first.fill(QColor("#112233"))
    cache.store(1_000, first)
    first.fill(QColor("#445566"))
    cached_first = cache.take(1_000)
    assert cached_first is not None
    assert cached_first.pixelColor(0, 0) == QColor("#112233")

    first.fill(QColor("#112233"))
    second.fill(QColor("#000000"))
    third.fill(QColor("#FFFFFF"))
    cache.store(1_000, first)
    cache.store(1_017, second)
    cache.store(1_033, third)

    assert cache.take(1_000) is None
    cached = cache.take(1_017)
    assert cached is not None
    assert cached.pixelColor(0, 0) == QColor("#000000")
    assert cache.take(1_017) is None


def test_native_preview_frame_cache_uses_fps_normalized_keys():
    from krok_helper.subtitle_render.frontend.preview_async import NativePreviewFrameCache

    cache = NativePreviewFrameCache(max_frames=2, fps=60)
    image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#223344"))

    cache.store(1_017, image)

    cached = cache.take(1_016)
    assert cached is not None
    assert cached.pixelColor(0, 0) == QColor("#223344")
    assert cache.take(1_017) is None


def test_native_preview_stats_snapshot_tracks_core_counters():
    from krok_helper.subtitle_render.frontend.preview_async import NativePreviewStats

    stats = NativePreviewStats()

    stats.note_cache_hit()
    stats.note_cache_miss()
    stats.note_future_frame_cached()
    stats.note_stale_frame_dropped()
    stats.note_generation_cancelled()
    stats.note_native_generation_cancelled_event()
    stats.note_range_done_event()

    assert stats.snapshot() == {
        "cache_hits": 1,
        "cache_misses": 1,
        "future_frames_cached": 1,
        "stale_frames_dropped": 1,
        "generations_cancelled": 1,
        "native_generation_cancelled_events": 1,
        "range_done_events": 1,
        "native_renderer_failures": 0,
    }


def test_async_preview_enabled_defaults_on_and_env_can_disable(monkeypatch):
    from krok_helper.subtitle_render.frontend.preview_async import async_preview_enabled

    monkeypatch.delenv("KROK_SUBTITLE_ASYNC_PREVIEW", raising=False)
    assert async_preview_enabled() is True

    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("KROK_SUBTITLE_ASYNC_PREVIEW", value)
        assert async_preview_enabled() is True

    for value in ("0", "false", "no", "off"):
        monkeypatch.setenv("KROK_SUBTITLE_ASYNC_PREVIEW", value)
        assert async_preview_enabled() is False


def test_native_preview_defaults_off_and_env_can_opt_in(monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa

    monkeypatch.delenv("KROK_SUBTITLE_NATIVE_RENDER", raising=False)
    assert pa.native_preview_enabled() is False

    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_RENDER", "1")
    assert pa.native_preview_enabled() is True

    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_RENDER", "0")
    assert pa.native_preview_enabled() is False

    monkeypatch.delenv("KROK_SUBTITLE_NATIVE_RENDER", raising=False)
    assert pa.native_preview_enabled() is False


def test_gpu_preview_defaults_to_g5_on_interactive_windows(monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa

    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.delenv("KROK_SUBTITLE_GPU_PREVIEW", raising=False)
    assert pa.gpu_preview_enabled() is (os.name == "nt")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    assert pa.gpu_preview_enabled() is False

    monkeypatch.setenv("KROK_SUBTITLE_GPU_PREVIEW", "1")
    assert pa.gpu_preview_enabled() is True

    monkeypatch.setenv("KROK_SUBTITLE_GPU_PREVIEW", "0")
    assert pa.gpu_preview_enabled() is False


def test_gpu_native_preview_is_hard_disabled(monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa

    monkeypatch.delenv("KROK_SUBTITLE_GPU_NATIVE_PREVIEW", raising=False)
    assert pa.gpu_native_preview_enabled() is False

    monkeypatch.setenv("KROK_SUBTITLE_GPU_NATIVE_PREVIEW", "1")
    assert pa.gpu_native_preview_enabled() is False

    monkeypatch.setenv("KROK_SUBTITLE_GPU_NATIVE_PREVIEW", "0")
    assert pa.gpu_native_preview_enabled() is False


def test_gpu_renderer_never_enters_g6_even_with_legacy_env_opt_in(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview_async import (
        GpuAsyncSubtitleRenderer,
    )

    monkeypatch.setenv("KROK_SUBTITLE_GPU_NATIVE_PREVIEW", "1")
    renderer = GpuAsyncSubtitleRenderer(320, 180)
    try:
        assert renderer.uses_native_preview is False
    finally:
        renderer.stop()


def test_async_preview_renderer_stops_qthread(qapp):
    from krok_helper.subtitle_render.frontend.preview_async import AsyncSubtitleRenderer

    renderer = AsyncSubtitleRenderer(320, 180)
    assert renderer._thread.isRunning()

    renderer.stop()

    assert not renderer._thread.isRunning()


def test_preview_graphics_updates_async_render_target(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    class FakeSignal:
        def connect(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FakeAsyncRenderer:
        instances = []

        def __init__(self, width, height, parent=None):
            self.init_args = (width, height, parent)
            self.frame_ready = FakeSignal()
            self.targets = []
            self.requests = []
            FakeAsyncRenderer.instances.append(self)

        def set_render_target(self, width, height, device_pixel_ratio=1.0):
            self.targets.append((width, height, device_pixel_ratio))

        def set_state(self, track, style):
            self.state = (track, style)

        def request(self, t_ms):
            self.requests.append(t_ms)

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "native_preview_enabled", lambda: False)
    monkeypatch.setattr(pg, "AsyncSubtitleRenderer", FakeAsyncRenderer)

    graphics = PreviewGraphicsView()
    try:
        renderer = FakeAsyncRenderer.instances[-1]
        assert renderer.targets

        graphics.set_output_size(1280, 720)

        width, height, dpr = renderer.targets[-1]
        assert (width, height) == (1280, 720)
        assert math.isclose(dpr, graphics._scene_device_pixel_ratio())
        assert renderer.requests[-1] == graphics.current_time_ms

        display_scale = graphics._scene_device_pixel_ratio()
        graphics.set_preview_quality("low")
        assert renderer.targets[-1][:2] == (1280, 720)
        assert math.isclose(renderer.targets[-1][2], min(display_scale, 0.25))
        assert renderer.requests[-1] == graphics.current_time_ms
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_debounces_interactive_resize(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    class FakeSignal:
        def connect(self, *args, **kwargs):
            pass

    class FakeAsyncRenderer:
        instances = []

        def __init__(self, width, height, parent=None):
            self.frame_ready = FakeSignal()
            self.targets = []
            self.requests = []
            FakeAsyncRenderer.instances.append(self)

        def set_render_target(self, width, height, device_pixel_ratio=1.0):
            self.targets.append((width, height, device_pixel_ratio))

        def set_state(self, *args, **kwargs):
            pass

        def request(self, t_ms):
            self.requests.append(t_ms)

        def stop(self):
            pass

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "gpu_preview_enabled", lambda: False)
    monkeypatch.setattr(pg, "native_preview_enabled", lambda: False)
    monkeypatch.setattr(pg, "AsyncSubtitleRenderer", FakeAsyncRenderer)

    graphics = PreviewGraphicsView()
    try:
        graphics.show()
        qapp.processEvents()
        renderer = FakeAsyncRenderer.instances[-1]
        before = len(renderer.targets)

        graphics.resize(900, 520)
        qapp.processEvents()
        assert len(renderer.targets) == before

        deadline = time.monotonic() + 1.0
        while len(renderer.targets) == before and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert len(renderer.targets) == before + 1
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_uses_native_async_renderer_when_enabled(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    class FakeSignal:
        def connect(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FakeNativeRenderer:
        instances = []

        def __init__(self, width, height, parent=None):
            self.init_args = (width, height, parent)
            self.frame_ready = FakeSignal()
            self.targets = []
            self.requests = []
            FakeNativeRenderer.instances.append(self)

        def set_render_target(self, width, height, device_pixel_ratio=1.0):
            self.targets.append((width, height, device_pixel_ratio))

        def set_state(self, track, style):
            self.state = (track, style)

        def request(self, t_ms):
            self.requests.append(t_ms)

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "native_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "NativeAsyncSubtitleRenderer", FakeNativeRenderer)

    graphics = PreviewGraphicsView()
    try:
        renderer = FakeNativeRenderer.instances[-1]
        assert renderer.init_args[:2] == (1920, 1080)
        assert renderer.targets
        assert renderer.requests[-1] == graphics.current_time_ms
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_gpu_opt_in_takes_precedence(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    class FakeSignal:
        def connect(self, *args, **kwargs):
            pass

    class FakeGpuRenderer:
        instances = []

        def __init__(self, width, height, parent=None):
            self.init_args = (width, height, parent)
            self.frame_ready = FakeSignal()
            FakeGpuRenderer.instances.append(self)

        def set_render_target(self, *args):
            pass

        def set_state(self, *args):
            pass

        def request(self, *args):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "gpu_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "native_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "GpuAsyncSubtitleRenderer", FakeGpuRenderer)

    graphics = PreviewGraphicsView()
    try:
        assert FakeGpuRenderer.instances[-1].init_args[:2] == (1920, 1080)
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_switches_gpu_backend_at_runtime(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    class FakeSignal:
        def connect(self, *args, **kwargs):
            pass

    class FakeRenderer:
        instances = []

        def __init__(self, width, height, parent=None):
            self.frame_ready = FakeSignal()
            self.stopped = False
            type(self).instances.append(self)

        def set_render_target(self, *args):
            pass

        def set_state(self, *args):
            pass

        def set_playing(self, *args):
            pass

        def request(self, *args):
            pass

        def stop(self):
            self.stopped = True

    class FakePainterRenderer(FakeRenderer):
        instances = []

    class FakeGpuRenderer(FakeRenderer):
        instances = []

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "gpu_preview_enabled", lambda: False)
    monkeypatch.setattr(pg, "native_preview_enabled", lambda: False)
    monkeypatch.setattr(pg, "AsyncSubtitleRenderer", FakePainterRenderer)
    monkeypatch.setattr(pg, "GpuAsyncSubtitleRenderer", FakeGpuRenderer)

    graphics = PreviewGraphicsView()
    try:
        first_painter = FakePainterRenderer.instances[-1]
        graphics.set_gpu_preview_enabled(True)
        assert first_painter.stopped is True
        gpu = FakeGpuRenderer.instances[-1]
        assert graphics._async_renderer is gpu

        graphics.set_gpu_preview_enabled(False)
        assert gpu.stopped is True
        assert graphics._async_renderer is FakePainterRenderer.instances[-1]
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_repeated_gpu_toggles_stop_worker_threads(qapp, monkeypatch):
    import threading

    from krok_helper.subtitle_render.frontend import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "gpu_preview_enabled", lambda: False)
    monkeypatch.setattr(pg, "native_preview_enabled", lambda: False)
    graphics = PreviewGraphicsView()
    try:
        for _ in range(12):
            graphics.set_gpu_preview_enabled(True)
            graphics.set_gpu_preview_enabled(False)
        assert not any(
            thread.is_alive() and thread.name == "subtitle-preview-gpu-render"
            for thread in threading.enumerate()
        )
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_g6_passes_native_hwnd_and_physical_scene_geometry(
    qapp, monkeypatch
):
    from krok_helper.subtitle_render.frontend import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    class FakeSignal:
        def connect(self, *args, **kwargs):
            pass

    class FakeNativePreviewRenderer:
        instances = []

        def __init__(self, width, height, parent=None):
            self.frame_ready = FakeSignal()
            self.frame_presented = FakeSignal()
            self.fallback_occurred = FakeSignal()
            self.uses_native_preview = True
            self.render_targets = []
            self.native_targets = []
            FakeNativePreviewRenderer.instances.append(self)

        def set_render_target(self, width, height, device_pixel_ratio=1.0):
            self.render_targets.append((width, height, device_pixel_ratio))

        def set_native_target(self, parent_hwnd, x, y, width, height):
            self.native_targets.append((parent_hwnd, x, y, width, height))

        def set_state(self, *args, **kwargs):
            pass

        def request(self, t_ms):
            pass

        def set_playing(self, playing):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "gpu_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "GpuAsyncSubtitleRenderer", FakeNativePreviewRenderer)
    graphics = PreviewGraphicsView()
    try:
        graphics.resize(800, 500)
        graphics.show()
        qapp.processEvents()
        graphics._refresh_async_target()  # noqa: SLF001

        renderer = FakeNativePreviewRenderer.instances[-1]
        logical_w, logical_h, render_dpr = renderer.render_targets[-1]
        parent_hwnd, _x, _y, physical_w, physical_h = renderer.native_targets[-1]
        expected_w, expected_h, _ = pg.preview_render_target_size(
            logical_w, logical_h, render_dpr
        )
        assert parent_hwnd == int(graphics.viewport().winId())
        assert (physical_w, physical_h) == (expected_w, expected_h)
        assert physical_w > 0 and physical_h > 0
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_gpu_async_renderer_queue_is_capacity_one_latest_wins(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    first_started = threading.Event()
    unblock = threading.Event()
    latest_finished = threading.Event()
    rendered: list[int] = []

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured"}

        def render_gpu_frame(self, t_ms, **kwargs):
            rendered.append(int(t_ms))
            if len(rendered) == 1:
                first_started.set()
                unblock.wait(timeout=2.0)
            if int(t_ms) == 2_099:
                latest_finished.set()
            return {
                "ok": True,
                "event": "gpu_frame_ready",
                "shm_key": "gpu-test-ring",
                "t_ms": int(t_ms),
            }

        def close(self):
            unblock.set()

    class FakeGpuReader:
        def __init__(self, shm_key):
            self.shm_key = shm_key

        @classmethod
        def from_event(cls, event):
            return cls(event["shm_key"])

        def read_qimage(self, event):
            image = QImage(8, 8, QImage.Format.Format_RGBA8888)
            image.fill(QColor("#112233"))
            return image

        def close(self):
            pass

    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    monkeypatch.setattr(pa, "SharedFrameRingReader", FakeGpuReader)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        assert first_started.wait(timeout=2.0)
        for t_ms in range(2_000, 2_100):
            renderer.request(t_ms)
        time.sleep(0.2)
        released_at = time.monotonic()
        unblock.set()
        assert latest_finished.wait(timeout=2.0)
        recovery_ms = (time.monotonic() - released_at) * 1000.0

        assert rendered == [1_000, 2_099]
        assert recovery_ms < 250.0
        stats = renderer.stats_snapshot()
        assert stats["requests"] == 101
        assert stats["pending_replaced"] == 99
        assert stats["max_pending"] == 1
        assert stats["configure_count"] == 1
        assert stats["stale_frames_dropped"] == 1
    finally:
        unblock.set()
        renderer.stop()


def test_gpu_native_preview_presents_without_shared_memory_or_qimage(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    presented_calls: list[tuple[int, dict]] = []
    presented_signal: list[int] = []
    finished = threading.Event()

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready", "native_preview_protocol": 1}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "native_preview": True}

        def present_gpu_frame(self, t_ms, **kwargs):
            presented_calls.append((int(t_ms), dict(kwargs)))
            finished.set()
            return {
                "ok": True,
                "event": "gpu_frame_presented",
                "t_ms": int(t_ms),
                "render_ms": 1.25,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "transport": "direct_composition",
            }

        def render_gpu_frame(self, *args, **kwargs):
            raise AssertionError("G6 native preview must not use shared-memory readback")

        def close(self):
            pass

    class UnexpectedReader:
        @classmethod
        def from_event(cls, event):
            raise AssertionError("G6 native preview must not construct a QImage reader")

    monkeypatch.setattr(pa, "gpu_native_preview_enabled", lambda: True)
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    monkeypatch.setattr(pa, "SharedFrameRingReader", UnexpectedReader)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    renderer.frame_presented.connect(presented_signal.append)
    try:
        renderer.set_native_target(12345, -10, 5, 320, 180)
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        assert finished.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while not presented_signal and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        assert presented_signal == [1_000]
        assert presented_calls == [
            (
                1_000,
                {
                    "parent_hwnd": 12345,
                    "x": -10,
                    "y": 5,
                    "width": 320,
                    "height": 180,
                    "force_warp": False,
                    "generation": 1,
                    "frame_index": 0,
                },
            )
        ]
        timings = renderer.timing_snapshot()
        assert timings["render_ms"]["mean"] == 1.25
        assert timings["present_ms"]["mean"] == 0.2
        assert timings["readback_ms"]["mean"] == 0.0
        assert renderer.stats_snapshot()["max_pending"] == 1
    finally:
        renderer.stop()


def test_gpu_async_renderer_failure_falls_back_to_painter(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    class FailingGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            raise pa.NativeRendererError("injected GPU failure")

        def close(self):
            pass

    monkeypatch.setattr(pa, "NativeRendererProcess", FailingGpuProcess)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    frames: list[tuple[QImage, int]] = []
    fallbacks: list[str] = []
    renderer.frame_ready.connect(lambda image, t_ms: frames.append((image, t_ms)))
    renderer.fallback_occurred.connect(fallbacks.append)
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        deadline = time.monotonic() + 2.0
        while not frames and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        assert frames and frames[0][1] == 1_000
        stats = renderer.stats_snapshot()
        assert stats["renderer_failures"] == 1
        assert stats["fallback_frames"] == 1
        assert len(fallbacks) == 1
        assert "injected GPU failure" in fallbacks[0]
        assert "Painter" in fallbacks[0]
    finally:
        renderer.stop()


def test_gpu_async_renderer_capability_fallback_skips_sidecar(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import (
        Style,
        TimingChar,
        TimingLine,
        TimingTrack,
    )

    constructed = 0

    class UnexpectedGpuProcess:
        def __init__(self, *args, **kwargs):
            nonlocal constructed
            constructed += 1
            raise AssertionError("unsupported scene must not start the GPU sidecar")

    monkeypatch.setattr(pa, "NativeRendererProcess", UnexpectedGpuProcess)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    frames: list[int] = []
    fallbacks: list[str] = []
    renderer.frame_ready.connect(lambda _image, t_ms: frames.append(int(t_ms)))
    renderer.fallback_occurred.connect(fallbacks.append)
    try:
        renderer.set_state(
                TimingTrack(
                    lines=[
                        TimingLine(
                            chars=[TimingChar("A", 0)],
                            end_ms=500,
                        )
                    ],
            ),
                Style(entry_anim="future_effect"),
        )
        renderer.request(1_000)
        deadline = time.monotonic() + 2.0
        while not frames and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        assert frames == [1_000]
        assert constructed == 0
        stats = renderer.stats_snapshot()
        assert stats["capability_fallbacks"] == 1
        assert stats["fallback_frames"] == 1
        assert stats["renderer_failures"] == 0
        assert len(fallbacks) == 1
        assert "未知整行动画" in fallbacks[0]
        assert "Painter" in fallbacks[0]
    finally:
        renderer.stop()


def test_gpu_async_renderer_one_frame_lookahead_uses_bounded_cache(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    rendered: list[int] = []
    future_cached = threading.Event()

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured"}

        def render_gpu_frame(self, t_ms, **kwargs):
            rendered.append(int(t_ms))
            return {
                "ok": True,
                "event": "gpu_frame_ready",
                "shm_key": "gpu-lookahead-ring",
                "t_ms": int(t_ms),
            }

        def close(self):
            pass

    class FakeGpuReader:
        def __init__(self, shm_key):
            self.shm_key = shm_key

        @classmethod
        def from_event(cls, event):
            return cls(event["shm_key"])

        def read_qimage(self, event):
            image = QImage(8, 8, QImage.Format.Format_RGBA8888)
            image.fill(QColor("#112233"))
            if int(event["t_ms"]) == 1_017:
                future_cached.set()
            return image

        def close(self):
            pass

    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    monkeypatch.setattr(pa, "SharedFrameRingReader", FakeGpuReader)
    monkeypatch.setenv("KROK_SUBTITLE_GPU_LOOKAHEAD_FRAMES", "1")
    monkeypatch.setenv("KROK_SUBTITLE_GPU_MAX_LOOKAHEAD_FRAMES", "1")
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.set_playing(True)
        renderer.request(1_000)
        assert future_cached.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while renderer.stats_snapshot()["future_frames_cached"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)

        renderer.set_playing(False)
        renderer.request(1_017)

        assert rendered == [1_017]
        stats = renderer.stats_snapshot()
        assert stats["future_frames_cached"] == 1
        assert stats["frames_emitted"] == 1
        assert stats["cache_hits"] == 1
        assert stats["max_pending"] == 1
    finally:
        renderer.stop()


def test_gpu_async_renderer_resize_rotates_shared_memory_generation(qapp):
    from krok_helper.subtitle_render.frontend import preview_async as pa

    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        first_key = renderer._shm_key
        first_generation = renderer._generation

        renderer.set_render_target(640, 360, 1.0)

        assert renderer._shm_key != first_key
        assert renderer._generation == first_generation + 1
        assert renderer._needs_configure is True
    finally:
        renderer.stop()


def test_gpu_async_renderer_uses_target_resize_after_initial_scene(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    first_finished = threading.Event()
    resized_finished = threading.Event()
    configure_calls = []
    resize_calls = []

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            configure_calls.append(dict(kwargs))
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}

        def resize_gpu_target(self, **kwargs):
            resize_calls.append(dict(kwargs))
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}

        def render_gpu_frame(self, t_ms, **kwargs):
            (resized_finished if int(t_ms) == 2_000 else first_finished).set()
            return {
                "ok": True,
                "event": "gpu_frame_ready",
                "shm_key": "gpu-resize-ring",
                "t_ms": int(t_ms),
            }

        def close(self):
            pass

    class FakeGpuReader:
        def __init__(self, shm_key):
            self.shm_key = shm_key

        @classmethod
        def from_event(cls, event):
            return cls(event["shm_key"])

        def read_qimage(self, event):
            return QImage(8, 8, QImage.Format.Format_RGBA8888)

        def close(self):
            pass

    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    monkeypatch.setattr(pa, "SharedFrameRingReader", FakeGpuReader)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        assert first_finished.wait(timeout=2.0)

        renderer.set_render_target(640, 360, 0.5)
        renderer.request(2_000)
        assert resized_finished.wait(timeout=2.0)

        assert len(configure_calls) == 1
        assert configure_calls[0]["defer_followers"] is True
        assert configure_calls[0]["defer_realizations_until_first_frame"] is True
        assert len(resize_calls) == 1
        assert resize_calls[0]["width"] == 640
        assert resize_calls[0]["height"] == 360
        assert resize_calls[0]["dpr"] == 0.5
    finally:
        renderer.stop()


def test_gpu_async_renderer_restarts_after_bounded_fallback(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    process_count = 0

    class RecoveringGpuProcess:
        def __init__(self, *args, **kwargs):
            nonlocal process_count
            process_count += 1
            self.number = process_count

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            if self.number == 1:
                raise pa.NativeRendererError("injected first-process failure")
            return {"ok": True, "event": "gpu_configured"}

        def render_gpu_frame(self, t_ms, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_ready",
                "shm_key": "gpu-recovered-ring",
                "t_ms": int(t_ms),
                "render_ms": 1.25,
                "readback_ms": 2.5,
            }

        def close(self):
            pass

    class FakeGpuReader:
        def __init__(self, shm_key):
            self.shm_key = shm_key

        @classmethod
        def from_event(cls, event):
            return cls(event["shm_key"])

        def read_qimage(self, event):
            image = QImage(8, 8, QImage.Format.Format_RGBA8888)
            image.fill(QColor("#112233"))
            return image

        def close(self):
            pass

    monkeypatch.setattr(pa, "NativeRendererProcess", RecoveringGpuProcess)
    monkeypatch.setattr(pa, "SharedFrameRingReader", FakeGpuReader)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    frames: list[int] = []
    renderer.frame_ready.connect(lambda _image, t_ms: frames.append(int(t_ms)))
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        deadline = time.monotonic() + 2.0
        while len(frames) < 1 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert frames == [1_000]

        renderer._retry_after = 0.0
        renderer.request(2_000)
        deadline = time.monotonic() + 2.0
        while len(frames) < 2 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        assert frames == [1_000, 2_000]
        stats = renderer.stats_snapshot()
        assert process_count == 2
        assert stats["renderer_failures"] == 1
        assert stats["renderer_restarts"] == 1
        assert stats["fallback_frames"] == 1
        timings = renderer.timing_snapshot()
        assert timings["render_ms"]["count"] == 1
        assert timings["render_ms"]["mean"] == 1.25
        assert timings["readback_ms"]["mean"] == 2.5
        assert timings["ready_latency_ms"]["count"] == 1
    finally:
        renderer.stop()


def test_gpu_async_renderer_pooled_batch_accepts_out_of_order_completion(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    pending: list[dict] = []
    emitted: list[int] = []

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {
                "ok": True,
                "event": "gpu_configured",
                "worker_count": 2,
                "dedicated_video_memory": 8 * 1024**3,
            }

        def begin_render_gpu_frame(self, t_ms, **kwargs):
            pending.append(
                {
                    "ok": True,
                    "event": "gpu_frame_ready",
                    "shm_key": "gpu-pool-ring",
                    "t_ms": int(t_ms),
                    "request_serial": int(kwargs["request_serial"]),
                    "render_ms": 1.0,
                    "readback_ms": 1.0,
                }
            )

        def finish_render_gpu_frame(self):
            return pending.pop()

        def send_cancel_generation(self, _generation):
            pass

        def close(self):
            pass

    class FakeGpuReader:
        def __init__(self, shm_key):
            self.shm_key = shm_key

        @classmethod
        def from_event(cls, event):
            return cls(event["shm_key"])

        def read_qimage(self, _event):
            image = QImage(8, 8, QImage.Format.Format_RGBA8888)
            image.fill(QColor("#112233"))
            return image

        def close(self):
            pass

    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    monkeypatch.setattr(pa, "SharedFrameRingReader", FakeGpuReader)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    renderer.frame_ready.connect(lambda _image, t_ms: emitted.append(int(t_ms)))
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.set_playing(True)
        renderer.request(1_000)
        deadline = time.monotonic() + 2.0
        while renderer.stats_snapshot()["future_frames_cached"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        renderer.set_playing(False)
        renderer.request(1_200)
        while not emitted and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert emitted == [1_200]
        stats = renderer.stats_snapshot()
        assert stats["worker_count"] == 2
        assert stats["max_in_flight"] == 2
        assert stats["future_frames_cached"] == 2
    finally:
        renderer.stop()


def test_gpu_async_renderer_reserves_final_ring_before_deferred_follower(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    slot_counts: list[int] = []
    pending: list[dict] = []
    first_ready = threading.Event()

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {
                "ok": True,
                "event": "gpu_configured",
                "worker_count": 1,
                "dedicated_video_memory": 8 * 1024**3,
            }

        def render_gpu_frame(self, t_ms, **kwargs):
            slot_counts.append(int(kwargs["slot_count"]))
            first_ready.set()
            return {
                "ok": True,
                "event": "gpu_frame_ready",
                "shm_key": "gpu-deferred-ring",
                "t_ms": int(t_ms),
                "worker_count_ready": 2,
            }

        def begin_render_gpu_frame(self, t_ms, **kwargs):
            slot_counts.append(int(kwargs["slot_count"]))
            pending.append(
                {
                    "ok": True,
                    "event": "gpu_frame_ready",
                    "shm_key": "gpu-deferred-ring",
                    "t_ms": int(t_ms),
                    "request_serial": int(kwargs["request_serial"]),
                    "worker_count_ready": 2,
                }
            )

        def finish_render_gpu_frame(self):
            return pending.pop()

        def send_cancel_generation(self, _generation):
            pass

        def close(self):
            pass

    class FakeGpuReader:
        def __init__(self, shm_key):
            self.shm_key = shm_key

        @classmethod
        def from_event(cls, event):
            return cls(event["shm_key"])

        def read_qimage(self, _event):
            return QImage(8, 8, QImage.Format.Format_RGBA8888)

        def close(self):
            pass

    monkeypatch.setenv("KROK_SUBTITLE_GPU_WORKERS", "2")
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    monkeypatch.setattr(pa, "SharedFrameRingReader", FakeGpuReader)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.set_playing(False)
        renderer.request(0)
        assert first_ready.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while renderer.stats_snapshot()["worker_count"] != 2 and time.monotonic() < deadline:
            time.sleep(0.01)

        renderer.set_playing(True)
        renderer.request(1_000)
        while len(slot_counts) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)

        assert slot_counts == [2, 2, 2]
    finally:
        renderer.stop()


def test_gpu_async_renderer_ignores_dropped_single_frame_without_fallback(
    qapp, monkeypatch
):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    calls = 0
    rendered = threading.Event()
    emitted: list[int] = []
    fallbacks: list[str] = []

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}

        def render_gpu_frame(self, t_ms, **kwargs):
            nonlocal calls
            calls += 1
            rendered.set()
            if calls == 1:
                return {
                    "ok": True,
                    "event": "gpu_frame_dropped",
                    "generation": kwargs["generation"],
                    "reason": "generation_cancelled",
                }
            return {
                "ok": True,
                "event": "gpu_frame_ready",
                "shm_key": "gpu-after-drop-ring",
                "t_ms": int(t_ms),
            }

        def send_cancel_generation(self, _generation):
            pass

        def close(self):
            pass

    class FakeGpuReader:
        def __init__(self, shm_key):
            self.shm_key = shm_key

        @classmethod
        def from_event(cls, event):
            assert event["event"] == "gpu_frame_ready"
            return cls(event["shm_key"])

        def read_qimage(self, _event):
            return QImage(8, 8, QImage.Format.Format_RGBA8888)

        def close(self):
            pass

    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    monkeypatch.setattr(pa, "SharedFrameRingReader", FakeGpuReader)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    renderer.frame_ready.connect(lambda _image, t_ms: emitted.append(int(t_ms)))
    renderer.fallback_occurred.connect(fallbacks.append)
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        assert rendered.wait(timeout=2.0)
        rendered.clear()

        renderer.request(2_000)
        assert rendered.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while not emitted and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        assert emitted == [2_000]
        assert fallbacks == []
        stats = renderer.stats_snapshot()
        assert stats["stale_frames_dropped"] == 1
        assert stats["renderer_failures"] == 0
    finally:
        renderer.stop()


def test_gpu_async_renderer_weak_gpu_shrinks_pool_to_one(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    configured_workers: list[int] = []
    delivered = threading.Event()

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            workers = int(kwargs["worker_count"])
            configured_workers.append(workers)
            return {
                "ok": True,
                "event": "gpu_configured",
                "worker_count": workers,
                "dedicated_video_memory": 1024**3,
            }

        def render_gpu_frame(self, t_ms, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_ready",
                "shm_key": "gpu-weak-ring",
                "t_ms": int(t_ms),
            }

        def send_cancel_generation(self, _generation):
            pass

        def close(self):
            pass

    class FakeGpuReader:
        def __init__(self, shm_key):
            self.shm_key = shm_key

        @classmethod
        def from_event(cls, event):
            return cls(event["shm_key"])

        def read_qimage(self, _event):
            image = QImage(8, 8, QImage.Format.Format_RGBA8888)
            image.fill(0)
            delivered.set()
            return image

        def close(self):
            pass

    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    monkeypatch.setattr(pa, "SharedFrameRingReader", FakeGpuReader)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        assert delivered.wait(timeout=2.0)
        assert configured_workers == [2, 1]
        assert renderer.stats_snapshot()["worker_count"] == 1
    finally:
        renderer.stop()


def test_preview_graphics_passes_playing_state_to_async_renderer(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    class FakeSignal:
        def connect(self, *args, **kwargs):
            pass

    class FakeAsyncRenderer:
        instances = []

        def __init__(self, width, height, parent=None):
            self.frame_ready = FakeSignal()
            self.playing_states = []
            FakeAsyncRenderer.instances.append(self)

        def set_render_target(self, width, height, device_pixel_ratio=1.0):
            pass

        def set_state(self, track, style):
            pass

        def request(self, t_ms):
            pass

        def set_playing(self, playing):
            self.playing_states.append(bool(playing))

        def stop(self):
            pass

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "native_preview_enabled", lambda: False)
    monkeypatch.setattr(pg, "AsyncSubtitleRenderer", FakeAsyncRenderer)

    graphics = PreviewGraphicsView()
    try:
        renderer = FakeAsyncRenderer.instances[-1]

        graphics.set_playing(True)
        graphics.set_playing(False)

        assert renderer.playing_states == [True, False]
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_native_async_renderer_cancels_active_generation_on_new_request(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    started = threading.Event()
    unblock = threading.Event()
    cancels: list[int] = []

    class FakeNativeRendererProcess:
        def __init__(self, *args, **kwargs):
            self.started_ranges: list[dict[str, object]] = []

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure(self, *args, **kwargs):
            return {"ok": True, "event": "configured"}

        def start_render_range(self, timestamps_ms, *, generation, threads, shm_key=None, ring_slots=3):
            self.started_ranges.append(
                {
                    "timestamps": list(timestamps_ms),
                    "generation": generation,
                    "threads": threads,
                    "shm_key": shm_key,
                    "ring_slots": ring_slots,
                }
            )
            started.set()
            return {"ok": True, "event": "range_started", "generation": generation}

        def read_event(self):
            unblock.wait(timeout=2.0)
            return {"ok": True, "event": "range_done", "generation": 1}

        def send_cancel_generation(self, generation):
            cancels.append(int(generation))
            unblock.set()

        def close(self):
            unblock.set()

    monkeypatch.setattr(pa, "NativeRendererProcess", FakeNativeRendererProcess)
    renderer = pa.NativeAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        assert started.wait(timeout=2.0)

        renderer.request(1_017)

        assert cancels == [2]
        assert renderer.stats_snapshot()["generations_cancelled"] == 1
    finally:
        renderer.stop()


def test_native_async_renderer_keeps_active_generation_for_sequential_playback_tick(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    started = threading.Event()
    unblock = threading.Event()
    cancels: list[int] = []

    class FakeNativeRendererProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure(self, *args, **kwargs):
            return {"ok": True, "event": "configured"}

        def start_render_range(self, *args, **kwargs):
            started.set()
            return {"ok": True, "event": "range_started"}

        def read_event(self):
            unblock.wait(timeout=2.0)
            return {"ok": True, "event": "range_done"}

        def send_cancel_generation(self, generation):
            cancels.append(int(generation))
            unblock.set()

        def close(self):
            unblock.set()

    monkeypatch.setattr(pa, "NativeRendererProcess", FakeNativeRendererProcess)
    renderer = pa.NativeAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.set_playing(True)
        renderer.request(1_000)
        assert started.wait(timeout=2.0)

        renderer.request(1_017)

        assert cancels == []
        assert renderer.stats_snapshot()["generations_cancelled"] == 0
    finally:
        unblock.set()
        renderer.stop()


def test_native_async_renderer_waiting_requests_use_frame_bucket(qapp):
    from krok_helper.subtitle_render.frontend import preview_async as pa

    renderer = pa.NativeAsyncSubtitleRenderer(320, 180)
    try:
        with renderer._condition:
            renderer._waiting_request_by_key[renderer._frame_cache.key_for(1_034)] = 1_034

        assert renderer._take_waiting_request_for_slot(1_033) == 1_034
        assert renderer._take_waiting_request_for_slot(1_033) is None
        assert renderer._mark_emitted_if_new(1_034) is False
    finally:
        renderer.stop()


def test_native_async_renderer_marks_restart_on_render_target_change(qapp):
    from krok_helper.subtitle_render.frontend import preview_async as pa

    renderer = pa.NativeAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_render_target(640, 360, 1.0)
        with renderer._condition:
            renderer._pending_t = 1_000
        snapshot = renderer._take_next_request()

        assert snapshot is not None
        restart_renderer = snapshot[7]
        assert restart_renderer is True
    finally:
        renderer.stop()


def test_native_async_renderer_purges_stale_waiting_requests(qapp):
    """G2 硬性要求 1：过期 waiting 请求被丢弃，不回灌新 range（§2.5 死亡螺旋修复）。"""
    from krok_helper.subtitle_render.frontend import preview_async as pa

    renderer = pa.NativeAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_playing(True)
        stale_key = renderer._frame_cache.key_for(1_017)
        current_key = renderer._frame_cache.key_for(1_034)
        with renderer._condition:
            renderer._waiting_request_by_key[stale_key] = 1_017
            renderer._waiting_request_by_key[renderer._frame_cache.key_for(1_033)] = 1_033
            renderer._purge_stale_waiting_locked(current_key)
            # 早于当前帧桶的请求被清除；同帧桶的毫秒抖动条目保留。
            assert stale_key not in renderer._waiting_request_by_key
            assert current_key in renderer._waiting_request_by_key
        assert renderer.stats_snapshot()["stale_frames_dropped"] == 1
    finally:
        renderer.stop()


def test_native_async_renderer_adaptive_lookahead_shrinks_and_recovers(qapp):
    """G2 硬性要求 6：range 耗时超过前瞻窗口时收缩前瞻，恢复后逐步回涨。"""
    from krok_helper.subtitle_render.frontend import preview_async as pa

    renderer = pa.NativeAsyncSubtitleRenderer(320, 180)
    try:
        assert renderer._effective_lookahead == renderer._lookahead_frames

        # 60fps、前瞻 6：窗口 ≈ 116.7ms。慢 range 连续对半收缩，最低到 0（纯 latest-wins）。
        renderer._adapt_lookahead(500.0, playing=True)
        assert renderer._effective_lookahead == 3
        renderer._adapt_lookahead(500.0, playing=True)
        assert renderer._effective_lookahead == 1
        renderer._adapt_lookahead(500.0, playing=True)
        assert renderer._effective_lookahead == 0

        # 快 range 每次 +1 回涨，封顶在配置值。
        for _ in range(10):
            renderer._adapt_lookahead(5.0, playing=True)
        assert renderer._effective_lookahead == renderer._lookahead_frames

        # 暂停态不调整。
        renderer._adapt_lookahead(500.0, playing=False)
        assert renderer._effective_lookahead == renderer._lookahead_frames
    finally:
        renderer.stop()


def test_native_async_renderer_handles_cancelled_event_before_range_done(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    started = threading.Event()
    cancel_sent = threading.Event()
    events = [
        {"ok": True, "event": "generation_cancelled", "generation": 2},
        {"ok": True, "event": "range_done", "generation": 2},
    ]
    cancels: list[int] = []

    class FakeNativeRendererProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure(self, *args, **kwargs):
            return {"ok": True, "event": "configured"}

        def start_render_range(self, *args, **kwargs):
            started.set()
            return {"ok": True, "event": "range_started"}

        def read_event(self):
            cancel_sent.wait(timeout=2.0)
            if events:
                return events.pop(0)
            return {"ok": True, "event": "range_done", "generation": 2}

        def send_cancel_generation(self, generation):
            cancels.append(int(generation))
            cancel_sent.set()

        def close(self):
            cancel_sent.set()

    monkeypatch.setattr(pa, "NativeRendererProcess", FakeNativeRendererProcess)
    renderer = pa.NativeAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        assert started.wait(timeout=2.0)

        renderer.set_render_target(640, 360, 1.0)
        deadline = time.monotonic() + 2.0
        while events and time.monotonic() < deadline:
            qapp.processEvents()
            cancel_sent.wait(timeout=0.01)
        assert events == []

        stats = renderer.stats_snapshot()
        assert cancels == [2]
        assert stats["generations_cancelled"] == 1
        assert stats["native_generation_cancelled_events"] == 1
        assert stats["range_done_events"] == 1
    finally:
        renderer.stop()


def test_native_async_renderer_stats_report_cache_counts(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa

    unblock = threading.Event()

    class FakeNativeRendererProcess:
        def start(self):
            return {"ok": True, "event": "ready"}

        def configure(self, *args, **kwargs):
            return {"ok": True, "event": "configured"}

        def start_render_range(self, *args, **kwargs):
            return {"ok": True, "event": "range_started"}

        def read_event(self):
            unblock.wait(timeout=2.0)
            return {"ok": True, "event": "range_done"}

        def send_cancel_generation(self, generation):
            unblock.set()

        def close(self):
            unblock.set()

    monkeypatch.setattr(pa, "NativeRendererProcess", FakeNativeRendererProcess)
    renderer = pa.NativeAsyncSubtitleRenderer(320, 180)
    try:
        image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#111111"))
        renderer._frame_cache.store(1_017, image)

        renderer.request(1_017)
        renderer.request(1_000)

        stats = renderer.stats_snapshot()
        assert stats["cache_hits"] == 1
        assert stats["cache_misses"] == 1
    finally:
        renderer.stop()


def test_native_async_renderer_skips_current_native_frame_after_cache_hit(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    started = threading.Event()
    unblock = threading.Event()
    started_timestamps: list[int] = []

    class FakeNativeRendererProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure(self, *args, **kwargs):
            return {"ok": True, "event": "configured"}

        def start_render_range(self, timestamps_ms, *, generation, threads, shm_key=None, ring_slots=3):
            started_timestamps.extend(int(value) for value in timestamps_ms)
            started.set()
            return {"ok": True, "event": "range_started"}

        def read_event(self):
            unblock.wait(timeout=0.05)
            return {"ok": True, "event": "range_done"}

        def send_cancel_generation(self, generation):
            unblock.set()

        def close(self):
            unblock.set()

    monkeypatch.setattr(pa, "NativeRendererProcess", FakeNativeRendererProcess)
    renderer = pa.NativeAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.set_playing(True)
        image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#111111"))
        renderer._frame_cache.store(1_017, image)

        renderer.request(1_017)

        assert started.wait(timeout=2.0)
        assert 1_017 not in started_timestamps
        assert started_timestamps
    finally:
        unblock.set()
        renderer.stop()


def test_native_async_renderer_defaults_keep_preview_ahead(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa

    monkeypatch.delenv("KROK_SUBTITLE_NATIVE_THREADS", raising=False)
    monkeypatch.delenv("KROK_SUBTITLE_NATIVE_RING_SLOTS", raising=False)
    monkeypatch.delenv("KROK_SUBTITLE_NATIVE_LOOKAHEAD_FRAMES", raising=False)
    monkeypatch.setattr(pa.os, "cpu_count", lambda: 12)

    renderer = pa.NativeAsyncSubtitleRenderer(320, 180)
    try:
        assert renderer._lookahead_frames == 6
        assert renderer._threads == 6
        assert renderer._ring_slots == 8
    finally:
        renderer.stop()


def test_native_async_renderer_reuses_shared_reader_for_range(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_async as pa
    from krok_helper.subtitle_render.models import Style, TimingTrack

    class FakeSlot:
        def __init__(self, t_ms: int) -> None:
            self.t_ms = int(t_ms)

        def to_qimage(self):
            image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor("#111111"))
            return image

    class FakeRingReader:
        created: list[str] = []

        def __init__(self, shm_key: str) -> None:
            self.shm_key = shm_key
            self.closed = False
            self.created.append(shm_key)

        @classmethod
        def from_event(cls, event):
            return cls(str(event["shm_key"]))

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()
            return None

        def close(self):
            self.closed = True

        def read_frame(self, event):
            return FakeSlot(int(event["t_ms"]))

    class FakeNativeRendererProcess:
        def __init__(self, *args, **kwargs):
            self.events: list[dict[str, object]] = []

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure(self, *args, **kwargs):
            return {"ok": True, "event": "configured"}

        def start_render_range(self, timestamps_ms, *, generation, threads, shm_key=None, ring_slots=3):
            for index, t_ms in enumerate(timestamps_ms[:3]):
                self.events.append(
                    {
                        "ok": True,
                        "event": "frame_ready",
                        "generation": generation,
                        "frame_index": index,
                        "t_ms": int(t_ms),
                        "payload": "shared_memory",
                        "shm_key": shm_key,
                    }
                )
            self.events.append({"ok": True, "event": "range_done", "generation": generation})
            return {"ok": True, "event": "range_started"}

        def read_event(self):
            return self.events.pop(0)

        def send_cancel_generation(self, generation):
            return None

        def close(self):
            return None

    monkeypatch.setattr(pa, "NativeRendererProcess", FakeNativeRendererProcess)
    monkeypatch.setattr(pa, "SharedFrameRingReader", FakeRingReader)
    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_LOOKAHEAD_FRAMES", "2")

    renderer = pa.NativeAsyncSubtitleRenderer(320, 180)
    try:
        renderer._render_native(
            TimingTrack(),
            Style(),
            width=320,
            height=180,
            dpr=1.0,
            t_ms=1_000,
            generation=renderer._generation,
            needs_configure=True,
            restart_renderer=False,
            playing=True,
            skip_current=False,
        )

        assert len(FakeRingReader.created) == 1
    finally:
        renderer.stop()


def test_preview_graphics_ignores_stale_async_frame(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: False)
    graphics = PreviewGraphicsView()
    try:
        graphics._subtitle_item.set_async_mode(True)
        graphics.set_time(2_000)
        stale = QImage(16, 9, QImage.Format.Format_ARGB32_Premultiplied)
        stale.fill(QColor("#FF0000"))

        graphics._on_async_frame(stale, 1_000)

        assert graphics._subtitle_item._async_image is None
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_ignores_late_async_frame_while_playing(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: False)
    graphics = PreviewGraphicsView()
    try:
        graphics._subtitle_item.set_async_mode(True)
        graphics.set_playing(True)
        graphics.set_time(2_000)
        old = QImage(16, 9, QImage.Format.Format_ARGB32_Premultiplied)
        old.fill(QColor("#0000FF"))

        graphics._on_async_frame(old, 1_000)

        assert graphics._subtitle_item._async_image is None
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_accepts_near_late_async_frame_while_playing(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: False)
    graphics = PreviewGraphicsView()
    try:
        graphics._subtitle_item.set_async_mode(True)
        graphics.set_playing(True)
        graphics.set_time(2_000)
        near_late = QImage(16, 9, QImage.Format.Format_ARGB32_Premultiplied)
        near_late.fill(QColor("#0000FF"))

        graphics._on_async_frame(near_late, 1_950)

        assert graphics._subtitle_item._async_image is not None
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_clears_async_frame_on_style_change(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView
    from krok_helper.subtitle_render.models import Style

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: False)
    graphics = PreviewGraphicsView()
    try:
        graphics._subtitle_item.set_async_mode(True)
        image = QImage(16, 9, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#00FF00"))
        graphics._on_async_frame(image, graphics.current_time_ms)
        assert graphics._subtitle_item._async_image is not None

        graphics.set_style(Style(font_size_px=72))

        assert graphics._subtitle_item._async_image is None
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 基础：set_time / timecode
# ---------------------------------------------------------------------------


def test_set_time_updates_slider_and_timecode(qapp):
    bar = _bar(qapp)
    bar.set_time(12_345)
    assert bar.current_time_ms == 12_345
    # 时间码 MM:SS.CC（厘秒精度，截断到 10ms）
    assert bar._timecode.text() == "00:12.34"


def test_set_time_clamps_to_range(qapp):
    bar = _bar(qapp)
    bar.set_duration(5_000)
    bar.set_time(99_999)
    assert bar.current_time_ms == 5_000
    bar.set_time(-100)
    assert bar.current_time_ms == 0


def test_set_time_emits_time_changed(qapp):
    bar = _bar(qapp)
    received: list[int] = []
    bar.timeChanged.connect(received.append)
    bar.set_time(2_000)
    bar.set_time(3_500)
    assert received == [2_000, 3_500]


# ---------------------------------------------------------------------------
# 无音频：QTimer tick 路径
# ---------------------------------------------------------------------------


def test_play_without_audio_starts_tick_timer(qapp):
    bar = _bar(qapp)
    assert not bar.is_playing()
    bar.play()
    assert bar.is_playing()
    assert bar._tick_timer.isActive()
    bar.pause()
    assert not bar.is_playing()
    assert not bar._tick_timer.isActive()


def test_playback_timers_use_precise_timer(qapp):
    bar = _bar(qapp)
    assert bar._tick_timer.timerType() == Qt.TimerType.PreciseTimer
    assert bar._position_poll_timer.timerType() == Qt.TimerType.PreciseTimer
    # 60Hz 对齐 vsync——见 preview_view._TICK_INTERVAL_MS 注释
    assert bar._tick_timer.interval() == 16
    assert bar._position_poll_timer.interval() == 16

    bar.set_preview_fps(120)
    assert bar._tick_timer.interval() == 8
    assert bar._position_poll_timer.interval() == 8


def test_toggle_play_alternates(qapp):
    bar = _bar(qapp)
    bar.toggle_play()
    assert bar.is_playing()
    bar.toggle_play()
    assert not bar.is_playing()


def test_stop_resets_visual_playback_to_start(qapp):
    bar = _bar(qapp)
    bar.set_time(3_000)
    bar.play()

    bar.stop()

    assert not bar.is_playing()
    assert bar.current_time_ms == 0
    assert bar._play_btn.accessibleName() == "播放"


def test_seek_relative_clamps_to_timeline(qapp):
    bar = _bar(qapp)
    bar.set_duration(10_000)
    bar.set_time(3_000)

    bar.seek_relative(-5_000)
    assert bar.current_time_ms == 0

    bar.seek_relative(15_000)
    assert bar.current_time_ms == 10_000


def test_play_button_icon_reflects_state(qapp):
    bar = _bar(qapp)
    play_icon = bar._play_btn.icon()
    assert bar._play_btn.accessibleName() == "播放"
    assert not play_icon.isNull()
    bar.play()
    pause_icon = bar._play_btn.icon()
    assert bar._play_btn.accessibleName() == "暂停"
    assert not pause_icon.isNull()
    assert pause_icon.cacheKey() != play_icon.cacheKey()
    bar.pause()
    assert bar._play_btn.accessibleName() == "播放"


def test_play_button_uses_app_owned_svg_icons(qapp):
    bar = _bar(qapp)
    assert (pv._TRANSPORT_ICON_DIR / "play.svg").is_file()
    assert (pv._TRANSPORT_ICON_DIR / "pause.svg").is_file()
    assert not bar._play_btn.icon().isNull()


def test_play_button_has_no_opaque_background(qapp):
    bar = _bar(qapp)
    button = bar._play_btn
    base = QColor("#376A42")
    image = QImage(button.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(base)

    painter = QPainter(image)
    button.render(painter)
    painter.end()

    assert image.pixelColor(0, 0) == base
    assert image.pixelColor(image.width() - 1, image.height() - 1) == base
    assert any(
        image.pixelColor(x, y).red() >= 240
        and image.pixelColor(x, y).green() >= 240
        and image.pixelColor(x, y).blue() >= 240
        for y in range(image.height())
        for x in range(image.width())
    )


def test_play_button_hover_feedback_is_circular(qapp):
    bar = _bar(qapp)
    button = bar._play_btn
    button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
    button.ensurePolished()
    base = QColor("#376A42")
    image = QImage(button.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(base)

    painter = QPainter(image)
    button.render(painter)
    painter.end()

    assert image.pixelColor(0, 0) == base
    assert image.pixelColor(image.width() - 1, 0) == base
    assert image.pixelColor(3, image.height() // 2) != base


def test_volume_slider_controls_legacy_audio_output(qapp):
    bar = _bar(qapp)
    assert isinstance(bar._volume_slider, pv.PlayerProgressSlider)
    bar._ensure_audio_player()

    bar.set_volume(35)

    assert bar._volume_slider.value() == 35
    assert bar._audio_out is not None
    assert bar._audio_out.volume() == pytest.approx(0.35)
    assert bar._volume_slider.toolTip() == "预览音量：35%"


def test_volume_slider_controls_shared_playback_controller(qapp):
    bar = _bar(qapp)
    volumes: list[float] = []

    class Controller:
        def set_volume(self, volume: float) -> None:
            volumes.append(volume)

        def has_media(self) -> bool:
            return False

    bar.attach_playback_controller(Controller())
    bar.set_volume(62)

    assert volumes == [1.0, 0.62]


def test_set_volume_clamps_to_slider_range(qapp):
    bar = _bar(qapp)

    bar.set_volume(150)
    assert bar._volume_slider.value() == 100
    bar.set_volume(-5)
    assert bar._volume_slider.value() == 0


def test_preview_fps_label_updates_from_painted_frames(qapp, monkeypatch):
    """note_preview_frame_painted 只累加新字幕帧计数；读数由 _refresh_fps_label 按周期统计。"""
    bar = _bar(qapp)
    bar.note_preview_frame_painted()
    bar.note_preview_frame_painted()
    assert bar._fps_window_frames == 2  # 仅计数，不直接刷新读数

    monkeypatch.setattr(bar, "is_playing", lambda: True)
    monkeypatch.setattr(bar._fps_timer, "elapsed", lambda: 1000)
    bar._refresh_fps_label()
    assert bar._fps_label.text() == "FPS 02"  # 2 新帧 / 1s


def test_tick_advances_slider(qapp, monkeypatch):
    bar = _bar(qapp)
    bar.set_time(1_000)
    bar.play()
    # 直接模拟 elapsed 200ms：把 QElapsedTimer.elapsed monkeypatch 掉
    monkeypatch.setattr(bar._tick_anchor_real, "elapsed", lambda: 200)
    bar._on_tick()
    assert bar.current_time_ms == 1_200
    bar.pause()


def test_tick_stops_at_max_duration(qapp, monkeypatch):
    bar = _bar(qapp)
    bar.set_duration(2_000)
    bar.set_time(1_900)
    bar.play()
    monkeypatch.setattr(bar._tick_anchor_real, "elapsed", lambda: 500)
    bar._on_tick()
    assert bar.current_time_ms == 2_000
    assert not bar.is_playing()


def test_preview_canvas_caches_scaled_video_frame(qapp):
    canvas = PreviewCanvas()
    canvas._video_image = QImage(64, 36, QImage.Format.Format_ARGB32_Premultiplied)
    canvas._video_image.fill(QColor("#223344"))
    canvas._scaled_background_video(320, 180, 1.0)
    cached = canvas._scaled_video_image
    cache_key = canvas._scaled_video_key

    canvas._scaled_background_video(320, 180, 1.0)

    assert cached is not None
    assert canvas._scaled_video_image is cached
    assert canvas._scaled_video_key == cache_key


def test_preview_canvas_fits_output_rect_to_widget(qapp):
    canvas = PreviewCanvas()
    canvas.set_output_size(1920, 1080)

    assert canvas._fit_output_rect(960, 540) == (0, 0, 960, 540)
    assert canvas._fit_output_rect(1000, 500) == (55, 0, 889, 500)


def test_preview_canvas_video_source_uses_qt_playback_proxy(qapp, monkeypatch, tmp_path):
    canvas = PreviewCanvas()
    source = tmp_path / "source.mp4"
    proxy = tmp_path / "proxy.mp4"
    source.write_bytes(b"placeholder")
    proxy.write_bytes(b"proxy")
    monkeypatch.setattr(pv, "qt_playback_source", lambda path: proxy)
    seen = {}

    class FakePlayer:
        def pause(self):
            seen["paused"] = True

        def setSource(self, url):
            seen["source"] = url.toLocalFile()

        def setPosition(self, ms):
            seen["position"] = ms

        def play(self):
            seen["played"] = True

    canvas._video_player = FakePlayer()

    canvas.set_video_source(source)

    assert canvas.has_video_source
    assert Path(seen["source"]) == proxy
    assert seen["position"] == 0


# ---------------------------------------------------------------------------
# 音频路径
# ---------------------------------------------------------------------------


def test_set_audio_source_activates_player_path(qapp, tmp_path):
    bar = _bar(qapp)
    assert not bar._has_audio

    # 用非空 .wav 路径触发 setSource（不实际播放，避免依赖音频后端解码）
    fake = tmp_path / "song.wav"
    fake.write_bytes(b"placeholder")
    bar.set_audio_source(fake)
    assert bar._has_audio


def test_set_audio_source_uses_qt_playback_proxy(qapp, monkeypatch, tmp_path):
    bar = _bar(qapp)
    source = tmp_path / "song.mp4"
    proxy = tmp_path / "proxy.mp4"
    source.write_bytes(b"placeholder")
    proxy.write_bytes(b"proxy")
    monkeypatch.setattr(pv, "qt_playback_source", lambda path: proxy)
    seen = {}

    class FakePlayer:
        def setSource(self, url):
            seen["source"] = url.toLocalFile()

        def setPosition(self, ms):
            seen["position"] = ms

    bar._player = FakePlayer()

    bar.set_audio_source(source)

    assert bar._has_audio
    assert Path(seen["source"]) == proxy
    assert seen["position"] == 0


def test_set_audio_source_none_clears_player(qapp, tmp_path):
    bar = _bar(qapp)
    fake = tmp_path / "song.wav"
    fake.write_bytes(b"placeholder")
    bar.set_audio_source(fake)
    assert bar._has_audio
    bar.set_audio_source(None)
    assert not bar._has_audio


def test_audio_playback_clock_uses_elapsed_timer(qapp, monkeypatch, tmp_path):
    """有音频播放时 UI 时间由 60fps elapsed clock 插值；音频位置一致时不跳到粗粒度 position。

    （音频锚定默认开，但位置落在 deadband 内 → 不纠偏 → 仍按 elapsed 平滑推进。）
    """
    bar = _bar(qapp)
    fake = tmp_path / "song.wav"
    fake.write_bytes(b"placeholder")
    bar.set_audio_source(fake)
    assert bar._player is not None

    bar.set_time(1_000)
    bar.play()
    monkeypatch.setattr(bar._tick_anchor_real, "elapsed", lambda: 240)
    # 音频位置与墙钟外推(1240)一致(deadband 内) → 不纠偏 → 按 elapsed 插值，不跳到粗粒度 position
    bar._player.position = lambda: 1_240  # type: ignore[assignment]

    bar._on_audio_clock_tick()

    assert bar.current_time_ms == 1_240
    bar.pause()


def test_audio_clock_resyncs_to_audio_on_large_drift(qapp, monkeypatch, tmp_path):
    """墙钟外推与音频位置大幅偏离（如卡顿后）→ 吸附到音频真实位置（默认开）。"""
    bar = _bar(qapp)
    fake = tmp_path / "song.wav"
    fake.write_bytes(b"placeholder")
    bar.set_audio_source(fake)
    bar.set_time(1_000)
    bar.play()
    monkeypatch.setattr(bar._tick_anchor_real, "elapsed", lambda: 240)  # 墙钟外推 → 1240
    bar._player.position = lambda: 500  # type: ignore[assignment]  # 音频实际只到 500（落后 740ms）

    bar._on_audio_clock_tick()

    assert bar.current_time_ms == 500  # 吸附到音频真实位置
    bar.pause()


def test_audio_clock_disabled_falls_back_to_wall_clock(qapp, monkeypatch, tmp_path):
    """KROK_SUBTITLE_AUDIO_CLOCK=0 → 纯墙钟外推，完全忽略 player.position（回退旧行为）。"""
    monkeypatch.setenv("KROK_SUBTITLE_AUDIO_CLOCK", "0")
    bar = _bar(qapp)
    fake = tmp_path / "song.wav"
    fake.write_bytes(b"placeholder")
    bar.set_audio_source(fake)
    bar.set_time(1_000)
    bar.play()
    monkeypatch.setattr(bar._tick_anchor_real, "elapsed", lambda: 240)
    bar._player.position = lambda: 100  # type: ignore[assignment]

    bar._on_audio_clock_tick()

    assert bar.current_time_ms == 1_240  # 墙钟外推，忽略 position
    bar.pause()


def test_player_position_ignored_while_audio_clock_running(qapp, tmp_path):
    bar = _bar(qapp)
    fake = tmp_path / "song.wav"
    fake.write_bytes(b"placeholder")
    bar.set_audio_source(fake)
    bar.set_time(1_000)
    bar.play()

    bar._on_player_position(5_000)

    assert bar.current_time_ms == 1_000
    bar.pause()


# ---------------------------------------------------------------------------
# 反馈环抑制
# ---------------------------------------------------------------------------


def test_player_position_callback_does_not_re_seek_player(qapp, tmp_path):
    """模拟 QMediaPlayer.positionChanged 触发 → 滑块更新 → 不应回写 player.setPosition。"""
    bar = _bar(qapp)
    fake = tmp_path / "song.wav"
    fake.write_bytes(b"placeholder")
    bar.set_audio_source(fake)

    calls: list[int] = []
    assert bar._player is not None
    bar._player.setPosition = lambda ms, _calls=calls: _calls.append(ms)  # type: ignore[assignment]

    bar._on_player_position(5_000)
    # 滑块应推进
    assert bar.current_time_ms == 5_000
    # 但 player.setPosition 不应被反向调用（_suppress_seek 起作用）
    assert calls == []


# ---------------------------------------------------------------------------
# 单播放器统一（步骤2）：attach_playback_controller 后传输委托给共享 controller
# ---------------------------------------------------------------------------
class _FakeController:
    """记录调用的轻量 PlaybackController 替身（不创建真实 QMediaPlayer）。"""

    def __init__(self) -> None:
        self._has = True
        self._playing = False
        self._pos = 0
        self.seeks: list[int] = []
        self.stops = 0

    def has_media(self) -> bool:
        return self._has

    def set_media(self, path) -> None:
        self._has = path is not None

    def play(self) -> None:
        self._playing = True

    def pause(self) -> None:
        self._playing = False

    def stop(self) -> None:
        self._playing = False
        self._pos = 0
        self.stops += 1

    def is_playing(self) -> bool:
        return self._playing

    def seek(self, ms: int) -> None:
        self._pos = int(ms)
        self.seeks.append(int(ms))

    def position(self) -> int:
        return self._pos


def test_transport_play_pause_delegate_to_controller(qapp):
    bar = _bar(qapp)
    ctrl = _FakeController()
    bar.attach_playback_controller(ctrl)
    bar.set_time(1_000)

    bar.play()
    assert ctrl.is_playing() is True
    assert bar.is_playing() is True
    assert ctrl.position() == 1_000  # play 把 controller seek 到锚点
    assert bar._player is None  # 不再自建音频 player

    bar.pause()
    assert ctrl.is_playing() is False
    assert bar.is_playing() is False


def test_transport_slider_seek_delegates_to_controller(qapp):
    bar = _bar(qapp)
    ctrl = _FakeController()
    bar.attach_playback_controller(ctrl)

    bar.set_time(3_000)  # → _on_slider_changed → controller.seek

    assert 3_000 in ctrl.seeks


def test_transport_stop_delegates_to_controller(qapp):
    bar = _bar(qapp)
    ctrl = _FakeController()
    bar.attach_playback_controller(ctrl)
    bar.set_time(3_000)
    bar.play()

    bar.stop()

    assert ctrl.stops == 1
    assert ctrl.is_playing() is False
    assert bar.current_time_ms == 0


def test_fps_readout_is_subtitle_render_rate(qapp, monkeypatch):
    """FPS 读数 = 字幕新帧/秒，按固定周期统计；暂停显示 --，播放时按计数算。"""
    bar = _bar(qapp)
    # 未播放 → FPS --
    monkeypatch.setattr(bar, "is_playing", lambda: False)
    bar.note_preview_frame_painted()
    bar._refresh_fps_label()
    assert bar._fps_label.text() == "FPS --"
    assert bar._fps_window_frames == 0  # 刷新后清零

    # 播放中：30 新帧 / 0.5s = 60fps
    monkeypatch.setattr(bar, "is_playing", lambda: True)
    for _ in range(30):
        bar.note_preview_frame_painted()
    monkeypatch.setattr(bar._fps_timer, "elapsed", lambda: 500)
    bar._refresh_fps_label()
    assert bar._fps_label.text() == "FPS 60"
    assert bar._fps_window_frames == 0

    # 播放中但本周期无新帧 → FPS --（不残留上次读数式的误导）
    monkeypatch.setattr(bar._fps_timer, "elapsed", lambda: 500)
    bar._refresh_fps_label()
    assert bar._fps_label.text() == "FPS --"


def test_audio_clock_uses_controller_position(qapp, monkeypatch):
    """attach controller 后，时钟锚定读 controller.position()（一致时按 elapsed 插值）。"""
    bar = _bar(qapp)
    ctrl = _FakeController()
    bar.attach_playback_controller(ctrl)
    bar.set_time(1_000)
    bar.play()
    monkeypatch.setattr(bar._tick_anchor_real, "elapsed", lambda: 240)
    ctrl._pos = 1_240  # 与墙钟外推一致（deadband 内）→ 不纠偏

    bar._on_audio_clock_tick()

    assert bar.current_time_ms == 1_240
    bar.pause()


def test_audio_clock_anchor_correction_deadband_resync_and_gain():
    """音频锚定时钟的纯纠偏逻辑（无 Qt 对象）。"""
    # 正常抖动（≤ deadband）→ 不纠
    assert pv._audio_clock_anchor_correction(1_000, 1_000 + pv._AUDIO_CLOCK_DEADBAND_MS) == 0
    assert pv._audio_clock_anchor_correction(1_000, 1_000 - pv._AUDIO_CLOCK_DEADBAND_MS) == 0
    # 大偏差（> resync，如卡顿/seek 后）→ 整段吸附到音频位置
    assert pv._audio_clock_anchor_correction(5_000, 5_000 + pv._AUDIO_CLOCK_RESYNC_MS + 100) == \
        pv._AUDIO_CLOCK_RESYNC_MS + 100
    # 「字幕跑在音频前」= target 比音频快 → drift<0 → 轻微回拉（按 gain 比例的负值）
    corr = pv._audio_clock_anchor_correction(2_000, 1_900)  # drift = -100, 在 deadband 与 resync 之间
    assert corr == int(-100 * pv._AUDIO_CLOCK_GAIN)
    assert corr < 0  # 向音频回拉，消除「字幕更快」
    # 收敛是单调缩小偏差：施加校正后，新 target 更接近音频
    assert abs((2_000 + corr) - 1_900) < abs(2_000 - 1_900)


# ------------------------------------------------------------------ background scaling preview

def test_preview_graphics_video_background_is_contain_with_black_bars(qapp):
    """视频背景 contain：等比完整放入 + 纯黑 letterbox 底（对齐导出 pad black）。"""
    from krok_helper.subtitle_render.frontend.preview_graphics import (
        PreviewGraphicsView,
    )
    from krok_helper.subtitle_render.models import BackgroundSource

    graphics = PreviewGraphicsView()
    try:
        graphics.set_background_source(
            BackgroundSource(kind="video", path=r"C:\fake\bg.mp4")
        )
        assert (
            graphics._video_item.aspectRatioMode()
            == Qt.AspectRatioMode.KeepAspectRatio
        )
        assert graphics._letterbox_rect.isVisible()
        rect = graphics._letterbox_rect.rect()
        assert (int(rect.width()), int(rect.height())) == (1920, 1080)
        # 背景底矩形永远可见：solid 填背景色本身，其余填纯黑。纯色不能靠
        # 场景底色显示——实测 view 的 QSS background 会整体盖住 scene
        # backgroundBrush（曾表现为「纯色预览永远是黑的」）。
        graphics.set_background_source(
            BackgroundSource(kind="solid", color="#123456")
        )
        assert graphics._letterbox_rect.isVisible()
        assert graphics._letterbox_rect.brush().color().name().upper() == "#123456"
    finally:
        graphics.deleteLater()


def test_preview_graphics_solid_background_renders_color(qapp):
    """纯色背景的像素级验证：渲染结果必须是背景色，而不是舞台底色。"""
    from krok_helper.subtitle_render.frontend.preview_view import PreviewPanel
    from krok_helper.subtitle_render.models import BackgroundSource

    panel = PreviewPanel()
    try:
        panel.resize(640, 400)
        panel.set_populated(True)
        panel.show()
        for color in ("#FF3050", "#30FF70"):
            panel.set_background_source(
                BackgroundSource(kind="solid", color=color)
            )
            qapp.processEvents()
            image = panel._canvas.grab().toImage()
            pixel = image.pixel(20, 20) & 0xFFFFFF
            assert f"#{pixel:06X}" == color
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_preview_graphics_image_fit_cover_and_contain(qapp, tmp_path):
    """图片背景按 image_fit 选择铺满（裁切）或黑边（完整放入）。"""
    from PyQt6.QtGui import QPixmap
    from krok_helper.subtitle_render.frontend.preview_graphics import (
        PreviewGraphicsView,
    )
    from krok_helper.subtitle_render.models import BackgroundSource

    image_path = tmp_path / "bg_4x3.png"
    pixmap = QPixmap(800, 600)
    pixmap.fill(QColor("#305070"))
    assert pixmap.save(str(image_path))

    graphics = PreviewGraphicsView()
    try:
        cover_source = BackgroundSource(
            kind="image", path=str(image_path), image_fit="cover"
        )
        graphics.set_background_source(cover_source)
        cover = graphics._image_item.pixmap()
        # 4:3 图片铺满 16:9 输出：等比放大到 1920x1440（上下被裁）
        assert (cover.width(), cover.height()) == (1920, 1440)

        contain_source = BackgroundSource(
            kind="image", path=str(image_path), image_fit="contain"
        )
        graphics.set_background_source(contain_source)
        contain = graphics._image_item.pixmap()
        # 完整放入：等比缩小到 1440x1080，左右黑边
        assert (contain.width(), contain.height()) == (1440, 1080)
        assert graphics._letterbox_rect.isVisible()
    finally:
        graphics.deleteLater()
