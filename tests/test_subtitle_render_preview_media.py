from __future__ import annotations

import subprocess
from pathlib import Path

from krok_helper.app_paths import TEMP_DIR_ENV
from krok_helper.subtitle_render.frontend.preview import preview_media


def test_qt_playback_source_remuxes_video_with_generated_pts(monkeypatch, tmp_path):
    monkeypatch.setenv(TEMP_DIR_ENV, str(tmp_path))  # 内嵌清扫不碰真实 Temp
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not really video")
    monkeypatch.setattr(preview_media, "_resolve_ffmpeg_path", lambda: "ffmpeg")
    proxy = tmp_path / "proxy.mp4"
    monkeypatch.setattr(preview_media, "_proxy_path_for", lambda _path: proxy)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"proxy")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(preview_media.subprocess, "run", fake_run)

    assert preview_media.qt_playback_source(source) == proxy
    assert commands
    command = commands[0]
    assert command[:4] == ["ffmpeg", "-y", "-hide_banner", "-loglevel"]
    assert "-fflags" in command
    assert "+genpts" in command
    assert command[command.index("-i") + 1] == str(source)
    assert "-avoid_negative_ts" in command


def test_qt_playback_source_falls_back_to_original_when_remux_fails(monkeypatch, tmp_path):
    monkeypatch.setenv(TEMP_DIR_ENV, str(tmp_path))  # 内嵌清扫不碰真实 Temp
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not really video")
    monkeypatch.setattr(preview_media, "_resolve_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(preview_media, "_proxy_path_for", lambda _path: tmp_path / "proxy.mp4")
    monkeypatch.setattr(
        preview_media.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "bad"),
    )

    assert preview_media.qt_playback_source(source) == source


def test_low_quality_transcodes_a_cached_540p_preview_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv(TEMP_DIR_ENV, str(tmp_path))  # 内嵌清扫不碰真实 Temp
    source = tmp_path / "source.mkv"
    source.write_bytes(b"not really video")
    proxy = tmp_path / "proxy-540p.mp4"
    monkeypatch.setattr(preview_media, "_resolve_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(
        preview_media,
        "_scaled_proxy_path_for",
        lambda _path, quality: proxy,
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"proxy")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(preview_media.subprocess, "run", fake_run)

    assert preview_media.qt_playback_source(source, "low") == proxy
    command = commands[0]
    assert "-vf" in command
    assert "min(ih,540)" in command[command.index("-vf") + 1]
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert "-avoid_negative_ts" not in command


def test_scaled_preview_proxy_cache_key_tracks_quality_and_source_metadata(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video-v1")

    low = preview_media._scaled_proxy_path_for(source, "low")
    medium = preview_media._scaled_proxy_path_for(source, "medium")

    assert low != medium
    assert low.name.endswith("-540p.mp4")
    assert medium.name.endswith("-1080p.mp4")

    source.write_bytes(b"video-v2-with-a-different-size")
    assert preview_media._scaled_proxy_path_for(source, "low") != low


def test_high_quality_preparation_remuxes_without_libx264(monkeypatch, tmp_path):
    monkeypatch.setenv(TEMP_DIR_ENV, str(tmp_path))
    source = tmp_path / "roll.mp4"
    source.write_bytes(b"video")
    preparation = preview_media.qt_playback_preparation(source, "high")
    assert preparation is not None
    command = preparation.command
    assert "-c" in command and command[command.index("-c") + 1] == "copy"
    assert "libx264" not in command
    assert "-vf" not in command
    assert "-fps_mode" not in command


def test_low_quality_preparation_reuses_normal_scaled_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv(TEMP_DIR_ENV, str(tmp_path))
    source = tmp_path / "roll.mp4"
    source.write_bytes(b"video")
    preparation = preview_media.qt_playback_preparation(source, "low")
    assert preparation is not None
    command = preparation.command
    assert "min(ih,540)" in command[command.index("-vf") + 1]
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert "libx264" in command


def test_active_preview_sources_keep_all_quality_proxies(monkeypatch, tmp_path):
    monkeypatch.setenv(TEMP_DIR_ENV, str(tmp_path))
    cache = tmp_path / "preview-cache"
    cache.mkdir()
    background = tmp_path / "background.mp4"
    background.write_bytes(b"bg")
    roll = tmp_path / "roll.mp4"
    roll.write_bytes(b"roll")
    kept: list[Path] = []
    for source in (background, roll):
        for quality in ("low", "medium", "high"):
            target = preview_media._playback_target_for(source, quality)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"proxy")
            kept.append(target)
    preview_media.protect_preview_source(background)
    preview_media.protect_preview_source(roll)
    preview_media.protect_preview_source(roll)  # 前/后黑同源引用计数
    orphan = cache / "orphan-abc123.mp4"
    orphan.write_bytes(b"orphan")
    monkeypatch.setattr(preview_media, "_resolve_ffmpeg_path", lambda: "ffmpeg")
    try:
        # 模拟“切换普通背景”：qt_playback_preparation 会清扫缓存。
        switched = preview_media.qt_playback_preparation(orphan, "high")
        assert switched is not None
        preview_media.discard_qt_playback_preparation(switched)
        for target in kept:
            assert target.is_file(), target
        preview_media.purge_preview_cache()
        for target in kept:
            assert target.is_file(), target
        assert not orphan.exists()
    finally:
        preview_media.unprotect_preview_source(background)
        preview_media.unprotect_preview_source(roll)
        preview_media.unprotect_preview_source(roll)
    preview_media.purge_preview_cache()
    assert not any(target.exists() for target in kept)


