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
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QUrl  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
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
        assert graphics._video_item.pos().x() < 0
        assert graphics._video_item.size().width() > graphics._output_w
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


def test_async_preview_target_size_uses_device_pixel_ratio():
    from krok_helper.subtitle_render.frontend.preview_async import preview_render_target_size

    assert preview_render_target_size(1920, 1080, 1.25) == (2400, 1350, 1.25)
    assert preview_render_target_size(0, 0, 0) == (1, 1, 1.0)
    assert preview_render_target_size(1, 1, -1.0) == (1, 1, 0.01)


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


def test_native_preview_enabled_requires_env_and_sidecar(monkeypatch, tmp_path):
    from krok_helper.subtitle_render.frontend import preview_async as pa

    sidecar = tmp_path / "krok_subtitle_renderer.exe"
    sidecar.write_bytes(b"placeholder")
    monkeypatch.setattr(pa, "resolve_native_renderer_path", lambda: sidecar)

    monkeypatch.delenv("KROK_SUBTITLE_NATIVE_RENDER", raising=False)
    assert pa.native_preview_enabled() is False

    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_RENDER", "1")
    assert pa.native_preview_enabled() is True

    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_RENDER", "0")
    assert pa.native_preview_enabled() is False

    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_RENDER", "1")
    monkeypatch.setattr(pa, "resolve_native_renderer_path", lambda: None)
    assert pa.native_preview_enabled() is False


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


def test_play_button_text_reflects_state(qapp):
    bar = _bar(qapp)
    assert bar._play_btn.text() == "▶"
    bar.play()
    assert bar._play_btn.text() == "⏸"
    bar.pause()
    assert bar._play_btn.text() == "▶"


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

    def has_media(self) -> bool:
        return self._has

    def set_media(self, path) -> None:
        self._has = path is not None

    def play(self) -> None:
        self._playing = True

    def pause(self) -> None:
        self._playing = False

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
