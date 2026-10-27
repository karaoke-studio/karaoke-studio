"""Qt worker adapters used by the subtitle-render window."""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal as Signal

from krok_helper.errors import ExportCancelled
from krok_helper.ffmpeg import probe_media, terminate_process
from krok_helper.models import MediaInfo
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