def test_purge_removes_all_proxies_but_keeps_non_mp4(monkeypatch, tmp_path):
    monkeypatch.setenv(TEMP_DIR_ENV, str(tmp_path))
    cache = tmp_path / "preview-cache"  # 统一根 %TEMP%\LinKLyrics 下的子目录
    cache.mkdir()
    orphan_tmp = cache / "video.9f2c1e.tmp.mp4"  # ffmpeg 中途崩溃遗留的半成品
    fresh_proxy = cache / "video-abc123.mp4"  # 不做保留期：代理可再生，一并删
    fresh_scaled = cache / "video-abc123-540p.mp4"
    unrelated = cache / "notes.txt"
    (cache / "subdir").mkdir()
    for path in (orphan_tmp, fresh_proxy, fresh_scaled, unrelated):
        path.write_bytes(b"data")

    preview_media.purge_preview_cache()

    assert not orphan_tmp.exists()
    assert not fresh_proxy.exists()
    assert not fresh_scaled.exists()
    assert unrelated.is_file()
    assert (cache / "subdir").is_dir()  # 非文件条目不动


def test_purge_tolerates_missing_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(TEMP_DIR_ENV, str(tmp_path / "no-such-dir"))
    preview_media.purge_preview_cache()  # 目录不存在时静默返回


def test_preparation_purges_other_sources_but_keeps_current_qualities(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(TEMP_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(preview_media, "_resolve_ffmpeg_path", lambda: "ffmpeg")

    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    other = tmp_path / "other.mp4"
    other.write_bytes(b"other-video")

    low_proxy = preview_media._scaled_proxy_path_for(source, "low")
    low_proxy.parent.mkdir(parents=True, exist_ok=True)
    low_proxy.write_bytes(b"low-proxy")

    # 缓存命中（同画质反复预览）：不触发清扫。
    assert preview_media.qt_playback_preparation(source, "low") is None
    assert low_proxy.is_file()

    # 缓存未命中（切到 medium）：建新代理前只清**其它源文件**的代理，
    # 同一源文件其它画质（low）保留，画质切回去不必重转。
    other_proxy = preview_media._proxy_path_for(other)
    other_proxy.write_bytes(b"dead")
    preparation = preview_media.qt_playback_preparation(source, "medium")

    assert preparation is not None
    assert low_proxy.is_file()
    assert not other_proxy.exists()
    assert preparation.target == preview_media._scaled_proxy_path_for(source, "medium")


def test_concurrent_roll_preparation_reuses_one_ffmpeg(monkeypatch, tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv(TEMP_DIR_ENV, str(tmp_path))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(preview_media, "_resolve_ffmpeg_path", lambda: "ffmpeg")
    entered, release = threading.Event(), threading.Event()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        entered.set()
        assert release.wait(5)
        Path(command[-1]).write_bytes(b"proxy")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(preview_media.subprocess, "run", run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(preview_media.qt_playback_source, source)
        assert entered.wait(5)
        second = pool.submit(preview_media.qt_playback_source, source)
        release.set()
        assert first.result(timeout=5) == second.result(timeout=5)
    assert len(calls) == 1


def test_real_remux_reports_progress(monkeypatch, tmp_path):
    import pytest
    from tests.test_subtitle_render_roll_export import _ffmpeg_path
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        pytest.skip("需要 ffmpeg")
    monkeypatch.setenv(TEMP_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(preview_media, "_resolve_ffmpeg_path", lambda: ffmpeg)
    source = tmp_path / "source.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=size=64x64:rate=30:duration=2", "-c:v", "libx264", str(source)], check=True)
    progress = []
    target = preview_media.qt_playback_source(source, progress_callback=progress.append, duration_seconds=2)
    assert target != source and target.is_file()
    assert progress and all(0 <= value <= 99 for value in progress)
