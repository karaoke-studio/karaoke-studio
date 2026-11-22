"""Skia export through the product pipe and real FFmpeg decoders."""

from dataclasses import replace
import subprocess
import wave

import numpy as np
import pytest
from PyQt6.QtGui import QColor, QImage

from krok_helper.errors import ProcessingError
from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.domain.frame_rate import frame_timestamp
from krok_helper.subtitle_render.domain.paint import KaraokeColorState, KaraokeColors, PaintFill
from krok_helper.subtitle_render.domain.roll import RollSegmentConfig
from krok_helper.subtitle_render.engine.export.render_job import (
    OUTPUT_FORMAT_MOV_QTRLE, OUTPUT_FORMAT_MOV_TRANSPARENT, OUTPUT_FORMAT_PNG_COMPOSITED,
    OUTPUT_FORMAT_PNG_TRANSPARENT, RenderJob, is_png_sequence,
)
from krok_helper.subtitle_render.engine.renderer import render_subtitle_video
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from test_subtitle_skia_protocol import application, binary, collect, export_ffmpeg, selection, supported_project


WIDTH, HEIGHT = 320, 180
AUDIO_RATE = 48000


@pytest.fixture(params=['gpu', 'raster'])
def export_target(request, monkeypatch):
    monkeypatch.setenv('KROK_SUBTITLE_RENDER_BACKEND', 'skia')
    monkeypatch.setenv('KROK_SUBTITLE_SKIA_TARGET', request.param)
    monkeypatch.setenv('KROK_SUBTITLE_SKIA_FALLBACK', 'none')
    return request.param


def write_tone(path, frequency, duration_ms):
    clock = np.arange(AUDIO_RATE * duration_ms // 1000)
    samples = (np.sin(2 * np.pi * frequency * clock / AUDIO_RATE) * 6000).astype('<i2')
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, AUDIO_RATE, len(samples), 'NONE', 'not compressed'))
        audio.writeframes(samples.tobytes())
    return samples


def decode(ffmpeg, path, *args):
    result = subprocess.run([ffmpeg, '-v', 'error', '-i', str(path), *args, '-'], capture_output=True, timeout=40)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    return result.stdout


def decode_frames(ffmpeg, path):
    data = decode(ffmpeg, path, '-map', '0:v:0', '-threads', '1', '-f', 'rawvideo', '-pix_fmt', 'rgba')
    return np.frombuffer(data, np.uint8).reshape(-1, HEIGHT, WIDTH, 4)


def decode_audio(ffmpeg, path):
    data = decode(ffmpeg, path, '-map', '0:a:0', '-f', 's16le', '-ac', '1', '-ar', str(AUDIO_RATE))
    return np.frombuffer(data, '<i2')


def read_png(path):
    image = QImage(str(path)).convertToFormat(QImage.Format.Format_RGBA8888)
    assert (image.width(), image.height()) == (WIDTH, HEIGHT)
    pointer = image.constBits()
    pointer.setsize(image.sizeInBytes())
    return np.frombuffer(bytes(pointer), np.uint8).reshape(HEIGHT, WIDTH, 4)


def export(job, ffmpeg, target):
    log, progress = [], []
    result = render_subtitle_video(job, ffmpeg_dir=ffmpeg.parent, logger=log.append,
                                   on_progress=lambda done, total: progress.append((done, total)))
    executor = 'GPU' if target == 'gpu' else 'Raster CPU'
    assert any(f'Skia 字幕后端：{executor}' in entry for entry in log), log
    assert progress == [(frame, len(progress)) for frame in range(1, len(progress) + 1)]
    return result, len(progress)


