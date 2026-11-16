"""波形画布的冒烟测试。

`WaveformView` 从 ``gui_qt`` 搬到 ``krok_helper.alignment`` 后补上覆盖：
构造、喂波形、真跑一遍 ``paintEvent``（缺 import 的话只有绘制时才炸）。
"""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QInputDevice, QNativeGestureEvent, QPointingDevice, QWheelEvent
from PyQt6.QtWidgets import QApplication

from krok_helper.alignment import WaveformView
from krok_helper.alignment import waveform_view as waveform_module
from krok_helper.audio_alignment import WaveformData
from krok_helper.settings import ALIGN_TARGET_AUDIO, ALIGN_TARGET_VIDEO


def _waveform(seed: float, duration: float = 30.0, peaks_per_second: int = 50) -> WaveformData:
    count = int(duration * peaks_per_second)
    peaks = [abs(math.sin(i / 9.0 + seed)) for i in range(count)]
    return WaveformData(
        path=Path(f"fake-{seed}.wav"),
        duration=duration,
        peaks_per_second=peaks_per_second,
        peaks=peaks,
    )


@pytest.fixture
def view():
    widget = WaveformView()
    widget.resize(880, 300)
    yield widget
    widget.deleteLater()


def test_empty_view_paints(view) -> None:
    view.grab()  # 没有波形时也要能画（空态提示）


def test_paints_with_waveforms(view) -> None:
    view.set_waveforms(video_waveform=_waveform(0.0), audio_waveform=_waveform(1.7, duration=28.0))
    view.set_offset(1.25)
    view.set_playhead(12.0)
    view.fit_to_waveforms()

    image = view.grab().toImage()

    assert image.width() > 0 and image.height() > 0


def test_offset_and_target_track_round_trip(view) -> None:
    view.set_waveforms(video_waveform=_waveform(0.0), audio_waveform=_waveform(1.0))

    view.set_offset(-2.5)
    assert view.offset_seconds == pytest.approx(-2.5)

    # 换对齐目标会把偏移归零 —— 上一条轨算出来的偏移不该带到另一条轨上。
    view.set_target_track(ALIGN_TARGET_AUDIO)
    assert view.target_track == ALIGN_TARGET_AUDIO
    assert view.offset_seconds == pytest.approx(0.0)

    view.set_target_track(ALIGN_TARGET_VIDEO)
    view.nudge_offset(0.5)
    assert view.offset_seconds == pytest.approx(0.5)


def test_clear_drops_waveforms(view) -> None:
    view.set_waveforms(video_waveform=_waveform(0.0), audio_waveform=_waveform(1.0))
    view.clear()

    assert view.video_waveform is None
    assert view.audio_waveform is None
    view.grab()


@pytest.fixture
def mac_view(view, monkeypatch):
    monkeypatch.setattr(waveform_module, 'sys', SimpleNamespace(platform='darwin'))
    view.set_waveforms(video_waveform=_waveform(0.0, 90), audio_waveform=_waveform(1.0, 90))
    view.pixels_per_second = 100.0
    view.view_start_seconds = 10.0
    view._auto_fit_view = False
    return view


@pytest.fixture
def touchpad():
    return QPointingDevice('test trackpad', 42, QInputDevice.DeviceType.TouchPad,
                           QPointingDevice.PointerType.Finger,
                           QInputDevice.Capability.Position, 2, 0)


def _wheel(view, pixels=(0, 0), angle=(0, 0), phase=Qt.ScrollPhase.ScrollUpdate,
           inverted=False, device=None):
    pos = QPointF(400, 100)
    return QWheelEvent(pos, view.mapToGlobal(pos), QPoint(*pixels), QPoint(*angle),
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                       phase, inverted, device=device or QPointingDevice.primaryPointingDevice())


def _gesture(view, kind, value=0.0, x=400.0, device=None):
    pos = QPointF(x, 100)
    return QNativeGestureEvent(kind, device or QPointingDevice.primaryPointingDevice(),
                               2, pos, pos, view.mapToGlobal(pos), value, QPointF())


@pytest.mark.parametrize('inverted', [False, True])
def test_mac_horizontal_pixels_pan_without_changing_audio_or_zoom(mac_view, inverted):
    changes = []
    mac_view.offsetChanged.connect(lambda _: changes.append('offset'))
    mac_view.playheadChanged.connect(lambda _: changes.append('seek'))
    mac_view.zoomChanged.connect(lambda _: changes.append('zoom'))
    mac_view.trimChanged.connect(lambda _: changes.append('trim'))
    event = _wheel(mac_view, pixels=(-25, 2), angle=(-120, 10), inverted=inverted)
    mac_view.wheelEvent(event)
    assert event.isAccepted()
    # Qt already inverted the delivered delta, regardless of the metadata flag.
    assert mac_view.view_start_seconds == pytest.approx(10.25)
    assert mac_view.pixels_per_second == 100
    assert changes == []


def test_mac_vertical_and_diagonal_scroll_never_zoom(mac_view):
    for pixels in [(0, 30), (2, 30), (10, 10)]:
        event = _wheel(mac_view, pixels=pixels, phase=Qt.ScrollPhase.NoScrollPhase)
        mac_view.wheelEvent(event)
        assert not event.isAccepted()
    assert mac_view.view_start_seconds == 10
    assert mac_view.pixels_per_second == 100


