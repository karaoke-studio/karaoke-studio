"""TransportBar 播放控制测试。

QMediaPlayer 的真实音频播放在 CI 不稳定，所以这里聚焦：

- play / pause / toggle_play 切按钮文字与播放状态
- 无音频时的 QTimer 视觉 tick 路径（直接调 ``_on_tick`` 模拟）
- ``set_audio_source`` 把 QMediaPlayer 切到音频路径
- ``timeChanged`` 信号在播放 / 拖动 / set_time 三个来源都能触发
- 抑制反馈环（_suppress_seek）：模拟 player.positionChanged 不会回写到 player
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend.preview_view import (  # noqa: E402
    PreviewCanvas,
    TransportBar,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _bar(qapp) -> TransportBar:
    bar = TransportBar()
    bar.set_duration(60_000)
    return bar


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


def test_set_audio_source_none_clears_player(qapp, tmp_path):
    bar = _bar(qapp)
    fake = tmp_path / "song.wav"
    fake.write_bytes(b"placeholder")
    bar.set_audio_source(fake)
    assert bar._has_audio
    bar.set_audio_source(None)
    assert not bar._has_audio


def test_audio_playback_clock_uses_elapsed_timer(qapp, monkeypatch, tmp_path):
    """有音频播放时 UI 时间用 60fps elapsed clock，不采样粗粒度 player.position。"""
    bar = _bar(qapp)
    fake = tmp_path / "song.wav"
    fake.write_bytes(b"placeholder")
    bar.set_audio_source(fake)
    assert bar._player is not None

    bar._player.position = lambda: 100  # type: ignore[assignment]
    bar.set_time(1_000)
    bar.play()
    monkeypatch.setattr(bar._tick_anchor_real, "elapsed", lambda: 240)

    bar._on_audio_clock_tick()

    assert bar.current_time_ms == 1_240
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