def test_export_page_skia_checkbox_switches_preview_and_freezes_export(binary, supported_project, monkeypatch, tmp_path):
    import time
    from PyQt6.QtWidgets import QApplication
    from krok_helper.subtitle_render.frontend.main_window import SubtitleRenderWindow
    from krok_helper.subtitle_render.frontend.preview.skia_async import SkiaAsyncSubtitleRenderer

    class Settings:
        def load(self):
            return {}

        def save(self, state):
            assert 'use_skia_backend' not in state.get('output', {})

    monkeypatch.delenv('KROK_SUBTITLE_RENDER_BACKEND', raising=False)
    monkeypatch.setenv('KROK_SUBTITLE_SKIA_FALLBACK', 'none')
    window = SubtitleRenderWindow(embedded=True, settings_provider=Settings())
    try:
        controls = window._export_controls
        assert not controls.skia_backend_check.isChecked()
        track, style = supported_project
        window._timing_track = track
        window._style = style
        controls.skia_backend_check.click()
        assert controls.skia_backend_check.isChecked()
        controls.gpu_preview_check.setChecked(True)
        controls.gpu_export_check.setChecked(True)
        panel = window._preview_panel
        assert isinstance(panel.canvas._async_renderer, SkiaAsyncSubtitleRenderer)
        panel.set_output_size(WIDTH, HEIGHT)
        panel.set_style(style)
        panel.set_track(track)
        panel.set_time(550)
        window._preview_window.show()
        deadline = time.monotonic() + 20
        while panel.current_subtitle_frame() is None and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(.01)
        assert panel.current_subtitle_frame() is not None
        assert panel.canvas._async_renderer.current_backend_mode == 'gpu'

        controls.format_combo.setCurrentIndex(controls.format_combo.findData(OUTPUT_FORMAT_PNG_TRANSPARENT))
        controls.directory_edit.setText(str(tmp_path))
        controls.name_edit.setText('ui-skia')
        job = window._build_render_job()
        assert job.use_skia_backend and job.gpu_export_enabled
        controls.gpu_preview_check.setChecked(False)
        assert panel.canvas._async_renderer.selection.target == 'raster'
        controls.gpu_export_check.setChecked(False)
        raster_job = window._build_render_job()
        assert raster_job.use_skia_backend and not raster_job.gpu_export_enabled
        controls.skia_backend_check.click()
        assert not isinstance(panel.canvas._async_renderer, SkiaAsyncSubtitleRenderer)
        assert not window._build_render_job().use_skia_backend
        job = replace(job, width=WIDTH, height=HEIGHT, fps=30, duration_ms=1000)
        result, count = export(job, export_ffmpeg(tmp_path), 'gpu')
        assert count == 30 and any(read_png(path)[..., 3].any() for path in result.glob('*.png'))
    finally:
        window._preview_window.close()
        window.close()
        window.deleteLater()
        QApplication.processEvents()


@pytest.mark.parametrize('fmt', [OUTPUT_FORMAT_PNG_TRANSPARENT, OUTPUT_FORMAT_PNG_COMPOSITED,
                                 OUTPUT_FORMAT_MOV_QTRLE, OUTPUT_FORMAT_MOV_TRANSPARENT])
def test_export_format_samples(binary, supported_project, export_target, fmt, tmp_path):
    ffmpeg = export_ffmpeg(tmp_path)
    track, style = supported_project
    colors = KaraokeColors(before=KaraokeColorState(text=PaintFill(color='#9CE6732A'), stroke=PaintFill(color='#B026348A')),
                          after=KaraokeColorState(text=PaintFill(color='#C050D19B'), stroke=PaintFill(color='#9019577D')))
    style = replace(style, stroke_width_px=3, karaoke_colors=colors)
    sequence = is_png_sequence(fmt)
    output = tmp_path / ('字幕の連番' if sequence else '字幕の映像.mov')
    tone_path = tmp_path / '歌声の音軌.wav'
    tone = write_tone(tone_path, 625, 1000)
    job = RenderJob(track, style, tmp_path / 'unused', output, fps=120, duration_ms=1000,
                    width=WIDTH, height=HEIGHT, gpu_export_enabled=export_target == 'gpu',
                    output_format=fmt, audio_path=tone_path,
                    background_source=BackgroundSource(kind='solid', color='#183048'))
    result, count = export(job, ffmpeg, export_target)
    assert result == output and count == 120
    indices = [32, 71, 109]
    times = [frame_timestamp(index, job.fps) for index in indices]
    with SkiaRendererProcess(binary, selection=selection(export_target)) as process:
        process.configure(track, style, width=WIDTH, height=HEIGHT, fps=job.fps, duration_ms=job.duration_ms)
        reference, _ = collect(process, times)
    if sequence:
        paths = sorted(result.iterdir())
        assert [path.name for path in paths] == [f'{output.name}_{i:06d}.png' for i in range(1, 121)]
        actual = np.array([read_png(paths[index]) for index in indices])
        before = paths[0].read_bytes()
        with pytest.raises(ProcessingError, match='文件夹已存在'):
            render_subtitle_video(job, ffmpeg_dir=ffmpeg.parent, on_process_started=lambda _: pytest.fail('started FFmpeg'))
        assert sorted(result.iterdir()) == paths and paths[0].read_bytes() == before
    else:
        frames = decode_frames(ffmpeg, result)
        assert len(frames) == 120
        actual = frames[indices]
        np.testing.assert_array_equal(decode_audio(ffmpeg, result), tone)

    reference = np.asarray(reference, dtype=np.int16)
    difference = np.abs(actual.astype(np.int16) - reference)
    alpha = reference[..., 3]
    assert (alpha == 0).any() and ((alpha > 32) & (alpha < 224)).sum() > 100
    assert np.any(reference[0] != reference[-1])
    if fmt == OUTPUT_FORMAT_PNG_COMPOSITED:
        background = np.array([24, 48, 72])
        assert (actual[..., 3] == 255).all()
        assert np.abs(actual[:, 0, 0, :3] - background).max() <= 2
        opacity = alpha[..., None] / 255
        expected = reference[..., :3] * opacity + background * (1 - opacity)
        assert np.abs(actual[..., :3] - expected)[alpha >= 16].mean() <= 2
    else:
        prores = fmt == OUTPUT_FORMAT_MOV_TRANSPARENT
        assert difference[..., 3].max() <= (1 if prores else 0)
        assert difference[..., :3][alpha >= 16].mean() <= (8 if prores else 1)