def test_scroll_sequence_locks_horizontal_axis_through_momentum(mac_view):
    mac_view.wheelEvent(_wheel(mac_view, phase=Qt.ScrollPhase.ScrollBegin))
    for delta, phase in [((-10, 1), Qt.ScrollPhase.ScrollUpdate),
                         ((-1, 15), Qt.ScrollPhase.ScrollUpdate),
                         ((-5, 0), Qt.ScrollPhase.ScrollMomentum)]:
        event = _wheel(mac_view, pixels=delta, phase=phase)
        mac_view.wheelEvent(event)
        assert event.isAccepted()
    assert mac_view.view_start_seconds == pytest.approx(10.16)
    end = _wheel(mac_view, phase=Qt.ScrollPhase.ScrollEnd)
    mac_view.wheelEvent(end)
    assert end.isAccepted()
    assert not mac_view.owns_navigation_sequence(QEvent.Type.Wheel)


def test_angle_only_touchpad_is_not_mistaken_for_mouse_zoom(mac_view, touchpad):
    event = _wheel(mac_view, angle=(-120, 3), phase=Qt.ScrollPhase.NoScrollPhase, device=touchpad)
    mac_view.wheelEvent(event)
    assert mac_view.view_start_seconds == pytest.approx(10.4)
    assert mac_view.pixels_per_second == 100


@pytest.mark.parametrize('platform', ['darwin', 'win32', 'linux'])
def test_physical_mouse_retains_notched_zoom(mac_view, monkeypatch, platform):
    monkeypatch.setattr(waveform_module, 'sys', SimpleNamespace(platform=platform))
    mac_view.wheelEvent(_wheel(mac_view, angle=(0, 120), phase=Qt.ScrollPhase.NoScrollPhase))
    assert mac_view.pixels_per_second == pytest.approx(115)


def test_pinch_keeps_cursor_time_fixed_and_emits_zoom(mac_view):
    emitted = []
    mac_view.zoomChanged.connect(emitted.append)
    anchor = 400.25
    left, _ = mac_view._plot_bounds()
    original_time = mac_view.view_start_seconds + (anchor-left)/mac_view.pixels_per_second
    for value in [.10, .25, -.20]:
        event = _gesture(mac_view, Qt.NativeGestureType.ZoomNativeGesture, value, anchor)
        QApplication.sendEvent(mac_view, event)
        assert event.isAccepted()
        assert mac_view.view_start_seconds + (anchor-left)/mac_view.pixels_per_second == pytest.approx(original_time)
    assert emitted == pytest.approx([110, 137.5, 110])


def test_minimum_zoom_preserves_anchor(mac_view):
    mac_view.pixels_per_second = .5
    mac_view.view_start_seconds = 0
    mac_view.handle_native_gesture(_gesture(mac_view, Qt.NativeGestureType.ZoomNativeGesture, 1))
    assert mac_view.pixels_per_second == 1
    assert mac_view.view_start_seconds == pytest.approx(210)


def test_pan_clamps_to_audio_bounds(mac_view):
    mac_view.wheelEvent(_wheel(mac_view, pixels=(99999, 0)))
    assert mac_view.view_start_seconds == 0
    mac_view.wheelEvent(_wheel(mac_view, pixels=(-99999, 0)))
    _, width = mac_view._plot_bounds()
    assert mac_view.view_start_seconds == pytest.approx(90-width/100)


def test_manual_navigation_suspends_following_until_six_seconds_after_end(mac_view, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(waveform_module.time, 'monotonic', lambda: clock[0])
    mac_view.wheelEvent(_wheel(mac_view, pixels=(-20, 0)))
    start = mac_view.view_start_seconds
    clock[0] += 20  # A held gesture must not expire mid-operation.
    mac_view.set_playhead(70, keep_visible=True)
    assert mac_view.view_start_seconds == start
    mac_view.wheelEvent(_wheel(mac_view, phase=Qt.ScrollPhase.ScrollEnd))
    clock[0] += 5.9
    mac_view.set_playhead(71, keep_visible=True)
    assert mac_view.view_start_seconds == start
    clock[0] += .2
    mac_view.set_playhead(72, keep_visible=True)
    assert mac_view.view_start_seconds > start


def test_pinch_end_and_deactivation_release_following(mac_view, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(waveform_module.time, 'monotonic', lambda: clock[0])
    mac_view.handle_native_gesture(_gesture(mac_view, Qt.NativeGestureType.BeginNativeGesture))
    mac_view.handle_native_gesture(_gesture(mac_view, Qt.NativeGestureType.ZoomNativeGesture, .1))
    assert mac_view._following_suspended()
    mac_view.handle_native_gesture(_gesture(mac_view, Qt.NativeGestureType.EndNativeGesture))
    clock[0] += 6.1
    assert not mac_view._following_suspended()
    mac_view.handle_native_gesture(_gesture(mac_view, Qt.NativeGestureType.BeginNativeGesture))
    QApplication.sendEvent(mac_view, QEvent(QEvent.Type.WindowDeactivate))
    assert not mac_view._following_suspended()


def test_panning_disables_auto_fit_and_clear_resets_gestures(mac_view):
    mac_view._auto_fit_view = True
    mac_view.wheelEvent(_wheel(mac_view, pixels=(-10, 0)))
    start = mac_view.view_start_seconds
    mac_view.grab()
    assert mac_view.view_start_seconds == start
    mac_view.clear()
    assert not mac_view._following_suspended()
    event = _gesture(mac_view, Qt.NativeGestureType.ZoomNativeGesture, .1)
    assert not mac_view.handle_native_gesture(event)
