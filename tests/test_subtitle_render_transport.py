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


def test_preview_fps_label_updates_from_painted_frames(qapp):
    bar = _bar(qapp)

    class FakeTimer:
        def __init__(self):
            self.elapsed_value = 1000

        def isValid(self):
            return True

        def start(self):
            return None

        def elapsed(self):
            return self.elapsed_value

        def restart(self):
            self.elapsed_value = 0

    bar._fps_timer = FakeTimer()
    bar.note_preview_frame_painted()

    assert bar._fps_label.text() == "FPS 01"


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
