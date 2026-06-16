"""SubtitleRenderWindow 的素材加载器测试（A1 / A2 / A3 + Sayatoo 布局）。

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


# ---------------------------------------------------------------------------
# A1 字幕：填充左侧歌词列表
# ---------------------------------------------------------------------------


def test_load_subtitle_populates_lyrics_list(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    lrc = tmp_path / "lyrics.lrc"
    lrc.write_bytes(
        b"\xef\xbb\xbf"
        + (
            "【ボーカル】[00:01:00]あ[00:01:50]\r\n"
            "[00:02:00]い[00:02:50]う[00:03:00]\r\n"
            "\r\n"
            "[00:04:00]え[00:04:50]\r\n"
            "\r\n"
            "@Title=Demo\r\n"
        ).encode("utf-8")
    )

    track = win.load_from_lrc(lrc)
    assert track is not None

    list_widget = win._lyrics_list
    # body 共 4 行（含中间空行）
    assert list_widget.rowCount() == 4

    # 行 0：S 标记 + 角色名 "ボーカル" + 内容 "あ"
    assert list_widget.item(0, list_widget.COL_SINGER_FLAG).text() == "S"
    assert list_widget.item(0, list_widget.COL_SINGER_NAME).text() == "ボーカル"
    assert list_widget.item(0, list_widget.COL_CONTENT).text() == "あ"

    # 行 1：未切换演唱者，S 留空
    assert list_widget.item(1, list_widget.COL_SINGER_FLAG).text() == ""
    assert list_widget.item(1, list_widget.COL_CONTENT).text() == "いう"

    # 行 2：空行
    assert list_widget.item(2, list_widget.COL_CONTENT).text() == ""

    # 行 3：有内容
    assert list_widget.item(3, list_widget.COL_CONTENT).text() == "え"


def test_load_subtitle_updates_status_label(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)
    lrc = tmp_path / "lyrics.lrc"
    lrc.write_bytes(
        b"\xef\xbb\xbf"
        + (
            "[00:01:00]a[00:01:50]b[00:02:00]\r\n\r\n@Title=Foo\r\n"
        ).encode("utf-8")
    )
    win.load_from_lrc(lrc)
    text = win._status_label.text()
    assert "字幕：lyrics.lrc" in text
    assert "1 行" in text
    assert "2 字" in text


# ---------------------------------------------------------------------------
# A2 / A3 视频 / 音频加载
# ---------------------------------------------------------------------------


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
    # 顶栏状态条应反映分辨率
    assert "1920×1080" in win._status_label.text()


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
    assert "44100Hz" in win._status_label.text()


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


def test_subtitle_video_audio_can_coexist_in_status(qapp, monkeypatch, tmp_path):
    win = _make_window(qapp, monkeypatch)

    # 字幕
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

    text = win._status_label.text()
    assert "字幕：lyrics.lrc" in text
    assert "视频：bg.mp4" in text and "1920×1080" in text
    assert "音频：song.wav" in text and "44100" in text


# ---------------------------------------------------------------------------
# 布局完整性
# ---------------------------------------------------------------------------


def test_window_shell_components_present(qapp, monkeypatch):
    win = _make_window(qapp, monkeypatch)

    # 四区 widget 都已挂载
    assert win._lyrics_list is not None
    assert win._preview_view is not None
    assert win._transport_bar is not None
    assert win._property_panel is not None
    assert win._waveform_view is not None
    assert win._tracks_view is not None

    # 属性面板 4 个 tab
    assert win._property_panel.count() == 4
    assert [win._property_panel.tabText(i) for i in range(4)] == [
        "基本",
        "字幕",
        "特效",
        "装饰",
    ]
