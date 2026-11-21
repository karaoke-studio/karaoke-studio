"""Prepare direct-preview revisions without blocking the Qt host."""
from __future__ import annotations

import json
from time import monotonic

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, pyqtSignal

from krok_helper.subtitle_render.engine.text.font_preparation import iter_prepare_fonts
from krok_helper.subtitle_render.native.skia_scene import prepare_skia_scene_ir


class _SceneResult(QObject):
    finished = pyqtSignal(object, object, str)


class _PrepareScene(QRunnable):
    def __init__(self, revision, track, style, options):
        super().__init__()
        self.result = _SceneResult()
        self.revision = revision
        self.track = track
        self.style = style
        self.options = options

    def run(self):
        try:
            ir = prepare_skia_scene_ir(self.track, self.style, **self.options)
            data = json.dumps(ir, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except Exception as exc:
            self.result.finished.emit(self.revision, None, str(exc) or type(exc).__name__)
        else:
            self.result.finished.emit(self.revision, data, '')


class SkiaQuickScene(QObject):
    preparation_failed = pyqtSignal(str)

    def __init__(self, canvas):
        super().__init__(canvas)
        self._canvas = canvas
        self._revision = 0
        self._pending = None
        self._running = None
        self._font_steps = None
        self._timer = QTimer(self)
        self._timer.setInterval(1)
        self._timer.timeout.connect(self._prepare_fonts)
        canvas.scene_prepared.connect(self._native_prepared)

    def set_scene(self, track, style, **options) -> int:
        self._revision += 1
        if not self._canvas.reserve_scene(self._revision):
            raise RuntimeError('Skia 预览场景版本必须递增')
        self._pending = (self._revision, track, style, options)
        self._font_steps = None
        self._timer.start()
        return self._revision

    def clear(self) -> None:
        self._revision += 1
        self._pending = None
        self._font_steps = None
        self._timer.stop()
        self._canvas.clear_scene(self._revision)

    def _prepare_fonts(self) -> None:
        if self._pending is None or self._running is not None:
            self._timer.stop()
            return
        revision, track, style, options = self._pending
        if self._font_steps is None:
            self._font_steps = iter_prepare_fonts(track, style, options.get('extra_tracks'))
        deadline = monotonic() + 0.008
        while monotonic() < deadline:
            try:
                next(self._font_steps)
            except StopIteration:
                self._timer.stop()
                self._font_steps = None
                self._pending = None
                task = _PrepareScene(revision, track, style, options)
                self._running = task
                task.result.finished.connect(self._python_prepared, Qt.ConnectionType.QueuedConnection)
                QThreadPool.globalInstance().start(task)
                break

    def _python_prepared(self, revision, data, error: str) -> None:
        self._running = None
        if self._pending is not None:
            self._timer.start()
        if revision != self._revision:
            return
        if error:
            self.preparation_failed.emit(error)
        elif not self._canvas.configure_scene(data, revision):
            self.preparation_failed.emit('Skia 预览场景配置已失效')

    def _native_prepared(self, revision, accepted: bool, error: str) -> None:
        if revision == self._revision and not accepted:
            self.preparation_failed.emit(error)
