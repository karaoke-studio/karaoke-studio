from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from krok_helper.video_download import ytdlp_service
from krok_helper.video_download.download_task import DownloadOptions
from krok_helper.video_download.ytdlp_service import (
    ARIA2C_DOWNLOAD_ARGS,
    BILIBILI_HTTP_CHUNK_SIZE,
    MIN_HTTP_RETRIES,
    YtDlpService,
)


BILIBILI_URL = "https://www.bilibili.com/video/BV1tAMy6tESy/"
YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _options(**overrides) -> DownloadOptions:
    base = {"save_dir": "out", "timeout": 10, "retry_count": 3}
    base.update(overrides)
    return DownloadOptions(**base)


def _service(aria2c: str = "") -> YtDlpService:
    service = YtDlpService()
    service._aria2c_path_cache = aria2c
    return service


def test_retry_count_is_floored_to_ytdlp_default() -> None:
    # 界面上的"重试次数"是任务级语义，不该把 HTTP 级重试压到 3 次。
    opts = _service()._download_resilience_opts(BILIBILI_URL, _options(retry_count=3))

    assert opts["retries"] == MIN_HTTP_RETRIES
    assert opts["fragment_retries"] == MIN_HTTP_RETRIES


def test_user_retry_count_above_floor_wins() -> None:
    opts = _service()._download_resilience_opts(BILIBILI_URL, _options(retry_count=25))

    assert opts["retries"] == 25


def test_bilibili_falls_back_to_http_chunking_without_aria2c() -> None:
    opts = _service(aria2c="")._download_resilience_opts(BILIBILI_URL, _options())

    assert opts["http_chunk_size"] == BILIBILI_HTTP_CHUNK_SIZE
    assert "external_downloader" not in opts


def test_bilibili_prefers_aria2c_when_available() -> None:
    opts = _service(aria2c=r"C:\tools\aria2c.exe")._download_resilience_opts(BILIBILI_URL, _options())

    assert opts["external_downloader"] == {"default": r"C:\tools\aria2c.exe"}
    assert opts["external_downloader_args"] == {"aria2c": list(ARIA2C_DOWNLOAD_ARGS)}
    # aria2c 自己会分片，再叠 http_chunk_size 没有意义
    assert "http_chunk_size" not in opts


def test_aria2c_can_be_disabled_by_setting() -> None:
    service = _service(aria2c=r"C:\tools\aria2c.exe")

    opts = service._download_resilience_opts(BILIBILI_URL, _options(use_aria2c=False))

    assert "external_downloader" not in opts
    assert opts["http_chunk_size"] == BILIBILI_HTTP_CHUNK_SIZE


def test_youtube_keeps_ytdlp_native_downloader() -> None:
    # YouTube 走 yt-dlp 自己的分片下载器已经是多连接，换 aria2c 更容易触发限速。
    opts = _service(aria2c=r"C:\tools\aria2c.exe")._download_resilience_opts(YOUTUBE_URL, _options())

    assert "external_downloader" not in opts
    assert "http_chunk_size" not in opts
    assert opts["retries"] == MIN_HTTP_RETRIES


def test_aria2c_args_do_not_include_lowest_speed_limit() -> None:
    # 实测 --lowest-speed-limit 会把"慢但活着"的连接一并掐掉且不补分片，
    # 连接数从 16 掉到 1，尾段反而更慢。
    assert not any(arg.startswith("--lowest-speed-limit") for arg in ARIA2C_DOWNLOAD_ARGS)


def test_cli_args_mirror_python_options() -> None:
    service = _service(aria2c=r"C:\tools\aria2c.exe")

    args = service._download_resilience_cli_args(BILIBILI_URL, _options())

    assert args[:6] == ["--retries", "10", "--fragment-retries", "10", "--socket-timeout", "10"]
    assert "--downloader" in args
    assert args[args.index("--downloader") + 1] == r"C:\tools\aria2c.exe"


def test_cli_args_use_chunking_without_aria2c() -> None:
    args = _service(aria2c="")._download_resilience_cli_args(BILIBILI_URL, _options())

    assert "--http-chunk-size" in args
    assert args[args.index("--http-chunk-size") + 1] == str(BILIBILI_HTTP_CHUNK_SIZE)
    assert "--downloader" not in args


