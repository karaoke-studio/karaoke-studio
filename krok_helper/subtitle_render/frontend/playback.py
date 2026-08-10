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

from PyQt6.QtCore import QObject, QProcess, QUrl, pyqtSignal as Signal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

from krok_helper.subtitle_render.frontend import preview_media


def unified_player_enabled() -> bool:
    """单播放器统一接线总开关（默认开，=0 回退旧三播放器路径）。

    真机交互验证（拖入视频 / 播放 / 拖动 / 切源 / 纯字幕）通过后于 2026-06-24 翻默认开。
    """
    return os.environ.get("KROK_SUBTITLE_UNIFIED_PLAYER", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
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
        self._media_path: Optional[Path] = None
        self._active_playback_path: Optional[Path] = None
        self._preview_quality = "high"
        self._source_restore: Optional[tuple[int, bool]] = None
        self._proxy_preparation: Optional[
            preview_media.QtPlaybackPreparation
        ] = None
        self._proxy_process: Optional[QProcess] = None
        # QMediaPlayer 的 position/duration 信号是 qint64，不能直接连到 int 信号；用 lambda 转发。
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(lambda ms: self.durationChanged.emit(int(ms)))
        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)

    # ------------------------------------------------------------------ media
    def set_media(self, path: Optional[Path]) -> None:
        """设 source 为承载音频的文件；非法 / None → 清空。切源后回到 0。"""
        self._cancel_proxy_preparation()
        if not _is_real_media_file(path):
            self._player.setSource(QUrl())
            self._has_media = False
            self._media_path = None
            self._active_playback_path = None
            self._source_restore = None
            return
        media_path = Path(path)
        self._media_path = media_path
        playback_path = preview_media.prepared_qt_playback_source(
            media_path,
            self._preview_quality,
        )
        self._set_player_source(playback_path or media_path, position_ms=0, resume=False)
        self._has_media = True
        if playback_path is None:
            self._start_proxy_preparation(media_path, self._preview_quality)

    def set_preview_quality(self, quality: object) -> None:
        """Select the cached 1080p/540p video source used only for preview."""
        normalized = preview_media.normalize_preview_media_quality(quality)
        if normalized == self._preview_quality:
            return
        self._preview_quality = normalized
        media_path = self._media_path
        if not self._has_media or media_path is None:
            return
        self._cancel_proxy_preparation()
        prepared = preview_media.prepared_qt_playback_source(media_path, normalized)
        if prepared is not None:
            self._set_player_source(
                prepared,
                position_ms=self.position(),
                resume=self.is_playing(),
            )
            return
        if normalized == "high":
            self._set_player_source(
                media_path,
                position_ms=self.position(),
                resume=self.is_playing(),
            )
        self._start_proxy_preparation(media_path, normalized)

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
            if self._source_restore is not None:
                self._source_restore = (self._source_restore[0], True)
            self._player.play()

    def pause(self) -> None:
        if self._source_restore is not None:
            self._source_restore = (self._source_restore[0], False)
        self._player.pause()

    def stop(self) -> None:
        self._player.stop()

    def is_playing(self) -> bool:
        if self._source_restore is not None and self._source_restore[1]:
            return True
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def seek(self, ms: int) -> None:
        target = max(0, int(ms))
        if self._source_restore is not None:
            self._source_restore = (target, self._source_restore[1])
        self._player.setPosition(target)

    def position(self) -> int:
        if self._source_restore is not None:
            return self._source_restore[0]
        return int(self._player.position())

    def duration(self) -> int:
        return int(self._player.duration())

    @property
    def media_player(self) -> QMediaPlayer:
        return self._player

    # ------------------------------------------------------------------ internal
    def shutdown(self) -> None:
        """Stop an in-progress preview transcode before the controller is destroyed."""
        self._cancel_proxy_preparation()
        if self._proxy_process is not None:
            self._proxy_process.deleteLater()
            self._proxy_process = None

    def _set_player_source(
        self,
        path: Path,
        *,
        position_ms: int,
        resume: bool,
    ) -> None:
        path = Path(path)
        target_position = max(int(position_ms), 0)
        if path == self._active_playback_path:
            self._player.setPosition(target_position)
            if resume:
                self._player.play()
            return
        self._source_restore = (target_position, bool(resume))
        self._active_playback_path = path
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.setPosition(target_position)
        if resume:
            self._player.play()

    def _start_proxy_preparation(self, path: Path, quality: str) -> None:
        preparation = preview_media.qt_playback_preparation(path, quality)
        if preparation is None:
            return
        self._proxy_preparation = preparation
        process = self._ensure_proxy_process()
        process.setProgram(preparation.command[0])
        process.setArguments(list(preparation.command[1:]))
        process.start()

    def _ensure_proxy_process(self) -> QProcess:
        process = self._proxy_process
        if process is not None:
            return process
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.started.connect(self._lower_proxy_process_priority)
        process.finished.connect(self._on_proxy_finished)
        process.errorOccurred.connect(self._on_proxy_error)
        self._proxy_process = process
        return process

    def _cancel_proxy_preparation(self) -> None:
        preparation = self._proxy_preparation
        self._proxy_preparation = None
        process = self._proxy_process
        if (
            process is not None
            and process.state() != QProcess.ProcessState.NotRunning
        ):
            process.kill()
            process.waitForFinished(500)
        if preparation is not None:
            preview_media.discard_qt_playback_preparation(preparation)

    def _lower_proxy_process_priority(self) -> None:
        if os.name != "nt":
            return
        process = self._proxy_process
        if process is None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            process_set_information = 0x0200
            below_normal_priority_class = 0x00004000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.SetPriorityClass.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                process_set_information,
                False,
                int(process.processId()),
            )
            if not handle:
                return
            try:
                kernel32.SetPriorityClass(handle, below_normal_priority_class)
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            pass

    def _on_proxy_error(self, error) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        preparation = self._proxy_preparation
        self._proxy_preparation = None
        if preparation is not None:
            preview_media.discard_qt_playback_preparation(preparation)

    def _on_proxy_finished(self, exit_code: int, _exit_status) -> None:
        preparation = self._proxy_preparation
        self._proxy_preparation = None
        if preparation is None:
            return
        if int(exit_code) != 0 or not preview_media.finalize_qt_playback_preparation(
            preparation
        ):
            preview_media.discard_qt_playback_preparation(preparation)
            return
        if preparation.source != self._media_path:
            return
        selected = preview_media.prepared_qt_playback_source(
            preparation.source,
            self._preview_quality,
        )
        if selected != preparation.target:
            return
        self._set_player_source(
            preparation.target,
            position_ms=self.position(),
            resume=self.is_playing(),
        )

    def _on_position_changed(self, ms: int) -> None:
        restore = self._source_restore
        if restore is not None and abs(int(ms) - restore[0]) > 80:
            return
        self.positionChanged.emit(int(ms))

    def _on_media_status_changed(self, status) -> None:
        if status not in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            return
        restore = self._source_restore
        if restore is None:
            return
        self._source_restore = None
        position_ms, resume = restore
        self._player.setPosition(position_ms)
        if resume:
            self._player.play()

    def _on_state_changed(self, state) -> None:
        if (
            self._source_restore is not None
            and self._source_restore[1]
            and state != QMediaPlayer.PlaybackState.PlayingState
        ):
            return
        self.playbackStateChanged.emit(state == QMediaPlayer.PlaybackState.PlayingState)
