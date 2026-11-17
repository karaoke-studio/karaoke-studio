"""BPM-driven shape-lamp controls and cancellable media tempo analysis."""

from __future__ import annotations

import math
import subprocess
import threading
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CheckBox, PushButton

from krok_helper.ffmpeg import _build_subprocess_kwargs
from krok_helper.subtitle_render.domain.models import Style
from .inputs import WheelFocusedComboBox, WheelFocusedDoubleSpinBox


class MediaBpmWorker(QObject):
    progress = pyqtSignal(float)
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, path: Path, ffmpeg: str):
        super().__init__()
        self.path = path
        self.ffmpeg = ffmpeg
        self._cancelled = threading.Event()

    def request_cancel(self):
        self._cancelled.set()

    def run(self):
        process = None
        try:
            if self._cancelled.is_set():
                self.finished.emit(None)
                return
            process = subprocess.Popen(
                [self.ffmpeg, "-v", "error", "-nostdin", "-i", str(self.path),
                 "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "22050",
                 "-f", "f32le", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **_build_subprocess_kwargs(),
            )
            while True:
                if self._cancelled.is_set():
                    process.kill()
                    process.communicate()
                    self.finished.emit(None)
                    return
                try:
                    samples, errors = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise RuntimeError(errors.decode("utf-8", errors="replace"))
            from strange_uta_game.backend.infrastructure.audio.spectrum import detect_bpm

            result = detect_bpm(
                np.frombuffer(samples, dtype="<f4"), 22050,
                progress_cb=self.progress.emit, cancel_check=self._cancelled.is_set,
            )
            self.finished.emit(result)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()


class LitBpmControls(QWidget):
    edited = pyqtSignal(bool, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._syncing = False
        self._worker = None
        self._source = None
        self._source_error = False
        self._source_provider = lambda: None
        self._number = 4
        self._waiting = 0
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.enabled = CheckBox("按 BPM 计算（每盏灯一拍）", self)
        root.addWidget(self.enabled)
        row = QHBoxLayout()
        self.bpm = WheelFocusedDoubleSpinBox(self)
        self.bpm.setRange(10, 600)
        self.bpm.setDecimals(2)
        self.bpm.setValue(120)
        self.bpm.setSuffix(" BPM")
        self.bpm.setMinimumWidth(180)
        row.addWidget(self.bpm)
        self.detect = PushButton("自动检测", self)
        self.detect.clicked.connect(self._detect_or_cancel)
        row.addWidget(self.detect)
        row.addStretch()
        root.addLayout(row)
        self.candidates = WheelFocusedComboBox(self)
        self._clear_candidates()
        self.candidates.currentIndexChanged.connect(self._choose)
        root.addWidget(self.candidates)
        self.summary = CaptionLabel("", self)
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        self.status = CaptionLabel("使用背景视频音轨或当前独立音频检测", self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.enabled.toggled.connect(self._toggle)
        self.bpm.valueChanged.connect(self._edit)
        self._refresh_summary()

    def set_source_provider(self, provider):
        self.cancel()
        self._source_provider = provider

    def source_changed(self):
        source = self._current_source()
        if source != self._source:
            self.cancel()
            self._source = source
            self._clear_candidates()
            self.status.setText("音源已更新，可自动检测或手动输入 BPM")

    def _current_source(self):
        self._source_error = False
        try:
            return self._source_provider()
        except Exception:
            self._source_error = True
            return None

    def sync(self, style: Style):
        self._syncing = True
        try:
            self.enabled.setChecked(style.lit_bpm_enabled)
            self.bpm.setValue(style.lit_bpm)
            self._number = style.lit_number
            self._waiting = max(0, style.lit_waiting_time_ms)
        finally:
            self._syncing = False
        self._refresh_summary()

    def _refresh_summary(self):
        beat = 60_000 / self.bpm.value()
        self.summary.setText(
            f"每拍 {beat:.3f}ms，{self._number} 盏灯倒计时 "
            f"{self._number * beat:.3f}ms；持续时间含结束等待 {self._waiting}ms。"
        )

    def _edit(self, *_args):
        if not self._syncing:
            self.edited.emit(self.enabled.isChecked(), self.bpm.value())
        self._refresh_summary()

    def _toggle(self, checked):
        if self._syncing:
            return
        self._edit()
        if checked:
            if self.candidates.count() <= 1 or self._source != self._current_source():
                self.start_detection()
        else:
            self.cancel()

    def _clear_candidates(self):
        self.candidates.clear()
        self.candidates.addItem("检测后自动填入，可选半速 / 原值 / 倍速")

    def _detect_or_cancel(self):
        if self._worker is None:
            self.start_detection()
        else:
            self.cancel()

    def start_detection(self):
        self.cancel()
        self._source = self._current_source()
        self._clear_candidates()
        if self._source is None:
            self.status.setText(
                "无法读取检测音源，请检查 FFmpeg 配置，也可手动输入 BPM"
                if self._source_error
                else "请先加载背景视频或独立音频，也可手动输入 BPM"
            )
            return
        from strange_uta_game.frontend.editor.timing import task_runner

        self._worker = MediaBpmWorker(self._source[0], self._source[1])
        self.detect.setText("取消检测")
        self.status.setText("正在检测音轨 BPM，完成后自动填入…")
        task_runner.start_task(
            self, self._worker, on_finished=self._finished, on_error=self._error,
            is_current=self._is_current,
        )

    def _is_current(self, worker):
        if worker is not self._worker:
            return False
        if self._source != self._current_source():
            self.source_changed()
            return False
        return True

    def cancel(self):
        if self._worker is not None:
            self._worker.request_cancel()
            self._worker = None
            self.detect.setText("自动检测")
            self.status.setText("检测已取消，可手动输入或重新检测 BPM")

    def _finished(self, result):
        self._worker = None
        self.detect.setText("自动检测")
        try:
            bpm = float(result["bpm"])
            confidence = float(result["confidence"])
            if not math.isfinite(bpm) or not 10 <= bpm <= 600:
                raise ValueError
            if not math.isfinite(confidence) or confidence < 0.05:
                raise ValueError
        except (TypeError, KeyError, ValueError):
            self.status.setText("未能可靠识别 BPM，请手动输入或重新检测")
            return
        detected_index = 0
        for value, label in ((bpm / 2, "半速"), (bpm, "检测值"), (bpm * 2, "倍速")):
            if 10 <= value <= 600:
                if value == bpm:
                    detected_index = self.candidates.count()
                self.candidates.addItem(f"{value:g} BPM（{label}）", userData=value)
        self.candidates.setCurrentIndex(detected_index)
        self.status.setText("已填入检测值，可选择半速 / 原值 / 倍速或手动修改")

    def _choose(self, index):
        value = self.candidates.itemData(index) if index >= 0 else None
        if value is not None:
            if self._source != self._current_source():
                self.source_changed()
                return
            self.bpm.setValue(value)

    def _error(self, _message):
        self._worker = None
        self.detect.setText("自动检测")
        self.status.setText("检测失败，请检查音轨和 FFmpeg 配置，或手动输入 BPM")
