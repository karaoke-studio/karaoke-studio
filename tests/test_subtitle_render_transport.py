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

from krok_helper.subtitle_render.frontend.preview import preview_view as pv  # noqa: E402
from krok_helper.subtitle_render.frontend.preview.preview_view import (  # noqa: E402
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
    from krok_helper.subtitle_render.native.backend import NativeRendererProcessOwner

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
    from krok_helper.subtitle_render.native.backend import NativeRendererProcessOwner

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
    from krok_helper.subtitle_render.native.backend import NativeRendererProcessOwner

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
    from krok_helper.subtitle_render.native.backend import NativeRendererProcessOwner

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

    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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
    from krok_helper.subtitle_render.frontend.preview.preview_async import preview_render_target_size

    assert preview_render_target_size(1920, 1080, 1.25) == (2400, 1350, 1.25)
    assert preview_render_target_size(0, 0, 0) == (1, 1, 1.0)
    assert preview_render_target_size(1, 1, -1.0) == (1, 1, 0.01)


def test_preview_quality_caps_lower_tiers_and_preserves_full_quality():
    from krok_helper.subtitle_render.frontend.preview.preview_async import (
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
    from krok_helper.subtitle_render.frontend.preview.preview_async import native_preview_timestamps

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

    from krok_helper.subtitle_render.frontend.preview.preview_async import (
        GpuAsyncSubtitleRenderer,
    )
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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
    from krok_helper.subtitle_render.frontend.preview.preview_async import (
        GpuAsyncSubtitleRenderer,
    )
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    monkeypatch.setenv("KROK_SUBTITLE_GPU_FORCE_WARP", "1")
    renderer = GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_state(TimingTrack(), Style())
        assert renderer._force_warp is True
        assert renderer.stats_snapshot()["warp_selected"] == 1
    finally:
        renderer.stop()


def test_gpu_preview_cpu_fallback_failure_does_not_kill_worker(qapp, monkeypatch):
    """回退帧渲染抛出非预期异常时只计数，worker 线程必须存活。

    旧行为：异常逃逸 ``_run`` → 线程死亡 → GPU 与 CPU 预览从此永久停摆。
    """
    import time

    import krok_helper.subtitle_render.frontend.preview.preview_async as preview_async
    from krok_helper.subtitle_render.domain.models import Style
    from krok_helper.subtitle_render.domain.timing import (
        TimingChar,
        TimingLine,
        TimingTrack,
    )
    from krok_helper.subtitle_render.native.backend import NativeRendererError

    class BrokenSidecar:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise NativeRendererError("sidecar unavailable in test")

        def close(self):
            return None

    monkeypatch.setattr(preview_async, "NativeRendererProcess", BrokenSidecar)

    def exploding_paint(*_args, **_kwargs):
        raise ValueError("painter exploded")

    monkeypatch.setattr(preview_async, "paint_frame_to_painter", exploding_paint)

    renderer = preview_async.GpuAsyncSubtitleRenderer(320, 180)
    try:
        track = TimingTrack(
            lines=[TimingLine(chars=[TimingChar("歌", 0)], end_ms=1_000)]
        )
        renderer.set_state(track, Style())
        renderer.request(100)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if renderer.stats_snapshot().get("fallback_failures", 0) > 0:
                break
            time.sleep(0.01)
        assert renderer.stats_snapshot()["fallback_failures"] > 0
        assert renderer._thread.is_alive()
    finally:
        renderer.stop()


def test_native_preview_frame_cache_detaches_and_evicts_oldest():
    from krok_helper.subtitle_render.frontend.preview.preview_async import NativePreviewFrameCache

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
    from krok_helper.subtitle_render.frontend.preview.preview_async import NativePreviewFrameCache

    cache = NativePreviewFrameCache(max_frames=2, fps=60)
    image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#223344"))

    cache.store(1_017, image)

    cached = cache.take(1_016)
    assert cached is not None
    assert cached.pixelColor(0, 0) == QColor("#223344")
    assert cache.take(1_017) is None


def test_native_preview_stats_snapshot_tracks_core_counters():
    from krok_helper.subtitle_render.frontend.preview.preview_async import NativePreviewStats

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
    from krok_helper.subtitle_render.frontend.preview.preview_async import async_preview_enabled

    monkeypatch.delenv("KROK_SUBTITLE_ASYNC_PREVIEW", raising=False)
    assert async_preview_enabled() is True

    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("KROK_SUBTITLE_ASYNC_PREVIEW", value)
        assert async_preview_enabled() is True

    for value in ("0", "false", "no", "off"):
        monkeypatch.setenv("KROK_SUBTITLE_ASYNC_PREVIEW", value)
        assert async_preview_enabled() is False


def test_native_preview_defaults_off_and_env_can_opt_in(monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa

    monkeypatch.delenv("KROK_SUBTITLE_NATIVE_RENDER", raising=False)
    assert pa.native_preview_enabled() is False

    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_RENDER", "1")
    assert pa.native_preview_enabled() is True

    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_RENDER", "0")
    assert pa.native_preview_enabled() is False

    monkeypatch.delenv("KROK_SUBTITLE_NATIVE_RENDER", raising=False)
    assert pa.native_preview_enabled() is False


def test_gpu_preview_defaults_to_g5_on_interactive_windows(monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa

    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.delenv("KROK_SUBTITLE_GPU_PREVIEW", raising=False)
    assert pa.gpu_preview_enabled() is (os.name == "nt")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    assert pa.gpu_preview_enabled() is False

    monkeypatch.setenv("KROK_SUBTITLE_GPU_PREVIEW", "1")
    assert pa.gpu_preview_enabled() is True

    monkeypatch.setenv("KROK_SUBTITLE_GPU_PREVIEW", "0")
    assert pa.gpu_preview_enabled() is False


def test_preview_render_target_size_clamps_to_engine_limit():
    """渲染目标单边不得超过 sidecar 的 8192 上限（按比例降 dpr）。

    4K 工程 × 高 DPR 大窗口曾越限报错，触发「杀进程重启 + 全量 configure
    2.5s/次 × 5 连败 ≈ 12 秒卡顿 + 降级弹窗」（2026-10 实测）。
    """
    from krok_helper.subtitle_render.frontend.preview.preview_async import (
        preview_render_target_size,
    )

    w, h, dpr = preview_render_target_size(3840, 2160, 3.0)
    assert max(w, h) <= 8192
    assert (w, h) == (8192, 4608)
    assert abs(dpr - 8192 / 3840) < 1e-6

    # 常规尺寸不受影响
    w2, h2, dpr2 = preview_render_target_size(1920, 1080, 2.0)
    assert (w2, h2, dpr2) == (3840, 2160, 2.0)


def test_gpu_native_preview_env_gate(monkeypatch):
    """G6 直画上屏：env 存在即权威（"1"开/"0"关），缺省才读磁盘偏好。"""
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa

    monkeypatch.delenv("KROK_SUBTITLE_GPU_NATIVE_PREVIEW", raising=False)
    # 偏好也关（默认）→ 关
    assert pa.gpu_native_preview_enabled() is False

    monkeypatch.setenv("KROK_SUBTITLE_GPU_NATIVE_PREVIEW", "1")
    assert pa.gpu_native_preview_enabled() is True

    monkeypatch.setenv("KROK_SUBTITLE_GPU_NATIVE_PREVIEW", "0")
    assert pa.gpu_native_preview_enabled() is False

    # 回归（2026-10 实测 G6 关不掉）：开关关闭时 env 写 "0"，而磁盘偏好
    # 是防抖落盘、此刻仍旧值 True——env 必须压过磁盘，否则渲染器重建后
    # 仍按旧偏好进入 G6，实时关闭失效。
    monkeypatch.setattr(pa, "_gpu_direct_present_preference", lambda: True)
    monkeypatch.setenv("KROK_SUBTITLE_GPU_NATIVE_PREVIEW", "0")
    assert pa.gpu_native_preview_enabled() is False
    monkeypatch.delenv("KROK_SUBTITLE_GPU_NATIVE_PREVIEW", raising=False)
    assert pa.gpu_native_preview_enabled() is True  # env 缺省时磁盘说了算


def test_gpu_renderer_enters_g6_with_env_opt_in(qapp, monkeypatch):
    """env=1 时渲染器进入 G6 模式（uses_native_preview=True）。"""
    from krok_helper.subtitle_render.frontend.preview.preview_async import (
        GpuAsyncSubtitleRenderer,
    )

    monkeypatch.setenv("KROK_SUBTITLE_GPU_NATIVE_PREVIEW", "1")
    renderer = GpuAsyncSubtitleRenderer(320, 180)
    try:
        assert renderer.uses_native_preview is True
    finally:
        renderer.stop()


def test_async_preview_renderer_stops_qthread(qapp):
    from krok_helper.subtitle_render.frontend.preview.preview_async import AsyncSubtitleRenderer

    renderer = AsyncSubtitleRenderer(320, 180)
    assert renderer._thread.isRunning()

    renderer.stop()

    assert not renderer._thread.isRunning()


def test_preview_graphics_updates_async_render_target(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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

    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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

        def set_native_target(self, parent_hwnd, x, y, width, height, src_x=0, src_y=0):
            self.native_targets.append((parent_hwnd, x, y, width, height, src_x, src_y))

        def clear_native_target(self):
            self.native_targets.clear()

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
        parent_hwnd, win_x, win_y, win_w, win_h, src_x, src_y = (
            renderer.native_targets[-1]
        )
        # 子窗口挂在顶层窗口 HWND 上（不是视口的原生 HWND——视口原生化会
        # 把悬浮播放窗的标题栏/传输条压到视频下面）。
        assert parent_hwnd == int(graphics.window().winId())
        # G6 渲染目标不做质量钳制：纹理=屏幕物理尺寸。
        display_scale = graphics._display_device_scale()  # noqa: SLF001
        assert render_dpr == display_scale
        expected_w, expected_h, _ = pg.preview_render_target_size(
            logical_w, logical_h, render_dpr
        )
        # 窗口矩形 = 视口可见部分映射到顶层客户区的物理像素，绝不超过纹理。
        assert 1 <= win_w <= expected_w
        assert 1 <= win_h <= expected_h
        # 未裁剪时（场景映射矩形完全在视口内）源偏移为 0；一旦被裁剪，
        # 偏移与窗口尺寸之和必须恰好铺满纹理。
        assert 0 <= src_x and src_x + win_w <= expected_w
        assert 0 <= src_y and src_y + win_h <= expected_h
        if src_x == 0 and win_w == expected_w:
            assert win_x >= 0
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_g6_clips_child_window_to_viewport(qapp, monkeypatch):
    """视口比场景更宽时（expanding fit 上下溢出），子窗口必须裁剪到视口可见区。

    裁掉的部分用户本来看不见；若照搬整个映射矩形，字幕会画到视口上下
    相邻的 UI 上。裁剪后用 src_x/src_y 标记纹理内的拷贝起点。
    """
    from PyQt6.QtCore import QRectF

    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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

        def set_native_target(self, parent_hwnd, x, y, width, height, src_x=0, src_y=0):
            self.native_targets.append((parent_hwnd, x, y, width, height, src_x, src_y))

        def clear_native_target(self):
            self.native_targets.clear()

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
        # 视口宽高比远大于 16:9 的输出画布：expanding fit 会让场景在竖直
        # 方向溢出视口（mapped.top() < 0），子窗口必须只取可见部分。
        graphics.set_output_size(1920, 1080)
        graphics.resize(1200, 300)
        graphics.show()
        qapp.processEvents()
        graphics._fit_scene_to_view()
        graphics._refresh_async_target()  # noqa: SLF001

        renderer = FakeNativePreviewRenderer.instances[-1]
        parent_hwnd, win_x, win_y, win_w, win_h, src_x, src_y = (
            renderer.native_targets[-1]
        )
        logical_w, logical_h, render_dpr = renderer.render_targets[-1]
        physical_w, physical_h, _ = pg.preview_render_target_size(
            logical_w, logical_h, render_dpr
        )
        dpr = graphics.window().devicePixelRatioF() or 1.0
        viewport_rect = QRectF(graphics.viewport().rect())
        mapped = graphics.mapFromScene(graphics.scene().sceneRect()).boundingRect()
        # 前置：expanding fit 让场景在竖直方向两侧都溢出视口。
        assert mapped.top() < 0 < mapped.bottom() - viewport_rect.height()
        # 窗口矩形 = 视口可见区（两侧裁剪后只剩中间），不超出纹理。
        assert win_w == min(int(round(viewport_rect.width() * dpr)), physical_w)
        assert win_h == min(int(round(viewport_rect.height() * dpr)), physical_h)
        assert win_h < physical_h
        # 顶部被裁掉的部分通过源偏移补回（可见左上角在映射矩形内的偏移）。
        expected_src_y = round((viewport_rect.top() - mapped.top()) * dpr)
        assert abs(src_y - expected_src_y) <= 1
        assert src_x == 0  # 水平方向铺满，无偏移
        assert parent_hwnd == int(graphics.window().winId())
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_g6_clears_native_target_when_hidden(qapp, monkeypatch):
    """视图隐藏（切标签页/关播放窗）必须撤掉 DComp 子窗口，否则残留画面浮在别的 UI 上。"""
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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
            self.cleared = 0
            FakeNativePreviewRenderer.instances.append(self)

        def set_render_target(self, width, height, device_pixel_ratio=1.0):
            self.render_targets.append((width, height, device_pixel_ratio))

        def set_native_target(self, parent_hwnd, x, y, width, height, src_x=0, src_y=0):
            self.native_targets.append((parent_hwnd, x, y, width, height, src_x, src_y))

        def clear_native_target(self):
            self.cleared += 1

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
        assert renderer.native_targets  # 已建立子窗口目标

        graphics.hide()
        qapp.processEvents()
        assert renderer.cleared >= 1  # 隐藏即撤掉
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_g6_toggle_while_hidden_engages_on_show(qapp, monkeypatch):
    """实时切换的完整链路：开关在导出页触发时预览画布隐藏——G6 模式已选定
    但子窗口不建立（没有可见画布）；回到预览页（showEvent）后必须建立。"""
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import (
        PreviewGraphicsView,
    )

    class FakeSignal:
        def connect(self, *args, **kwargs):
            pass

    class FakeGpuRenderer:
        instances = []

        def __init__(self, width, height, parent=None):
            self.frame_ready = FakeSignal()
            self.frame_presented = FakeSignal()
            self.fallback_occurred = FakeSignal()
            self.uses_native_preview = True
            self.render_targets = []
            self.native_targets = []
            self.cleared = 0
            FakeGpuRenderer.instances.append(self)

        def set_render_target(self, width, height, device_pixel_ratio=1.0):
            self.render_targets.append((width, height, device_pixel_ratio))

        def set_native_target(self, parent_hwnd, x, y, width, height, src_x=0, src_y=0):
            self.native_targets.append((parent_hwnd, x, y, width, height, src_x, src_y))

        def clear_native_target(self):
            self.cleared += 1

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
    monkeypatch.setattr(pg, "GpuAsyncSubtitleRenderer", FakeGpuRenderer)
    monkeypatch.setenv("KROK_SUBTITLE_GPU_NATIVE_PREVIEW", "1")
    graphics = PreviewGraphicsView()
    try:
        # 画布隐藏时切换（等价于在导出页点开 GPU 直画开关）
        graphics.set_gpu_preview_enabled(True)
        renderer = FakeGpuRenderer.instances[-1]
        assert renderer.uses_native_preview is True
        assert renderer.native_targets == []  # 隐藏：不建立子窗口

        graphics.show()
        qapp.processEvents()
        # 回到预览页：showEvent → _refresh_async_target → 建立直画目标
        assert renderer.native_targets
        parent_hwnd = renderer.native_targets[-1][0]
        assert parent_hwnd == int(graphics.window().winId())
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_gpu_async_renderer_queue_is_capacity_one_latest_wins(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame(self, t_ms, **kwargs):
            rendered.append(int(t_ms))
            if len(rendered) == 1:
                first_started.set()
                unblock.wait(timeout=2.0)
            if int(t_ms) == 2_100:  # request(2_099) 吸附到帧键网格 2100
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

        # request(2_099) 已吸附到 60fps 帧键网格（2100）再下渲染请求。
        assert rendered == [1_000, 2_100]
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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame_direct(self, t_ms, **kwargs):
            presented_calls.append((int(t_ms), dict(kwargs)))
            finished.set()
            return {
                "ok": True,
                "event": "gpu_frame_rendered_direct",
                "t_ms": int(t_ms),
                "render_ms": 1.25,
            }

        def present_rendered_gpu_frame(self, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_presented",
                "t_ms": int(kwargs.get("t_ms", 0)),
                "render_ms": 0.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "child_hwnd": 4321,
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


def test_gpu_native_preview_closes_child_window_when_target_cleared(qapp, monkeypatch):
    """clear_native_target（视图隐藏）后 worker 必须撤掉 sidecar 里的 DComp 子窗口。

    子窗口挂在顶层窗口 HWND 上、不随视口隐藏；残留的话会一直浮在
    其他 UI 上（2026-10 G6 定位返工时引入的显式撤销路径）。
    """
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    presented_calls: list[int] = []
    close_calls: list[bool] = []
    first_present = threading.Event()
    closed = threading.Event()

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready", "native_preview_protocol": 1}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "native_preview": True}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame_direct(self, t_ms, **kwargs):
            presented_calls.append(int(t_ms))
            first_present.set()
            return {
                "ok": True,
                "event": "gpu_frame_rendered_direct",
                "t_ms": int(t_ms),
                "render_ms": 1.25,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "transport": "direct_composition",
            }

        def present_rendered_gpu_frame(self, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_presented",
                "t_ms": int(kwargs.get("t_ms", 0)),
                "render_ms": 0.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "child_hwnd": 4321,
                "transport": "direct_composition",
            }

        def render_gpu_frame(self, *args, **kwargs):
            raise AssertionError("G6 native preview must not use shared-memory readback")

        def close_gpu_preview(self, **kwargs):
            close_calls.append(bool(kwargs.get("force_warp", False)))
            closed.set()

        def close(self):
            pass

    monkeypatch.setattr(pa, "gpu_native_preview_enabled", lambda: True)
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_native_target(12345, 0, 0, 320, 180)
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        assert first_present.wait(timeout=2.0)

        renderer.clear_native_target()
        # target 清空后 pending 已丢弃；worker 醒来后只应关闭子窗口，
        # 不应再渲染/呈现任何帧。
        assert renderer._native_target is None  # noqa: SLF001
        assert closed.wait(timeout=2.0)
        assert close_calls
        presented_after_clear = len(presented_calls)
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert len(presented_calls) == presented_after_clear
    finally:
        renderer.stop()


def test_gpu_native_preview_skips_redundant_same_key_frames(qapp, monkeypatch):
    """G6 同键去重：暂停态重复请求同一帧键不重渲（无效帧），但 delivery 照常闭合。"""
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    presented: list[int] = []
    emitted: list[int] = []
    first_present = threading.Event()

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready", "native_preview_protocol": 1}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "native_preview": True}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame_direct(self, t_ms, **kwargs):
            presented.append(int(t_ms))
            if not presented[:-1]:
                first_present.set()
            return {
                "ok": True,
                "event": "gpu_frame_rendered_direct",
                "t_ms": int(t_ms),
                "render_ms": 5.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "transport": "direct_composition",
            }

        def present_rendered_gpu_frame(self, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_presented",
                "t_ms": int(kwargs.get("t_ms", 0)),
                "render_ms": 0.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "child_hwnd": 4321,
                "transport": "direct_composition",
            }

        def render_gpu_frame(self, *args, **kwargs):
            raise AssertionError("G6 native preview must not use shared-memory readback")

        def close(self):
            pass

    monkeypatch.setattr(pa, "gpu_native_preview_enabled", lambda: True)
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    renderer.frame_presented.connect(emitted.append)
    try:
        renderer.set_native_target(12345, 0, 0, 320, 180)
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        assert first_present.wait(timeout=2.0)
        qapp.processEvents()
        time.sleep(0.05)
        qapp.processEvents()
        renderer.request(1_000)  # 同一帧键：去重
        deadline = time.monotonic() + 2.0
        while len(emitted) < 2 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert len(presented) == 1  # GPU 只渲染了一次
        assert len(emitted) == 2  # 两次请求都闭合了 delivery
        assert renderer.stats_snapshot()["native_redundant_frames_skipped"] >= 1
    finally:
        renderer.stop()


def test_gpu_native_preview_projects_ahead_by_render_latency(qapp, monkeypatch):
    """播放态时延感知投喂：渲染耗时 EMA 生效后，直画目标戳前移（追帧）。"""
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    presented: list[tuple[int, int]] = []  # (requested, presented)
    made = threading.Event()

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready", "native_preview_protocol": 1}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "native_preview": True}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame_direct(self, t_ms, **kwargs):
            presented.append((int(kwargs.get("generation", 0)), int(t_ms)))
            made.set()
            return {
                "ok": True,
                "event": "gpu_frame_rendered_direct",
                "t_ms": int(t_ms),
                "render_ms": 40.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "transport": "direct_composition",
            }

        def present_rendered_gpu_frame(self, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_presented",
                "t_ms": int(kwargs.get("t_ms", 0)),
                "render_ms": 0.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "child_hwnd": 4321,
                "transport": "direct_composition",
            }

        def render_gpu_frame(self, *args, **kwargs):
            raise AssertionError("G6 native preview must not use shared-memory readback")

        def close(self):
            pass

    monkeypatch.setattr(pa, "gpu_native_preview_enabled", lambda: True)
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_native_target(12345, 0, 0, 320, 180)
        renderer.set_state(TimingTrack(), Style())
        renderer.set_playing(True)
        requests = [1_000, 2_000, 3_000, 4_000]
        for i, t in enumerate(requests):
            renderer.request(t)
            if i == 0:
                assert made.wait(timeout=2.0)
                made.clear()
            time.sleep(0.02)
            qapp.processEvents()
        deadline = time.monotonic() + 2.0
        while len(presented) < 3 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        # 首帧 EMA 未建立（0）→ 前移≈0；此后 EMA=40ms ≥2 个帧键 → 目标戳前移。
        # 断言末次请求（4000ms）的直画戳明显前移（>8ms，即至少越过半个帧键）。
        assert any(presented_t > 4_008 for _, presented_t in presented)
    finally:
        renderer.stop()


def test_gpu_native_preview_idle_pumps_while_paused(qapp, monkeypatch):
    """空闲心跳：暂停/无 present 时周期泵 sidecar 消息队列（投递鼠标转发消息）。"""
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    pumps: list[float] = []
    first_present = threading.Event()

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready", "native_preview_protocol": 1}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "native_preview": True}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame_direct(self, t_ms, **kwargs):
            first_present.set()
            return {
                "ok": True,
                "event": "gpu_frame_rendered_direct",
                "t_ms": int(t_ms),
                "render_ms": 5.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "transport": "direct_composition",
            }

        def present_rendered_gpu_frame(self, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_presented",
                "t_ms": int(kwargs.get("t_ms", 0)),
                "render_ms": 0.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "child_hwnd": 4321,
                "transport": "direct_composition",
            }

        def pump_native_preview(self, **kwargs):
            pumps.append(time.monotonic())

        def render_gpu_frame(self, *args, **kwargs):
            raise AssertionError("G6 native preview must not use shared-memory readback")

        def close(self):
            pass

    monkeypatch.setattr(pa, "gpu_native_preview_enabled", lambda: True)
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_native_target(12345, 0, 0, 320, 180)
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        assert first_present.wait(timeout=2.0)
        deadline = time.monotonic() + 1.5
        while len(pumps) < 3 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert len(pumps) >= 3  # ~30ms 间隔的空闲心跳
        assert renderer.stats_snapshot()["native_idle_pumps"] >= 3
    finally:
        renderer.stop()


def test_gpu_native_preview_schedules_at_capacity_rate_without_waste(qapp, monkeypatch):
    """按当前吞吐调度（2026-10 用户问询的实证）：模拟 45ms/帧的慢机，
    以 60Hz 请求节拍打 1 秒——渲染数应 ≈ 1s/45ms ≈ 22（不是请求数 60），
    且每次渲染的帧键互不相同（无效帧为零）。
    """
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    presented_keys: list[int] = []
    lock = threading.Lock()

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready", "native_preview_protocol": 1}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "native_preview": True}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame_direct(self, t_ms, **kwargs):
            time.sleep(0.045)  # 模拟慢机：一帧 45ms（≈22fps 吞吐）
            with lock:
                presented_keys.append(int(t_ms))
            return {
                "ok": True,
                "event": "gpu_frame_rendered_direct",
                "t_ms": int(t_ms),
                "render_ms": 45.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "transport": "direct_composition",
            }

        def present_rendered_gpu_frame(self, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_presented",
                "t_ms": int(kwargs.get("t_ms", 0)),
                "render_ms": 0.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "child_hwnd": 4321,
                "transport": "direct_composition",
            }

        def render_gpu_frame(self, *args, **kwargs):
            raise AssertionError("G6 native preview must not use shared-memory readback")

        def close(self):
            pass

    monkeypatch.setattr(pa, "gpu_native_preview_enabled", lambda: True)
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_native_target(12345, 0, 0, 320, 180)
        renderer.set_state(TimingTrack(), Style())
        renderer.set_playing(True)
        # 60Hz 请求节拍打 ~1.05 秒（模拟媒体时钟）
        for i in range(63):
            renderer.request(60_000 + i * 16)
            qapp.processEvents()
            time.sleep(0.0167)
        # 等最后一个在途渲染完成
        time.sleep(0.2)
        qapp.processEvents()

        keys = [renderer._frame_cache.key_for(t) for t in presented_keys]  # noqa: SLF001
        render_count = len(presented_keys)
        # 渲染数≈吞吐率（1.05s/45ms≈23），远小于请求数 63
        assert 16 <= render_count <= 30, f"渲染数 {render_count} 应≈吞吐率而非请求率"
        # 每次渲染的帧键互不相同：没有一帧浪费在已上屏内容上
        assert len(set(keys)) == len(keys), f"出现重复帧键: {keys}"
    finally:
        renderer.stop()


def test_gpu_native_preview_recovery_stays_monotonic(qapp, monkeypatch):
    """恢复瞬态（2026-10 用户问询 + 拍板语义）：

    - 吞吐骤升（166ms→瞬时）时呈现键严格递增（不倒走）；
    - **到点才播放**：渲染提前完成也不得提前上屏——呈现戳与当时媒体
      时钟之差不得超过一个小余量（≈2 帧，含测试时钟的量化误差）。
    """
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    presented: list[tuple[int, int, int]] = []  # (generation, t, media_at_present)
    clock = {"t": 60_000, "t0": 0.0}
    lock = threading.Lock()
    slow_remaining = {"n": 3}

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready", "native_preview_protocol": 1}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "native_preview": True}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame_direct(self, t_ms, **kwargs):
            if slow_remaining["n"] > 0:
                slow_remaining["n"] -= 1
                time.sleep(0.166)  # 慢机 6fps
                duration = 166.0
            else:
                duration = 1.0  # 骤快（缓存热了）
            return {
                "ok": True,
                "event": "gpu_frame_rendered_direct",
                "t_ms": int(t_ms),
                "render_ms": duration,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "transport": "direct_composition",
            }

        def present_rendered_gpu_frame(self, **kwargs):
            # 到点持有发生在 present 之前：媒体时钟必须在上屏时刻读取，
            # 在渲染时刻读会把持有量本身误判成提前量。
            with lock:
                presented.append(
                    (
                        int(kwargs.get("generation", 0)),
                        int(kwargs.get("t_ms", 0)),
                        int(60_000 + (time.monotonic() - clock["t0"]) * 1000.0),
                    )
                )
            return {
                "ok": True,
                "event": "gpu_frame_presented",
                "t_ms": int(kwargs.get("t_ms", 0)),
                "render_ms": 0.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "child_hwnd": 4321,
                "transport": "direct_composition",
            }

        def render_gpu_frame(self, *args, **kwargs):
            raise AssertionError("G6 native preview must not use shared-memory readback")

        def close(self):
            pass

    monkeypatch.setattr(pa, "gpu_native_preview_enabled", lambda: True)
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_native_target(12345, 0, 0, 320, 180)
        renderer.set_state(TimingTrack(), Style())
        renderer.set_playing(True)
        # 媒体时钟用墙钟驱动（生产中由 QElapsedTimer 平滑驱动，与墙钟
        # 1:1）——离散步进（每拍 sleep 开销 >16.7ms 只走 16ms）会比真实
        # 媒体慢，导致到点持有的模型被误判为提前上屏。
        clock["t0"] = time.monotonic()
        t0 = clock["t0"]
        while time.monotonic() - t0 < 1.2:
            with lock:
                clock["t"] = 60_000 + int((time.monotonic() - t0) * 1000.0)
            renderer.request(clock["t"])
            qapp.processEvents()
            time.sleep(0.016)
        time.sleep(0.25)
        qapp.processEvents()

        keys = [renderer._frame_cache.key_for(t) for _, t, _ in presented]  # noqa: SLF001
        assert len(keys) >= 10, f"应有足够样本（实际 {len(keys)}）"
        backward = [(a, b) for a, b in zip(keys, keys[1:]) if b <= a]
        assert not backward, f"恢复瞬态出现回退呈现: {backward}"
        # 到点才播放：呈现戳最多比当时媒体时钟早 ~2 帧（测试时钟 16.7ms
        # 量化 + 持有 4ms 粒度的余量）。
        early = [
            (t, media)
            for _, t, media in presented
            if t - media > 40
        ]
        assert not early, f"提前上屏未到点的帧: {early[:6]}"
    finally:
        renderer.stop()


def test_gpu_native_due_scheduler_fills_during_recovery(qapp, monkeypatch):
    """恢复期填缝（2026-10 用户模型的核心断言）：吞吐骤升后，渲染要跑在
    出队前面（队列积累、GPU 不空转），而不是渲一帧睡到到点。
    """
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    renders: list[tuple[float, int]] = []  # (wall, t)
    presents: list[tuple[float, int]] = []
    slow = {"n": 3}
    lock = threading.Lock()

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready", "native_preview_protocol": 1}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "native_preview": True}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame_direct(self, t_ms, **kwargs):
            if slow["n"] > 0:
                slow["n"] -= 1
                time.sleep(0.166)
                dur = 166.0
            else:
                dur = 1.0
            with lock:
                renders.append((time.monotonic(), int(t_ms)))
            return {"ok": True, "event": "gpu_frame_rendered_direct",
                    "t_ms": int(t_ms), "render_ms": dur}

        def present_rendered_gpu_frame(self, **kwargs):
            with lock:
                presents.append((time.monotonic(), int(kwargs.get("t_ms", 0))))
            return {"ok": True, "event": "gpu_frame_presented",
                    "t_ms": int(kwargs.get("t_ms", 0)), "render_ms": 0.0,
                    "present_ms": 0.2, "readback_ms": 0.0,
                    "child_hwnd": 1, "transport": "direct_composition"}

        def render_gpu_frame(self, *args, **kwargs):
            raise AssertionError("G6 native preview must not use shared-memory readback")

        def close(self):
            pass

    monkeypatch.setattr(pa, "gpu_native_preview_enabled", lambda: True)
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_native_target(12345, 0, 0, 320, 180)
        renderer.set_state(TimingTrack(), Style())
        renderer.set_playing(True)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.2:
            renderer.request(60_000 + int((time.monotonic() - t0) * 1000.0))
            qapp.processEvents()
            time.sleep(0.016)
        time.sleep(0.2)
        qapp.processEvents()

        # 骤快起点 = 第 4 次渲染的墙钟
        fast_at = renders[3][0]
        fast_renders = [t for w, t in renders if w >= fast_at]
        fast_presents = [t for w, t in presents if w >= fast_at]
        # 填缝：骤快后渲染显著多于呈现（多出来的在队列里等待到点）。
        assert len(fast_renders) >= len(fast_presents) + 3, (
            f"恢复期未填缝: renders={len(fast_renders)} presents={len(fast_presents)}"
        )
        # 呈现节奏 ≈ 帧间隔（到点逐帧放，不是渲完立即全部放完）。
        assert len(fast_presents) >= 5, "应有持续到点出队"
    finally:
        renderer.stop()


def test_gpu_scheduler_evicts_stale_cache_keys_instead_of_wedging(qapp, monkeypatch):
    """G5 播放几秒后永久冻结的回归（2026-10 长跑探针 WEDGED）。

    请求跳过的已填键是死键：take() 只按精确键命中、请求只前进、seek
    （>250ms 跳变）才清缓存。死键累积占满容量后 free_slots 恒 0，填充
    永久停止——真实工程 15s 探针 t=4s 缓存 29/29、t=5s 起 hits+0。
    调度器必须每轮清扫播放头之前的键（evict_before）。
    """
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    pending: list[dict] = []
    cached_events = threading.Event()

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured"}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}

        def begin_render_gpu_frame(self, t_ms, **kwargs):
            pending.append(
                {
                    "ok": True,
                    "event": "gpu_frame_ready",
                    "shm_key": "gpu-wedge-ring",
                    "t_ms": int(t_ms),
                    "request_serial": int(kwargs.get("request_serial", 0)),
                }
            )

        def try_finish_render_gpu_frame(self, _timeout_s):
            return pending.pop(0) if pending else None

        def close(self):
            pass

    class FakeGpuReader:
        def __init__(self, shm_key):
            self.shm_key = shm_key

        @classmethod
        def from_event(cls, event):
            return cls(event["shm_key"])

        def read_qimage(self, _event):
            cached_events.set()
            return QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)

        def close(self):
            pass

    monkeypatch.setenv("KROK_SUBTITLE_GPU_MAX_LOOKAHEAD_FRAMES", "2")
    monkeypatch.setenv("KROK_SUBTITLE_GPU_LOOKAHEAD_FRAMES", "1")
    monkeypatch.setenv("KROK_SUBTITLE_GPU_WORKERS", "1")
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    monkeypatch.setattr(pa, "SharedFrameRingReader", FakeGpuReader)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        assert renderer._frame_cache.capacity() == 4  # 2 max + 1 worker + 1
        renderer.set_state(TimingTrack(), Style())
        renderer.set_playing(True)

        def fill_count():
            return renderer.stats_snapshot()["future_frames_cached"]

        # 初始窗口填充起来。
        renderer.request(1_000)
        deadline = time.monotonic() + 2.0
        while fill_count() < 3 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert fill_count() >= 3

        # 连续 200ms 步进（< 250ms seek 阈值，不触发清缓存）：每步跳过
        # 大部分已填键，制造死键积累。旧代码数步内 free_slots 归零、
        # future_frames_cached 冻结；有清扫则持续增长。
        for step in range(12):
            before = fill_count()
            renderer.request(1_000 + 200 * (step + 1))
            deadline = time.monotonic() + 1.0
            while fill_count() <= before and time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.005)
            assert fill_count() > before, (
                f"第 {step} 步后填充停止：缓存被死键占满（楔死回归）"
            )
        stats = renderer.stats_snapshot()
        assert stats["cache_misses"] > 0
        assert renderer._frame_cache.size() <= renderer._frame_cache.capacity()
    finally:
        renderer.set_playing(False)
        renderer.stop()


def test_gpu_native_mode_hot_switch_keeps_sidecar_and_flips_transport(qapp, monkeypatch):
    """G6↔G5 热切换：同一 sidecar 内翻转直画/读回，进程不重建。

    每次切换杀进程重建会重付重特效场景的秒级 configure 成本（用户观感
    「多切几次越来越慢」的热切换根因——泄漏探针显示进程/显存本就干净）。
    """
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    present_calls: list[int] = []
    render_calls: list[int] = []
    close_process_calls: list[str] = []
    step = threading.Event()

    class FakeReader:
        @classmethod
        def from_event(cls, event):
            return cls()

        def read_qimage(self, event):
            return QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)

        @property
        def shm_key(self):
            return "fake"

        def close(self):
            pass

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready", "native_preview_protocol": 1}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "native_preview": True}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame_direct(self, t_ms, **kwargs):
            present_calls.append(int(t_ms))
            step.set()
            return {
                "ok": True,
                "event": "gpu_frame_rendered_direct",
                "t_ms": int(t_ms),
                "render_ms": 5.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "transport": "direct_composition",
            }

        def present_rendered_gpu_frame(self, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_presented",
                "t_ms": int(kwargs.get("t_ms", 0)),
                "render_ms": 0.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "child_hwnd": 4321,
                "transport": "direct_composition",
            }

        def render_gpu_frame(self, t_ms, **kwargs):
            render_calls.append(int(t_ms))
            step.set()
            return {
                "ok": True,
                "event": "gpu_frame_rendered",
                "t_ms": int(t_ms),
                "render_ms": 5.0,
                "readback_ms": 1.0,
                "shm_key": "fake",
                "readback_bands": [],
            }

        def close_gpu_preview(self, **kwargs):
            close_process_calls.append("gpu_preview")
            return {"ok": True, "event": "gpu_preview_closed"}

        def close(self):
            close_process_calls.append("process")

    monkeypatch.setattr(pa, "gpu_native_preview_enabled", lambda: True)
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    monkeypatch.setattr(pa, "SharedFrameRingReader", FakeReader)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        assert renderer.uses_native_preview is True
        renderer.set_native_target(12345, 0, 0, 320, 180)
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        assert step.wait(timeout=2.0)
        step.clear()
        deadline = time.monotonic() + 2.0
        while len(present_calls) < 1 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert present_calls, "G6 模式应走 present 直画"

        # 热切 G5：进程保留，读回路径接管
        assert renderer.set_native_mode(False) is True
        assert renderer.uses_native_preview is False
        renderer.request(2_000)
        deadline = time.monotonic() + 2.0
        while len(render_calls) < 1 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert render_calls, "热切 G5 后应走 render_gpu_frame 读回"

        # 热切回 G6：直画恢复，进程仍未重建
        assert renderer.set_native_mode(True) is True
        renderer.set_native_target(12345, 0, 0, 320, 180)
        renderer.request(3_000)
        deadline = time.monotonic() + 2.0
        while len(present_calls) < 2 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert len(present_calls) == 2, "热切回 G6 后应恢复 present"
        assert "process" not in close_process_calls, "热切换不得杀进程"
        assert renderer.stats_snapshot()["native_mode_switches"] == 2
    finally:
        renderer.stop()


def test_finish_render_gpu_frame_raises_typed_queue_full_error():
    """gpu_queue_full 是流控信号：必须抛 NativeQueueFullError 而不是裸错误。

    预览调度器据此做背压重发（不杀进程），2026-10 前它被 _expect_ok 当成
    渲染器故障直接触发重启链。
    """
    import krok_helper.subtitle_render.native.backend as backend

    from collections import deque

    renderer = backend.NativeRendererProcess.__new__(backend.NativeRendererProcess)
    renderer._process = object()  # _current_process 只要求非 None
    renderer.response_timeout_s = 2.0
    renderer.configure_timeout_s = 10.0
    renderer.gpu_configure_timeout_s = 30.0
    renderer._event_backlog = deque()

    def _fake_read_response(**_kwargs):
        return {
            "ok": False,
            "event": "gpu_queue_full",
            "error": "GPU preview in-flight limit reached",
        }

    renderer._read_response = _fake_read_response

    import pytest

    with pytest.raises(backend.NativeQueueFullError) as excinfo:
        renderer.finish_render_gpu_frame()
    assert isinstance(excinfo.value, backend.NativeRendererError)
    assert "in-flight limit reached" in str(excinfo.value)


def test_gpu_async_renderer_queue_full_is_backpressure_not_failure(
    qapp, monkeypatch
):
    """in-flight 池满只计背压，不进 renderer_failed 重启链。"""
    import time

    import krok_helper.subtitle_render.frontend.preview.preview_async as preview_async
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack
    from krok_helper.subtitle_render.native.backend import NativeQueueFullError

    calls = {"render": 0}

    class QueueThenGoodProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame(self, *args, **kwargs):
            calls["render"] += 1
            if calls["render"] == 1:
                raise NativeQueueFullError(
                    {"ok": False, "error": "GPU preview in-flight limit reached"}
                )
            return {
                "ok": True,
                "event": "gpu_frame_ready",
                "t_ms": 1_000,
                "shm_key": "test-shm",
                "slot_offset": 0,
                "header_bytes": 64,
                "payload_offset": 64,
                "payload_bytes": 256,
                "slot_bytes": 320,
                "pixel_format": "bgra8888_premultiplied",
            }

        def close(self):
            pass

    class FakeReader:
        shm_key = "test-shm"

        @classmethod
        def from_event(cls, _event):
            return cls()

        def read_qimage(self, _event):
            image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor("#112233"))
            return image

        def close(self):
            pass

    monkeypatch.setattr(preview_async, "NativeRendererProcess", QueueThenGoodProcess)
    monkeypatch.setattr(preview_async, "SharedFrameRingReader", FakeReader)
    renderer = preview_async.GpuAsyncSubtitleRenderer(320, 180)
    frames: list[tuple[QImage, int]] = []
    renderer.frame_ready.connect(lambda image, t_ms: frames.append((image, t_ms)))
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        deadline = time.monotonic() + 5.0
        while not frames and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        stats = renderer.stats_snapshot()
        assert frames and frames[0][1] == 1_000
        assert stats["queue_full_backpressure"] == 1
        assert stats["renderer_failures"] == 0
        assert stats["renderer_restarts"] == 0
    finally:
        renderer.stop()


def test_gpu_async_renderer_frame_error_retries_once_before_restart(
    qapp, monkeypatch
):
    """首个帧级错误原样重试（不重启、不上报），第二次才进失败链。"""
    import time

    import krok_helper.subtitle_render.frontend.preview.preview_async as preview_async
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack
    from krok_helper.subtitle_render.native.backend import NativeRendererError

    attempts = {"render": 0}

    class FailTwiceThenDeadProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame(self, *args, **kwargs):
            attempts["render"] += 1
            raise NativeRendererError(f"injected frame failure #{attempts['render']}")

        def close(self):
            pass

    monkeypatch.setattr(
        preview_async, "NativeRendererProcess", FailTwiceThenDeadProcess
    )
    renderer = preview_async.GpuAsyncSubtitleRenderer(320, 180)
    fallbacks: list[str] = []
    renderer.fallback_occurred.connect(fallbacks.append)
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        deadline = time.monotonic() + 5.0
        while not fallbacks and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        stats = renderer.stats_snapshot()
        assert attempts["render"] >= 2
        # 连续失败阈值 5（2026-10）：前 4 次重试，第 5 次进重启链
        assert stats["frame_error_retries"] == 4
        assert stats["renderer_failures"] == 1
        assert "injected frame failure #5" in fallbacks[0]
    finally:
        renderer.stop()


def test_gpu_async_renderer_failure_falls_back_to_painter(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import (
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


def test_fallback_report_gate_surfaces_each_reason_once(qapp, caplog):
    """回退上报闸：相同原因静默；新原因冷却期内落日志、冷却后弹出。"""
    import logging

    from krok_helper.subtitle_render.frontend.preview import preview_async as pa

    emitted: list[str] = []
    gate = pa._FallbackReportGate(emitted.append, log_label="GPU 字幕预览回退")

    with caplog.at_level(logging.WARNING, logger=pa._log.name):
        gate.report("原因 A")
        gate.report("原因 A")
        gate.report("原因 B")
    assert emitted == ["原因 A"]
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "原因 A" in logged
    assert "原因 B" in logged

    # 冷却结束后，新原因再次命中才弹出；之后又归于静默。
    gate._emitted_at -= gate._cooldown_s + 1.0
    gate.report("原因 B")
    gate.report("原因 B")
    assert emitted == ["原因 A", "原因 B"]


def test_gpu_async_renderer_surfaces_changed_fallback_reason(qapp, monkeypatch):
    """原因变化的 GPU 预览回退必须再次上报，不能被第一次回退永远吞掉。"""
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    failures = iter(range(100))

    class FlakyGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            raise pa.NativeRendererError(f"injected failure #{next(failures)}")

        def close(self):
            pass

    monkeypatch.setattr(pa, "NativeRendererProcess", FlakyGpuProcess)
    monkeypatch.setattr(pa, "_FALLBACK_CHANGED_COOLDOWN_S", 0.0)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    fallbacks: list[str] = []
    renderer.fallback_occurred.connect(fallbacks.append)
    try:
        renderer.set_state(TimingTrack(), Style())
        # 失败后进入 1s 重试窗口；持续请求直到第二个不同原因被上报。
        deadline = time.monotonic() + 5.0
        while len(fallbacks) < 2 and time.monotonic() < deadline:
            renderer.request(1_000)
            qapp.processEvents()
            time.sleep(0.05)

        assert len(fallbacks) >= 2
        # configure 阶段失败 2026-10 起不做帧级温和重试（sidecar native 楔死
        # 时重发只会白等超时）：第一次失败即上报、立即进重启链。
        assert "injected failure #0" in fallbacks[0]
        assert any("injected failure #1" in message for message in fallbacks[1:])
    finally:
        renderer.stop()


def test_native_async_renderer_failure_reports_fallback_reason(qapp, monkeypatch):
    """native 预览路径失败必须给出原因（此前完全静默地退到 CPU 帧）。"""
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    class FailingNativeProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure(self, *args, **kwargs):
            raise pa.NativeRendererError("injected native failure")

        def close(self):
            pass

    monkeypatch.setattr(pa, "NativeRendererProcess", FailingNativeProcess)
    renderer = pa.NativeAsyncSubtitleRenderer(320, 180)
    frames: list[int] = []
    fallbacks: list[str] = []
    renderer.frame_ready.connect(lambda _image, t_ms: frames.append(int(t_ms)))
    renderer.fallback_occurred.connect(fallbacks.append)
    try:
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        deadline = time.monotonic() + 2.0
        while (not frames or not fallbacks) and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        assert frames and frames[0] == 1_000
        assert len(fallbacks) == 1
        assert "injected native failure" in fallbacks[0]
        assert "Painter" in fallbacks[0]
    finally:
        renderer.stop()


def test_gpu_async_renderer_one_frame_lookahead_uses_bounded_cache(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    rendered: list[int] = []
    future_cached = threading.Event()

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured"}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame(self, t_ms, **kwargs):
            rendered.append(int(t_ms))
            return {
                "ok": True,
                "event": "gpu_frame_ready",
                "shm_key": "gpu-lookahead-ring",
                "t_ms": int(t_ms),
            }

        def begin_render_gpu_frame(self, t_ms, **kwargs):
            rendered.append(int(t_ms))
            self._completed.append(
                {
                    "ok": True,
                    "event": "gpu_frame_ready",
                    "shm_key": "gpu-lookahead-ring",
                    "t_ms": int(t_ms),
                    "request_serial": int(kwargs.get("request_serial", 0)),
                }
            )

        _completed: list[dict] = []

        def try_finish_render_gpu_frame(self, _timeout_s):
            return self._completed.pop(0) if self._completed else None

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

        # 填缝调度语义：窗口帧预渲入缓存（受缓存容量约束、无重渲
        # 抖动），暂停请求 1017 时缓存命中。
        assert 1_017 in rendered
        assert len(rendered) <= 24, f"出现窗口重渲抖动: {len(rendered)} 次"
        stats = renderer.stats_snapshot()
        assert stats["future_frames_cached"] >= 1
        assert stats["frames_emitted"] == 1
        assert stats["cache_hits"] == 1
        assert stats["max_pending"] == 1
    finally:
        renderer.stop()


def test_gpu_async_renderer_resize_rotates_shared_memory_generation(qapp):
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa

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


def test_gpu_native_hot_switch_rotates_ring_key_and_restores_workers(qapp, monkeypatch):
    """G6→G5 热切换的环/池参数回归（2026-10 G5 死亡链）。

    死亡链：G6 建立的渲染器切回 G5 后，同 key 以新几何（G6 用未钳制显示
    缩放、G5 用质量钳制 DPR）重建共享环；GUI 进程的 ring reader 仍 attach
    着旧段，Windows 命名对象不销毁 → sidecar create() 报 already exists →
    每帧失败 → G5 永不出帧。修复要点：
    1. set_native_mode 双向轮换 _shm_key；
    2. resize 成功也轮换 key（几何口径随模式变）；
    3. _worker_count_requested 不在 __init__ 冻结，切回 G5 的 resize 按
       模式取满额 worker（native/WARP 才压 1）；
    4. 单帧读回的 slot_count 与池化 ring_slots 一致（同 key 下 ensure
       参数必须恒定）。
    """
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    render_calls: list[dict] = []
    present_calls: list[int] = []
    resize_calls: list[dict] = []
    configure_calls: list[dict] = []
    step = threading.Event()

    class FakeReader:
        @classmethod
        def from_event(cls, event):
            return cls()

        def read_qimage(self, event):
            return QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)

        @property
        def shm_key(self):
            return "fake"

        def close(self):
            pass

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready", "native_preview_protocol": 1}

        def configure_gpu(self, *args, **kwargs):
            configure_calls.append(kwargs)
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}

        def resize_gpu_target(self, *args, **kwargs):
            resize_calls.append(kwargs)
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}

        def render_gpu_frame_direct(self, t_ms, **kwargs):
            present_calls.append(int(t_ms))
            step.set()
            return {
                "ok": True,
                "event": "gpu_frame_rendered_direct",
                "t_ms": int(t_ms),
                "render_ms": 5.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "transport": "direct_composition",
            }

        def present_rendered_gpu_frame(self, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_presented",
                "t_ms": int(kwargs.get("t_ms", 0)),
                "render_ms": 0.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "child_hwnd": 4321,
                "transport": "direct_composition",
            }

        def render_gpu_frame(self, t_ms, **kwargs):
            render_calls.append({"t_ms": int(t_ms), **kwargs})
            step.set()
            return {
                "ok": True,
                "event": "gpu_frame_rendered",
                "t_ms": int(t_ms),
                "render_ms": 5.0,
                "readback_ms": 1.0,
                "shm_key": "fake",
                "readback_bands": [],
            }

        def close_gpu_preview(self, **kwargs):
            return {"ok": True, "event": "gpu_preview_closed"}

        def close(self):
            pass

    monkeypatch.setenv("KROK_SUBTITLE_GPU_WORKERS", "2")
    monkeypatch.setattr(pa, "gpu_native_preview_enabled", lambda: True)
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    monkeypatch.setattr(pa, "SharedFrameRingReader", FakeReader)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        assert renderer.uses_native_preview is True
        # native 建立的渲染器不再把 env 申请的 worker 数冻结成 1：
        # 模式约束在调用点生效。
        assert renderer._worker_count_requested == 2
        renderer.set_native_target(12345, 0, 0, 320, 180)
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1_000)
        assert step.wait(timeout=2.0)
        step.clear()
        deadline = time.monotonic() + 2.0
        while len(present_calls) < 1 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert present_calls, "G6 模式应走 present 直画"
        # G6 下的 configure/resize 都必须压 worker=1（直画渲染走主后端，
        # 多 worker 池化 configure 不会配置主后端）。
        assert configure_calls and all(c.get("worker_count") == 1 for c in configure_calls)

        key_before_switch = renderer._shm_key
        assert renderer.set_native_mode(False) is True
        assert renderer._shm_key != key_before_switch, "切回 G5 必须轮换共享环 key"

        renderer.request(2_000)
        deadline = time.monotonic() + 2.0
        while len(render_calls) < 1 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert render_calls, "热切 G5 后应走 render_gpu_frame 读回"
        # 切回 G5 的 resize 恢复满额 worker；单帧读回的 slot_count 与
        # ring_slots 同值（同 key 下 ensure 参数恒定）。
        assert resize_calls, "热切换应触发强制 resize"
        assert any(c.get("worker_count") == 2 for c in resize_calls), (
            "G6→G5 后 resize 应申请满额 worker（不再被 init 冻结成 1）"
        )
        assert all(c["slot_count"] == renderer._readback_slot_count() for c in render_calls), (
            "单帧读回的 slot_count 必须与池化 begin 的环槽位数同值"
        )

        key_before_back = renderer._shm_key
        assert renderer.set_native_mode(True) is True
        assert renderer._shm_key != key_before_back, "切回 G6 也必须轮换共享环 key"
    finally:
        renderer.stop()


def test_gpu_async_renderer_uses_target_resize_after_initial_scene(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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

        def try_finish_render_gpu_frame(self, _timeout_s):
            return pending.pop() if pending else None

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
        assert stats["future_frames_cached"] >= 2
    finally:
        renderer.stop()


def test_gpu_async_renderer_reserves_final_ring_before_deferred_follower(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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

        def try_finish_render_gpu_frame(self, _timeout_s):
            return pending.pop() if pending else None

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

        # 填缝调度器会持续补批（窗口随媒体时钟推进），取前三个断言
        # 「首批即按终容量预留共享环」的语义。
        assert slot_counts[:3] == [2, 2, 2]
    finally:
        renderer.stop()


def test_gpu_async_renderer_ignores_dropped_single_frame_without_fallback(
    qapp, monkeypatch
):
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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
        def resize_gpu_target(self, *args, **kwargs):
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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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
        # 默认申请 4 worker（2026-10 用户拍板），弱显存机器收缩到 1。
        assert configured_workers == [4, 1]
        assert renderer.stats_snapshot()["worker_count"] == 1
    finally:
        renderer.stop()


def test_preview_graphics_passes_playing_state_to_async_renderer(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa

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
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

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
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

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


def test_preview_graphics_accepts_snapped_async_frame_while_paused(qapp, monkeypatch):
    """暂停态必须收下当前帧键的吸附回帧。

    GPU 预览渲染器把请求时刻吸附到 60fps 帧键网格（最大偏半格 ≈8.3ms）后
    按吸附值回帧；暂停态若要求回帧时间戳与媒体原始毫秒严格相等，当前位帧
    会被全部丢弃——「字幕渲染中」徽标等不到收帧而无限计时（暂停/暂停中
    拖动进度条均触发）。
    """
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: False)
    graphics = PreviewGraphicsView()
    try:
        graphics._subtitle_item.set_async_mode(True)
        graphics.set_time(10_008)
        snapped = QImage(16, 9, QImage.Format.Format_ARGB32_Premultiplied)
        snapped.fill(QColor("#0000FF"))

        graphics._on_async_frame(snapped, 10_000)

        assert graphics._subtitle_item._async_image is not None
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_clears_async_frame_on_style_change(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView
    from krok_helper.subtitle_render.domain.models import Style

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
# 渲染忙碌徽标：百分比进度反馈
# ---------------------------------------------------------------------------


def _badge_view(monkeypatch):
    """构造带 fake 异步 renderer（含 renderProgress 信号）的预览画布。"""
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import (
        PreviewGraphicsView,
    )
    from krok_helper.subtitle_render.domain.models import (
        TimingChar,
        TimingLine,
        TimingTrack,
    )

    class FakeSignal:
        def __init__(self):
            self.slots = []

        def connect(self, slot, *args, **kwargs):
            self.slots.append(slot)

        def emit(self, *args):
            for slot in self.slots:
                slot(*args)

    class FakeAsyncRenderer:
        instances = []

        def __init__(self, width, height, parent=None):
            self.frame_ready = FakeSignal()
            self.renderProgress = FakeSignal()
            self.requests = []
            FakeAsyncRenderer.instances.append(self)

        def set_render_target(self, width, height, device_pixel_ratio=1.0):
            pass

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

    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("テ", 0)], end_ms=900)]
    )
    graphics = PreviewGraphicsView()
    graphics.set_track(track)
    return graphics


def test_preview_graphics_render_badge_tracks_progress(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg

    monkeypatch.setattr(pg, "_RENDER_BUSY_DELAY_S", 0.0)
    graphics = _badge_view(monkeypatch)
    try:
        badge = graphics._render_busy_badge
        assert graphics._render_pending_since is not None

        # 无帧区间超阈值 → 徽标出现（尚无刻度时为不定态文本 + 走字耗时）。
        graphics._update_render_busy_badge()
        assert not badge.isHidden()
        assert badge._text.startswith("字幕渲染中 · ")

        # 进度事件 → 百分比文本实时更新（未停驻时保留阶段与百分比）。
        graphics._on_render_progress(43, "逐行排版")
        graphics._update_render_busy_badge()
        assert badge._text.startswith("字幕渲染 · 逐行排版 43% · ")

        # 进度停驻超阈值（sidecar 无刻度等待）→ 撤掉冻结的百分比、保留阶段名。
        graphics._render_progress_at = time.monotonic() - 2.0
        graphics._update_render_busy_badge()
        assert badge._text.startswith("字幕渲染 · 逐行排版中 · ")

        # 历史样本给出「预计还需」尾缀。
        graphics._render_duration_history = [3.0, 4.0, 5.0]
        graphics._render_progress_text = None
        graphics._render_progress_at = None
        graphics._update_render_busy_badge()
        assert "预计还需" in badge._text
        # 超出预估后回退为走字耗时。
        graphics._render_pending_since = time.monotonic() - 10.0
        graphics._update_render_busy_badge()
        assert "预计还需" not in badge._text
        assert "10.0s" in badge._text

        # 可接受的新帧到达 → 徽标隐藏、区间闭合，且慢渲染耗时进入历史。
        graphics._render_pending_since = time.monotonic() - 1.0
        image = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
        graphics._on_async_frame(image, graphics.current_time_ms)
        assert badge.isHidden()
        assert graphics._render_pending_since is None
        assert graphics._render_duration_history  # ≥ 阈值的样本被记录

        # 过期帧（超出容差）不能闭合区间，徽标再次出现后保持。
        graphics.set_time(5_000)
        graphics._on_async_frame(image, 1_000)
        assert graphics._render_pending_since is not None
        graphics._update_render_busy_badge()
        assert not badge.isHidden()
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_render_badge_hidden_for_fast_render(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg

    # 阈值拉长：快速回帧的正常渲染绝不显示徽标（防闪烁）。
    monkeypatch.setattr(pg, "_RENDER_BUSY_DELAY_S", 5.0)
    graphics = _badge_view(monkeypatch)
    try:
        badge = graphics._render_busy_badge
        assert graphics._render_pending_since is not None
        graphics._update_render_busy_badge()
        assert badge.isHidden()

        image = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
        graphics._on_async_frame(image, graphics.current_time_ms)
        assert badge.isHidden()
        assert not graphics._render_busy_timer.isActive()
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_render_badge_closes_on_empty_track(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg

    monkeypatch.setattr(pg, "_RENDER_BUSY_DELAY_S", 0.0)
    graphics = _badge_view(monkeypatch)
    try:
        assert graphics._render_pending_since is not None
        # 卸载字幕轨：请求不会再有帧回来，区间必须闭合，徽标不悬死。
        graphics.set_track(None)
        assert graphics._render_pending_since is None
        assert graphics._render_busy_badge.isHidden()
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_render_badge_closes_on_snapped_frame_while_paused(
    qapp, monkeypatch
):
    """回归：暂停态收到吸附回帧必须闭合徽标区间；容差外仍拒绝。

    修复前暂停态容差为 0，GPU 吸附回帧（偏 ≤8.3ms）全部被丢，无帧区间
    永不闭合 →「字幕渲染中」秒数无限增长。
    """
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg

    monkeypatch.setattr(pg, "_RENDER_BUSY_DELAY_S", 0.0)
    graphics = _badge_view(monkeypatch)
    try:
        badge = graphics._render_busy_badge
        # 暂停态（默认未播放）请求 10_008 → 徽标出现。
        graphics.set_time(10_008)
        graphics._update_render_busy_badge()
        assert not badge.isHidden()

        # GPU 回帧按吸附值 10_000（偏 -8ms）到达 → 收帧并闭合区间、停表。
        image = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
        graphics._on_async_frame(image, 10_000)
        assert badge.isHidden()
        assert graphics._render_pending_since is None
        assert not graphics._render_busy_timer.isActive()

        # 超出暂停容差（9ms）的帧仍被拒绝：暂停容差是帧键吸附级别，
        # 不是播放级的 120ms。
        graphics.set_time(10_008)
        assert graphics._render_pending_since is not None
        graphics._on_async_frame(image, 10_050)
        assert graphics._render_pending_since is not None
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_render_backend_label_tracks_actual_mode(qapp, monkeypatch):
    """画布的实际后端标签：以真实出帧后端为准，渲染器切换时发信号。"""
    import krok_helper.subtitle_render.frontend.preview.preview_async as preview_async
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import (
        PreviewGraphicsView,
    )
    from krok_helper.subtitle_render.domain.models import (
        TimingChar,
        TimingLine,
        TimingTrack,
    )

    from krok_helper.subtitle_render.native.backend import NativeRendererError

    class BrokenSidecar:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise NativeRendererError("sidecar unavailable in test")

        def close(self):
            return None

    monkeypatch.setattr(preview_async, "NativeRendererProcess", BrokenSidecar)
    monkeypatch.setattr(pg, "async_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "gpu_preview_enabled", lambda: True)

    graphics = PreviewGraphicsView()
    try:
        # 尚无帧定论：按选择态显示 GPU。
        assert graphics.render_backend_label() == "GPU"

        # sidecar 起不来 → 故障回退 Painter，标签翻转为 CPU。
        track = TimingTrack(
            lines=[TimingLine(chars=[TimingChar("歌", 0)], end_ms=1_000)]
        )
        graphics.set_track(track)
        deadline = time.monotonic() + 5.0
        while graphics.render_backend_label() != "CPU" and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert graphics.render_backend_label() == "CPU"

        # 运行时切渲染器：切到 Painter → CPU；切回 GPU（无帧定论）→ GPU。
        received: list[str] = []
        graphics.renderBackendChanged.connect(received.append)
        graphics.set_gpu_preview_enabled(False)
        assert received == ["CPU"]
        assert graphics.render_backend_label() == "CPU"
        graphics.set_gpu_preview_enabled(True)
        assert received == ["CPU", "GPU"]
        assert graphics.render_backend_label() == "GPU"
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_render_backend_emits_in_sync_painter_mode(qapp, monkeypatch):
    """async 全关时手动开/关 GPU：两次切换都必须发信号（关回同步模式不漏报）。"""
    import krok_helper.subtitle_render.frontend.preview.preview_async as preview_async
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import (
        PreviewGraphicsView,
    )
    from krok_helper.subtitle_render.native.backend import NativeRendererError

    class BrokenSidecar:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise NativeRendererError("sidecar unavailable in test")

        def close(self):
            return None

    monkeypatch.setattr(preview_async, "NativeRendererProcess", BrokenSidecar)
    monkeypatch.setattr(pg, "async_preview_enabled", lambda: False)
    monkeypatch.setattr(pg, "gpu_preview_enabled", lambda: False)

    graphics = PreviewGraphicsView()
    try:
        assert graphics.render_backend_label() == "CPU"  # 同步 QPainter，无渲染器
        received: list[str] = []
        graphics.renderBackendChanged.connect(received.append)

        graphics.set_gpu_preview_enabled(True)
        assert received == ["GPU"]
        assert graphics.render_backend_label() == "GPU"

        # 关回同步模式：async 全关的早退分支也必须通知 UI。
        graphics.set_gpu_preview_enabled(False)
        assert received == ["GPU", "CPU"]
        assert graphics.render_backend_label() == "CPU"
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_ignores_stale_backend_signal_after_renderer_swap(
    qapp, monkeypatch
):
    """渲染器热切换后，旧渲染器仍在事件队列里的迟到信号不得污染新状态。"""
    import krok_helper.subtitle_render.frontend.preview.preview_async as preview_async
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import (
        PreviewGraphicsView,
    )
    from krok_helper.subtitle_render.native.backend import NativeRendererError

    class BrokenSidecar:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise NativeRendererError("sidecar unavailable in test")

        def close(self):
            return None

    monkeypatch.setattr(preview_async, "NativeRendererProcess", BrokenSidecar)
    monkeypatch.setattr(pg, "async_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "gpu_preview_enabled", lambda: True)

    graphics = PreviewGraphicsView()
    try:
        old_renderer = graphics._async_renderer
        assert old_renderer is not None
        received: list[str] = []
        graphics.renderBackendChanged.connect(received.append)

        graphics.set_gpu_preview_enabled(False)  # → Painter
        graphics.set_gpu_preview_enabled(True)  # → 新 GPU 渲染器
        assert received == ["CPU", "GPU"]
        assert graphics.render_backend_label() == "GPU"

        # 旧渲染器的迟到信号（断开连接 + sender 双保险）不得翻回 CPU。
        old_renderer.backendModeChanged.emit("cpu")
        qapp.processEvents()
        assert graphics.render_backend_label() == "GPU"
        assert received == ["CPU", "GPU"]
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_graphics_backend_label_follows_gpu_failure_and_recovery(
    qapp, monkeypatch
):
    """端到端恢复环：GPU 故障 → 回退 CPU 出帧 → 冷却后自动重试拉起 GPU。

    覆盖用户列出的切换场景：sidecar 故障/显存爆（异常路径）、Painter 回退帧、
    CPU 态自动重试恢复 GPU，标签全程跟随真实出帧后端。
    """
    import krok_helper.subtitle_render.frontend.preview.preview_async as preview_async
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import (
        PreviewGraphicsView,
    )
    from krok_helper.subtitle_render.domain.models import (
        TimingChar,
        TimingLine,
        TimingTrack,
    )
    from krok_helper.subtitle_render.native.backend import NativeRendererError

    start_attempts = 0

    class FlakySidecar:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            nonlocal start_attempts
            start_attempts += 1
            if start_attempts <= 5:
                # 前五次拉起失败（模拟 GPU 访问异常 / 显存爆）。
                # 连续失败阈值 5（2026-10）：前四次被帧级重试静默吸收，
                # 第五次进入失败链出 CPU 回退帧 → 标签 CPU。
                raise NativeRendererError("flaky sidecar first start fails")
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame(self, t_ms, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_ready",
                "shm_key": "gpu-recovery-ring",
                "t_ms": int(t_ms),
            }

        def close(self):
            return None

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

    monkeypatch.setattr(preview_async, "NativeRendererProcess", FlakySidecar)
    monkeypatch.setattr(preview_async, "SharedFrameRingReader", FakeGpuReader)
    monkeypatch.setattr(pg, "async_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "gpu_preview_enabled", lambda: True)

    graphics = PreviewGraphicsView()
    try:
        track = TimingTrack(
            lines=[TimingLine(chars=[TimingChar("歌", 0)], end_ms=1_000)]
        )
        graphics.set_track(track)

        # 首帧：sidecar 前两次拉起失败（第一次被重试吸收，第二次进失败链）
        # → 回退 Painter 出帧 → 标签 CPU。
        graphics.set_time(1_000)
        deadline = time.monotonic() + 5.0
        while graphics.render_backend_label() != "CPU" and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert graphics.render_backend_label() == "CPU"
        assert start_attempts == 5

        # 冷却结束后新请求 → 自动重试拉起 sidecar（这次成功）→ 标签 GPU。
        graphics._async_renderer._retry_after = 0.0
        graphics.set_time(2_000)
        deadline = time.monotonic() + 5.0
        while graphics.render_backend_label() != "GPU" and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert graphics.render_backend_label() == "GPU"
        assert start_attempts == 6
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_player_window_backend_indicator(qapp, monkeypatch):
    """预览窗口标题栏的实际渲染后端指示：位于标题与预览质量之间并联动。"""
    from PyQt6.QtWidgets import QWidget

    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.player_window import (
        PreviewPlayerWindow,
    )

    monkeypatch.setattr(pg, "async_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "gpu_preview_enabled", lambda: False)

    owner = QWidget()
    window = PreviewPlayerWindow(owner)
    try:
        layout = window._top_controls.layout()
        title_idx = layout.indexOf(window._title_label)
        backend_idx = layout.indexOf(window._backend_label)
        quality_idx = layout.indexOf(window._transport_bar._preview_quality_label)
        assert title_idx < backend_idx < quality_idx

        # offscreen 下画布是 Painter 异步渲染器 → 初始即 CPU。
        assert window._backend_label.text() == "CPU渲染中"
        window._set_render_backend_label("GPU")
        assert window._backend_label.text() == "GPU渲染中"

        window._collapse_window()
        assert window._backend_label.isHidden()
        window._restore_from_collapsed()
        assert not window._backend_label.isHidden()
    finally:
        window._preview_panel.canvas.close()
        window.close()
        window.deleteLater()
        owner.deleteLater()
        qapp.processEvents()


def test_preview_graphics_pause_re_requests_current_frame(qapp, monkeypatch):
    """回归：暂停必须补发当前位帧请求。

    GPU 播放路径的当前帧依赖前瞻缓存命中，暂停会丢弃投机批次；画布若不
    补发请求，暂停位的帧无人渲染，已打开的徽标区间悬死。
    """
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg

    graphics = _badge_view(monkeypatch)
    try:
        renderer = graphics._async_renderer
        graphics.set_time(10_008)
        renderer.requests.clear()
        graphics._render_pending_since = None
        graphics._render_busy_timer.stop()

        graphics.set_playing(False)

        # 暂停补发的请求用的是画布当前媒体时刻（原始毫秒，吸附在渲染器内做）。
        assert renderer.requests == [10_008]
        assert graphics._render_pending_since is not None
        # 无轨道时不得补发（set_track(None) 自身的刷新请求不算）。
        graphics.set_track(None)
        renderer.requests.clear()
        graphics.set_playing(False)
        assert renderer.requests == []
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_preview_pause_slow_configure_still_shows_stage_badge(qapp, monkeypatch):
    """回归护栏：暂停补帧修复不得吞掉慢 configure 的阶段提示。

    真实 GpuAsyncSubtitleRenderer + 真实画布，fake sidecar 的第二次
    configure_gpu（样式变化触发的全量重排）阻塞模拟慢场景构建：期间
    「场景构建」进度事件应照常点亮徽标，暂停位帧到达后徽标按常归隐。
    （用户反馈核对：暂停修复后阶段提示是否还能出现。）
    """
    import krok_helper.subtitle_render.frontend.preview.preview_async as preview_async
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import (
        PreviewGraphicsView,
    )
    from krok_helper.subtitle_render.domain.models import (
        Style,
        TimingChar,
        TimingLine,
        TimingTrack,
    )

    configure_count = 0
    slow_configure_entered = threading.Event()
    release_slow_configure = threading.Event()

    class SlowConfigureProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, progress=None, **kwargs):
            nonlocal configure_count
            configure_count += 1
            if configure_count < 2:
                return {"ok": True, "event": "gpu_configured", "worker_count": 1}
            if progress is not None:
                progress()  # IR 重排完成 → renderer 侧 emit(80, "场景构建")
            slow_configure_entered.set()
            release_slow_configure.wait(timeout=5.0)
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}

        def render_gpu_frame(self, t_ms, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_ready",
                "shm_key": "gpu-slow-configure-ring",
                "t_ms": int(t_ms),
            }

        def close(self):
            release_slow_configure.set()

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

    monkeypatch.setattr(preview_async, "NativeRendererProcess", SlowConfigureProcess)
    monkeypatch.setattr(preview_async, "SharedFrameRingReader", FakeGpuReader)
    monkeypatch.setattr(pg, "async_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "gpu_preview_enabled", lambda: True)
    monkeypatch.setattr(pg, "_RENDER_BUSY_DELAY_S", 0.0)

    graphics = PreviewGraphicsView()
    try:
        track = TimingTrack(
            lines=[TimingLine(chars=[TimingChar("歌", 0)], end_ms=1_000)]
        )
        graphics.set_track(track)
        graphics.set_time(10_008)
        # 初始场景构建（第一次 configure，fake 立即返回）+ 首帧回账。
        deadline = time.monotonic() + 5.0
        while graphics._render_pending_since is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert configure_count == 1
        assert graphics._render_pending_since is None

        # 暂停态下改样式 → 全量重排（第二次 configure）阻塞在场景构建。
        graphics.set_style(Style(font_size_px=96))
        assert slow_configure_entered.wait(timeout=5.0)
        qapp.processEvents()  # 投递 emit(80, "场景构建") 队列信号
        graphics._update_render_busy_badge()

        badge = graphics._render_busy_badge
        assert not badge.isHidden()
        assert "场景构建" in badge._text

        # 场景构建完成、暂停位帧（吸附时刻 10_000）到达 → 徽标归隐、区间闭合。
        release_slow_configure.set()
        deadline = time.monotonic() + 5.0
        while (graphics._render_pending_since is not None or badge.isHidden()) and (
            time.monotonic() < deadline
        ):
            qapp.processEvents()
            time.sleep(0.01)
        assert graphics._render_pending_since is None
        assert badge.isHidden()
        assert graphics._subtitle_item._async_image is not None
    finally:
        release_slow_configure.set()
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


def test_render_progress_reporter_composes_monotonic_percent():
    from krok_helper.subtitle_render.frontend.preview.preview_async import (
        _RENDER_STAGE_SPANS_PAINTER,
        _render_progress_reporter,
    )

    emitted: list[tuple[int, str]] = []
    reporter = _render_progress_reporter(
        _RENDER_STAGE_SPANS_PAINTER, lambda pct, label: emitted.append((pct, label))
    )

    reporter("display", 1, 7)   # 0 + 0.60 × 1/7 ≈ 9%
    reporter("display", 4, 7)   # ≈ 34%
    reporter("display", 2, 7)   # 回退刻度必须被丢弃
    reporter("unknown", 1, 1)   # 未映射阶段忽略
    reporter("lines", 1, 10)    # 0.82 + 0.18 × 1/10 = 84%
    reporter("lines", 10, 10)   # 100% 封顶到 99，留给真实帧完成

    assert emitted == [
        (9, "显示窗口"),
        (34, "显示窗口"),
        (84, "逐行排版"),
        (99, "逐行排版"),
    ]


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
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import (
        PreviewGraphicsView,
    )
    from krok_helper.subtitle_render.domain.models import BackgroundSource

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
    from krok_helper.subtitle_render.frontend.preview.preview_view import PreviewPanel
    from krok_helper.subtitle_render.domain.models import BackgroundSource

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
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import (
        PreviewGraphicsView,
    )
    from krok_helper.subtitle_render.domain.models import BackgroundSource

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


# ---------------------------------------------------------------------------
# GPU 预览 request 的帧键吸附（防相邻键交替消费）
# ---------------------------------------------------------------------------


def _broken_sidecar_renderer(monkeypatch, qapp):
    """构造一个 sidecar 永远起不来的 GPU 预览渲染器（不拉真实进程）。"""

    import krok_helper.subtitle_render.frontend.preview.preview_async as preview_async
    from krok_helper.subtitle_render.native.backend import NativeRendererError

    class BrokenSidecar:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise NativeRendererError("sidecar unavailable in test")

        def close(self):
            return None

    monkeypatch.setattr(preview_async, "NativeRendererProcess", BrokenSidecar)
    return preview_async.GpuAsyncSubtitleRenderer(320, 180)


def test_gpu_realization_transition_purges_raw_cache_and_queued_images(qapp, monkeypatch):
    renderer = _broken_sidecar_renderer(monkeypatch, qapp)
    try:
        generation = renderer._generation
        with renderer._condition:
            renderer._latest_t = 0
        raw = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
        raw.fill(0)
        raw.setText("gpu_realization_path", "raw")
        raw.setText("gpu_generation", str(generation))
        renderer._cache_speculative(raw, 200, generation)
        assert renderer._frame_cache.size() == 1
        assert renderer.accepts_realization_image(raw.copy())

        # A raw frame discarded by native publication is itself sufficient to
        # advance the floor, before any baked image has reached Qt.
        assert not renderer._accept_realization_event({
            "event": "gpu_frame_dropped", "generation": generation,
            "realization_ready": True, "realization_path_ready": False,
        }, generation)
        assert renderer._frame_cache.size() == 0
        assert not renderer.accepts_realization_image(raw.copy())
        renderer._cache_speculative(raw, 200, generation)
        assert renderer._frame_cache.size() == 0
        assert not renderer._accept_realization_event({
            "generation": generation, "realization_ready": False,
            "realization_path_ready": False,
        }, generation)

        baked = raw.copy()
        baked.setText("gpu_realization_path", "baked")
        renderer._cache_speculative(baked, 200, generation)
        assert renderer._frame_cache.size() == 1
        assert renderer.accepts_realization_image(baked)
        assert renderer.accepts_realization_image(QImage())  # CPU fallback

        with renderer._condition:
            renderer._generation += 1
        assert not renderer.accepts_realization_image(baked)
        assert not renderer._accept_realization_event({
            "generation": generation, "realization_ready": True,
        }, generation)
        raw.setText("gpu_generation", str(renderer._generation))
        assert renderer.accepts_realization_image(raw)
    finally:
        renderer.stop()


def test_gpu_realization_display_gate_rejects_queued_raw_frame(qapp, monkeypatch):
    from types import SimpleNamespace
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

    renderer = _broken_sidecar_renderer(monkeypatch, qapp)
    delivered = []
    try:
        generation = renderer._generation
        image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        image.setText("gpu_generation", str(generation))
        image.setText("gpu_realization_path", "raw")
        view = SimpleNamespace(
            _async_renderer=renderer, _t_ms=0,
            _note_frame_delivered=lambda: delivered.append(True),
            _subtitle_item=SimpleNamespace(set_async_image=lambda image: None),
        )
        renderer._accept_realization_event({"realization_ready": True}, generation)
        PreviewGraphicsView._on_async_frame(view, image, 0)
        assert not delivered
        image.setText("gpu_realization_path", "baked")
        PreviewGraphicsView._on_async_frame(view, image, 0)
        assert delivered == [True]
    finally:
        renderer.stop()


def test_gpu_realization_paused_drop_retries_foreground_without_replacing_new_request(qapp, monkeypatch):
    renderer = _broken_sidecar_renderer(monkeypatch, qapp)
    event = {"reason": "realization_ready"}
    try:
        # Hold the scheduler lock so its thread cannot consume the assertion's
        # pending request, then clear it before releasing the lock.
        with renderer._condition:
            generation = renderer._generation
            serial = renderer._request_serial
            renderer._retry_after_realization_drop(event, 750, serial, generation)
            assert renderer._pending[:3] == (750, serial, False)
            renderer._retry_after_realization_drop(event, 500, serial, generation)
            assert renderer._pending[0] == 750
            renderer._pending = None
            renderer._retry_after_realization_drop(event, 750, serial - 1, generation)
            assert renderer._pending is None
            renderer._retry_after_realization_drop(event, 750, serial, generation - 1)
            assert renderer._pending is None
            renderer._retry_after_realization_drop({"reason": "generation_cancelled"}, 750, serial, generation)
            assert renderer._pending is None
            renderer._playing = True
            renderer._retry_after_realization_drop(event, 750, serial, generation)
            assert renderer._pending is None
    finally:
        renderer.stop()


def test_gpu_preview_request_snaps_to_frame_key_grid(qapp, monkeypatch):
    """request 的 t 必须吸附到 60fps 帧键网格再消费/下渲染请求。

    媒体时钟的原始毫秒在键边界附近抖动时，未吸附的 request 会让缓存
    消费键与 speculative 键序列相位错开，emit 的帧内容时刻来回漂移
    （快动画字表现为逐帧位置抖动）。
    """

    from krok_helper.subtitle_render.domain.models import Style
    from krok_helper.subtitle_render.domain.timing import (
        TimingChar,
        TimingLine,
        TimingTrack,
    )

    renderer = _broken_sidecar_renderer(monkeypatch, qapp)
    try:
        track = TimingTrack(
            lines=[TimingLine(chars=[TimingChar("歌", 0)], end_ms=1_000)]
        )
        renderer.set_state(track, Style())
        # 键 600 的网格时刻是 10000；9995..10008 都量化进键 600。
        for raw_t in (9_996, 10_000, 10_007, 10_008):
            renderer.request(raw_t)
            assert renderer._latest_t == 10_000, (
                f"request({raw_t}) 应吸附到网格时刻 10000，"
                f"实际 {renderer._latest_t}"
            )
        # 跨过键边界（10_009 → 键 601）吸附到下一格 10017。
        renderer.request(10_009)
        assert renderer._latest_t == 10_017
    finally:
        renderer.stop()


def test_gpu_preview_request_snap_is_deterministic_for_same_frame(qapp, monkeypatch):
    """同一逻辑帧内的 ±毫秒抖动必须产生完全相同的吸附值（无交替）。"""

    from krok_helper.subtitle_render.domain.models import Style
    from krok_helper.subtitle_render.domain.timing import (
        TimingChar,
        TimingLine,
        TimingTrack,
    )

    renderer = _broken_sidecar_renderer(monkeypatch, qapp)
    try:
        track = TimingTrack(
            lines=[TimingLine(chars=[TimingChar("歌", 0)], end_ms=1_000)]
        )
        renderer.set_state(track, Style())
        seen = set()
        for raw_t in (10_006, 10_008, 10_007, 10_008, 10_006):
            renderer.request(raw_t)
            seen.add(renderer._latest_t)
        assert seen == {10_000}
    finally:
        renderer.stop()


def test_gpu_renderer_backend_mode_reports_gpu_on_delivered_frame(qapp, monkeypatch):
    """实际后端上报：sidecar 出帧到达 → gpu。"""

    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}
        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}


        def render_gpu_frame(self, t_ms, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_ready",
                "shm_key": "gpu-backend-mode-ring",
                "t_ms": int(t_ms),
            }

        def close(self):
            return None

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
        deadline = time.monotonic() + 5.0
        while renderer.current_backend_mode != "gpu" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert renderer.current_backend_mode == "gpu"
    finally:
        renderer.stop()


def test_gpu_renderer_backend_mode_flips_to_cpu_on_sidecar_failure(qapp, monkeypatch):
    """实际后端上报：sidecar 起不来（故障路径）→ cpu，与选择态相反。"""

    from krok_helper.subtitle_render.domain.models import Style
    from krok_helper.subtitle_render.domain.timing import (
        TimingChar,
        TimingLine,
        TimingTrack,
    )

    renderer = _broken_sidecar_renderer(monkeypatch, qapp)
    try:
        track = TimingTrack(
            lines=[TimingLine(chars=[TimingChar("歌", 0)], end_ms=1_000)]
        )
        renderer.set_state(track, Style())
        assert renderer.current_backend_mode is None  # 尚无帧定论
        renderer.request(1_000)
        deadline = time.monotonic() + 5.0
        while renderer.current_backend_mode != "cpu" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert renderer.current_backend_mode == "cpu"
    finally:
        renderer.stop()


def test_native_preview_frame_cache_key_grid_roundtrip():
    """key_for / timestamp_for_key 的往返必须幂等（吸附值自身落在网格上）。"""

    from krok_helper.subtitle_render.frontend.preview.preview_async import (
        NativePreviewFrameCache,
    )

    cache = NativePreviewFrameCache(max_frames=1, fps=60)
    for raw_t in range(9_990, 10_030):
        key = cache.key_for(raw_t)
        snapped = cache.timestamp_for_key(key)
        assert cache.key_for(snapped) == key
        assert abs(snapped - raw_t) <= 1000 / 60 / 2 + 1


# ---------------------------------------------------------------------------
# 看门狗（2026-10）：progress 心跳续租 + GPU 重启断路器。
# 旧机制纯墙钟超时（30s）+ 1s 后无限重试：合法长任务（密集符号场景构建/
# realization 预热可达数十秒）被误杀，重启丢光进度后重付同样的工作再次超
# 时——「活着却被反复重启」的西西弗斯循环。看门狗以进度心跳续租区分「忙
# 碌」与「死锁」，断路器在窗口超限时熔断，禁止无限重启叠加。
# ---------------------------------------------------------------------------

def test_gpu_restart_breaker_trips_within_window():
    from krok_helper.subtitle_render.frontend.preview.preview_async import (
        _GpuRestartBreaker,
    )

    breaker = _GpuRestartBreaker(window_s=60.0, limit=3)
    assert breaker.record(now=0.0) is False
    assert breaker.record(now=1.0) is False
    assert breaker.record(now=2.0) is True
    assert breaker.open


def test_gpu_restart_breaker_expires_old_restarts():
    from krok_helper.subtitle_render.frontend.preview.preview_async import (
        _GpuRestartBreaker,
    )

    breaker = _GpuRestartBreaker(window_s=60.0, limit=3)
    breaker.record(now=0.0)
    breaker.record(now=1.0)
    # 旧记录滑出窗口后不计：t=70 时窗内只剩它自己。
    assert breaker.record(now=70.0) is False
    assert breaker.open is False


def _fake_heartbeat_renderer(**attrs):
    """构造不spawn进程的 NativeRendererProcess 测试件（心跳续租用）。"""
    import collections
    import queue as queue_mod
    import threading

    from krok_helper.subtitle_render.native.backend import NativeRendererProcess

    proc = object.__new__(NativeRendererProcess)
    proc._stdout_queue = queue_mod.Queue()
    proc._event_backlog = collections.deque()
    proc._stderr_tail = collections.deque(maxlen=80)
    proc._stdout_noise_tail = collections.deque(maxlen=20)
    proc._stderr_lock = threading.Lock()
    proc._stdout_noise_lock = threading.Lock()
    proc._last_heartbeat_monotonic = 0.0

    class _FakeProcess:
        pid = 424242

        def poll(self):
            return None

    proc._process = _FakeProcess()
    proc.response_timeout_s = 0.2
    for key, value in attrs.items():
        setattr(proc, key, value)
    return proc


def test_read_until_event_heartbeat_lease_keeps_busy_sidecar_alive():
    """progress 心跳续租：目标事件晚于初始超时到达也能等到（不误杀忙碌 sidecar）。"""
    from krok_helper.subtitle_render.native.backend import NativeRendererProcess

    proc = _fake_heartbeat_renderer()

    def late_heartbeat_then_target():
        # 初始超时（0.2s）到期前推进心跳时间戳（任务内逐段喂狗），目标
        # 事件在初始超时之后才送达——只有续租能救。
        time.sleep(0.12)
        proc._last_heartbeat_monotonic = time.monotonic()
        time.sleep(0.25)
        proc._stdout_queue.put(
            '{"ok": true, "event": "gpu_configured"}'
        )

    threading.Thread(target=late_heartbeat_then_target, daemon=True).start()
    payload = proc._read_until_event(
        "gpu_configured", timeout_s=0.2, heartbeat_lease_s=5.0
    )
    assert payload["event"] == "gpu_configured"


def test_read_until_event_heartbeat_stall_still_times_out():
    """心跳停滞后超过租期必须照常超时（真死锁不被续租掩盖）。"""
    from krok_helper.subtitle_render.native.backend import NativeRendererError

    proc = _fake_heartbeat_renderer()
    # 心跳时间戳保持远古值（从未喂狗）：租期不给任何信用。
    with pytest.raises(NativeRendererError):
        proc._read_until_event(
            "gpu_configured", timeout_s=0.2, heartbeat_lease_s=0.3
        )


def test_enqueue_stdout_records_heartbeat_without_queueing():
    """progress 心跳只刷新存活时间戳、不进响应队列——空闲喂狗不会堆积。"""
    import io
    import json as json_mod
    import queue as queue_mod

    proc = _fake_heartbeat_renderer()
    stream = io.StringIO(
        '{"ok":true,"event":"progress","phase":"idle","done":0,"total":0}' + "\n"
        '{"ok": true, "event": "frame", "frame": 7}' + "\n"
    )
    proc._enqueue_stdout(stream)
    lines = []
    while True:
        item = proc._stdout_queue.get_nowait()
        if item is None:
            break
        lines.append(item)
    assert len(lines) == 1
    assert json_mod.loads(lines[0])["event"] == "frame"
    assert proc._last_heartbeat_monotonic > 0.0
    # 阶段快照同步记录（GUI 忙碌徽标显示「卡在哪一段」用）。
    snapshot = proc.progress_snapshot()
    assert snapshot is not None
    assert snapshot["phase"] == "idle"
    assert snapshot["done"] == 0 and snapshot["total"] == 0


def test_busy_badge_prefers_fresh_sidecar_stage(qapp, monkeypatch):
    """徽标阶段文本优先用 sidecar 心跳阶段（D2D 大任务的地面真相）。"""
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_graphics import PreviewGraphicsView

    class FakeRenderer:
        def __init__(self, snapshot):
            self._snapshot = snapshot

        def progress_snapshot(self):
            return self._snapshot

    graphics = PreviewGraphicsView()
    try:
        now = time.monotonic()
        # 带计数的 realize 阶段（新鲜）→ 显示中文标签 + done/total。
        graphics._async_renderer = FakeRenderer({  # noqa: SLF001
            "phase": "realize", "done": 128, "total": 598, "at": now - 0.2,
        })
        assert graphics._sidecar_stage_text(time.monotonic()) == (  # noqa: SLF001
            "字幕渲染 · 字形烘焙中 128/598"
        )
        # D2D 大任务阶段（无计数）→ 只有标签。
        graphics._async_renderer = FakeRenderer({  # noqa: SLF001
            "phase": "d2d-widen", "done": 0, "total": 0, "at": time.monotonic(),
        })
        assert graphics._sidecar_stage_text(time.monotonic()) == (  # noqa: SLF001
            "字幕渲染 · 描边展开中"
        )
        # idle 不展示（无任务状态不顶掉 Python 侧进度文本）。
        graphics._async_renderer = FakeRenderer({  # noqa: SLF001
            "phase": "idle", "done": 0, "total": 0, "at": time.monotonic(),
        })
        assert graphics._sidecar_stage_text(time.monotonic()) is None  # noqa: SLF001
        # 过期快照视为阶段已结束。
        graphics._async_renderer = FakeRenderer({  # noqa: SLF001
            "phase": "realize", "done": 1, "total": 9, "at": now - 30.0,
        })
        assert graphics._sidecar_stage_text(time.monotonic()) is None  # noqa: SLF001
        # CPU 渲染器（无 progress_snapshot 接口）→ None，徽标回落旧行为。
        graphics._async_renderer = object()  # noqa: SLF001
        assert graphics._sidecar_stage_text(time.monotonic()) is None  # noqa: SLF001
        graphics._async_renderer = None  # noqa: SLF001 - 还原，避免 close 调 stop
    finally:
        graphics.close()
        graphics.deleteLater()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# G6 直画帧仓（2026-10）：present 按 (generation, t_ms) 身份取帧
# ---------------------------------------------------------------------------


def test_native_due_queue_capacity_clamped_to_frame_store():
    """到点队列容量必须钳到帧仓容量：队列超仓只会制造登记不进仓的无效渲染。"""
    from krok_helper.subtitle_render.frontend.preview.preview_async import (
        native_due_queue_capacity,
    )

    # 60fps：容忍窗 120ms ≈ 8 帧 < 仓容量，钳制不生效。
    assert native_due_queue_capacity(120.0, 60, 25) == 8
    # 默认 25 槽仓下 120fps 容忍窗 15 装得下，钳制不生效。
    assert native_due_queue_capacity(120.0, 120, 25) == 15
    # env 缩仓（如 12）时 120fps 容忍窗 15 > 12 → 钳到仓容量。
    assert native_due_queue_capacity(120.0, 120, 12) == 12
    # env 覆盖仓容量后随动。
    assert native_due_queue_capacity(120.0, 120, 3) == 3
    # 仓容量 1 时队列也钳到 1：单槽仓渲染下一帧必然顶掉未呈现帧。
    assert native_due_queue_capacity(120.0, 60, 1) == 1


def test_gpu_native_paused_dropped_present_is_frame_drop(qapp, monkeypatch):
    """G6 暂停态 present 帧仓未命中：按丢帧收场——不上屏、不记账、不进失败链。

    帧仓语义（2026-10 用户拍板）：present 吐出的像素必须属于它宣称的
    (generation, t_ms)；仓里没有这帧就丢帧，屏幕延续上一帧，绝不允许
    单纹理时代「拿最新渲染结果冒充到点帧」的错帧上屏。
    """
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    presents: list[dict] = []
    presented_signals: list[int] = []

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "native_preview": True}

        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}

        def render_gpu_frame_direct(self, t_ms, **kwargs):
            return {
                "ok": True,
                "event": "gpu_frame_rendered_direct",
                "t_ms": int(t_ms),
                "render_ms": 1.0,
            }

        def present_rendered_gpu_frame(self, **kwargs):
            presents.append(dict(kwargs))
            return {
                "ok": True,
                "event": "gpu_frame_dropped",
                "dropped": True,
                "t_ms": int(kwargs.get("t_ms", 0)),
                "generation": int(kwargs.get("generation", 0)),
            }

        def close(self):
            pass

    monkeypatch.setattr(pa, "gpu_native_preview_enabled", lambda: True)
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    renderer.frame_presented.connect(presented_signals.append)
    try:
        renderer.set_native_target(12345, 0, 0, 320, 180)
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1000)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not presents:
            qapp.processEvents()
            time.sleep(0.01)
        assert presents, "暂停态应已完成一次 render+present 尝试"
        # present 必须携带帧身份（帧仓按 (generation, t_ms) 取帧）。
        assert "t_ms" in presents[0] and "generation" in presents[0]
        stats = renderer.stats_snapshot()
        assert stats["stale_frames_dropped"] >= 1
        assert stats["renderer_failures"] == 0
        assert presented_signals == [], "丢帧不得发 frame_presented"
    finally:
        renderer.stop()


def test_gpu_native_due_scheduler_dropped_present_keeps_bookkeeping_monotonic(
    qapp, monkeypatch
):
    """播放态帧仓丢帧：跳过该拍、成功呈现的帧序严格单调、不进重启链。"""
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack

    lock = threading.Lock()
    renders: list[int] = []
    successes: list[int] = []
    drops: list[int] = []
    call_count = {"n": 0}

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "native_preview": True}

        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}

        def render_gpu_frame_direct(self, t_ms, **kwargs):
            with lock:
                renders.append(int(t_ms))
            return {
                "ok": True,
                "event": "gpu_frame_rendered_direct",
                "t_ms": int(t_ms),
                "render_ms": 1.0,
            }

        def present_rendered_gpu_frame(self, **kwargs):
            t = int(kwargs.get("t_ms", 0))
            with lock:
                call_count["n"] += 1
                drop = call_count["n"] % 3 == 0
                (drops if drop else successes).append(t)
            if drop:
                return {
                    "ok": True,
                    "event": "gpu_frame_dropped",
                    "dropped": True,
                    "t_ms": t,
                    "generation": int(kwargs.get("generation", 0)),
                }
            return {
                "ok": True,
                "event": "gpu_frame_presented",
                "t_ms": t,
                "render_ms": 0.0,
                "present_ms": 0.2,
                "readback_ms": 0.0,
                "child_hwnd": 1,
                "transport": "direct_composition",
            }

        def close(self):
            pass

    monkeypatch.setattr(pa, "gpu_native_preview_enabled", lambda: True)
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_native_target(12345, 0, 0, 320, 180)
        renderer.set_state(TimingTrack(), Style())
        renderer.set_playing(True)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.2:
            renderer.request(60_000 + int((time.monotonic() - t0) * 1000.0))
            qapp.processEvents()
            time.sleep(0.016)
        time.sleep(0.2)
        qapp.processEvents()

        assert len(drops) >= 3, "测试期内应制造过丢帧"
        assert len(successes) >= 5, "丢帧不阻断后续到点呈现"
        # 成功呈现的帧序严格递增：丢帧只跳拍，记账不倒退（回退根因回归）。
        assert all(
            successes[i] < successes[i + 1] for i in range(len(successes) - 1)
        ), f"成功呈现帧序倒退: {successes}"
        # 每个被成功呈现的 t 都确实渲染过（present 忠实于存在的帧）。
        rendered_set = set(renders)
        assert all(t in rendered_set for t in successes)
        stats = renderer.stats_snapshot()
        assert stats["renderer_failures"] == 0
        assert stats["stale_frames_dropped"] >= len(drops)
    finally:
        renderer.stop()


