"""Qt worker adapters used by the subtitle-render window."""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal as Signal
from PyQt6.QtGui import QImage, QImageReader

from krok_helper.errors import ExportCancelled
from krok_helper.ffmpeg import probe_media, terminate_process
from krok_helper.models import MediaInfo
from krok_helper.subtitle_render.domain.background import (
    BackgroundSource,
    background_sequence_frame_path,
    count_image_sequence_frames,
    infer_image_sequence_pattern,
)
from krok_helper.subtitle_render.frontend.preview.preview_media import (
    qt_playback_source,
)
from krok_helper.subtitle_render.engine.export.progress_watchdog import (
    ExportProgressMonitor,
    clear_active_export_monitor,
    set_active_export_monitor,
)
from krok_helper.subtitle_render.engine.export.render_job import RenderJob
from krok_helper.subtitle_render.engine.renderer import render_subtitle_video
from krok_helper.subtitle_render.project.store import save_recovery_project


class _N3ImportWorker(QObject):
    """Load an ``.n3proj`` (含字幕源 ``.sug`` 重建) outside the UI thread.

    ``loader`` 由主窗口注入（穿好进度回调），返回 :class:`N3ImportResult`；
    进度经 :attr:`progressChanged` 跨线程回到 UI，结果/错误经
    :attr:`succeeded` / :attr:`failed` 交还。读取/重建全程不碰任何 Qt 控件。
    """

    progressChanged = Signal(int, str)
    succeeded = Signal(object, object)  # (worker, N3ImportResult)
    failed = Signal(object, str)  # (worker, message)

    def __init__(self, path: Path, loader: Callable[[Path], object]) -> None:
        super().__init__()
        self.path = Path(path)
        self._loader = loader

    def run(self) -> None:
        try:
            result = self._loader(self.path)
        except (OSError, ValueError) as exc:
            self.failed.emit(self, str(exc))
            return
        self.succeeded.emit(self, result)


class _RecoverySaveWorker(QObject):
    """Persist one immutable recovery snapshot outside the UI thread."""

    saved = Signal(object, int, int, int, bool)
    failed = Signal(object, int, int, int, str)

    def __init__(
        self,
        path: Path,
        payload: dict,
        generation: int,
        revision: int,
        snapshot_id: int,
    ) -> None:
        super().__init__()
        self._path = path
        self._payload = payload
        self._generation = generation
        self._revision = revision
        self._snapshot_id = snapshot_id

    def run(self) -> None:
        try:
            try:
                written = save_recovery_project(self._path, self._payload)
            except (OSError, TypeError, ValueError) as exc:
                self.failed.emit(
                    self._path,
                    self._generation,
                    self._revision,
                    self._snapshot_id,
                    str(exc),
                )
                return
            self.saved.emit(
                self._path,
                self._generation,
                self._revision,
                self._snapshot_id,
                written,
            )
        finally:
            QThread.currentThread().quit()


class _MediaProbeWorker(QObject):
    """Probe handoff media in the background and return to the UI thread."""

    probed = Signal(object, object)  # (worker, MediaInfo | None)

    def __init__(self, ffprobe_path: str, media_path: Path, as_video: bool) -> None:
        super().__init__()
        self._ffprobe_path = ffprobe_path
        self.media_path = media_path
        self.as_video = as_video

    def run(self) -> None:
        try:
            info: Optional[MediaInfo]
            try:
                info = probe_media(self._ffprobe_path, self.media_path)
            except Exception:  # noqa: BLE001 - preserve the synchronous fallback path
                info = None
            self.probed.emit(self, info)
        finally:
            QThread.currentThread().quit()


