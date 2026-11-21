"""Own the opt-in Quick layer inside the existing detached preview."""
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtQuick import QQuickWindow

from krok_helper.subtitle_render.native.skia_process import resolve_skia_renderer_path
from .quick_graphics_api import required_quick_graphics_api
from .skia_quick_canvas import SkiaQuickCanvas
from .skia_quick_clock import SkiaQuickClock
from .skia_quick_scene import SkiaQuickScene


class SkiaQuickPreview(QObject):
    failed = pyqtSignal(str)
    frame_painted = pyqtSignal(int)
    established_changed = pyqtSignal()

    def __init__(self, viewport):
        super().__init__(viewport)
        if QQuickWindow.graphicsApi() != required_quick_graphics_api():
            raise RuntimeError('Skia 直画需要在窗口创建前配置 Qt Quick 图形接口')
        self._binary = resolve_skia_renderer_path()
        self._canvas = SkiaQuickCanvas(self._binary.parent / 'qml', viewport)
        self._canvas.hide()
        self._canvas.graphics_reported.connect(self._check_graphics)
        self._canvas.frame_submitted.connect(self._frame_submitted)
        self.clock = SkiaQuickClock(self._canvas, fps=60)
        self._scene = SkiaQuickScene(self._canvas)
        self._scene.preparation_failed.connect(self.failed.emit)
        self.active = False
        self.established = False
        self._require_hardware = True
        self._target = None
        self._source = None

    def activate(self, player, background, *, require_hardware: bool) -> None:
        self.active = True
        self._require_hardware = require_hardware
        self.set_background(background)
        player.set_video_output(self._canvas.video_output)
        self.resize()
        self._canvas.show()
        self._canvas.raise_()
        renderer = self._canvas.renderer
        if backend := renderer.property('graphicsBackend'):
            self._check_graphics(backend, renderer.property('graphicsBackendAccepted'),
                                 renderer.property('graphicsHardware'))

    def deactivate(self) -> None:
        self.active = False
        self.established = False
        self._canvas.hide()
        self._scene.clear()
        self._source = self._target = None

    def resize(self) -> None:
        self._canvas.setGeometry(self.parent().rect())

    def set_background(self, source) -> None:
        self.clock.refresh_background()
        self._canvas.set_background_source(source)

    def set_target(self, width: int, height: int, dpr: float) -> None:
        target = (width, height, dpr)
        if target != self._target:
            self._target = target
            self._canvas.set_output_size(width, height)
            self._reconfigure()

    def set_source(self, track, style, extra_tracks, duration) -> None:
        if track is None or (self._source is not None and self._source[0] is not track):
            self._scene.clear()
        self._source = (track, style, extra_tracks, duration) if track is not None else None
        self._reconfigure()

    def _reconfigure(self) -> None:
        if self._source is None or self._target is None:
            return
        track, style, extras, duration = self._source
        width, height, dpr = self._target
        self._scene.set_scene(track, style, executable=self._binary, width=width, height=height, dpr=dpr,
                              fps=60, extra_tracks=extras, duration_ms=duration)

    def _check_graphics(self, backend: str, accepted: bool, hardware: bool) -> None:
        if self.active and (not accepted or (self._require_hardware and not hardware)):
            self.failed.emit(f'Skia 直画设备不可用（{backend}）')

    def _frame_submitted(self, _revision, _generation, time_ms: int) -> None:
        if self.active:
            if not self.established:
                self.established = True
                self.established_changed.emit()
            self.frame_painted.emit(time_ms)
