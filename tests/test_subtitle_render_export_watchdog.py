"""Export heartbeat / watchdog behaviour (no wall-clock timeouts)."""

from __future__ import annotations

from collections import deque
import os
from pathlib import Path
import shutil
import time

import pytest

from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import (
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.engine.export import progress_watchdog as pw
from krok_helper.subtitle_render.engine.export.io_paths import is_remote_path
from krok_helper.subtitle_render.engine.export.render_job import RenderJob
from krok_helper.subtitle_render.engine.renderer import (
    _drain_process_output,
    render_subtitle_video,
)


class _Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture(autouse=True)
def _default_graces(monkeypatch):
    for name in (
        "KROK_SUBTITLE_EXPORT_WRITE_STALL_S",
        "KROK_SUBTITLE_GPU_SEND_STALL_S",
        "KROK_SUBTITLE_GPU_FRAME_STALL_S",
        "KROK_SUBTITLE_EXPORT_HEARTBEAT_S",
    ):
        monkeypatch.delenv(name, raising=False)


def _monitor(clock: _Clock, logs: list[str], **kwargs) -> pw.ExportProgressMonitor:
    return pw.ExportProgressMonitor(
        logger=logs.append, interval_s=10.0, clock=clock, **kwargs
    )


def test_parse_ffmpeg_progress_line_only_accepts_protocol_keys() -> None:
    assert pw.parse_ffmpeg_progress_line("frame=12") == ("frame", "12")
    assert pw.parse_ffmpeg_progress_line("progress=end") == ("progress", "end")
    assert pw.parse_ffmpeg_progress_line("speed=1.02x") == ("speed", "1.02x")
    assert pw.parse_ffmpeg_progress_line("[h264] warning=0x1") is None
    assert pw.parse_ffmpeg_progress_line("plain warning") is None


def test_heartbeat_is_batched_and_carries_stage_and_progress() -> None:
    clock = _Clock(100.0)
    logs: list[str] = []
    monitor = _monitor(clock, logs)
    monitor.configure(total_frames=100, fps=60)
    monitor.set_stage("帧渲染")

    for written in range(1, 11):
        monitor.frame_written(written, 100)
        clock.advance(1.0)
    # 逐帧更新不产生日志；心跳只在批次间隔到点后写一条。
    assert logs == []

    monitor.poll_once(109.9)
    assert logs == []

    monitor.poll_once(110.0)
    assert len(logs) == 1
    assert "帧渲染" in logs[0]
    assert "已写 10/100" in logs[0]
    assert "近 10s 写 10 帧" in logs[0]
    assert "累计 00:10" in logs[0]

    for written in range(11, 30):
        monitor.frame_written(written, 100)
    monitor.poll_once(111.0)
    assert len(logs) == 1


def test_heartbeat_includes_ffmpeg_progress_and_finalize_state() -> None:
    clock = _Clock(0.0)
    logs: list[str] = []
    monitor = _monitor(clock, logs)
    monitor.configure(total_frames=300, fps=60)
    monitor.frame_written(300, 300)
    monitor.observe_ffmpeg_progress("frame", "120")
    monitor.observe_ffmpeg_progress("fps", "60.2")
    monitor.observe_ffmpeg_progress("total_size", "100000000")
    monitor.observe_ffmpeg_progress("speed", "1.02x")
    monitor.observe_ffmpeg_progress("progress", "end")
    monitor.set_stage("编码收尾")

    monitor.poll_once(10.0)

    line = logs[0]
    assert "编码收尾" in line
    assert "ffmpeg 已输出 120 帧" in line
    assert "95.4 MB" in line
    assert "60.2fps" in line
    assert "1.02x" in line
    assert "progress=end" in line


def test_hardware_write_stall_triggers_cpu_retry() -> None:
    clock = _Clock(0.0)
    logs: list[str] = []
    process = _FakeProcess()
    monitor = _monitor(clock, logs)
    monitor.configure(total_frames=10, fps=2)
    monitor.set_ffmpeg_process(process, hardware_encoder=True)
    monitor.frame_written(1, 10)
    monitor.observe_ffmpeg_progress("frame", "1")
    monitor.begin_write(1)

    clock.advance(241.0)
    monitor.poll_once()

    assert monitor.stall_action == "restart_cpu"
    assert process.terminated is True
    assert any("自动切换 CPU" in line for line in logs)


def test_cpu_write_stall_reports_without_killing_the_encoder() -> None:
    clock = _Clock(0.0)
    logs: list[str] = []
    process = _FakeProcess()
    monitor = _monitor(clock, logs)
    monitor.configure(total_frames=10, fps=2)
    monitor.set_ffmpeg_process(process, hardware_encoder=False)
    monitor.frame_written(1, 10)
    monitor.observe_ffmpeg_progress("frame", "1")
    monitor.begin_write(1)

    clock.advance(121.0)
    monitor.poll_once()

    assert monitor.stall_action is None
    assert process.terminated is False
    assert any("未从管道读取新帧" in line for line in logs)


def test_ffmpeg_progress_advance_keeps_a_slow_frame_alive() -> None:
    clock = _Clock(0.0)
    logs: list[str] = []
    process = _FakeProcess()
    monitor = _monitor(clock, logs)
    monitor.configure(total_frames=10, fps=2)
    monitor.set_ffmpeg_process(process, hardware_encoder=True)
    monitor.frame_written(1, 10)
    monitor.observe_ffmpeg_progress("frame", "1")
    monitor.begin_write(1)

    clock.advance(60.0)
    monitor.observe_ffmpeg_progress("frame", "2")
    clock.advance(61.0)
    monitor.poll_once()

    assert monitor.stall_action is None
    assert process.terminated is False


def test_no_ffmpeg_progress_only_reports_and_never_kills() -> None:
    """启动/首帧阶段没有 -progress 只能回报：长素材打开/冷启动不是楔死。"""

    clock = _Clock(0.0)
    logs: list[str] = []
    process = _FakeProcess()
    monitor = _monitor(clock, logs)
    monitor.configure(total_frames=10, fps=2)
    monitor.set_ffmpeg_process(process, hardware_encoder=True)
    monitor.begin_write(0)

    clock.advance(301.0)
    monitor.poll_once()

    assert monitor.stall_action is None
    assert process.terminated is False
    assert any("仍没有任何编码进度" in line for line in logs)


def test_first_frame_cpu_wait_is_reported_not_killed() -> None:
    clock = _Clock(0.0)
    logs: list[str] = []
    process = _FakeProcess()
    monitor = _monitor(clock, logs)
    monitor.configure(total_frames=10, fps=2)
    monitor.set_ffmpeg_process(process, hardware_encoder=False)
    monitor.observe_ffmpeg_progress("frame", "0")
    monitor.begin_write(0)

    clock.advance(301.0)
    monitor.poll_once()

    assert monitor.stall_action is None
    assert process.terminated is False
    assert any("首帧写入已等待" in line for line in logs)


def test_first_frame_hardware_stall_switches_to_cpu() -> None:
    clock = _Clock(0.0)
    logs: list[str] = []
    process = _FakeProcess()
    monitor = _monitor(clock, logs)
    monitor.configure(total_frames=10, fps=2)
    monitor.set_ffmpeg_process(process, hardware_encoder=True)
    monitor.observe_ffmpeg_progress("frame", "0")
    monitor.begin_write(0)

    clock.advance(301.0)
    monitor.poll_once()

    assert monitor.stall_action == "restart_cpu"
    assert process.terminated is True
    assert any("自动切换 CPU" in line for line in logs)


def test_write_stall_after_ffmpeg_advanced_then_froze() -> None:
    """先排空几帧再挂死也要能判停滞，不能依赖“写开始时的同一帧号”。"""

    clock = _Clock(0.0)
    logs: list[str] = []
    process = _FakeProcess()
    monitor = _monitor(clock, logs)
    monitor.configure(total_frames=10, fps=2)
    monitor.set_ffmpeg_process(process, hardware_encoder=True)
    monitor.frame_written(2, 10)
    monitor.observe_ffmpeg_progress("frame", "1")
    monitor.begin_write(2)

    clock.advance(60.0)
    monitor.observe_ffmpeg_progress("frame", "2")
    clock.advance(300.0)
    monitor.poll_once()

    assert monitor.stall_action == "restart_cpu"
    assert process.terminated is True


def test_output_growth_prevents_cpu_switch(monkeypatch) -> None:
    clock = _Clock(0.0)
    logs: list[str] = []
    process = _FakeProcess()
    monitor = _monitor(clock, logs)
    monitor.configure(total_frames=10, fps=2)
    monitor.set_ffmpeg_process(process, hardware_encoder=True)
    monitor.frame_written(2, 10)
    monitor.observe_ffmpeg_progress("frame", "1")
    monitor.observe_ffmpeg_progress("total_size", "100")
    monitor.begin_write(2)

    clock.advance(300.0)
    monitor.observe_ffmpeg_progress("total_size", "200")
    clock.advance(60.0)
    monitor.poll_once()

    assert monitor.stall_action is None
    assert process.terminated is False
    assert any("输出文件仍在增长" in line for line in logs)


def test_remote_output_stall_does_not_switch_to_cpu(monkeypatch) -> None:
    monkeypatch.setattr(pw, "is_remote_path", lambda _path: True)
    clock = _Clock(0.0)
    logs: list[str] = []
    process = _FakeProcess()
    monitor = _monitor(clock, logs)
    monitor.configure(
        total_frames=10,
        fps=2,
        output_path=Path(r"\\server\share\out.mp4"),
    )
    monitor.set_ffmpeg_process(process, hardware_encoder=True)
    monitor.frame_written(2, 10)
    monitor.observe_ffmpeg_progress("frame", "2")
    monitor.begin_write(2)

    clock.advance(241.0)
    monitor.poll_once()

    assert monitor.stall_action is None
    assert process.terminated is False
    assert any("网络盘" in line for line in logs)


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
def test_is_remote_path_treats_unc_as_remote() -> None:
    assert is_remote_path(Path(r"\\server\share\out.mp4")) is True


def test_monitor_stop_suppresses_late_feedback() -> None:
    clock = _Clock(0.0)
    logs: list[str] = []
    monitor = _monitor(clock, logs)
    monitor.start()
    monitor.stop()
    monitor.note_repair("这条不应该出现")
    monitor.poll_once(100.0)

    assert logs == []


def test_parser_rejects_malformed_progress_values() -> None:
    assert pw.parse_ffmpeg_progress_line("frame=abc") is None
    assert pw.parse_ffmpeg_progress_line("progress=weird") is None
    assert pw.parse_ffmpeg_progress_line("speed=fast") is None
    assert pw.parse_ffmpeg_progress_line("frame=12") == ("frame", "12")
    # 编码刚启动时 speed=N/A、多输出 stream_1_0_q 都是合法 -progress 行。
    assert pw.parse_ffmpeg_progress_line("speed=N/A") == ("speed", "N/A")
    assert pw.parse_ffmpeg_progress_line("stream_1_0_q=2.0") == (
        "stream_1_0_q",
        "2.0",
    )
    assert pw.parse_ffmpeg_progress_line("stream_12_3_q=0.0") == (
        "stream_12_3_q",
        "0.0",
    )


def test_sidecar_send_stall_aborts_wedged_sidecar() -> None:
    class _Sidecar:
        def __init__(self) -> None:
            self.aborted: list[str] = []

        def send_in_flight_age_s(self) -> float:
            return 61.0

        def abort(self, *, reason: str = "") -> None:
            self.aborted.append(reason)

        def progress_snapshot(self):
            return {"phase": "scene", "done": 1, "total": 10}

    clock = _Clock(0.0)
    logs: list[str] = []
    sidecar = _Sidecar()
    monitor = _monitor(clock, logs)
    monitor.set_sidecar(sidecar)

    monitor.poll_once(10.0)

    assert sidecar.aborted
    assert any("未接收新请求" in line for line in logs)


def test_note_sidecar_restart_keeps_written_progress() -> None:
    clock = _Clock(0.0)
    logs: list[str] = []
    monitor = _monitor(clock, logs)
    monitor.configure(total_frames=100, fps=60)
    monitor.frame_written(40, 100)

    monitor.note_sidecar_restart(1, 40, 100, "frame wait stall")
    clock.advance(10.0)
    monitor.poll_once()

    line = logs[-1]
    assert "GPU 重启" in line
    assert "已写 40/100" in line
    assert "从第 40/100 帧继续" in line


def test_note_retry_resets_frame_counters() -> None:
    clock = _Clock(0.0)
    logs: list[str] = []
    monitor = _monitor(clock, logs)
    monitor.configure(total_frames=100, fps=60)
    monitor.frame_written(50, 100)
    monitor.note_retry("GPU 回退 CPU")

    clock.advance(10.0)
    monitor.poll_once()

    assert "已写 0/100" in logs[-1]
    assert "重试" in logs[-1]


def test_drain_process_output_swallows_progress_and_keeps_warnings() -> None:
    class _Stdout:
        def __iter__(self):
            return iter(
                [
                    b"frame=12\n",
                    b"progress=end\n",
                    b"warning: decoder said something\n",
                ]
            )

    class _Process:
        stdout = _Stdout()

    class _Monitor:
        def __init__(self) -> None:
            self.observed: list[tuple[str, str]] = []

        def observe_ffmpeg_progress(self, key: str, value: str) -> None:
            self.observed.append((key, value))

    monitor = _Monitor()
    tail: deque[str] = deque()
    logs: list[str] = []

    _drain_process_output(_Process(), logs.append, tail, monitor)

    assert logs == ["warning: decoder said something"]
    assert list(tail) == ["warning: decoder said something"]
    assert monitor.observed == [("frame", "12"), ("progress", "end")]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_export_frame_stall_grace_defaults_and_override(monkeypatch) -> None:
    monkeypatch.delenv("KROK_SUBTITLE_GPU_FRAME_STALL_S", raising=False)
    assert pw.export_frame_stall_grace_s(True) == pytest.approx(120.0)
    assert pw.export_frame_stall_grace_s(False) == pytest.approx(600.0)
    monkeypatch.setenv("KROK_SUBTITLE_GPU_FRAME_STALL_S", "0.2")
    assert pw.export_frame_stall_grace_s(True) == pytest.approx(0.2)
    assert pw.export_frame_stall_grace_s(False) == pytest.approx(0.2)


def test_drain_process_output_feeds_progress_to_monitor_without_logging() -> None:
    class _Process:
        stdout = [
            b"frame=5\n",
            b"fps=30.0\n",
            b"speed=N/A\n",
            b"stream_1_0_q=2.0\n",
            b"progress=continue\n",
            b"real decoder warning\n",
        ]

    clock = _Clock(0.0)
    logs: list[str] = []
    tail: deque[str] = deque(maxlen=80)
    monitor = _monitor(clock, logs)
    monitor.set_ffmpeg_process(_FakeProcess(), hardware_encoder=False)

    _drain_process_output(
        _Process(), logs.append, tail, monitor
    )

    assert monitor._ffmpeg_frame == 5  # noqa: SLF001
    assert list(tail) == ["real decoder warning"]
    assert logs == ["real decoder warning"]


def test_real_export_emits_batch_heartbeat_and_swallows_progress_lines(
    tmp_path, monkeypatch
) -> None:
    """真实 ffmpeg：-progress 能被解析、心跳成批出现、进度行不刷日志。"""

    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "0")
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_BANDS", "0")
    monkeypatch.setenv("KROK_SUBTITLE_GPU_EXPORT", "0")
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("a", 0)], end_ms=4000)]
    )
    job = RenderJob(
        track=track,
        style=Style(),
        background_video_path=None,
        output_path=tmp_path / "heartbeat.mp4",
        background_source=BackgroundSource(kind="solid", color="#101010"),
        width=640,
        height=360,
        fps=30,
        duration_ms=8000,
        include_audio=False,
        encoder_mode="cpu",
        crf=28,
        preset="medium",
        gpu_export_enabled=False,
        native_export_enabled=False,
    )
    logs: list[str] = []
    monitor = pw.ExportProgressMonitor(
        logger=logs.append, interval_s=0.1, tick_interval_s=0.05
    )
    pw.set_active_export_monitor(monitor)
    monitor.start()
    # 真实 ffmpeg 在快的机器上可能不到 2s 就结束；强制先产出一条批量
    # 心跳，避免「导出太快、心跳线程还没 tick」造成 flaky。
    monitor.poll_once(time.monotonic() + 1.0)
    try:
        output = render_subtitle_video(job, logger=logs.append)
    finally:
        monitor.stop()
        pw.clear_active_export_monitor(monitor)

    assert output.is_file() and output.stat().st_size > 0
    assert any("字幕视频导出完成" in line for line in logs)
    assert any("导出心跳" in line for line in logs)
    # -progress 的 frame=/speed=/stream_*_q 行只喂心跳，绝不该出现在用户日志里。
    assert not any(
        line.startswith(("frame=", "speed=", "stream_", "progress="))
        for line in logs
    )
