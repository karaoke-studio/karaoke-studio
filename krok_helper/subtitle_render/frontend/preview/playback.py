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
from PyQt6.QtMultimedia import (
    QAudioOutput, QMediaFormat, QMediaMetaData, QMediaPlayer, QPlaybackOptions,
)

from krok_helper.qt_audio import follow_default_audio_output
from krok_helper.subtitle_render.frontend.preview import preview_media


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
        follow_default_audio_output(self._audio_out)
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
        self._player.errorOccurred.connect(self._on_player_error)

    # ------------------------------------------------------------------ media
    def set_media(self, path: Optional[Path]) -> None:
        """设 source 为承载音频的文件；非法 / None → 清空。切源后回到 0。"""
        if self._media_path != path:
            preview_media.unprotect_preview_source(self._media_path)
            preview_media.protect_preview_source(path)
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
        # 停止是用户的明确意图：丢弃挂起的换源恢复，否则 position()/is_playing()
        # 会冻结在旧目标上，且恢复补位还会在下一个 LoadedMedia 把刚停下的
        # 播放重新拉起来（2026-11 代码评审发现）。
        self._source_restore = None

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
        """Release media decoding and any preview transcode before host exit."""
        self._cancel_proxy_preparation()
        self._player.stop()
        self._player.setSource(QUrl())
        self._player.setAudioOutput(None)
        self._has_media = False
        self._media_path = None
        self._active_playback_path = None
        self._source_restore = None
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
            # 同路径重设（重选同一背景/复用命中缓存的代理）：旧恢复若还挂着，
            # 必须按新目标重写——否则 position() 冻结在旧目标、低于旧目标的
            # 位置事件全部被吞，时间轴被钉死在换源点（2026-11 代码评审发现）。
            self._source_restore = (target_position, bool(resume))
            self._player.setPosition(target_position)
            if resume:
                self._player.play()
            return
        self._source_restore = (target_position, bool(resume))
        self._active_playback_path = path
        options = self._player.playbackOptions()
        options.resetPlaybackIntent()
        self._player.setPlaybackOptions(options)
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
        if restore is not None:
            if int(ms) < restore[0] - 80:
                # 未恢复到目标点的迟到位置（重载回 0 等）不上抛。
                return
            if (
                not restore[1]
                or self._player.playbackState()
                == QMediaPlayer.PlaybackState.PlayingState
            ):
                # 已到/过恢复点（resume 时还需真正在播）：恢复完成。
                self._source_restore = None
        self.positionChanged.emit(int(ms))

    def _on_media_status_changed(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            # 播放到末尾：挂起的换源恢复不会再有 Loaded/Buffered 确认，丢弃
            # 以免 position()/is_playing() 冻结在旧目标（2026-11 代码评审发现）。
            self._source_restore = None
            return
        if status not in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            return
        if self._apply_vp9_decoder_workaround():
            return
        restore = self._source_restore
        if restore is None:
            return
        position_ms, resume = restore
        if not resume:
            # 静默换源 / 首次加载：加载完成即恢复完成（旧语义）。位置没对齐
            # 才补一次 seek；不在播放中，恢复不能再武装——否则 positionChanged
            # 的首个事件可能直接越过 80ms 确认窗，恢复永远无法确认。
            if int(self._player.position()) < position_ms - 80:
                self._player.setPosition(position_ms)
            self._source_restore = None
            return
        # 播放中换源（resume）：恢复不能在首次 LoadedMedia 就销毁（2026-11
        # 播放中代理换源回退首撞）——Qt FFmpeg 后端在 Buffering 中再 setPosition
        # 会触发一次内部重载，第二次 LoadingMedia 把 position 冲回 0，提前
        # 消费恢复会让播放直接从头重播。这里每次到达 Loaded/Buffered 都幂等
        # 补位 + 续播，恢复保持武装直到 _on_position_changed 确认「播放已到/
        # 过恢复点」。已在目标点前方时不再回 seek，避免反复回拽。
        if int(self._player.position()) < position_ms - 80:
            self._player.setPosition(position_ms)
        if self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self._player.play()

    def _on_player_error(self, _error) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            # 瞬时错误（坏包等）：播放仍在继续，恢复必须保持武装——后续
            # Loaded/Buffered 仍会补位；此刻丢弃会让内部重载落回 0 后无人
            # 纠正，等于把「从头重播」的老 bug 经由良性信号复活（2026-11
            # 代码评审发现）。
            return
        # 真正的加载失败不会有 LoadedMedia，恢复目标永远无法确认；继续武装
        # 会让 position()/is_playing() 冻结在旧目标上，把跟随音频的 UI 时钟
        # 永久拽死在换源点（2026-11 复现的「播放中卡死」形态）。丢弃恢复，
        # 让播放器实际状态成为唯一事实。
        self._source_restore = None

    def _apply_vp9_decoder_workaround(self) -> bool:
        """Avoid VP9 frame-thread reference retention in bundled FFmpeg 7.1.

        LOW_DELAY disables frame threading (slice threading and hardware decode
        remain available). This avoids old next_refs being revived by VP9's
        update_thread_context after a flush. Qt applies options on the next
        source load, so reopen once after the codec metadata becomes available.
        Other codecs keep their normal buffering/reordering policy.
        """
        if os.name != "nt" or self._active_playback_path is None:
            return False
        codec = self._player.metaData().value(QMediaMetaData.Key.VideoCodec)
        if codec != QMediaFormat.VideoCodec.VP9:
            return False
        options = self._player.playbackOptions()
        intent = QPlaybackOptions.PlaybackIntent.LowLatencyStreaming
        if options.playbackIntent() == intent:
            return False
        restore = self._source_restore or (self.position(), self.is_playing())
        source = self._player.source()
        self._source_restore = restore
        options.setPlaybackIntent(intent)
        self._player.setPlaybackOptions(options)
        # Emptying the source closes the decoder and releases its entire pool.
        # Setting the same URL directly would be a QMediaPlayer no-op.
        self._player.setSource(QUrl())
        self._player.setSource(source)
        return True

    def _on_state_changed(self, state) -> None:
        if (
            self._source_restore is not None
            and self._source_restore[1]
            and state != QMediaPlayer.PlaybackState.PlayingState
        ):
            return
        self.playbackStateChanged.emit(state == QMediaPlayer.PlaybackState.PlayingState)
