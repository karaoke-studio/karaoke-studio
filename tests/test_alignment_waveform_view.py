"""波形画布的冒烟测试。

`WaveformView` 从 ``gui_qt`` 搬到 ``krok_helper.alignment`` 后补上覆盖：
构造、喂波形、真跑一遍 ``paintEvent``（缺 import 的话只有绘制时才炸）。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from krok_helper.alignment import WaveformView
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
