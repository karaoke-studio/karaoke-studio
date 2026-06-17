"""SubtitleRenderWindow 的素材加载器测试。

通过 monkeypatch ``probe_media`` 避免真实 ffprobe 调用；通过
``QT_QPA_PLATFORM=offscreen`` 保证无显示器环境也能构造 Qt widget。
"""

from __future__ import annotations

import os

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
    monkeypatch.setattr(mw.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(
        mw.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    return mw.SubtitleRenderWindow(embedded=False)


# ---------------------------------------------------------------------------
# A1 字幕：填充左侧歌词列表
# ---------------------------------------------------------------------------


def test_load_subtitle_wires_preview_and_transport(qapp, monkeypatch, tmp_path):
    """A4：加载字幕后预览面板 / 时间轴滑块都应该联动起来。"""
    win = _make_window(qapp, monkeypatch)
    assert not win._preview_panel.is_populated()

    lrc = tmp_path / "demo.lrc"
    lrc.write_bytes(
        b"\xef\xbb\xbf"
        + (
            "[00:01:00]a[00:01:50]b[00:02:00]c[00:02:50]\r\n"
            "\r\n"
            "@Title=Foo\r\n"
        ).encode("utf-8")
    )
    track = win.load_from_lrc(lrc)
    assert track is not None

    # 预览面板被切到 populated 状态，canvas 拿到了 track
    assert win._preview_panel.is_populated()
    assert win._preview_panel.canvas._track is track

    # transport 滑块上限按 track 时长收敛（行末 2500ms）
    assert win._transport_bar._slider.maximum() == 2500

    # 滑块拖动 → preview canvas 同步时间
    win._transport_bar.set_time(1700)
    assert win._preview_panel.canvas.current_time_ms == 1700


def test_load_subtitle_populates_lyrics_panel(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    assert not win._lyrics_panel.is_populated()

    lrc = tmp_path / "lyrics.lrc"
    lrc.write_bytes(
        b"\xef\xbb\xbf"
        + (
            "[00:01:00]あ[00:01:50]\r\n"
            "[00:02:00]い[00:02:50]う[00:03:00]\r\n"
            "\r\n"
            "[00:04:00]え[00:04:50]\r\n"
            "\r\n"
            "@Title=Demo\r\n"
        ).encode("utf-8")
    )

    track = win.load_from_lrc(lrc)
    assert track is not None
    assert win._lyrics_panel.is_populated()

    list_widget = win._lyrics_panel.list_widget
    # body 4 行（含中间空行）
    assert list_widget.count() == 4
    assert list_widget.item(0).text() == "あ"
    assert list_widget.item(1).text() == "いう"
    assert list_widget.item(2).text() == ""  # 空行
    assert list_widget.item(3).text() == "え"


# ---------------------------------------------------------------------------
# A2 / A3 视频 / 音频加载
# ---------------------------------------------------------------------------


def test_load_video_populates_preview_panel(qapp, monkeypatch, tmp_path):
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

    assert not win._preview_panel.is_populated()
    result = win.load_video(tmp_path / "bg.mp4")
    assert result is fake_info
    assert win.video_info is fake_info
    assert win._video_path == tmp_path / "bg.mp4"
    assert win._preview_panel.is_populated()
    assert win._preview_panel.canvas.has_video_source


def test_load_video_with_missing_test_file_does_not_start_video_player(qapp, monkeypatch, tmp_path):
    """A7 稳定性：probe 已 mock 时，假路径不应启动 Qt Multimedia 后台线程。"""
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "bg.mp4",
        duration=10.0,
        video_streams=1,
        audio_streams=0,
        subtitle_streams=0,
        video_width=1280,
        video_height=720,
        video_fps=30.0,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    result = win.load_video(tmp_path / "bg.mp4")

    assert result is fake_info
    assert win._preview_panel.canvas.has_video_source
    assert win._preview_panel.canvas._video_player is None


def test_transport_playback_state_syncs_to_preview_canvas(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)

    win._transport_bar.play()
    assert win._preview_panel.canvas._video_playing is True
    win._transport_bar.pause()

    assert win._preview_panel.canvas._video_playing is False


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
    assert not win._preview_panel.is_populated()


def test_load_audio_via_api_sets_audio_info(qapp, monkeypatch, tmp_path):
    """load_audio 公开 API 仍可用——给将来高级用户 / A10 嵌入工作流喂独立音频。

    UI 当前不暴露此入口（音频从视频自动取），但 API 必须保持 round-trip。
    """
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
    assert win._audio_path == tmp_path / "song.wav"


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


def test_load_video_auto_loads_audio_from_same_file(qapp, monkeypatch, tmp_path):
    """新增 A7 后行为：视频含音频流时，load_video 自动把视频路径喂给 TransportBar。"""
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
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
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    win.load_video(tmp_path / "bg.mp4")
    # audio_path / audio_info 应该同步指向视频文件
    assert win._audio_path == tmp_path / "bg.mp4"
    assert win.audio_info is fake_info


def test_load_video_without_audio_stream_keeps_audio_unset(qapp, monkeypatch, tmp_path):
    """视频无音频流时不应错误地把视频路径设为 audio_source。"""
    win = _make_window(qapp, monkeypatch)
    fake_info = MediaInfo(
        path=tmp_path / "silent.mp4",
        duration=60,
        video_streams=1,
        audio_streams=0,
        subtitle_streams=0,
        video_width=1280,
        video_height=720,
        video_fps=30.0,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: fake_info)

    win.load_video(tmp_path / "silent.mp4")
    assert win._audio_path is None
    assert win.audio_info is None


def test_subtitle_and_video_panels_can_coexist(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)

    # 字幕
    lrc = tmp_path / "lyrics.lrc"
    lrc.write_bytes(
        b"\xef\xbb\xbf"
        + "[00:01:00]a[00:01:50]b[00:02:00]\r\n\r\n@Title=Test\r\n".encode("utf-8")
    )
    win.load_from_lrc(lrc)

    # 视频（带音频流）
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

    assert win._lyrics_panel.is_populated()
    assert win._preview_panel.is_populated()
    # 音频自动来自视频
    assert win.audio_info is video_info


def test_export_tab_builds_render_job_from_loaded_media(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)

    lrc = tmp_path / "lyrics.lrc"
    lrc.write_bytes(
        b"\xef\xbb\xbf"
        + "[00:01:00]a[00:01:50]b[00:02:00]\r\n\r\n@Title=Test\r\n".encode("utf-8")
    )
    win.load_from_lrc(lrc)

    video_info = MediaInfo(
        path=tmp_path / "bg.mp4",
        duration=5.0,
        video_streams=1,
        audio_streams=1,
        subtitle_streams=0,
        video_width=1920,
        video_height=1080,
        video_fps=60.0,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: video_info)
    video = tmp_path / "bg.mp4"
    win.load_video(video)

    output = tmp_path / "custom.mp4"
    win._export_output_edit.setText(str(output))
    win._export_width_spin.setValue(1280)
    win._export_height_spin.setValue(720)
    win._export_fps_spin.setValue(60)
    job = win._build_render_job()

    assert job.background_video_path == video
    assert job.output_path == output
    assert job.width == 1280
    assert job.height == 720
    assert job.fps == 60
    assert job.duration_ms == 5000
    assert job.include_audio is True


def test_stop_render_export_requests_worker_cancel(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)

    class FakeThread:
        def isRunning(self):
            return True

    class FakeWorker:
        def __init__(self):
            self.cancel_called = False

        def cancel(self):
            self.cancel_called = True

    worker = FakeWorker()
    win._render_thread = FakeThread()
    win._render_worker = worker
    win._export_stop_button.setEnabled(True)

    win._stop_render_export()

    assert worker.cancel_called is True
    assert win._export_stop_button.isEnabled() is False
    assert "停止导出" in win._export_status_label.text()


# ---------------------------------------------------------------------------
# 布局完整性
# ---------------------------------------------------------------------------


def test_window_shell_components_present(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)

    # Pivot + stack
    assert win._pivot is not None
    assert win._stack.count() == 2

    # 四区 widget 已挂载
    assert win._lyrics_panel is not None
    assert win._preview_panel is not None
    assert win._transport_bar is not None
    assert win._property_panel is not None
    assert win._waveform_panel is not None
    assert win._tracks_view is not None
    assert win._export_start_button is not None
    assert win._export_stop_button is not None
    assert win._export_stop_button.isEnabled() is False

    # 属性面板 4 个 tab
    assert win._property_panel.count() == 4
    assert [win._property_panel.tabText(i) for i in range(4)] == [
        "基本",
        "字幕",
        "特效",
        "装饰",
    ]


def test_drop_panel_accepts_correct_extensions(qapp, monkeypatch, tmp_path):
    """歌词 / 预览两个拖拽面板的扩展名校验。

    波形面板被改成被动展示后已不再是 DropPanel，所以不出现在这里。
    """
    win = _make_window(qapp, monkeypatch)

    lrc = tmp_path / "x.lrc"
    lrc.write_text("[00:00:00]a[00:00:50]", encoding="utf-8")
    mp4 = tmp_path / "x.mp4"
    mp4.write_bytes(b"\x00")

    assert win._lyrics_panel.accepts(lrc) is True
    assert win._lyrics_panel.accepts(mp4) is False

    assert win._preview_panel.accepts(mp4) is True
    assert win._preview_panel.accepts(lrc) is False
