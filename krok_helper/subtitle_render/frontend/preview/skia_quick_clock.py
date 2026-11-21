"""Submit the newest transport timestamp to an opted-in Skia Quick canvas."""
from math import ceil
from time import monotonic_ns

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
        self._next_submit_ns = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._submit_latest)
        self.set_frame_rate(fps)
        canvas.scene_prepared.connect(self._scene_prepared)
        canvas.scene_cleared.connect(self._scene_cleared)

    def set_frame_rate(self, fps: int | float) -> None:
        self._play_interval_ns = max(1_000_000, ceil(1_000_000_000 / frame_rate(fps)))
        self._next_submit_ns = None
        if self._timer.isActive():
            self._timer.stop()
            self._schedule()

    def _scene_prepared(self, revision, accepted: bool, _error: str) -> None:
        if accepted:
            self._revision = int(revision)
            self._submit_soon()

    def _scene_cleared(self) -> None:
        self._timer.stop()
        self._revision = 0
        self._last_request = None
        self._next_submit_ns = None
        self._schedule()

    def set_playing(self, playing: bool) -> None:
        if self._playing != playing:
            self._playing = playing
            self._next_submit_ns = None
            self._timer.stop()
            self._schedule()

    def set_time(self, time_ms: int) -> None:
        self._time_ms = time_ms
        deadline = self._next_submit_ns
        if self._playing and self._revision and deadline is not None and monotonic_ns() >= deadline:
            # A transport tick already delivered the latest time on the GUI
            # thread. Submit an overdue frame without another timer event.
            self._timer.stop()
            self._submit_latest()
        else:
            self._schedule()

    def refresh_background(self) -> None:
        self._canvas.set_background_time(self._time_ms)

    def seek(self, time_ms: int) -> None:
        self._generation += 1
        self._time_ms = time_ms
        self._submit_soon()

    def _schedule(self) -> None:
        if (self._revision or self._canvas.has_timed_background) and not self._timer.isActive():
            delay = 0
            if self._playing and self._next_submit_ns is not None:
                remaining = self._next_submit_ns - monotonic_ns()
                delay = max(0, ceil(remaining / 1_000_000))
            self._timer.start(delay)

    def _submit_soon(self) -> None:
        if self._revision or self._canvas.has_timed_background:
            self._next_submit_ns = None
            self._timer.start(0)

    def _submit_latest(self) -> None:
        request = (self._revision, self._generation, self._time_ms)
        if not self._revision:
            self.refresh_background()
        elif request != self._last_request and self._canvas.request_frame(*request):
            self._last_request = request
        else:
            return
        if self._playing:
            now = monotonic_ns()
            deadline = self._next_submit_ns if self._next_submit_ns is not None else now
            interval = self._play_interval_ns
            # Skip expired deadlines and keep timer rounding from accumulating.
            periods = max(1, (now - deadline) // interval + 1)
            self._next_submit_ns = deadline + periods * interval
