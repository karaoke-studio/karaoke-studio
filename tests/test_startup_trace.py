"""启动面包屑：日志系统还没就绪时唯一的现场记录，所以它自己绝不能出事。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from krok_helper import startup_trace  # noqa: E402


@pytest.fixture
def trace_file(tmp_path, monkeypatch):
    path = tmp_path / "startup-trace.log"
    monkeypatch.setenv("KARAOKE_STUDIO_STARTUP_TRACE", str(path))
    return path


def test_mark_records_step_pid_and_detail(trace_file) -> None:
    startup_trace.mark("boot.enter")
    startup_trace.mark("gui.failed", "RuntimeError: 炸了")

    lines = trace_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert f"pid={os.getpid()}" in lines[0]
    assert lines[0].endswith("boot.enter")
    assert lines[1].endswith("gui.failed RuntimeError: 炸了")


def test_each_mark_lands_on_disk_immediately(trace_file) -> None:
    """进程随时可能被 abort 打死，缓冲在内存里的面包屑等于没有。"""
    startup_trace.mark("boot.enter")

    assert "boot.enter" in trace_file.read_text(encoding="utf-8")


def test_mark_never_raises_when_the_path_is_unusable(tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")
    monkeypatch.setenv("KARAOKE_STUDIO_STARTUP_TRACE", str(blocker / "trace.log"))

    startup_trace.mark("boot.enter")  # 不抛就是通过


def test_oversized_trace_starts_over(trace_file, monkeypatch) -> None:
    monkeypatch.setattr(startup_trace, "_MAX_BYTES", 200)
    for index in range(40):
        startup_trace.mark(f"step.{index}")

    text = trace_file.read_text(encoding="utf-8")
    assert len(text) <= 400
    assert "step.39" in text
    assert "step.0 " not in text


def test_qt_fatal_messages_are_captured(trace_file) -> None:
    """qFatal 一响进程就没了，那句话必须在死前落到文件里。"""
    from PyQt6.QtCore import qInstallMessageHandler, qWarning

    assert startup_trace.install_qt_message_capture() is True
    try:
        qWarning(b"QWidget: Must construct a QApplication first")
    finally:
        qInstallMessageHandler(None)

    text = trace_file.read_text(encoding="utf-8")
    assert "qt.warning" in text
    assert "Must construct a QApplication first" in text


def test_default_location_is_outside_the_settings_dir(monkeypatch) -> None:
    """设置目录本身可能就是出问题的那一环，面包屑不能跟它绑在一起。"""
    monkeypatch.delenv("KARAOKE_STUDIO_STARTUP_TRACE", raising=False)

    path = startup_trace.trace_path()

    assert path.name == "startup-trace.log"
    assert path.parent.name == "LinKLyrics"


def test_append_does_not_fsync_per_line(trace_file, monkeypatch) -> None:
    """面包屑可能在校持 Qt 内部锁的线程里被调（Qt 消息处理器）。

    每行 fsync 会把持锁线程拖在同步落盘上，等同一把锁的 GUI 线程随之
    「未响应」（2026-10 打开工程卡死根因放大器）。这里只允许 flush：
    OS 页缓存挺得过进程 abort，内容仍立即落盘可读。
    """
    calls: list[int] = []
    monkeypatch.setattr(startup_trace.os, "fsync", lambda fd: calls.append(fd))

    startup_trace.mark("boot.enter")
    startup_trace.mark("gui.event_loop_exited")

    assert trace_file.read_text(encoding="utf-8").count("\n") == 2
    assert calls == []


def test_message_handler_survives_warning_storm(trace_file) -> None:
    """告警风暴下处理器自身不崩、内容全落盘（放大器修复的端到端形态）。"""
    from PyQt6.QtCore import qInstallMessageHandler, qWarning

    assert startup_trace.install_qt_message_capture() is True
    try:
        for index in range(200):
            qWarning(f"qt.qpa.fonts: CreateFontFaceFromHDC() failed #{index}".encode())
    finally:
        qInstallMessageHandler(None)

    text = trace_file.read_text(encoding="utf-8")
    assert text.count("CreateFontFaceFromHDC") == 200