class _RenderWorker(QObject):
    """Run one subtitle export while exposing Qt-safe progress signals."""

    progressChanged = Signal(int, int)
    logMessage = Signal(str)
    finished = Signal(Path)
    cancelled = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        job: RenderJob,
        ffmpeg_dir: Optional[Path],
        preview_image_path: Optional[Path] = None,
        preview_width: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._job = job
        self._ffmpeg_dir = ffmpeg_dir
        self._preview_image_path = preview_image_path
        self._preview_width = preview_width
        self._process: Optional[subprocess.Popen] = None
        self._cancel_requested = False

    def run(self) -> None:
        worker_log = logging.getLogger("krok_helper.subtitle_render.export")
        worker_log.info(
            "字幕视频导出开始 output=%s size=%sx%s fps=%s",
            self._job.output_path,
            self._job.width,
            self._job.height,
            self._job.fps,
        )

        def emit_log(message: str) -> None:
            worker_log.info("字幕视频导出: %s", message)
            self.logMessage.emit(message)

        # 心跳/看门狗：导出期间每 ~10 秒一条批次状态（阶段、帧数、最近吞吐、
        # ffmpeg -progress、sidecar 心跳），真停滞时才自动修复；导出结束先
        # 停掉它再发终态信号，避免心跳把「导出失败/已停止」文案覆盖掉。
        monitor = ExportProgressMonitor(logger=emit_log)
        set_active_export_monitor(monitor)
        monitor.start()
        outcome: tuple[str, object] | None = None
        try:
            output = render_subtitle_video(
                self._job,
                ffmpeg_dir=self._ffmpeg_dir,
                logger=emit_log,
                should_cancel=self.should_cancel,
                on_progress=self.progressChanged.emit,
                on_process_started=self._set_process,
                preview_image_path=self._preview_image_path,
                preview_width=self._preview_width,
            )
            outcome = ("finished", output)
        except ExportCancelled as exc:
            worker_log.info("字幕视频导出取消: %s", exc)
            outcome = ("cancelled", str(exc))
        except Exception as exc:  # noqa: BLE001
            worker_log.exception("字幕视频导出失败")
            outcome = ("failed", str(exc))
        finally:
            monitor.stop()
            clear_active_export_monitor(monitor)

        if outcome is None:  # pragma: no cover - defensive; try covers all paths
            outcome = ("failed", "导出未正常结束")
        kind, payload = outcome
        if kind == "finished":
            worker_log.info("字幕视频导出完成 output=%s", payload)
            self.finished.emit(payload)
            return
        if kind == "cancelled":
            self.cancelled.emit(str(payload))
            return
        self.failed.emit(str(payload))

    def cancel(self) -> None:
        self._cancel_requested = True
        process = self._process
        if process is not None:
            terminate_process(process)

    def should_cancel(self) -> bool:
        return self._cancel_requested

    def _set_process(self, process: Optional[subprocess.Popen]) -> None:
        self._process = process


class RollRefreshResult:
    """前黑/后黑联动重算的后台结果（纯数据，跨线程传回 GUI）。"""

    __slots__ = ("generation", "plan", "lanes")

    def __init__(self, generation: int, plan, lanes) -> None:
        self.generation = int(generation)
        self.plan = plan
        """展示时间线；``None`` = 计算失败，GUI 应回退同步重建。"""
        self.lanes = lanes
        """按展示偏移重建的轨道块；``plan is None`` 时无效。"""


class RollMediaLoadResult:
    """前黑/后黑素材异步读取结果（纯数据，跨线程回 GUI）。"""

    __slots__ = (
        "generation",
        "segment",
        "kind",
        "source",
        "source_duration_ms",
        "source_has_audio",
        "source_width",
        "source_height",
        "preview_frame_path",
        "preview_frame",
        "error",
    )

    def __init__(self, generation: int, segment: str, kind: str) -> None:
        self.generation = int(generation)
        self.segment = segment
        self.kind = kind
        self.source = None
        self.source_duration_ms = 0
        self.source_has_audio = False
        self.source_width = 0
        self.source_height = 0
        self.preview_frame_path = None
        self.preview_frame = None
        self.error = ""


