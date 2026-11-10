"""PlaybackController（单播放器统一 步骤2）单元测试。

真实音视频解码在 CI 不稳定，这里聚焦无需真实播放即可验证的契约：
flag 解析、媒体文件有效性判定、source 切换 / has_media、音量钳制、
播放状态三态→布尔映射、seek/position 不崩。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QUrl  # noqa: E402
from PyQt6.QtMultimedia import QMediaPlayer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend.preview.playback import (  # noqa: E402
    PlaybackController,
    _is_real_media_file,
    unified_player_enabled,
)
from krok_helper.subtitle_render.frontend.preview import playback  # noqa: E402


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
        ctrl.shutdown()
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


def test_shutdown_clears_decoder_source(make_controller, tmp_path):
    ctrl = make_controller()
    media = tmp_path / "preview.wav"
    media.write_bytes(b"placeholder")
    ctrl.set_media(media)
    assert not ctrl.media_player.source().isEmpty()
    ctrl.shutdown()
    ctrl.shutdown()
    assert ctrl.media_player.source().isEmpty()
    assert ctrl.media_player.audioOutput() is None
    assert not ctrl.has_media()


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


def test_preview_quality_switches_shared_player_to_cached_proxy(
    make_controller, monkeypatch, tmp_path
):
    ctrl = make_controller()
    source = tmp_path / "source.mp4"
    proxy = tmp_path / "source-540p.mp4"
    source.write_bytes(b"source")
    proxy.write_bytes(b"proxy")

    def prepared(path, quality="high"):
        return proxy if quality == "low" else Path(path)

    selected: list[Path] = []
    monkeypatch.setattr(
        playback.preview_media,
        "prepared_qt_playback_source",
        prepared,
    )
    monkeypatch.setattr(
        ctrl,
        "_set_player_source",
        lambda path, **_kwargs: selected.append(Path(path)),
    )

    ctrl.set_media(source)
    ctrl.set_preview_quality("low")

    assert selected == [source, proxy]


def test_preview_quality_cache_miss_keeps_current_source_while_preparing(
    make_controller, monkeypatch, tmp_path
):
    ctrl = make_controller()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    requested: list[tuple[Path, str]] = []
    selected: list[Path] = []
    monkeypatch.setattr(
        playback.preview_media,
        "prepared_qt_playback_source",
        lambda path, quality="high": Path(path) if quality == "high" else None,
    )
    monkeypatch.setattr(
        ctrl,
        "_start_proxy_preparation",
        lambda path, quality: requested.append((Path(path), quality)),
    )
    monkeypatch.setattr(
        ctrl,
        "_set_player_source",
        lambda path, **_kwargs: selected.append(Path(path)),
    )

    ctrl.set_media(source)
    ctrl.set_preview_quality("medium")

    assert selected == [source]
    assert requested == [(source, "medium")]


class _RecordingPlayer:
    """PlaybackController 各 handler 用到的 QMediaPlayer 最小面。

    直接替换 controller._player 驱动 handler 级单测（构造时连接的信号落在
    真播放器上，测试里手工调用 handler 即可）。
    """

    def __init__(self):
        self._position = 0
        self._state = QMediaPlayer.PlaybackState.StoppedState
        self.seeks: list[int] = []
        self.plays = 0

    def position(self) -> int:
        return self._position

    def playbackState(self):
        return self._state

    def setPosition(self, ms: int) -> None:
        self.seeks.append(int(ms))
        self._position = int(ms)

    def play(self) -> None:
        self.plays += 1

    # teardown 兜底用的 no-op 面
    def stop(self) -> None: ...
    def setSource(self, _url) -> None: ...
    def setAudioOutput(self, _output) -> None: ...
    def deleteLater(self) -> None: ...


def _armed_controller(make_controller, *, target_ms: int, resume: bool):
    ctrl = make_controller()
    player = _RecordingPlayer()
    ctrl._player = player  # noqa: SLF001
    ctrl._source_restore = (target_ms, resume)  # noqa: SLF001
    return ctrl, player


def test_resume_restore_survives_backend_reload(make_controller):
    """播放中换源：后端二次重载把 position 冲回 0 时，恢复要能补回来。

    2026-11 真机首撞：代理转码完成后播放中换源，首次 LoadedMedia 就销毁
    恢复 + Buffering 中再 seek 触发 FFmpeg 后端二次重载（第二次 LoadingMedia
    落回 pos=0），播放直接从头重播。
    """
    ctrl, player = _armed_controller(make_controller, target_ms=68150, resume=True)
    emitted: list[int] = []
    ctrl.positionChanged.connect(emitted.append)

    # 首次 LoadedMedia：位置已在目标点（setSource 后的 setPosition 生效过）
    # → 不再回 seek（避免主动触发重载），只补播放。
    player._position = 68150  # noqa: SLF001
    ctrl._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)  # noqa: SLF001
    assert player.seeks == []
    assert player.plays == 1
    assert ctrl._source_restore == (68150, True)  # noqa: SLF001 — 恢复保持武装

    # 后端内部重载：position 冲回 0、再次到 LoadedMedia → 幂等补位 + 续播。
    player._position = 0  # noqa: SLF001
    ctrl._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)  # noqa: SLF001
    assert player.seeks == [68150]
    assert player.plays == 2

    # 播放确认回到目标点：恢复完成，位置事件恢复上抛。
    player._state = QMediaPlayer.PlaybackState.PlayingState  # noqa: SLF001
    player._position = 68200  # noqa: SLF001
    ctrl._on_position_changed(68200)  # noqa: SLF001
    assert ctrl._source_restore is None  # noqa: SLF001
    assert emitted == [68200]
    # 确认后的正常位置事件不再受恢复闸门影响。
    ctrl._on_position_changed(68700)  # noqa: SLF001
    assert emitted == [68200, 68700]


def test_resume_restore_gates_backward_positions_until_confirmed(make_controller):
    """恢复武装期间，重载回 0 的迟到位置不能上抛（UI 时钟不能被拽回起点）。"""
    ctrl, player = _armed_controller(make_controller, target_ms=68150, resume=True)
    emitted: list[int] = []
    ctrl.positionChanged.connect(emitted.append)

    ctrl._on_position_changed(0)  # noqa: SLF001 — 重载落回 0
    ctrl._on_position_changed(500)  # noqa: SLF001
    assert emitted == []
    assert ctrl._source_restore == (68150, True)  # noqa: SLF001


def test_resume_restore_drops_on_player_error(make_controller):
    """换源后加载失败：恢复必须丢弃，position()/is_playing() 不能冻结。"""
    ctrl, player = _armed_controller(make_controller, target_ms=68150, resume=True)
    ctrl._on_player_error(QMediaPlayer.Error.ResourceError)  # noqa: SLF001
    assert ctrl._source_restore is None  # noqa: SLF001
    assert ctrl.position() == player.position()
    assert ctrl.is_playing() is False


def test_silent_restore_completes_at_load(make_controller):
    """静默换源 / 首次加载：LoadedMedia 即完成（旧语义），不等待播放确认。

    若 resume=False 也保持武装，positionChanged 的首个事件（往往已越过
    80ms 确认窗）会被永久吞掉。
    """
    ctrl, player = _armed_controller(make_controller, target_ms=0, resume=False)
    emitted: list[int] = []
    ctrl.positionChanged.connect(emitted.append)

    player._position = 0  # noqa: SLF001
    ctrl._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)  # noqa: SLF001
    assert player.seeks == []
    assert ctrl._source_restore is None  # noqa: SLF001
    # 恢复完成后，位置事件（包括距离旧目标很远的）正常上抛。
    ctrl._on_position_changed(25123)  # noqa: SLF001
    assert emitted == [25123]


def test_silent_restore_seeks_when_load_lands_before_target(make_controller):
    ctrl, player = _armed_controller(make_controller, target_ms=5000, resume=False)
    player._position = 0  # noqa: SLF001
    ctrl._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)  # noqa: SLF001
    assert player.seeks == [5000]
    assert ctrl._source_restore is None  # noqa: SLF001



@pytest.mark.parametrize("codec_name", ["VP9", "H264"])
def test_vp9_decoder_workaround_reopens_once_and_preserves_transport(codec_name):
    from types import SimpleNamespace
    from PyQt6.QtMultimedia import QMediaFormat, QMediaMetaData, QPlaybackOptions

    class Player:
        def __init__(self):
            self.options = QPlaybackOptions()
            self.url = QUrl.fromLocalFile("C:/preview.mp4")
            self.sources = []
        def metaData(self):
            metadata = QMediaMetaData()
            metadata.insert(QMediaMetaData.Key.VideoCodec,
                            getattr(QMediaFormat.VideoCodec, codec_name))
            return metadata
        def playbackOptions(self):
            return QPlaybackOptions(self.options)
        def setPlaybackOptions(self, options):
            self.options = QPlaybackOptions(options)
        def source(self):
            return self.url
        def setSource(self, source):
            self.sources.append(source)
            self.url = source

    player = Player()
    ctrl = SimpleNamespace(_player=player, _active_playback_path=Path("C:/preview.mp4"),
                           _source_restore=None, position=lambda: 4321,
                           is_playing=lambda: True)
    changed = PlaybackController._apply_vp9_decoder_workaround(ctrl)
    expected = os.name == "nt" and codec_name == "VP9"
    assert changed is expected
    if expected:
        assert player.sources == [QUrl(), QUrl.fromLocalFile("C:/preview.mp4")]
        assert ctrl._source_restore == (4321, True)
        assert player.options.playbackIntent() == QPlaybackOptions.PlaybackIntent.LowLatencyStreaming
        assert not PlaybackController._apply_vp9_decoder_workaround(ctrl)
        assert len(player.sources) == 2
    else:
        assert not player.sources
