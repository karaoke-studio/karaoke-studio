"""SubtitleRenderWindow 的素材加载器测试（A2 / A3）。

通过 monkeypatch ``probe_media`` 避免真实 ffprobe 调用；通过 ``QT_QPA_PLATFORM=offscreen``
保证无显示器环境也能构造 Qt widget。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.models import MediaInfo  # noqa: E402
from krok_helper.subtitle_render.frontend import main_window as mw  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_window(qapp, monkeypatch):
    # 把弹错对话框 stub 掉，避免测试期间真的弹窗
    monkeypatch.setattr(mw.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: None)
    # ffprobe 路径解析也 stub，避免真的去找
    monkeypatch.setattr(
        mw.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    return mw.SubtitleRenderWindow(embedded=False)


def test_load_video_stores_info_on_success(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "bg.mp4",
        duration=120.5,
        video_streams=1,
        audio_streams=1,
        subtitle_streams=0,
        sample_rate=48000,
        channels=2,
        video_width=1920,
        video_height=1080,
        video_fps=59.94,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    result = win.load_video(tmp_path / "bg.mp4")

    assert result is fake_info
    assert win.video_info is fake_info
    assert win._video_path == tmp_path / "bg.mp4"
    # 摘要应该至少含分辨率与 fps
    assert "1920×1080" in win._summary_label.text()
    assert "59.940" in win._summary_label.text() or "59.94" in win._summary_label.text()


def test_load_video_rejects_audio_only_file(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "song.flac",
        duration=180,
        video_streams=0,
        audio_streams=1,
        subtitle_streams=0,
        sample_rate=44100,
        channels=2,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    result = win.load_video(tmp_path / "song.flac")
    assert result is None
    assert win.video_info is None


def test_load_audio_stores_info_on_success(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "song.wav",
        duration=200.0,
        video_streams=0,
        audio_streams=1,
        subtitle_streams=0,
        sample_rate=44100,
        channels=2,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    result = win.load_audio(tmp_path / "song.wav")
    assert result is fake_info
    assert win.audio_info is fake_info
    assert "44100" in win._summary_label.text()
    assert "2 声道" in win._summary_label.text()


def test_load_audio_rejects_video_with_no_audio(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "silent.mp4",
        duration=60,
        video_streams=1,
        audio_streams=0,
        subtitle_streams=0,
        video_width=1920,
        video_height=1080,
        video_fps=30.0,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    result = win.load_audio(tmp_path / "silent.mp4")
    assert result is None
    assert win.audio_info is None


def test_subtitle_video_audio_can_coexist_in_summary(qapp, monkeypatch, tmp_path):
    # 字幕 + 视频 + 音频三段同时加载，摘要三段都应出现
    win = _make_window(qapp, monkeypatch)

    # 字幕：手写一行最小内容
    lrc = tmp_path / "lyrics.lrc"
    lrc.write_bytes(
        b"\xef\xbb\xbf"
        + "[00:01:00]a[00:01:50]b[00:02:00]\r\n\r\n@Title=Test\r\n".encode("utf-8")
    )
    win.load_from_lrc(lrc)

    # 视频
    video_info = MediaInfo(
        path=tmp_path / "bg.mp4",
        duration=60,
        video_streams=1,
        audio_streams=1,
        subtitle_streams=0,
        sample_rate=48000,
        channels=2,
        video_width=1920,
        video_height=1080,
        video_fps=60.0,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: video_info)
    win.load_video(tmp_path / "bg.mp4")

    # 音频
    audio_info = MediaInfo(
        path=tmp_path / "song.wav",
        duration=60,
        video_streams=0,
        audio_streams=1,
        subtitle_streams=0,
        sample_rate=44100,
        channels=2,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: audio_info)
    win.load_audio(tmp_path / "song.wav")

    text = win._summary_label.text()
    assert "【字幕】" in text and "Test" in text
    assert "【背景视频】" in text and "1920×1080" in text
    assert "【音频】" in text and "44100" in text
