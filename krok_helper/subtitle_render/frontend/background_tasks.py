"""Qt worker adapters used by the subtitle-render window."""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess
from typing import Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal as Signal

from krok_helper.errors import ExportCancelled
from krok_helper.ffmpeg import probe_media, terminate_process
from krok_helper.models import MediaInfo
from krok_helper.subtitle_render.engine.renderer import RenderJob, render_subtitle_video
from krok_helper.subtitle_render.project_store import save_recovery_project


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
        except ExportCancelled as exc:
            worker_log.info("字幕视频导出取消: %s", exc)
            self.cancelled.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            worker_log.exception("字幕视频导出失败")
            self.failed.emit(str(exc))
            return
        worker_log.info("字幕视频导出完成 output=%s", output)
        self.finished.emit(output)

    def cancel(self) -> None:
        self._cancel_requested = True
        process = self._process
        if process is not None:
            terminate_process(process)

    def should_cancel(self) -> bool:
        return self._cancel_requested

    def _set_process(self, process: Optional[subprocess.Popen]) -> None:
        self._process = process