def test_part_progress_watcher_reports_newest_part_file(tmp_path) -> None:
    service = _service()
    stem = "song [1080p]"
    (tmp_path / f"{stem}.f30112.mp4.part").write_bytes(b"x" * 2048)
    (tmp_path / f"{stem}.f30112.mp4").write_bytes(b"y" * 16)  # 已完成的阶段不该被上报

    newest = service._newest_part_file(tmp_path, stem)

    assert newest is not None
    path, size = newest
    assert path.name == f"{stem}.f30112.mp4.part"
    assert size == 2048


def test_part_progress_watcher_ignores_unrelated_files(tmp_path) -> None:
    service = _service()
    (tmp_path / "other video.f30112.mp4.part").write_bytes(b"x" * 100)

    assert service._newest_part_file(tmp_path, "song [1080p]") is None


def _task_with_duration(tmp_path, duration: float, name: str = "out.mp4"):
    from krok_helper.video_download.download_task import DownloadTask, VideoInfo

    tmp_path.mkdir(parents=True, exist_ok=True)
    media = tmp_path / name
    media.write_bytes(b"fake")
    info = VideoInfo(url="u", source="Bilibili", title="t", uploader="", duration=duration)
    task = DownloadTask(task_id="t", url="u", title="t", source="Bilibili", info=info)
    task.local_file = media
    return task, media


def test_verify_rejects_truncated_video_track(tmp_path, monkeypatch) -> None:
    # 真实事故：视频轨 10 秒、音轨 240 秒，容器时长跟着音轨走所以看不出问题。
    service = _service()
    task, media = _task_with_duration(tmp_path, 240.0)
    monkeypatch.setattr(service, "_probe_shortest_stream_duration", lambda _p: 10.0)

    with pytest.raises(ytdlp_service.VideoDownloadError) as excinfo:
        service._verify_downloaded_media(task)

    assert "不完整" in str(excinfo.value)
    assert not media.exists()      # 坏文件不能留着喂给后面的流程
    assert task.local_file is None


def test_verify_accepts_small_legitimate_shortfall(tmp_path, monkeypatch) -> None:
    # P3 实测：视频轨 236.9s vs 元数据 240.1s，属于正常误差
    service = _service()
    task, media = _task_with_duration(tmp_path, 240.095833)
    monkeypatch.setattr(service, "_probe_shortest_stream_duration", lambda _p: 236.899625)

    service._verify_downloaded_media(task)

    assert media.exists()


def test_verify_tolerance_has_absolute_floor_for_short_clips(tmp_path, monkeypatch) -> None:
    # 6 秒的片子按比例只能容 0.9 秒，太苛刻；下限 3 秒兜底
    service = _service()
    task, _media = _task_with_duration(tmp_path, 6.0)
    monkeypatch.setattr(service, "_probe_shortest_stream_duration", lambda _p: 4.0)
    service._verify_downloaded_media(task)  # 不该抛

    task2, media2 = _task_with_duration(tmp_path / "x", 6.0)
    monkeypatch.setattr(service, "_probe_shortest_stream_duration", lambda _p: 1.0)
    with pytest.raises(ytdlp_service.VideoDownloadError):
        service._verify_downloaded_media(task2)
    assert not media2.exists()


def test_verify_skips_when_probe_unavailable(tmp_path, monkeypatch) -> None:
    # 探测不了就放行——校验的职责是挡住已证实损坏的文件，不是给正常下载添堵
    service = _service()
    task, media = _task_with_duration(tmp_path, 240.0)
    monkeypatch.setattr(service, "_probe_shortest_stream_duration", lambda _p: None)

    service._verify_downloaded_media(task)

    assert media.exists()


