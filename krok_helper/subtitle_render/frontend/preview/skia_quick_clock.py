"""Submit the newest transport timestamp to an opted-in Skia Quick canvas."""
from math import ceil

from PyQt6.QtCore import QObject, Qt, QTimer

from krok_helper.subtitle_render.domain.frame_rate import frame_rate


class SkiaQuickClock(QObject):
    def __init__(self, canvas, *, fps: int | float):
        super().__init__(canvas)
        self._canvas = canvas
        self._revision = 0
        self._generation = 1
        self._time_ms = 0
        self._last_request = None
        self._playing = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._submit_latest)
        self.set_frame_rate(fps)
        canvas.scene_prepared.connect(self._scene_prepared)

    def set_frame_rate(self, fps: int | float) -> None:
        self._play_interval_ms = max(1, ceil(1000 / frame_rate(fps)))

    def _scene_prepared(self, revision, accepted: bool, _error: str) -> None:
        if accepted:
            self._revision = int(revision)
            self._submit_soon()

    def set_playing(self, playing: bool) -> None:
        if self._playing != playing:
            self._playing = playing
            self._timer.stop()
            self._schedule()

    def set_time(self, time_ms: int) -> None:
        self._time_ms = time_ms
        self._schedule()

    def seek(self, time_ms: int) -> None:
        self._generation += 1
        self._time_ms = time_ms
        self._submit_soon()

    def _schedule(self) -> None:
        if self._revision and not self._timer.isActive():
            self._timer.start(self._play_interval_ms if self._playing else 0)

    def _submit_soon(self) -> None:
        if self._revision:
            self._timer.start(0)

    def _submit_latest(self) -> None:
        request = (self._revision, self._generation, self._time_ms)
        if request != self._last_request and self._canvas.request_frame(*request):
            self._last_request = request