class _RollMediaLoadWorker(QObject):
    """读取/探测前黑后黑素材并预热预览代理，全程不碰 GUI 控件。"""

    done = Signal(object)  # RollMediaLoadResult
    metadataReady = Signal(object)  # 探针/首帧信息已就绪（预览准备前）
    stageChanged = Signal(str)  # 当前加载阶段（中文提示）
    progressChanged = Signal(int, str, int)  # generation, segment, percent

    def __init__(
        self,
        generation: int,
        segment: str,
        kind: str,
        path: Path,
        *,
        ffprobe_path: str,
        preview_fps: int | float,
        preview_quality: str,
    ) -> None:
        super().__init__()
        self._generation = int(generation)
        self._segment = segment
        self._kind = kind
        self._path = Path(path)
        self._ffprobe_path = ffprobe_path
        self._preview_fps = max(float(preview_fps), 1.0)
        self._preview_quality = str(preview_quality)

    def run(self) -> None:
        result = RollMediaLoadResult(self._generation, self._segment, self._kind)
        try:
            if self._kind == "video":
                self._load_video(result)
            elif self._kind == "image_sequence":
                self._load_image_sequence(result)
            elif self._kind == "image":
                self._load_image(result)
            else:
                result.error = "未知的素材类型。"
        except Exception as exc:  # noqa: BLE001 - 错误文本回 GUI 提示
            result.error = str(exc) or exc.__class__.__name__
        self.done.emit(result)
        QThread.currentThread().quit()

    def _load_video(self, result: RollMediaLoadResult) -> None:
        self.stageChanged.emit("读取素材信息…")
        info = probe_media(self._ffprobe_path, self._path)
        if info is None or info.video_streams <= 0:
            result.error = f"该文件不含视频流：\n{self._path}"
            return
        result.source = BackgroundSource(kind="video", path=str(self._path))
        result.source_duration_ms = (
            max(int(round(info.duration * 1000)), 0) if info.duration > 0 else 0
        )
        result.source_has_audio = info.audio_streams > 0
        result.source_width = info.video_width or 0
        result.source_height = info.video_height or 0
        # 先回填面板并建立时间映射，再准备预览源/首帧。
        self.metadataReady.emit(result)
        # 复用普通背景的预览准备：高画质 -c copy 封装，中低画质缩小代理；
        # 不做全片 CFR 转码。失败只影响首次播放，不阻断素材选择。
        self.stageChanged.emit("准备预览…")
        try:
            qt_playback_source(self._path, self._preview_quality,
                duration_seconds=info.duration,
                progress_callback=lambda percent: self.progressChanged.emit(
                    self._generation, self._segment, percent))
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).debug(
                "前黑/后黑预览源准备失败: %s", self._path, exc_info=True
            )

    def _load_image_sequence(self, result: RollMediaLoadResult) -> None:
        self.stageChanged.emit("读取素材信息…")
        image = QImage(str(self._path))
        if image.isNull():
            result.error = f"无法读取首帧： {self._path}"
            return
        pattern, start_number = infer_image_sequence_pattern(self._path)
        if pattern == self._path:
            result.error = "首帧文件名需要以连续编号结尾，例如 frame_0001.png。"
            return
        fps = max(int(round(self._preview_fps)), 1)
        frames = count_image_sequence_frames(pattern, start_number)
        result.source = BackgroundSource(
            kind="image_sequence",
            path=str(pattern),
            source_fps=fps,
            sequence_start_number=start_number,
        )
        result.source_duration_ms = max(frames * 1000 // max(fps, 1), 0)
        result.source_width = image.width()
        result.source_height = image.height()
        if frames > 0:
            result.preview_frame_path = background_sequence_frame_path(
                result.source, 0
            )
            result.preview_frame = image
        self.metadataReady.emit(result)

    def _load_image(self, result: RollMediaLoadResult) -> None:
        self.stageChanged.emit("读取素材信息…")
        reader = QImageReader(str(self._path))
        image = reader.read()
        if image.isNull():
            result.error = f"无法读取图片： {self._path}"
            return
        source_duration_ms = 0
        if reader.supportsAnimation() and reader.imageCount() > 1:
            try:
                info = probe_media(self._ffprobe_path, self._path)
            except Exception:  # noqa: BLE001 - 探测失败不阻断选图
                info = None
            if info is not None and info.duration > 0:
                source_duration_ms = max(int(round(info.duration * 1000)), 1)
        result.source = BackgroundSource(kind="image", path=str(self._path))
        result.source_duration_ms = source_duration_ms
        result.source_width = image.width()
        result.source_height = image.height()
        result.preview_frame_path = self._path
        result.preview_frame = image
        self.metadataReady.emit(result)


class _RollRefreshWorker(QObject):
    """开关前黑/后黑后的联动重算后台任务（纯计算，不碰任何 Qt 控件）。

    联动重活是展示时间线推导 + 轨道块重建（O(全轴行数×字符)，大工程上
    到百毫秒级）；两者都是纯函数，放到 worker 线程算，GUI 线程只做控件
    赋值级别的应用——开关回调立即返回，UI 不被重算卡住。结果携带代数：
    更新请求再次到来时，过期结果在 GUI 侧直接丢弃。

    轨道/样式对象按引用只读快照传入；worker 运行期间 GUI 线程极小概率
    并发改动歌词数据时计算可能失败，``plan=None`` 触发 GUI 同步回退（各
    编辑路径本来就会再触发一次轨道重建，正确性不受影响）。
    """

    done = Signal(object)  # RollRefreshResult

    def __init__(
        self,
        generation: int,
        *,
        pre,
        post,
        named_tracks,
        tracks_style,
        main_media_end_ms,
        main_audio_end_ms,
        background_kind: str,
    ) -> None:
        super().__init__()
        self._generation = int(generation)
        self._pre = pre
        self._post = post
        self._named_tracks = list(named_tracks)
        self._tracks_style = tracks_style
        self._main_media_end_ms = main_media_end_ms
        self._main_audio_end_ms = main_audio_end_ms
        self._background_kind = str(background_kind)

    def run(self) -> None:
        from krok_helper.subtitle_render.domain.roll import build_roll_timeline
        from krok_helper.subtitle_render.engine.timing.timeline import track_duration_ms
        from krok_helper.subtitle_render.frontend.editor.timeline_view import build_lanes

        try:
            track_end = max(
                (track_duration_ms(track) for _name, track in self._named_tracks),
                default=0,
            )
            plan = build_roll_timeline(
                self._pre,
                self._post,
                track_end_ms=track_end,
                main_media_end_ms=self._main_media_end_ms,
                main_audio_end_ms=self._main_audio_end_ms,
                background_kind=self._background_kind,
            )
            lanes = (
                build_lanes(self._named_tracks, self._tracks_style, plan.lyric_shift_ms)
                if self._named_tracks
                else ()
            )
            self.done.emit(RollRefreshResult(self._generation, plan, tuple(lanes)))
        except Exception:  # noqa: BLE001 - 计算失败让 GUI 回退同步路径
            logging.getLogger(__name__).exception("前黑/后黑联动重算失败，回退同步路径")
            self.done.emit(RollRefreshResult(self._generation, None, None))
        finally:
            QThread.currentThread().quit()
