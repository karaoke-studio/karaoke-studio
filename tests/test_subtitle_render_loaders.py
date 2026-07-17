"""SubtitleRenderWindow 的素材加载器测试。

通过 monkeypatch ``probe_media`` 避免真实 ffprobe 调用；通过
``QT_QPA_PLATFORM=offscreen`` 保证无显示器环境也能构造 Qt widget。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from qfluentwidgets import (  # noqa: E402
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SegmentedWidget,
    SpinBox,
    TransparentToolButton,
)

from krok_helper.models import MediaInfo  # noqa: E402
from krok_helper.subtitle_render.models import (  # noqa: E402
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.frontend import main_window as mw  # noqa: E402
from krok_helper.subtitle_render.frontend.lyrics_list import COL_CONTENT  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_window(qapp, monkeypatch):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)
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

    table = win._lyrics_panel.table_widget
    # body 4 行（含中间空行）
    assert table.rowCount() == 4
    assert table.item(0, COL_CONTENT).text() == "あ"
    assert table.item(1, COL_CONTENT).text() == "いう"
    assert table.item(2, COL_CONTENT).text() == ""  # 空行
    assert table.item(3, COL_CONTENT).text() == "え"


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


def test_static_sequence_and_solid_backgrounds_populate_preview(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    image = tmp_path / "frame_0000.png"
    frame = QImage(64, 36, QImage.Format.Format_RGB32)
    frame.fill(QColor("#123456"))
    assert frame.save(str(image))

    assert win.load_background_image(image) is True
    assert win._background_source.kind == "image"
    assert win._preview_panel.is_populated()

    assert win.load_background_sequence(image, 24) is True
    assert win._background_source.kind == "image_sequence"
    assert win._background_source.path.endswith("frame_%04d.png")
    assert win._background_source.source_fps == 24
    assert win._background_source.sequence_start_number == 0

    win.set_solid_background("#654321")
    assert win._background_source.kind == "solid"
    assert win._background_source.color == "#654321"


def test_browse_background_sequence_uses_fluent_fps_input(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    image = tmp_path / "frame_0000.png"
    frame = QImage(64, 36, QImage.Format.Format_RGB32)
    frame.fill(QColor("#123456"))
    assert frame.save(str(image))
    monkeypatch.setattr(
        mw.QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(image), ""),
    )
    captured: dict[str, object] = {}

    def get_int(parent, title, label, **kwargs):
        captured.update(parent=parent, title=title, label=label, kwargs=kwargs)
        return 24, True

    monkeypatch.setattr(mw, "fluent_get_int", get_int)
    expected_default_fps = win._screen_settings.fps
    win._browse_background_sequence()

    assert captured == {
        "parent": win,
        "title": "图片序列帧率",
        "label": "源帧率（每秒图片数）",
        "kwargs": {
            "value": expected_default_fps,
            "minimum": 1,
            "maximum": 240,
        },
    }
    assert win._background_source.kind == "image_sequence"
    assert win._background_source.source_fps == 24


def test_build_render_job_uses_independent_audio_with_static_background(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    lrc = tmp_path / "lyrics.lrc"
    lrc.write_text("[00:00:00]a[00:01:00]\n", encoding="utf-8")
    win.load_from_lrc(lrc)
    win.set_solid_background("#000000")

    audio = tmp_path / "song.wav"
    audio.write_bytes(b"fake")
    audio_info = MediaInfo(
        path=audio, duration=5.0, video_streams=0, audio_streams=1,
        subtitle_streams=0, sample_rate=48000, channels=2,
    )
    monkeypatch.setattr(mw, "probe_media", lambda probe, path: audio_info)
    win.load_audio(audio)
    win._export_dir_edit.setText(str(tmp_path))
    win._export_name_edit.setText("out")

    job = win._build_render_job()

    assert job.background_video_path is None
    assert job.background_source.kind == "solid"
    assert job.audio_path == audio
    assert job.include_audio is True
    assert job.duration_ms == 5000


def test_video_background_rejects_independent_audio(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    video = tmp_path / "bg.mp4"
    audio = tmp_path / "song.wav"
    video_info = MediaInfo(
        path=video, duration=5.0, video_streams=1, audio_streams=1,
        subtitle_streams=0, sample_rate=48000, channels=2,
        video_width=320, video_height=180, video_fps=60.0,
    )
    audio_info = MediaInfo(
        path=audio, duration=5.0, video_streams=0, audio_streams=1,
        subtitle_streams=0, sample_rate=48000, channels=2,
    )
    monkeypatch.setattr(
        mw, "probe_media", lambda probe, path: video_info if path == video else audio_info
    )

    win.load_video(video)
    assert win.load_audio(audio) is None
    assert win._audio_path == video
    assert all(not action.isEnabled() for action in win._audio_menu_actions)


def test_switching_to_video_clears_existing_independent_audio(
    qapp, monkeypatch, tmp_path
):
    win = _make_window(qapp, monkeypatch)
    audio = tmp_path / "song.wav"
    video = tmp_path / "silent.mp4"
    audio_info = MediaInfo(
        path=audio, duration=5.0, video_streams=0, audio_streams=1,
        subtitle_streams=0, sample_rate=48000, channels=2,
    )
    video_info = MediaInfo(
        path=video, duration=5.0, video_streams=1, audio_streams=0,
        subtitle_streams=0, video_width=320, video_height=180, video_fps=60.0,
    )
    monkeypatch.setattr(
        mw, "probe_media", lambda probe, path: audio_info if path == audio else video_info
    )

    win.set_solid_background("#000000")
    assert win.load_audio(audio) is audio_info
    win.load_video(video)

    assert win._audio_path is None
    assert win._audio_info is None


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
    win._export_dir_edit.setText(str(tmp_path))
    win._export_name_edit.setText("custom")
    win._export_width_spin.setValue(1280)
    win._export_height_spin.setValue(720)
    win._export_fps_combo.setCurrentIndex(win._export_fps_combo.findData(120))
    win._export_encoder_combo.setCurrentIndex(win._export_encoder_combo.findData("nvenc"))
    win._export_preset_combo.setCurrentText("slow")
    win._export_crf_spin.setValue(23)
    blocked = win._export_native_check.blockSignals(True)
    win._export_native_check.setChecked(True)
    win._export_native_check.blockSignals(blocked)
    job = win._build_render_job()

    assert job.background_video_path == video
    assert job.output_path == output
    assert job.width == 1280
    assert job.height == 720
    assert job.fps == 120
    assert job.duration_ms == 5000
    assert job.include_audio is True
    assert job.encoder_mode == "nvenc"
    assert job.preset == "slow"
    assert job.crf == 23
    assert job.native_export_enabled is False


def test_export_output_prefills_dir_and_yurika_name(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)

    def _info(path):
        return MediaInfo(
            path=path, duration=5.0, video_streams=1, audio_streams=0,
            subtitle_streams=0, video_width=320, video_height=180, video_fps=60.0,
        )

    monkeypatch.setattr(mw, "probe_media", lambda probe, path: _info(path))

    win.load_video(tmp_path / "Dark spiral journey.mp4")
    assert win._export_dir_edit.text() == str(tmp_path)
    assert win._export_name_edit.text() == "Dark spiral journey_yurika出力"

    # 文件名未被用户改过 → 跟随视频切换更新
    win.load_video(tmp_path / "second.mp4")
    assert win._export_name_edit.text() == "second_yurika出力"

    # 用户自定义文件名后，切换视频不再覆盖
    win._export_name_edit.setText("my custom")
    win.load_video(tmp_path / "third.mp4")
    assert win._export_name_edit.text() == "my custom"


def _install_active_render(win):
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
    return worker


def test_stop_render_export_confirms_before_requesting_cancel(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    worker = _install_active_render(win)
    questions = []

    def confirm(*args, **kwargs):
        questions.append((args, kwargs))
        return True

    monkeypatch.setattr(mw, "fluent_question", confirm)

    win._stop_render_export()

    assert len(questions) == 1
    args, kwargs = questions[0]
    assert args[0] is win
    assert args[1] == "停止导出"
    assert "未完成文件" in args[2]
    assert kwargs == {
        "yes_text": "停止导出",
        "no_text": "继续导出",
        "default_cancel": True,
    }
    assert worker.cancel_called is True
    assert win._export_stop_button.isEnabled() is False
    assert "停止导出" in win._export_status_label.text()


def test_stop_render_export_keeps_running_when_confirmation_is_rejected(
    qapp, monkeypatch
):
    win = _make_window(qapp, monkeypatch)
    worker = _install_active_render(win)
    win._export_status_label.setText("正在导出… 10/100 帧")
    monkeypatch.setattr(mw, "fluent_question", lambda *args, **kwargs: False)

    win._stop_render_export()

    assert worker.cancel_called is False
    assert win._export_stop_button.isEnabled() is True
    assert win._export_status_label.text() == "正在导出… 10/100 帧"


def test_render_log_does_not_flash_ffmpeg_command_in_status(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    win._export_status_label.setText("正在准备导出…")

    win._on_render_log("执行命令:")
    assert win._export_status_label.text() == "正在准备导出…"

    win._on_render_log("ffmpeg -y -c:v h264_nvenc output.mp4")
    assert win._export_status_label.text() == "正在准备导出…"

    win._on_render_log("多进程导出: 8 个 worker")
    assert win._export_status_label.text() == "多进程导出: 8 个 worker"


def test_render_log_does_not_flash_late_sei_warning_in_status(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    progress = "正在导出… 3002/10079 帧"
    win._export_status_label.setText(progress)

    win._on_render_log(
        "[h264 @ 000001b8440d22c0] Late SEI is not implemented. "
        "Update your FFmpeg version to the newest one from Git."
    )
    assert win._export_status_label.text() == progress

    win._on_render_log(
        "[h264 @ 000001b8440d22c0] If you want to help, upload a sample of this file "
        "to https://streams.videolan.org/upload/ and contact the ffmpeg-devel mailing list."
    )
    assert win._export_status_label.text() == progress


# ---------------------------------------------------------------------------
# 布局完整性
# ---------------------------------------------------------------------------


def test_window_shell_components_present(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)

    # 底部导航按钮 + stack
    assert win._nav_btns is not None
    assert len(win._nav_btns) == 2
    assert win._stack.count() == 2

    # 四区 widget 已挂载
    assert win._lyrics_panel is not None
    assert win._preview_panel is not None
    assert win._transport_bar is not None
    assert win._property_panel is not None
    assert win._tracks_view is not None
    assert win._export_start_button is not None
    assert win._export_stop_button is not None
    assert win._export_stop_button.isEnabled() is False
    assert win._export_encoder_combo is not None
    assert win._export_preset_combo is not None
    assert win._export_crf_spin.value() == 18
    assert win._export_native_check is not None
    assert isinstance(win._bottom_navigation, SegmentedWidget)
    assert isinstance(win._export_dir_edit, LineEdit)
    assert isinstance(win._export_name_edit, LineEdit)
    assert isinstance(win._export_codec_combo, ComboBox)
    assert win._export_codec_combo.currentData() == "h264"
    assert isinstance(win._export_encoder_combo, ComboBox)
    assert isinstance(win._export_crf_spin, SpinBox)
    assert isinstance(win._export_progress, ProgressBar)
    assert isinstance(win._export_start_button, PrimaryPushButton)
    assert isinstance(win._export_stop_button, PushButton)
    assert isinstance(win._lyrics_panel._add_source_btn, TransparentToolButton)
    assert isinstance(win._lyrics_panel._remove_source_btn, TransparentToolButton)

    # 属性面板 5 个分类页（顶部 segmented 导航，页面用 accessibleName 标注）
    assert win._property_panel.count() == 5
    assert [win._property_panel.widget(i).accessibleName() for i in range(5)] == [
        "字体",
        "布局",
        "时间",
        "特效",
        "标题",
    ]


def test_bottom_navigation_switches_export_and_back_to_preview(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)

    win._nav_btns["export"].click()
    qapp.processEvents()
    assert win._stack.currentWidget() is win._export_tab
    assert win._bottom_navigation.currentRouteKey() == "export"

    win._nav_btns["preview"].click()
    qapp.processEvents()
    assert win._stack.currentWidget() is win._preview_tab
    assert win._bottom_navigation.currentRouteKey() == "preview"


def test_preview_window_is_visible_only_on_preview_tab_and_pauses(
    qapp, monkeypatch
):
    win = _make_window(qapp, monkeypatch)
    win.resize(1280, 720)
    win.show()
    qapp.processEvents()
    paused: list[bool] = []
    monkeypatch.setattr(win._transport_bar, "pause", lambda: paused.append(True))
    win._transport_bar.set_time(1234)

    win._show_preview_window()
    qapp.processEvents()
    preview_geometry = win._preview_window.geometry()
    assert win._preview_window.isVisible() is True

    win._nav_btns["export"].click()
    qapp.processEvents()
    assert win._preview_window.isVisible() is False
    assert paused
    assert win._transport_bar.current_time_ms == 1234

    win._nav_btns["preview"].click()
    qapp.processEvents()
    assert win._preview_window.isVisible() is True
    assert win._preview_window.geometry() == preview_geometry
    assert win._transport_bar.current_time_ms == 1234
    win.close()


def test_preview_window_hides_across_workflow_visibility_and_restores_paused(
    qapp, monkeypatch
):
    win = _make_window(qapp, monkeypatch)
    win.show()
    qapp.processEvents()
    paused: list[bool] = []
    monkeypatch.setattr(win._transport_bar, "pause", lambda: paused.append(True))
    win._show_preview_window()
    qapp.processEvents()
    assert win._preview_window.isVisible() is True

    win.hide()
    qapp.processEvents()
    assert win._preview_window.isVisible() is False
    assert paused

    win.show()
    qapp.processEvents()
    assert win._preview_window.isVisible() is True
    win.close()


def test_user_closed_preview_does_not_reopen_after_context_switch(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    win.show()
    qapp.processEvents()
    win._show_preview_window()
    qapp.processEvents()

    win._preview_window._close_button.click()
    qapp.processEvents()
    assert win._preview_window_requested is False
    assert win._preview_window.isVisible() is False

    win._nav_btns["export"].click()
    win._nav_btns["preview"].click()
    qapp.processEvents()
    assert win._preview_window.isVisible() is False
    win.close()


def test_preview_request_is_deferred_while_hidden_or_exporting(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    win._request_preview_window()
    assert win._preview_window.isVisible() is False

    win.show()
    qapp.processEvents()
    assert win._preview_window.isVisible() is True

    win._render_thread = object()
    win._sync_preview_window_visibility()
    qapp.processEvents()
    assert win._preview_window.isVisible() is False

    win._render_thread = None
    win._sync_preview_window_visibility()
    qapp.processEvents()
    assert win._preview_window.isVisible() is True
    win.close()


def test_lyrics_list_centers_only_explicit_single_line_page(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    lines = [
        TimingLine(chars=[TimingChar(text=text, start_ms=i * 1000)], end_ms=(i + 1) * 1000)
        for i, text in enumerate(("感傷", "哀しみ", "真っ白", "わたし"))
    ]
    lines[2].break_before = "page"
    lines[3].break_before = "paragraph"
    win._lyrics_panel.set_track(TimingTrack(lines=lines))

    second_alignment = win._lyrics_panel._table.item(1, COL_CONTENT).textAlignment()
    single_alignment = win._lyrics_panel._table.item(2, COL_CONTENT).textAlignment()
    assert second_alignment & Qt.AlignmentFlag.AlignRight
    assert single_alignment & Qt.AlignmentFlag.AlignHCenter


def test_preview_window_corners_stay_above_player_controls(qapp, monkeypatch):
    """控制栏显示后，四角仍须命中无边框窗口的 resize grip。"""
    win = _make_window(qapp, monkeypatch)
    preview = win._preview_window
    preview.resize(800, 450)
    preview.show()
    qapp.processEvents()
    preview.show_controls()

    edge = mw.Qt.Edge
    expected = {
        QPoint(2, 2): (edge.TopEdge | edge.LeftEdge).value,
        QPoint(preview.width() - 3, 2): (edge.TopEdge | edge.RightEdge).value,
        QPoint(2, preview.height() - 3): (edge.BottomEdge | edge.LeftEdge).value,
        QPoint(preview.width() - 3, preview.height() - 3): (
            edge.BottomEdge | edge.RightEdge
        ).value,
    }
    for point, edge_bits in expected.items():
        hit = preview.childAt(point)
        assert isinstance(hit, mw._WindowEdgeGrip)
        assert hit._edge_bits == edge_bits


def test_preview_window_resizes_from_all_four_corners(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)
    preview = win._preview_window
    preview.show()
    qapp.processEvents()

    edge = Qt.Edge
    cases = [
        (edge.TopEdge | edge.LeftEdge, -30, -20),
        (edge.TopEdge | edge.RightEdge, 30, -20),
        (edge.BottomEdge | edge.LeftEdge, -30, 20),
        (edge.BottomEdge | edge.RightEdge, 30, 20),
    ]
    for edges, dx, dy in cases:
        preview.setGeometry(100, 100, 800, 450)
        qapp.processEvents()
        grip = next(g for g in preview._edge_grips if g._edge_bits == edges.value)
        QTest.mousePress(grip, Qt.MouseButton.LeftButton, pos=QPoint(7, 7))
        QTest.mouseMove(grip, QPoint(7 + dx, 7 + dy))
        QTest.mouseRelease(
            grip,
            Qt.MouseButton.LeftButton,
            pos=QPoint(7 + dx, 7 + dy),
        )
        qapp.processEvents()

        assert preview.width() == 830
        assert preview.height() == 470


def test_drop_panel_accepts_correct_extensions(qapp, monkeypatch, tmp_path):
    """歌词 / 预览两个拖拽面板的扩展名校验。"""
    win = _make_window(qapp, monkeypatch)

    lrc = tmp_path / "x.lrc"
    lrc.write_text("[00:00:00]a[00:00:50]", encoding="utf-8")
    mp4 = tmp_path / "x.mp4"
    mp4.write_bytes(b"\x00")
    png = tmp_path / "x.png"
    png.write_bytes(b"\x00")

    assert win._lyrics_panel.accepts(lrc) is True
    assert win._lyrics_panel.accepts(mp4) is False

    assert win._preview_panel.accepts(mp4) is True
    assert win._preview_panel.accepts(png) is True
    assert win._preview_panel.accepts(lrc) is False

    actions = [
        win._preview_panel._empty_actions.itemAt(i).widget().text()
        for i in range(win._preview_panel._empty_actions.count())
    ]
    assert actions == ["视频", "静态图", "图片序列", "纯色"]