def test_gpu_native_frame_store_default_matches_g5_cache_formula(qapp, monkeypatch):
    """帧仓默认容量与 G5 帧缓存同口径：max_lookahead(24)+native 单 worker(1)+1 = 25。

    两侧（Python env 解析 / sidecar Impl）必须同 env 同默认，否则调度队列
    与仓容量失配：队列按 Python 侧容量放行、仓按 C++ 侧容量登记。
    """
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa

    monkeypatch.delenv("KROK_SUBTITLE_GPU_FRAME_STORE", raising=False)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        assert renderer._native_frame_store_capacity == 25  # noqa: SLF001
    finally:
        renderer.stop()


def test_gpu_native_frame_failure_keeps_sidecar_alive(qapp, monkeypatch):
    """G6 帧级瞬态失败不杀 sidecar（频闪三笔之三，2026-10）。

    杀进程 = DComp 子窗口随进程销毁，字幕层整层消失、1 秒重启后闪现——
    低配机上反复发生就是频闪。帧级失败（streak 耗尽进 renderer_failed）
    必须保留进程与子窗口，屏幕冻结在最后一帧呈现上；断路器熔断与
    configure 阶段失败（楔死信号）才走杀进程链。
    """
    from krok_helper.subtitle_render.frontend.preview import preview_async as pa
    from krok_helper.subtitle_render.domain.models import Style, TimingTrack
    from krok_helper.subtitle_render.native.backend import NativeRendererError

    close_calls = {"n": 0}

    class FakeGpuProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return {"ok": True, "event": "ready"}

        def configure_gpu(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "native_preview": True}

        def resize_gpu_target(self, *args, **kwargs):
            return {"ok": True, "event": "gpu_configured", "worker_count": 1}

        def render_gpu_frame_direct(self, t_ms, **kwargs):
            raise NativeRendererError("bounded frame timeout (simulated)")

        def close(self):
            close_calls["n"] += 1

    monkeypatch.setattr(pa, "gpu_native_preview_enabled", lambda: True)
    monkeypatch.setattr(pa, "NativeRendererProcess", FakeGpuProcess)
    renderer = pa.GpuAsyncSubtitleRenderer(320, 180)
    try:
        renderer.set_native_target(12345, 0, 0, 320, 180)
        renderer.set_state(TimingTrack(), Style())
        renderer.request(1000)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            qapp.processEvents()
            if renderer.stats_snapshot()["renderer_failures"] >= 1:
                break
            time.sleep(0.01)
        stats = renderer.stats_snapshot()
        assert stats["renderer_failures"] >= 1, "应到达失败链"
        # 帧级 streak 重试链先走满（streak 1..4），第 5 次失败才进失败链。
        assert stats["frame_error_retries"] >= 4
        assert close_calls["n"] == 0, "G6 帧级失败不得杀 sidecar（子窗口须存活）"
        assert stats["gpu_circuit_open"] == 0, "单次失败不应熔断"
    finally:
        renderer.stop()


def test_clear_async_image_is_noop_without_residual_image():
    """clear_async_image 空图零操作（频闪三笔之一，2026-10）。

    G6 到点呈现每拍都清一次：空图时再无条件 update() 会让视口按呈现节拍
    整块重绘（含视频区域），弱合成器上表现为频闪。真有 CPU 残留图时仍须
    清掉并重绘一次（双绘防护语义不变）。
    """
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.frontend.preview.preview_graphics import (
        SubtitleGraphicsItem,
    )

    item = SubtitleGraphicsItem(320, 180)
    updates = {"n": 0}
    original_update = item.update

    def counting_update(*args, **kwargs):
        updates["n"] += 1
        return original_update(*args, **kwargs)

    item.update = counting_update
    # 空图清零：零操作。
    item.clear_async_image()
    assert updates["n"] == 0
    # 有残留图：清掉并触发一次重绘（G5→G6 切换/失败恢复的 CPU 幽灵帧防护）。
    item.set_async_image(QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied))
    updates["n"] = 0
    item.clear_async_image()
    assert updates["n"] == 1
    assert item._async_image is None  # noqa: SLF001
