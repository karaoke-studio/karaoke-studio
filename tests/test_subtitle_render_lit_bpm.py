"""BPM lamp timing, property linkage, and current-media detection."""

from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess
import wave

import numpy as np
import pytest

from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.domain.models import (
    Style, resolve_lit_bpm_timing, style_from_dict, style_to_dict,
)
from krok_helper.subtitle_render.engine.layout.display.signal import signal_lead_in_ms
from krok_helper.subtitle_render.engine.render.elements.signal import shape_active_index_and_phase
from krok_helper.subtitle_render.frontend.properties.controls.lit_bpm import (
    LitBpmControls, MediaBpmWorker,
)
from krok_helper.subtitle_render.settings.property_controllers import PropertyStyleController


@pytest.mark.parametrize("bpm,count,expected", [(120, 4, 2000), (60, 4, 4000), (140, 4, 1714), (137, 8, 3504)])
def test_bpm_materializes_existing_duration_and_uses_full_count_offset(bpm, count, expected):
    style = resolve_lit_bpm_timing(Style(
        lit_enabled=True, lit_bpm_enabled=True, lit_bpm=bpm, lit_number=count,
        lit_waiting_time_ms=250,
    ))
    assert style.signals_duration_ms == expected + 250
    assert signal_lead_in_ms(style) == expected
    assert style.volume_duration_ms == 4000


def test_bpm_lamp_countdown_has_one_lamp_per_quarter_note():
    style = resolve_lit_bpm_timing(Style(lit_bpm_enabled=True, lit_bpm=120, lit_number=4))
    assert [shape_active_index_and_phase(t, style.signals_duration_ms, 4)[0]
            for t in (1, 501, 1001, 1501, 2000)] == [3, 2, 1, 0, -1]


def test_old_styles_keep_manual_duration_and_bpm_styles_round_trip():
    legacy = style_from_dict({"lit_number": 6, "signals_duration_ms": 7000})
    assert not legacy.lit_bpm_enabled
    assert legacy.signals_duration_ms == 7000
    style = style_from_dict({
        "lit_bpm_enabled": True, "lit_bpm": 140, "lit_number": 6,
        "lit_waiting_time_ms": 300, "signals_duration_ms": 1000,
    })
    assert style.signals_duration_ms == 2871
    restored = style_from_dict(style_to_dict(style))
    for key in ("lit_bpm_enabled", "lit_bpm", "lit_number", "lit_waiting_time_ms", "signals_duration_ms"):
        assert getattr(restored, key) == getattr(style, key)


@pytest.mark.parametrize("value", [None, "wrong", 0, -120, float("nan"), float("inf")])
def test_invalid_saved_bpm_uses_finite_default(value):
    style = style_from_dict({"lit_bpm_enabled": True, "lit_bpm": value})
    assert style.lit_bpm == 120
    assert style.signals_duration_ms == 2000


def test_count_and_waiting_edits_recalculate_duration_and_trigger_layout_change():
    controller = PropertyStyleController()
    result = controller.update(Style(), {"lit_bpm_enabled": True, "lit_bpm": 120})
    assert result.style.signals_duration_ms == 2000
    assert "signals_duration_ms" in result.changed_fields
    result = controller.update(result.style, {"lit_number": 6})
    assert result.style.signals_duration_ms == 3000
    assert "signals_duration_ms" in result.changed_fields
    result = controller.update(result.style, {"lit_waiting_time_ms": 300})
    assert result.style.signals_duration_ms == 3300
    result = controller.update(result.style, {"lit_bpm_enabled": False})
    result = controller.update(result.style, {"signals_duration_ms": 1234, "lit_number": 8})
    assert result.style.signals_duration_ms == 1234


def test_panel_bpm_controls_link_duration_and_restore_saved_mode(qtbot):
    from krok_helper.subtitle_render.frontend.properties.property_panel import PropertyPanel

    panel = PropertyPanel()
    qtbot.addWidget(panel)
    panel.set_style(Style())
    panel._lit_bpm_controls.enabled.click()
    assert panel.subtitle_style.signals_duration_ms == 2000
    assert panel._lit_duration_spin.value() == 2000
    assert not panel._lit_duration_spin.isEnabled()
    panel._lit_bpm_controls.bpm.setValue(140)
    assert panel._lit_duration_spin.value() == 1714
    panel._lit_number_spin.setValue(6)
    assert panel._lit_duration_spin.value() == 2571
    panel._lit_waiting_time_spin.setValue(300)
    assert panel._lit_duration_spin.value() == 2871
    assert "6 盏灯" in panel._lit_bpm_controls.summary.text()
    saved = style_from_dict(style_to_dict(panel.subtitle_style))
    panel.set_style(Style())
    panel.set_style(saved)
    assert panel._lit_bpm_controls.enabled.isChecked()
    assert panel._lit_bpm_controls.bpm.value() == 140
    assert panel._lit_duration_spin.value() == 2871
    panel._lit_bpm_controls.enabled.click()
    assert panel._lit_duration_spin.isEnabled()


@pytest.fixture
def tasks(monkeypatch):
    from strange_uta_game.frontend.editor.timing import task_runner

    captured = []

    def start(owner, worker, **callbacks):
        relay = task_runner.TaskRelay(
            owner, lambda: callbacks["is_current"](worker),
            on_finished=callbacks["on_finished"], on_error=callbacks["on_error"],
        )
        captured.append((worker, relay))
        return relay

    monkeypatch.setattr(task_runner, "start_task", start)
    return captured