@pytest.mark.parametrize('fit', ['contain', 'cover'])
def test_export_mp4_rolls_keep_picture_and_audio_timeline(binary, supported_project, export_target, tmp_path, fit):
    ffmpeg = export_ffmpeg(tmp_path)
    track, style = supported_project
    track = replace(track, lines=[replace(line, display_start_override_ms=0, display_end_override_ms=1000)
                                  for line in track.lines])
    image_path = tmp_path / '背景の画像.png'
    image = QImage(160, 120, QImage.Format.Format_RGB32)
    image.fill(QColor('#17D72F'))
    assert image.save(str(image_path))
    main_audio = tmp_path / '本編.wav'
    write_tone(main_audio, 625, 1200)

    def roll(color, name, frequency):
        path = tmp_path / name
        write_tone(path, frequency, 200)
        return RollSegmentConfig(enabled=True, source=BackgroundSource(kind='solid', color=color),
                                 duration_ms=200, main_transition_ms=0, self_transition_ms=0, audio_path=str(path))

    output = tmp_path / '動画の書き出し.mp4'
    job = RenderJob(track, style, tmp_path / 'unused', output, width=WIDTH, height=HEIGHT,
                    background_source=BackgroundSource(kind='image', path=str(image_path), image_fit=fit),
                    audio_path=main_audio, main_audio_end_ms=1200, fps=60, preset='ultrafast',
                    gpu_export_enabled=export_target == 'gpu', pre_roll=roll('#D12331', '前奏.wav', 390),
                    post_roll=roll('#2331D1', '後奏.wav', 885))
    result, count = export(job, ffmpeg, export_target)
    frames = decode_frames(ffmpeg, result)
    assert len(frames) == count == 96
    for index, expected in [(6, [209, 35, 49]), (39, [23, 215, 47]), (90, [35, 49, 209])]:
        assert np.abs(frames[index, 10, WIDTH // 2, :3] - expected).max() <= 4
    expected_edge = [0, 0, 0] if fit == 'contain' else [23, 215, 47]
    assert np.abs(frames[39, 10, 3, :3] - expected_edge).max() <= 4
    assert (frames[39, ..., :3].min(axis=-1) > 160).sum() > 100
    assert (frames[[6, 90], ..., :3].min(axis=-1) > 160).sum() == 0

    samples = decode_audio(ffmpeg, result)
    assert abs(len(samples) - int(AUDIO_RATE * 1.6)) <= 1024  # AAC packet padding.
    for center_ms, frequency in [(100, 390), (650, 625), (1500, 885)]:
        center = AUDIO_RATE * center_ms // 1000
        radius = AUDIO_RATE // 20
        window = samples[center - radius:center + radius]
        spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window))))
        assert np.sqrt(np.mean(window.astype(float) ** 2)) > 1500
        assert abs(np.argmax(spectrum) * AUDIO_RATE / len(window) - frequency) <= 10
