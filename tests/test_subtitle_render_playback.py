"""PlaybackController（单播放器统一 步骤2）单元测试。

真实音视频解码在 CI 不稳定，这里聚焦无需真实播放即可验证的契约：
flag 解析、媒体文件有效性判定、source 切换 / has_media、音量钳制、
播放状态三态→布尔映射、seek/position 不崩。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QUrl  # noqa: E402
from PyQt6.QtMultimedia import QMediaPlayer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend.playback import (  # noqa: E402
    PlaybackController,
    _is_real_media_file,
    unified_player_enabled,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _new_controller(controllers: list[PlaybackController]) -> PlaybackController:
    ctrl = PlaybackController()
    controllers.append(ctrl)
    return ctrl


@pytest.fixture
def make_controller(qapp):  # noqa: ARG001
    """创建 controller 并在测试后确定性释放底层媒体对象（避免 3.14 退出期段错误）。"""
    controllers: list[PlaybackController] = []
    yield lambda: _new_controller(controllers)
    for ctrl in controllers:
        player = ctrl.media_player
        player.stop()
        player.setSource(QUrl())
        player.setAudioOutput(None)  # type: ignore[arg-type]
        player.deleteLater()
    qapp.processEvents()


def test_unified_player_flag(monkeypatch):
    monkeypatch.delenv("KROK_SUBTITLE_UNIFIED_PLAYER", raising=False)
    assert unified_player_enabled() is True  # 默认开
    for v in ("1", "true", "on", "YES"):
        monkeypatch.setenv("KROK_SUBTITLE_UNIFIED_PLAYER", v)
        assert unified_player_enabled() is True
    for v in ("0", "false", "off"):
        monkeypatch.setenv("KROK_SUBTITLE_UNIFIED_PLAYER", v)
        assert unified_player_enabled() is False


def test_is_real_media_file(tmp_path):
    assert _is_real_media_file(None) is False
    assert _is_real_media_file(tmp_path / "nope.mp4") is False
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert _is_real_media_file(empty) is False
    real = tmp_path / "x.wav"
    real.write_bytes(b"placeholder")
    assert _is_real_media_file(real) is True


def test_set_media_toggles_has_media(make_controller, tmp_path):
    ctrl = make_controller()
    assert ctrl.has_media() is False
    real = tmp_path / "song.wav"
    real.write_bytes(b"placeholder")
    ctrl.set_media(real)
    assert ctrl.has_media() is True
    ctrl.set_media(None)
    assert ctrl.has_media() is False
    # 非法路径也清空
    ctrl.set_media(tmp_path / "missing.mp4")
    assert ctrl.has_media() is False


def test_set_volume_clamps(make_controller):
    ctrl = make_controller()
    ctrl.set_volume(2.0)
    assert ctrl._audio_out.volume() == pytest.approx(1.0)  # noqa: SLF001
    ctrl.set_volume(-1.0)
    assert ctrl._audio_out.volume() == pytest.approx(0.0)  # noqa: SLF001
    ctrl.set_volume(0.5)
    assert ctrl._audio_out.volume() == pytest.approx(0.5)  # noqa: SLF001


def test_playback_state_maps_to_bool(make_controller):
    ctrl = make_controller()
    seen: list[bool] = []
    ctrl.playbackStateChanged.connect(seen.append)
    ctrl._on_state_changed(QMediaPlayer.PlaybackState.PlayingState)  # noqa: SLF001
    ctrl._on_state_changed(QMediaPlayer.PlaybackState.PausedState)  # noqa: SLF001
    ctrl._on_state_changed(QMediaPlayer.PlaybackState.StoppedState)  # noqa: SLF001
    assert seen == [True, False, False]


def test_play_noop_without_media(make_controller):
    ctrl = make_controller()
    # 无媒体时 play 不应进入播放态（避免空 source 的后端噪声）
    ctrl.play()
    assert ctrl.is_playing() is False


def test_seek_and_position_do_not_crash(make_controller):
    ctrl = make_controller()
    ctrl.seek(5_000)
    assert isinstance(ctrl.position(), int)
    assert isinstance(ctrl.duration(), int)