@pytest.mark.parametrize("detected", [60, 90, 120, 137, 140, 175, 200])
def test_detection_fills_bpm_and_provides_three_multiples(qtbot, tasks, detected):
    controls = LitBpmControls()
    qtbot.addWidget(controls)
    controls.set_source_provider(lambda: (Path("song.mp4"), "ffmpeg", 1))
    changes = []
    controls.edited.connect(lambda enabled, bpm: changes.append((enabled, bpm)))
    controls.enabled.click()
    tasks[-1][1]._finished({"bpm": detected, "confidence": 0.8})
    assert controls.bpm.value() == detected
    assert [controls.candidates.itemData(i) for i in (1, 2, 3)] == [detected / 2, detected, detected * 2]
    controls.candidates.setCurrentIndex(1)
    assert changes[-1] == (True, detected / 2)
    controls.cancel()


@pytest.mark.parametrize("action", ["source", "cancel", "restart"])
def test_stale_or_cancelled_detection_cannot_overwrite_bpm(qtbot, tasks, action):
    controls = LitBpmControls()
    qtbot.addWidget(controls)
    source = [Path("song.mp4"), "ffmpeg", 1]
    controls.set_source_provider(lambda: tuple(source))
    controls.bpm.setValue(95)
    controls.start_detection()
    worker, relay = tasks[-1]
    if action == "source":
        source[0] = Path("other.mp4")
    elif action == "cancel":
        controls.cancel()
    else:
        controls.start_detection()
    relay._finished({"bpm": 140, "confidence": 0.8})
    assert controls.bpm.value() == 95
    assert worker._cancelled.is_set()
    controls.cancel()


def test_missing_ffmpeg_keeps_manual_bpm_available(qtbot):
    controls = LitBpmControls()
    qtbot.addWidget(controls)

    def unavailable():
        raise FileNotFoundError("ffmpeg")

    controls.set_source_provider(unavailable)
    controls.enabled.click()
    assert "FFmpeg" in controls.status.text()
    controls.bpm.setValue(140)
    assert controls.bpm.value() == 140


def test_completed_candidates_are_discarded_on_media_change(qtbot, tasks):
    controls = LitBpmControls()
    qtbot.addWidget(controls)
    source = [Path("song.mp4"), "ffmpeg", 1]
    controls.set_source_provider(lambda: tuple(source))
    controls.start_detection()
    tasks[-1][1]._finished({"bpm": 140, "confidence": 0.8})
    source[2] = 2
    controls.candidates.setCurrentIndex(1)
    assert controls.bpm.value() == 140
    assert controls.candidates.count() == 1


def test_enabling_bpm_preserves_an_already_chosen_candidate(qtbot, tasks):
    controls = LitBpmControls()
    qtbot.addWidget(controls)
    controls.set_source_provider(lambda: (Path("song.mp4"), "ffmpeg", 1))
    controls.start_detection()
    tasks[-1][1]._finished({"bpm": 140, "confidence": 0.8})
    controls.candidates.setCurrentIndex(1)
    controls.enabled.click()
    assert controls.bpm.value() == 70
    assert len(tasks) == 1


def test_video_audio_takes_priority_and_image_uses_independent_audio(monkeypatch):
    from krok_helper.subtitle_render.frontend import main_window

    monkeypatch.setattr(main_window, "find_tool", lambda *_args: "ffmpeg")
    owner = SimpleNamespace(
        _background_source=BackgroundSource(kind="video", path="movie.mp4"),
        _audio_path=Path("separate.wav"), _project_document=object(),
        _resolve_ffmpeg_dir=lambda: None,
    )
    assert main_window.SubtitleRenderWindow._lit_bpm_source(owner)[0] == Path("movie.mp4")
    owner._background_source = BackgroundSource(kind="image", path="cover.png")
    assert main_window.SubtitleRenderWindow._lit_bpm_source(owner)[0] == Path("separate.wav")
    owner._audio_path = None
    assert main_window.SubtitleRenderWindow._lit_bpm_source(owner) is None


def test_decode_cancellation_terminates_ffmpeg(monkeypatch):
    from krok_helper.subtitle_render.frontend.properties.controls import lit_bpm

    worker = MediaBpmWorker(Path("song.mp4"), "ffmpeg")

    class Process:
        killed = False

        def communicate(self, timeout=None):
            if timeout is not None:
                worker.request_cancel()
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            return b"", b""

        def kill(self):
            self.killed = True

        def poll(self):
            return -1 if self.killed else None

    process = Process()
    monkeypatch.setattr(lit_bpm.subprocess, "Popen", lambda *_args, **_kwargs: process)
    results = []
    worker.finished.connect(results.append)
    worker.run()
    assert process.killed
    assert results == [None]


def test_real_media_decode_and_detection(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg unavailable")
    samples = np.zeros(22050 * 30, dtype=np.float32)
    pulse = np.hanning(256).astype(np.float32)
    for start in range(0, len(samples) - len(pulse), 11025):
        samples[start:start + len(pulse)] = pulse
    path = tmp_path / "节拍.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes((samples * 32767).astype("<i2").tobytes())
    results, errors = [], []
    worker = MediaBpmWorker(path, ffmpeg)
    worker.finished.connect(results.append)
    worker.error.connect(errors.append)
    worker.run()
    assert not errors
    assert results[0]["bpm"] == pytest.approx(120, abs=2)
