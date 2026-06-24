"""单一共享媒体播放器（单播放器统一 步骤2，§10.9）。

统一前预览把同一文件用 **3 个** ``QMediaPlayer`` 各自解码（TransportBar 音频 /
PreviewGraphicsView 视频 / PreviewCanvas raster 视频），仅靠墙钟 + 漂移 seek 松散对齐
= 音画 / 字幕失步的根源。

``PlaybackController`` 持**唯一** ``QMediaPlayer`` + ``QAudioOutput``：

- ``set_media(path)``：source = 承载可听音频的文件。导入视频 → 该视频本身（含音视频，A/V 由
  播放器**天然锁帧**）；将来「图片 + 音频」一图流 → 音频文件（无视频流，背景静止图不参与同步）。
  视频也可能无音频流（静音播放），同样由这一个播放器驱动。
- ``set_video_output`` / ``set_video_sink``：若媒体含视频，把视频喂到预览显示。
- ``position`` / ``duration`` / ``play`` / ``pause`` / ``seek``：供 TransportBar 做传输，
  不再另起音频 player；预览也不再自建视频 player（其 ``set_time`` 只驱动字幕层）。

flag ``KROK_SUBTITLE_UNIFIED_PLAYER`` 默认关；开启后由 ``main_window`` 走单播放器接线，
旧的三播放器路径原样保留以便 A/B 与回退（§7）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QUrl, pyqtSignal as Signal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

from krok_helper.subtitle_render.frontend.preview_media import qt_playback_source


def unified_player_enabled() -> bool:
    """单播放器统一接线总开关（默认关，开启后 main_window 走 PlaybackController）。"""
    return os.environ.get("KROK_SUBTITLE_UNIFIED_PLAYER", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _is_real_media_file(path: Optional[Path]) -> bool:
    if path is None:
        return False
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


class PlaybackController(QObject):
    """持有唯一 QMediaPlayer 的播放控制器；TransportBar 与预览共用它。"""

    positionChanged = Signal(int)
    """``QMediaPlayer.position()`` 变化（粒度粗，仅供 TransportBar 做漂移锚定/反馈）。"""
    durationChanged = Signal(int)
    playbackStateChanged = Signal(bool)
    """``True`` = 正在播放（把 Qt 三态映射成布尔，与 TransportBar 既有约定一致）。"""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_out)
        self._has_media = False
        # QMediaPlayer 的 position/duration 信号是 qint64，不能直接连到 int 信号；用 lambda 转发。
        self._player.positionChanged.connect(lambda ms: self.positionChanged.emit(int(ms)))
        self._player.durationChanged.connect(lambda ms: self.durationChanged.emit(int(ms)))
        self._player.playbackStateChanged.connect(self._on_state_changed)

    # ------------------------------------------------------------------ media
    def set_media(self, path: Optional[Path]) -> None:
        """设 source 为承载音频的文件；非法 / None → 清空。切源后回到 0。"""
        if not _is_real_media_file(path):
            self._player.setSource(QUrl())
            self._has_media = False
            return
        playback_path = qt_playback_source(path)
        self._player.setSource(QUrl.fromLocalFile(str(playback_path)))
        self._has_media = True
        self._player.setPosition(0)

    def has_media(self) -> bool:
        return self._has_media

    def set_video_output(self, output) -> None:
        """把视频输出接到 QGraphicsVideoItem（graphics 预览）。"""
        self._player.setVideoOutput(output)

    def set_video_sink(self, sink) -> None:
        """把视频输出接到 QVideoSink（raster 预览）。"""
        self._player.setVideoSink(sink)

    def set_volume(self, volume: float) -> None:
        self._audio_out.setVolume(max(0.0, min(1.0, float(volume))))

    # ------------------------------------------------------------------ transport
    def play(self) -> None:
        if self._has_media:
            self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def seek(self, ms: int) -> None:
        self._player.setPosition(max(0, int(ms)))

    def position(self) -> int:
        return int(self._player.position())

    def duration(self) -> int:
        return int(self._player.duration())

    @property
    def media_player(self) -> QMediaPlayer:
        return self._player

    # ------------------------------------------------------------------ internal
    def _on_state_changed(self, state) -> None:
        self.playbackStateChanged.emit(state == QMediaPlayer.PlaybackState.PlayingState)