def test_verify_skips_when_duration_unknown(tmp_path, monkeypatch) -> None:
    service = _service()
    task, media = _task_with_duration(tmp_path, 0.0)
    monkeypatch.setattr(
        service,
        "_probe_shortest_stream_duration",
        lambda _p: pytest.fail("时长未知时不该去探测"),
    )

    service._verify_downloaded_media(task)

    assert media.exists()


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="需要 ffprobe")
def test_probe_returns_shortest_stream_not_container_duration(tmp_path) -> None:
    """用真 ffmpeg 造一个「视频轨短、音轨长」的文件，验证取的是最短流。"""
    media = tmp_path / "mixed.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"), "-v", "error", "-y",
            "-f", "lavfi", "-t", "1", "-i", "testsrc=size=64x64:rate=10",
            "-f", "lavfi", "-t", "5", "-i", "sine=frequency=440",
            "-c:v", "libx264", "-c:a", "aac", str(media),
        ],
        check=True,
        capture_output=True,
    )

    duration = _service()._probe_shortest_stream_duration(media)

    assert duration is not None
    assert duration < 2.0  # 视频轨 1 秒，而容器/音轨是 5 秒


def _write_aria2_control(part_path, *, piece_length: int, total_length: int, completed_pieces: int) -> None:
    import struct

    piece_count = (total_length + piece_length - 1) // piece_length
    bits = bytearray((piece_count + 7) // 8)
    for index in range(completed_pieces):
        bits[index // 8] |= 0x80 >> (index % 8)
    blob = (
        struct.pack(">H", 1)              # version
        + b"\0\0\0\0"                     # extension
        + struct.pack(">I", 0)            # infohash length（HTTP 下载没有 infohash）
        + struct.pack(">I", piece_length)
        + struct.pack(">Q", total_length)
        + struct.pack(">Q", 0)            # upload length
        + struct.pack(">I", len(bits))
        + bytes(bits)
        + struct.pack(">I", 0)            # in-flight piece count
    )
    part_path.with_name(part_path.name + ".aria2").write_bytes(blob)


def test_aria2_progress_comes_from_control_file_not_file_size(tmp_path) -> None:
    # aria2 切 16 段并发，最后一段一开工文件长度就涨到 ~94%。拿文件大小当进度会让
    # 进度条一开始就卡在 80~90%（这也正是 HTTP 416 的同一个成因）。
    service = _service()
    part = tmp_path / "song [2160p].f30120.mp4.part"
    total = 619_514_521
    part.write_bytes(b"")
    os.truncate(part, total)  # 文件长度已经是满的（aria2 的最后一段写到了末尾）
    _write_aria2_control(part, piece_length=1_048_576, total_length=total, completed_pieces=59)

    progress = service._read_aria2_control(part)

    assert progress is not None
    downloaded, reported_total = progress
    assert reported_total == total
    assert downloaded == 59 * 1_048_576
    # 关键：真实进度 ~10%，而文件大小会谎报成 100%
    assert downloaded * 100 // total == 9
    assert part.stat().st_size == total


def test_aria2_progress_is_none_without_control_file(tmp_path) -> None:
    service = _service()
    part = tmp_path / "song.f1.mp4.part"
    part.write_bytes(b"x" * 1024)

    assert service._read_aria2_control(part) is None


def test_aria2_progress_rejects_unknown_control_version(tmp_path) -> None:
    service = _service()
    part = tmp_path / "song.f1.mp4.part"
    part.write_bytes(b"x")
    part.with_name(part.name + ".aria2").write_bytes(b"\x00\x63" + b"\0" * 64)

    assert service._read_aria2_control(part) is None


def test_aria2_progress_caps_at_total_length(tmp_path) -> None:
    # 最后一个分片通常不满，popcount * piece_length 会略微超过总长
    service = _service()
    part = tmp_path / "song.f1.mp4.part"
    part.write_bytes(b"x")
    _write_aria2_control(part, piece_length=1_048_576, total_length=3_000_000, completed_pieces=3)

    downloaded, total = service._read_aria2_control(part)

    assert total == 3_000_000
    assert downloaded == 3_000_000


def test_clear_partial_downloads_removes_aria2c_residue(tmp_path) -> None:
    # aria2c 多分片并发写，.part 的大小是「写到过的最大偏移」而非「已连续下好的
    # 字节数」；不清掉的话 yt-dlp 会拿它当续传起点，直接 HTTP 416。
    service = _service()
    stem = "song [2160p]"
    part = tmp_path / f"{stem}.f30120.mp4.part"
    control = tmp_path / f"{stem}.f30120.mp4.part.aria2"
    done = tmp_path / f"{stem}.f30251.m4a"  # 已下完的流不该被删
    unrelated = tmp_path / "other.mp4.part"
    for path in (part, control, done, unrelated):
        path.write_bytes(b"x")

    service._clear_partial_downloads(tmp_path, stem)

    assert not part.exists()
    assert not control.exists()
    assert done.exists()
    assert unrelated.exists()


def test_should_retry_without_aria2c_only_when_aria2c_was_used(tmp_path, monkeypatch) -> None:
    service = _service(aria2c=r"C:\tools\aria2c.exe")
    aria2c_error = "\x1b[0;31mERROR:\x1b[0m aria2c exited with code 1"

    assert service._should_retry_without_aria2c(BILIBILI_URL, _options(), aria2c_error)
    # 已经是降级路径了，不该再降一次（否则会无限重试）
    assert not service._should_retry_without_aria2c(BILIBILI_URL, _options(use_aria2c=False), aria2c_error)
    # 与 aria2c 无关的失败照常上抛
    assert not service._should_retry_without_aria2c(BILIBILI_URL, _options(), "HTTP Error 403")


def test_error_message_strips_ansi_colour_codes() -> None:
    # yt-dlp 给 "ERROR:" 加了颜色码，直接进 Qt label 会显示成 [0;31mERROR:[0m
    service = _service()

    message = service._normalize_error_message(RuntimeError("\x1b[0;31mERROR:\x1b[0m HTTP Error 403"))

    assert "\x1b" not in message
    assert "[0;31m" not in message


def _isolate_aria2c_search(service: YtDlpService, root, monkeypatch) -> None:
    """把查找范围锁在 tmp_path 内。

    否则测试会看见开发机上真实存在的 aria2c（repo 的 build/vendor/aria2/ 或
    系统 PATH），"找不到" 类用例就会随机失败。
    """
    from pathlib import Path

    monkeypatch.setattr(
        service,
        "_aria2c_search_locations",
        lambda: [(Path(root), ("tools", "aria2")), (Path(root), ("build", "vendor", "aria2"))],
    )


def _write_aria2c(root, *parts) -> "Path":
    from pathlib import Path

    exe_dir = Path(root).joinpath(*parts)
    exe_dir.mkdir(parents=True, exist_ok=True)
    exe = exe_dir / ("aria2c.exe" if os.name == "nt" else "aria2c")
    exe.write_bytes(b"MZ")
    return exe


def test_search_locations_include_frozen_bundle_dir(tmp_path, monkeypatch) -> None:
    # 打包版里 --add-binary 的内容落在 _internal/ 下，也就是 sys._MEIPASS。
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    locations = YtDlpService()._aria2c_search_locations()

    assert (tmp_path, ("tools", "aria2")) in locations
    # 源码运行时也认 fetch_aria2.py 的产出目录
    assert any(subdir == ("build", "vendor", "aria2") for _base, subdir in locations)


def test_find_aria2c_uses_bundled_copy(tmp_path, monkeypatch) -> None:
    exe = _write_aria2c(tmp_path, "tools", "aria2")
    service = YtDlpService()
    _isolate_aria2c_search(service, tmp_path, monkeypatch)
    monkeypatch.setattr(ytdlp_service.shutil, "which", lambda _name: None)

    assert service.find_aria2c() == str(exe.resolve())


def test_bundled_aria2c_wins_over_path(tmp_path, monkeypatch) -> None:
    # 自带那份版本确定、参数行为可预期；PATH 上的可能是很老的构建。
    exe = _write_aria2c(tmp_path, "tools", "aria2")
    service = YtDlpService()
    _isolate_aria2c_search(service, tmp_path, monkeypatch)
    monkeypatch.setattr(ytdlp_service.shutil, "which", lambda _name: "C:/stale/aria2c.exe")

    assert service.find_aria2c() == str(exe.resolve())


def test_bundled_tools_dir_beats_fetch_output(tmp_path, monkeypatch) -> None:
    bundled = _write_aria2c(tmp_path, "tools", "aria2")
    _write_aria2c(tmp_path, "build", "vendor", "aria2")
    service = YtDlpService()
    _isolate_aria2c_search(service, tmp_path, monkeypatch)
    monkeypatch.setattr(ytdlp_service.shutil, "which", lambda _name: None)

    assert service.find_aria2c() == str(bundled.resolve())


def test_find_aria2c_uses_fetch_script_output_dir(tmp_path, monkeypatch) -> None:
    # 源码运行时直接用 scripts/fetch_aria2.py 的产出，开发机不用再单独装一份。
    exe = _write_aria2c(tmp_path, "build", "vendor", "aria2")
    service = YtDlpService()
    _isolate_aria2c_search(service, tmp_path, monkeypatch)
    monkeypatch.setattr(ytdlp_service.shutil, "which", lambda _name: None)

    assert service.find_aria2c() == str(exe.resolve())


def test_find_aria2c_falls_back_to_path(tmp_path, monkeypatch) -> None:
    service = YtDlpService()
    _isolate_aria2c_search(service, tmp_path, monkeypatch)  # tmp_path 下什么都没有
    monkeypatch.setattr(ytdlp_service.shutil, "which", lambda _name: str(tmp_path / "aria2c.exe"))

    assert service.find_aria2c() == str((tmp_path / "aria2c.exe").resolve())


def test_find_aria2c_returns_empty_when_missing(tmp_path, monkeypatch) -> None:
    service = YtDlpService()
    _isolate_aria2c_search(service, tmp_path, monkeypatch)
    monkeypatch.setattr(ytdlp_service.shutil, "which", lambda _name: None)

    assert service.find_aria2c() == ""


def test_aria2c_setting_reaches_download_options() -> None:
    from PyQt6.QtWidgets import QApplication

    from krok_helper.settings import AppSettings
    from krok_helper.video_download.video_download_page import VideoDownloadPage

    app = QApplication.instance() or QApplication([])
    for name in ("_refresh_cookie_status", "_refresh_youtube_cookie_status", "_ensure_qr_login"):
        setattr(VideoDownloadPage, name, lambda _self: None)

    page = VideoDownloadPage(AppSettings(video_download_use_aria2c=False), lambda: None)
    try:
        assert page._build_download_options().use_aria2c is False
        page.settings.video_download_use_aria2c = True
        assert page._build_download_options().use_aria2c is True
        assert page._aria2c_availability_text()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_settings_roundtrip_keeps_aria2c_flag(tmp_path, monkeypatch) -> None:
    from krok_helper.settings import AppSettings, load_app_settings, save_app_settings

    monkeypatch.setattr("krok_helper.settings.get_settings_path", lambda: tmp_path / "settings.json")
    save_app_settings(AppSettings(video_download_use_aria2c=False, video_download_timeout=30))

    loaded = load_app_settings()

    assert loaded.video_download_use_aria2c is False
    assert loaded.video_download_timeout == 30


@pytest.mark.parametrize("cli", [False, True])
def test_task_retries_keep_partial_data(tmp_path, monkeypatch, make_download_task, cli) -> None:
    service = _service()
    task = make_download_task(url=BILIBILI_URL)
    calls = []
    payloads = []
    monkeypatch.setattr(service, "_import_ytdlp", lambda: object)
    monkeypatch.setattr(service, "_should_prefer_cli_backend", lambda _url: cli)
    monkeypatch.setattr(ytdlp_service.time, "sleep", lambda _seconds: None)
    clock = iter(range(100))
    monkeypatch.setattr(ytdlp_service.time, "monotonic", lambda: next(clock))

    def attempt(*args, **kwargs):
        part = tmp_path / (kwargs["output_stem"] + ".mp4.part")
        calls.append(part.read_bytes() if part.exists() else b"")
        if len(calls) == 1:
            part.write_bytes(b"downloaded prefix")
            raise ytdlp_service.VideoDownloadError("Connection reset by peer")

    monkeypatch.setattr(service, "_download_with_cli_retry" if cli else "_download_with_python_retry", attempt)
    service.download(task, _options(save_dir=str(tmp_path)), payloads.append)

    assert calls == [b"", b"downloaded prefix"]
    assert payloads[0]["status"] == "retrying"


def test_aria2_retries_resume_before_switching_to_http(tmp_path, monkeypatch, make_download_task) -> None:
    service = _service(aria2c="aria2c")
    task = make_download_task(url=BILIBILI_URL)
    attempts = []
    monkeypatch.setattr(service, "_import_ytdlp", lambda: object)
    monkeypatch.setattr(service, "_should_prefer_cli_backend", lambda _url: False)
    monkeypatch.setattr(ytdlp_service.time, "sleep", lambda _seconds: None)
    clock = iter(range(100))
    monkeypatch.setattr(ytdlp_service.time, "monotonic", lambda: next(clock))

    def attempt(_ydl, _task, options, _callback, **kwargs):
        part = tmp_path / (kwargs["output_stem"] + ".mp4.part")
        if not attempts:
            part.write_bytes(b"a" * 16)
            _write_aria2_control(part, piece_length=4, total_length=16, completed_pieces=3)
        attempts.append((options.use_aria2c, part.stat().st_size))
        if options.use_aria2c:
            assert part.with_name(part.name + ".aria2").exists()
            raise ytdlp_service.VideoDownloadError("aria2c exited with code 1")
        assert not part.with_name(part.name + ".aria2").exists()

    monkeypatch.setattr(service, "_download_with_python_retry", attempt)
    service.download(task, _options(save_dir=str(tmp_path), retry_count=2), lambda _: None)

    assert attempts == [(True, 16), (True, 16), (True, 16), (False, 12)]


def test_native_resume_stops_at_first_missing_piece(tmp_path) -> None:
    part = tmp_path / "song.mp4.part"
    part.write_bytes(b"abcdefghijklmnop")
    _write_aria2_control(part, piece_length=4, total_length=16, completed_pieces=4)
    control = part.with_name(part.name + ".aria2")
    data = bytearray(control.read_bytes())
    data[34] = 0b10110000  # First, third and fourth pieces; second is missing.
    control.write_bytes(data)

    _service()._prepare_native_resume(tmp_path, "song")

    assert part.read_bytes() == b"abcd"
    assert not control.exists()


def test_retry_budget_is_bounded(tmp_path, monkeypatch, make_download_task) -> None:
    service = _service()
    attempts = []
    monkeypatch.setattr(service, "_import_ytdlp", lambda: object)
    monkeypatch.setattr(service, "_should_prefer_cli_backend", lambda _: False)
    clock = iter(range(100))
    monkeypatch.setattr(ytdlp_service.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(ytdlp_service.time, "sleep", lambda _: None)

    def attempt(*args, **kwargs):
        attempts.append(1)
        raise ytdlp_service.VideoDownloadError("HTTP Error 503")

    monkeypatch.setattr(service, "_download_with_python_retry", attempt)
    with pytest.raises(ytdlp_service.VideoDownloadError):
        service.download(make_download_task(), _options(save_dir=str(tmp_path), retry_count=2), lambda _: None)
    assert len(attempts) == 3


def test_cancel_during_retry_does_not_start_another_attempt(tmp_path, monkeypatch, make_download_task) -> None:
    service = _service()
    task = make_download_task()
    attempts = []
    monkeypatch.setattr(service, "_import_ytdlp", lambda: object)
    monkeypatch.setattr(service, "_should_prefer_cli_backend", lambda _: False)

    def attempt(*args, **kwargs):
        attempts.append(1)
        raise ytdlp_service.VideoDownloadError("Connection reset by peer")

    def progress(_payload):
        task.cancel_requested = True

    monkeypatch.setattr(service, "_download_with_python_retry", attempt)
    with pytest.raises(ytdlp_service.DownloadCancelledError):
        service.download(task, _options(save_dir=str(tmp_path)), progress)
    assert len(attempts) == 1


@pytest.mark.parametrize("message", ["The read operation timed out", "HTTP Error 403: Forbidden"])
def test_transient_detection_survives_friendly_error_translation(message) -> None:
    service = _service()
    assert service._is_transient_download_error(service._normalize_error_message(RuntimeError(message)))


def test_real_ytdlp_resumes_near_completed_http_download(tmp_path, make_download_task) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    pytest.importorskip("yt_dlp")
    content = bytes(range(256)) * 1024
    prefix = len(content) - 4096
    ranges = []

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()

        def do_GET(self):
            range_header = self.headers.get("Range", "")
            ranges.append(range_header)
            start = int(range_header.split("=")[1].split("-")[0]) if range_header else 0
            self.send_response(206 if range_header else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(content) - start))
            if range_header:
                self.send_header("Content-Range", f"bytes {start}-{len(content) - 1}/{len(content)}")
            self.end_headers()
            self.wfile.write(content[start:])

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    task = make_download_task(url=f"http://127.0.0.1:{server.server_port}/video.mp4")
    task.info.duration = None
    (tmp_path / "Title.mp4.part").write_bytes(content[:prefix])
    try:
        _service().download(task, _options(save_dir=str(tmp_path), use_aria2c=False), lambda _: None)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert f"bytes={prefix}-" in ranges
    assert task.local_file.read_bytes() == content


@pytest.mark.parametrize("cli", [False, True])
@pytest.mark.parametrize("merge", [False, True])
def test_real_ytdlp_merge_toggle_preserves_separate_tracks(tmp_path, monkeypatch, cli, merge):
    import functools
    import json
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from krok_helper.settings import AppSettings
    from krok_helper.video_download.download_task import DownloadTask
    from krok_helper.video_download.format_parser import FormatParser

    yt_dlp = pytest.importorskip("yt_dlp")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe required")
    # Same extension deliberately exercises filename collision prevention.
    for name, args in [
        ("video.mp4", ["-f", "lavfi", "-i", "color=size=160x90:rate=25", "-an", "-c:v", "libx264"]),
        ("audio.mp4", ["-f", "lavfi", "-i", "sine=frequency=440", "-vn", "-c:a", "aac"]),
    ]:
        subprocess.run([ffmpeg, "-v", "error", *args, "-t", "1", str(tmp_path / name)], check=True)

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    formats = [
        dict(format_id="v", url=base + "/video.mp4", ext="mp4", vcodec="avc1", acodec="none", width=160, height=90),
        dict(format_id="a", url=base + "/audio.mp4", ext="mp4", vcodec="none", acodec="mp4a", abr=128),
    ]
    info = dict(id="probe", title="probe", extractor="local", webpage_url=base, formats=formats)

    class LocalYDL(yt_dlp.YoutubeDL):
        def extract_info(self, url, download=True, **kwargs):
            return self.process_ie_result(json.loads(json.dumps(info)), download=download)

    service = YtDlpService(app_settings=AppSettings())
    monkeypatch.setattr(service, "_configured_ffmpeg_location", lambda: ffmpeg)
    monkeypatch.setattr(service, "_should_prefer_cli_backend", lambda url: cli)
    monkeypatch.setattr(service, "_import_ytdlp", lambda: LocalYDL)
    if cli:
        metadata = tmp_path / "info.json"
        metadata.write_text(json.dumps(info), encoding="utf-8")
        real_popen = subprocess.Popen

        def run_cli(command, **kwargs):
            if command[0] != "yt-dlp":
                return real_popen(command, **kwargs)
            return real_popen([sys.executable, "-m", "yt_dlp", "--ignore-config", "--load-info-json", str(metadata), *command[1:]], **kwargs)

        monkeypatch.setattr(service, "_find_ytdlp_cli", lambda: "yt-dlp")
        monkeypatch.setattr(ytdlp_service.subprocess, "Popen", run_cli)
    output = tmp_path / "output"
    task = DownloadTask(task_id="probe", url=base, title="probe", source="local",
                        selected_format=FormatParser().parse_formats(formats)[0])
    try:
        service.download(task, _options(save_dir=str(output), merge_video_audio=merge, use_aria2c=False), lambda _: None)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if cli:
            monkeypatch.setattr(ytdlp_service.subprocess, "Popen", real_popen)
    files = sorted(output.iterdir())
    assert len(files) == (1 if merge else 2)

    def streams(path):
        result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
                                capture_output=True, text=True, check=True)
        return {stream["codec_type"] for stream in json.loads(result.stdout)["streams"]}

    assert streams(task.local_file) == ({"video", "audio"} if merge else {"video"})
    if not merge:
        audio = next(path for path in files if path != task.local_file)
        assert streams(audio) == {"audio"}
