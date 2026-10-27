"""Export liveness heartbeat and watchdog.

Long exports cannot be judged by wall-clock timeouts: a 4K / low-end CPU /
PNG sequence / ProRes run can legitimately take tens of minutes, and a fixed
timeout would either kill it or be so large that it never helps.  This module
uses two layers instead:

* **Heartbeat** — one batch log line every few seconds with the current stage,
  frame counters, recent throughput, the encoder's own ``-progress`` counters
  and the GPU sidecar heartbeat.  "Slow but moving" and "stopped" become
  distinguishable in the log and in the export status label.
* **Watchdog** — acts only when a stage has genuinely stopped advancing.
  ffmpeg's ``-progress`` frame counter is the external heartbeat for the
  encode pipe; a blocked stdin write with no counter movement for a long
  grace is a real encoder / IO stall (hardware encoders are switched to CPU,
  CPU encoders are only reported), not a slow frame.

Nothing here aborts an export merely because it is old.  The only automatic
repairs are the ones the caller already knows how to finish (hardware ->
CPU retry, GPU sidecar -> CPU fallback).
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from krok_helper.ffmpeg import terminate_process
from krok_helper.subtitle_render.engine.export.io_paths import is_remote_path
from krok_helper.types import Logger

_DEFAULT_HEARTBEAT_INTERVAL_S = 10.0
_DEFAULT_WRITE_STALL_GRACE_S = 120.0
_DEFAULT_SIDECAR_SEND_STALL_GRACE_S = 60.0
_DEFAULT_GPU_FRAME_STALL_GRACE_S = 600.0
_DEFAULT_EXPORT_HEARTBEAT_LEASE_S = 30.0
_DEFAULT_MULTIWORKER_FRAME_STALL_GRACE_S = 120.0
_DEFAULT_TICK_INTERVAL_S = 0.5

# ffmpeg -progress 的 key=value 行。只认这些键，避免把普通 warning
# （可能也带 '='）当成进度吞掉。
_FFMPEG_PROGRESS_KEYS = frozenset(
    {
        "frame",
        "fps",
        "stream_0_0_q",
        "bitrate",
        "total_size",
        "out_time_us",
        "out_time_ms",
        "out_time",
        "dup_frames",
        "drop_frames",
        "speed",
        "progress",
    }
)
# 多输出 ffmpeg 命令会发 stream_<输入>_<输出>_q（如 stream_1_0_q），
# 不能只认 stream_0_0_q，否则这些机器进度会刷满用户日志/失败尾巴。
_STREAM_PROGRESS_KEY_RE = re.compile(r"stream_\d+_\d+_q")


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def export_heartbeat_interval_s() -> float:
    return _env_float("KROK_SUBTITLE_EXPORT_HEARTBEAT_S", _DEFAULT_HEARTBEAT_INTERVAL_S)


def ffmpeg_write_stall_grace_s() -> float:
    """写管道 + ffmpeg 进度双静止多久算真停滞（不是总时长超时）。"""
    return _env_float(
        "KROK_SUBTITLE_EXPORT_WRITE_STALL_S", _DEFAULT_WRITE_STALL_GRACE_S
    )


def sidecar_send_stall_grace_s() -> float:
    """sidecar 的 JSON 请求（正常微秒级）多久没写出去算楔死。"""
    return _env_float(
        "KROK_SUBTITLE_GPU_SEND_STALL_S", _DEFAULT_SIDECAR_SEND_STALL_GRACE_S
    )


def gpu_frame_stall_grace_s() -> float:
    """GPU 单帧 / 单批结果多久没有任何返回才算 sidecar 楔死。"""
    return _env_float(
        "KROK_SUBTITLE_GPU_FRAME_STALL_S", _DEFAULT_GPU_FRAME_STALL_GRACE_S
    )


def export_heartbeat_lease_s() -> float:
    """Export-only sidecar heartbeat lease (multi-worker frame waits)."""

    return _env_float(
        "KROK_SUBTITLE_EXPORT_HEARTBEAT_LEASE_S",
        _DEFAULT_EXPORT_HEARTBEAT_LEASE_S,
    )


def export_frame_stall_grace_s(multiworker: bool) -> float:
    """No-frame backstop: pooled workers should answer within ~2 minutes.

    Single-worker exports render synchronously on the sidecar main loop, so
    they keep the generous backstop; an explicit env override wins for both.
    """

    raw = os.environ.get("KROK_SUBTITLE_GPU_FRAME_STALL_S", "").strip()
    if raw:
        try:
            if float(raw) > 0:
                return gpu_frame_stall_grace_s()
        except ValueError:
            pass
    if multiworker:
        return _DEFAULT_MULTIWORKER_FRAME_STALL_GRACE_S
    return gpu_frame_stall_grace_s()


def parse_ffmpeg_progress_line(line: str) -> tuple[str, str] | None:
    """Return ``(key, value)`` for an ffmpeg ``-progress`` line, else ``None``."""

    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    if (
        key not in _FFMPEG_PROGRESS_KEYS
        and _STREAM_PROGRESS_KEY_RE.fullmatch(key) is None
    ):
        return None
    value = value.strip()
    if key in {"frame", "total_size", "dup_frames", "drop_frames"}:
        if not value.isdigit():
            return None
    elif key == "progress":
        if value not in {"continue", "end"}:
            return None
    elif key in {"fps", "speed"}:
        # 编码刚启动时 ffmpeg 会发 speed=N/A；这是合法进度值，不能漏进日志。
        if value != "N/A":
            try:
                float(value.rstrip("x"))
            except ValueError:
                return None
    return key, value


def format_duration(seconds: float) -> str:
    total = max(int(seconds), 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_megabytes(num_bytes: float) -> str:
    return f"{max(num_bytes, 0.0) / (1024 * 1024):.1f} MB"


def _as_int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _request_sidecar_stall_abort(sidecar: Any) -> None:
    """Abort a sidecar from the heartbeat thread, marking it as a stall."""

    abort = getattr(sidecar, "abort", None)
    if not callable(abort):
        return
    try:
        abort(reason="sidecar send stall", stalled=True)
        return
    except TypeError:
        pass
    except Exception:  # noqa: BLE001
        return
    try:
        abort(reason="sidecar send stall")
    except TypeError:
        try:
            abort()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


class ExportProgressMonitor:
    """Collect export progress, emit batch heartbeats and run the watchdog."""

    def __init__(
        self,
        *,
        logger: Logger | None = None,
        interval_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        tick_interval_s: float = _DEFAULT_TICK_INTERVAL_S,
        hardware_encoder: bool = False,
    ) -> None:
        self._logger = logger or (lambda _message: None)
        self._clock = clock
        self._interval_s = max(
            float(interval_s if interval_s is not None else export_heartbeat_interval_s()),
            0.1,
        )
        self._tick_interval_s = max(float(tick_interval_s), 0.05)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = clock()
        self._last_log_at = self._started_at
        self._last_log_written = 0
        self._finished = False
        self._closed = False
        self._io_is_remote = False

        self._stage = "准备"
        self._stage_detail = ""
        self._stage_started_at = self._started_at
        self._total_frames = 0
        self._fps = 0
        self._written = 0
        self._rendered = 0
        self._prescan_done = 0
        self._prescan_total = 0
        self._frame_index = -1
        self._frame_started_at: float | None = None
        self._last_frame_render_s = 0.0
        self._last_write_block_s = 0.0
        self._total_write_block_s = 0.0
        self._write_started_at: float | None = None
        self._write_in_progress = False
        self._write_index = -1

        self._ffmpeg_frame: int | None = None
        self._ffmpeg_progress_end = False
        self._last_ffmpeg_progress_at: float | None = None
        self._ffmpeg_fps: float | None = None
        self._ffmpeg_total_size: int | None = None
        self._last_total_size_advance_at: float | None = None
        self._ffmpeg_speed: float | None = None
        self._ffmpeg_process: Any = None
        self._ffmpeg_started_at: float | None = None
        self._hardware_encoder = bool(hardware_encoder)
        self._sidecar: Any = None
        self._gpu_frame_index = -1
        self._gpu_frame_started_at: float | None = None
        self._last_gpu_frame_wait_s = 0.0
        self._stall_action: str | None = None
        self._write_stall_reported = False
        self._stall_decision_reported = False
        self._send_stall_reported = False
        self._no_progress_reported = False
        self._first_frame_wait_reported = False

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._closed = False
            self._finished = False
            self._stop = threading.Event()
            self._started_at = self._clock()
            self._last_log_at = self._started_at
            thread = threading.Thread(
                target=self._run, name="subtitle-export-heartbeat", daemon=True
            )
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        # 先立 closed/finished：正在执行中的 tick 也不能再向 UI/日志发心跳，
        # 否则排队信号会在终态（失败/已停止）之后覆盖状态文案。
        with self._lock:
            self._closed = True
            self._finished = True
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(self._tick_interval_s * 4, 1.0))
        with self._lock:
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._tick_interval_s):
            try:
                self._tick(self._clock())
            except Exception:  # noqa: BLE001 - telemetry must never kill an export
                # 心跳是遥测：任何异常都不能让导出线程跟着死。
                pass

    def poll_once(self, now: float | None = None) -> None:
        """Run one watchdog tick (deterministic tests / injected schedulers)."""

        self._tick(self._clock() if now is None else float(now))

    # -- state updates -------------------------------------------------

    def _set_stage_locked(self, stage: str, detail: str = "") -> None:
        now = self._clock()
        if stage != self._stage:
            self._stage = stage
            self._stage_started_at = now
        self._stage_detail = detail

    def set_stage(self, stage: str, detail: str = "") -> None:
        with self._lock:
            self._set_stage_locked(stage, detail)

    def configure(
        self,
        *,
        total_frames: int | None = None,
        fps: int | None = None,
        output_path: Path | None = None,
        io_is_remote: bool | None = None,
    ) -> None:
        with self._lock:
            if total_frames is not None:
                self._total_frames = max(int(total_frames), 0)
            if fps is not None:
                self._fps = max(int(fps), 0)
            if io_is_remote is not None:
                self._io_is_remote = bool(io_is_remote)
            if output_path is not None:
                self._stage_detail = str(output_path.name)
                if io_is_remote is None:
                    self._io_is_remote = is_remote_path(output_path)

    def set_ffmpeg_process(self, process: Any, *, hardware_encoder: bool) -> None:
        with self._lock:
            self._ffmpeg_process = process
            self._ffmpeg_started_at = self._clock() if process is not None else None
            self._hardware_encoder = bool(hardware_encoder)

    def set_sidecar(self, sidecar: Any | None) -> None:
        with self._lock:
            self._sidecar = sidecar

    def set_prescan(self, done: int, total: int) -> None:
        with self._lock:
            self._prescan_done = max(int(done), 0)
            self._prescan_total = max(int(total), 0)
            self._set_stage_locked("预扫字幕范围")

    def set_rendered_frames(self, count: int) -> None:
        with self._lock:
            self._rendered = max(int(count), 0)

    def begin_frame(self, index: int) -> None:
        with self._lock:
            self._frame_index = int(index)
            self._frame_started_at = self._clock()

    def end_frame(self, index: int) -> None:
        with self._lock:
            if self._frame_started_at is not None:
                self._last_frame_render_s = max(
                    self._clock() - self._frame_started_at, 0.0
                )
            self._frame_started_at = None

    def begin_gpu_frame(self, index: int) -> None:
        with self._lock:
            self._gpu_frame_index = int(index)
            self._gpu_frame_started_at = self._clock()

    def end_gpu_frame(self) -> None:
        with self._lock:
            if self._gpu_frame_started_at is not None:
                self._last_gpu_frame_wait_s = max(
                    self._clock() - self._gpu_frame_started_at, 0.0
                )
            self._gpu_frame_started_at = None

    def begin_write(self, index: int) -> None:
        with self._lock:
            self._write_index = int(index)
            self._write_started_at = self._clock()
            self._write_in_progress = True
            self._write_stall_reported = False
            self._stall_decision_reported = False

    def end_write(self, index: int) -> None:
        with self._lock:
            if self._write_started_at is not None:
                block_s = max(self._clock() - self._write_started_at, 0.0)
                self._last_write_block_s = block_s
                self._total_write_block_s += block_s
            self._write_started_at = None
            self._write_in_progress = False
            self._write_index = -1

    def frame_written(self, count: int, total: int) -> None:
        with self._lock:
            self._written = max(int(count), 0)
            self._total_frames = max(int(total), 0)

    def observe_ffmpeg_progress(self, key: str, value: str) -> None:
        with self._lock:
            if key == "frame":
                parsed = _as_int(value)
                if parsed is not None:
                    parsed = max(parsed, 0)
                    if parsed != self._ffmpeg_frame:
                        self._last_ffmpeg_progress_at = self._clock()
                    self._ffmpeg_frame = parsed
            elif key == "fps":
                self._ffmpeg_fps = _as_float(value)
            elif key == "total_size":
                parsed = _as_int(value)
                if parsed is not None:
                    parsed = max(parsed, 0)
                    if parsed != self._ffmpeg_total_size:
                        self._last_total_size_advance_at = self._clock()
                    self._ffmpeg_total_size = parsed
            elif key == "speed":
                self._ffmpeg_speed = _as_float(str(value).strip().rstrip("x"))
            elif key == "progress":
                self._ffmpeg_progress_end = value.strip() == "end"

    def note_ffmpeg_progress_end(self) -> None:
        with self._lock:
            self._ffmpeg_progress_end = True

    def note_retry(self, reason: str) -> None:
        """A new attempt starts from frame zero; keep the heartbeat truthful."""
        with self._lock:
            self._written = 0
            self._rendered = 0
            self._last_log_written = 0
            self._write_started_at = None
            self._write_in_progress = False
            self._write_index = -1
            self._ffmpeg_frame = None
            self._ffmpeg_progress_end = False
            self._last_ffmpeg_progress_at = None
            self._ffmpeg_fps = None
            self._ffmpeg_total_size = None
            self._last_total_size_advance_at = None
            self._ffmpeg_speed = None
            self._ffmpeg_process = None
            self._ffmpeg_started_at = None
            self._sidecar = None
            self._gpu_frame_started_at = None
            self._last_gpu_frame_wait_s = 0.0
            self._prescan_done = 0
            self._prescan_total = 0
            self._stall_action = None
            self._write_stall_reported = False
            self._stall_decision_reported = False
            self._send_stall_reported = False
            self._no_progress_reported = False
            self._first_frame_wait_reported = False
            self._set_stage_locked("重试", reason)

    @property
    def stall_action(self) -> str | None:
        with self._lock:
            return self._stall_action

    def note_repair(self, message: str) -> None:
        """Immediate user-facing feedback for an automatic repair decision."""

        self._emit(message)

    def note_sidecar_restart(
        self,
        attempt: int,
        written: int,
        total: int,
        reason: str = "",
    ) -> None:
        """A resumed GPU attempt: keep ffmpeg progress, reset sidecar state."""

        detail = " ".join(str(reason).split())[:80]
        with self._lock:
            # ffmpeg 进程没有变，已写帧/其 -progress 计数必须保留；只重置
            # sidecar 相关状态，避免心跳把“已写 X 帧”清成 0。
            self._sidecar = None
            self._gpu_frame_started_at = None
            self._send_stall_reported = False
            suffix = f"（{detail}）" if detail else ""
            self._set_stage_locked(
                "GPU 重启",
                f"第 {attempt} 次，从第 {written}/{total} 帧继续{suffix}",
            )

    def clear_stall_action(self) -> None:
        with self._lock:
            self._stall_action = None

    def mark_finished(self) -> None:
        with self._lock:
            self._finished = True

    # -- watchdog / logging -------------------------------------------

    def _output_size_recently_advanced(self, now: float, grace_s: float) -> bool:
        last = self._last_total_size_advance_at
        return (
            last is not None
            and self._ffmpeg_total_size is not None
            and self._ffmpeg_total_size > 0
            and now - last < grace_s
        )

    def _tick(self, now: float) -> None:
        actions: list[Callable[[], None]] = []
        messages: list[str] = []
        heartbeat: str | None = None

        with self._lock:
            if self._finished:
                return
            process = self._ffmpeg_process
            process_alive = bool(
                process is not None and getattr(process, "poll", lambda: None)() is None
            )

            # 1) ffmpeg 写管道停滞：写阻塞且 ffmpeg 帧计数已经很久没有前进。
            # 用最后一次前进时间做参照；动作阈值取 2× 宽限，且网络输入/输出或
            # 输出文件仍在增长时只回报，避免把 IO 阻塞误判成编码器楔死并切 CPU。
            grace_s = ffmpeg_write_stall_grace_s()
            stalled_for = 0.0
            if self._write_in_progress and self._write_started_at is not None:
                last_progress_at = self._last_ffmpeg_progress_at
                reference = max(
                    self._write_started_at,
                    last_progress_at
                    if last_progress_at is not None
                    else self._write_started_at,
                )
                stalled_for = max(now - reference, 0.0)
            write_stall_active = (
                self._write_in_progress
                and self._write_started_at is not None
                and self._written > 0
                and process_alive
            )
            output_moving = self._output_size_recently_advanced(now, grace_s)
            if write_stall_active and stalled_for >= grace_s * 2.0:
                if self._hardware_encoder and self._stall_action is None:
                    if not self._stall_decision_reported:
                        self._stall_decision_reported = True
                        self._write_stall_reported = True
                        if self._io_is_remote or output_moving:
                            reason = (
                                "输入/输出路径是网络盘"
                                if self._io_is_remote
                                else "输出文件仍在增长"
                            )
                            messages.append(
                                f"ffmpeg 已 {stalled_for:.0f}s 未从管道读取新帧，"
                                f"{reason}，可能是 IO 阻塞；"
                                "不自动切 CPU（如确认停止可点「停止导出」）"
                            )
                        else:
                            self._stall_action = "restart_cpu"
                            messages.append(
                                "检测到硬件编码器长时间未从管道读取任何新帧"
                                f"（{stalled_for:.0f}s），"
                                "自动切换 CPU 编码从头重试"
                            )
                            actions.append(
                                lambda proc=process: terminate_process(proc)
                            )
                elif not self._write_stall_reported:
                    self._write_stall_reported = True
                    messages.append(
                        f"ffmpeg 已 {stalled_for:.0f}s 未从管道读取新帧，"
                        "导出仍在等待（磁盘/编码器可能很慢）；如确认停止可点「停止导出」"
                    )
            elif (
                write_stall_active
                and stalled_for >= grace_s
                and not self._write_stall_reported
            ):
                self._write_stall_reported = True
                messages.append(
                    f"ffmpeg 已 {stalled_for:.0f}s 未从管道读取新帧，"
                    "导出仍在等待（磁盘/编码器可能很慢）；如确认停止可点「停止导出」"
                )

            # 1b) 启动/首帧阶段没有可用的 -progress 计数：只反馈，不做任何
            # 误杀。真实的长素材打开、冷启动编码器可以远超一两分钟。
            if (
                self._written == 0
                and self._ffmpeg_frame is None
                and self._ffmpeg_started_at is not None
                and now - self._ffmpeg_started_at >= 300.0
                and not self._no_progress_reported
            ):
                self._no_progress_reported = True
                messages.append(
                    f"ffmpeg 启动后 {now - self._ffmpeg_started_at:.0f}s 仍没有"
                    "任何编码进度（可能在打开背景素材或初始化编码器）；"
                    "如长时间无变化可点「停止导出」"
                )
            elif (
                self._written == 0
                and self._write_in_progress
                and self._write_started_at is not None
                and self._ffmpeg_frame is not None
                and process_alive
                and stalled_for >= 300.0
            ):
                if self._hardware_encoder and self._stall_action is None:
                    if not self._first_frame_wait_reported:
                        self._first_frame_wait_reported = True
                        if self._io_is_remote or output_moving:
                            reason = (
                                "输入/输出路径是网络盘"
                                if self._io_is_remote
                                else "输出文件仍在增长"
                            )
                            messages.append(
                                f"首帧写入已等待 {stalled_for:.0f}s，{reason}，"
                                "可能是 IO 阻塞；"
                                "不自动切 CPU（如确认停止可点「停止导出」）"
                            )
                        else:
                            self._stall_action = "restart_cpu"
                            messages.append(
                                f"首帧写入已等待 {stalled_for:.0f}s 且编码器未消费新帧，"
                                "自动切换 CPU 编码从头重试"
                            )
                            actions.append(
                                lambda proc=process: terminate_process(proc)
                            )
                elif not self._first_frame_wait_reported:
                    self._first_frame_wait_reported = True
                    messages.append(
                        f"首帧写入已等待 {stalled_for:.0f}s，"
                        "编码器尚未消费新帧；导出仍在等待"
                    )

            # 2) sidecar 的 JSON 请求写不出去：正常是微秒级，长时间不动就是主
            # 循环楔死。杀掉 sidecar 会让阻塞的 write 立刻返回，交给上层回退。
            if self._sidecar is not None and not self._send_stall_reported:
                age_getter = getattr(self._sidecar, "send_in_flight_age_s", None)
                try:
                    send_age = float(age_getter()) if callable(age_getter) else 0.0
                except Exception:  # noqa: BLE001
                    send_age = 0.0
                if send_age >= sidecar_send_stall_grace_s():
                    self._send_stall_reported = True
                    messages.append(
                        f"GPU 字幕渲染器 {send_age:.0f}s 未接收新请求（可能已楔死），"
                        "正在终止它（上层会按额度重启 GPU 进程）"
                    )
                    abort = getattr(self._sidecar, "abort", None)
                    if callable(abort):
                        actions.append(
                            lambda sidecar=self._sidecar: _request_sidecar_stall_abort(
                                sidecar
                            )
                        )

            # 3) 批量心跳：每 interval 一条，绝不逐帧写日志。
            if now - self._last_log_at >= self._interval_s:
                heartbeat = self._format_heartbeat_locked(now)
                self._last_log_at = now
                self._last_log_written = self._written

        for action in actions:
            try:
                action()
            except Exception:  # noqa: BLE001 - watchdog actions are best effort
                pass
        for message in messages:
            self._emit(message)
        if heartbeat is not None:
            self._emit(heartbeat)

    def _format_heartbeat_locked(self, now: float) -> str:
        parts: list[str] = [f"导出心跳：{self._stage}"]
        span_s = max(now - self._last_log_at, 0.1)
        if self._stage_detail:
            parts.append(self._stage_detail)
        if self._prescan_total > 0:
            percent = min(100, self._prescan_done * 100 // self._prescan_total)
            parts.append(f"预扫 {self._prescan_done}/{self._prescan_total}（{percent}%）")
        if self._total_frames > 0:
            percent = min(100, self._written * 100 // self._total_frames)
            parts.append(f"已写 {self._written}/{self._total_frames}（{percent}%）")
            delta = self._written - self._last_log_written
            if delta > 0:
                parts.append(f"近 {span_s:.0f}s 写 {delta} 帧")
        if self._rendered > self._written:
            parts.append(f"worker 已渲染 {self._rendered} 帧")
        if self._frame_started_at is not None:
            parts.append(f"本帧渲染 {now - self._frame_started_at:.1f}s")
        elif self._last_frame_render_s:
            parts.append(f"上帧渲染 {self._last_frame_render_s:.1f}s")
        if self._gpu_frame_started_at is not None:
            parts.append(
                f"等待 GPU 第 {self._gpu_frame_index} 帧"
                f" {now - self._gpu_frame_started_at:.1f}s"
            )
        elif self._last_gpu_frame_wait_s:
            parts.append(f"上帧 GPU 等待 {self._last_gpu_frame_wait_s:.1f}s")
        if self._write_in_progress and self._write_started_at is not None:
            parts.append(f"管道写入中 {now - self._write_started_at:.1f}s")
        elif self._last_write_block_s:
            parts.append(f"上帧管道等待 {self._last_write_block_s:.2f}s")
        if self._total_write_block_s >= 1.0:
            parts.append(f"累计管道等待 {self._total_write_block_s:.1f}s")
        if self._ffmpeg_frame is not None:
            ffmpeg_part = f"ffmpeg 已输出 {self._ffmpeg_frame} 帧"
            if self._ffmpeg_total_size is not None:
                ffmpeg_part += f"/{format_megabytes(self._ffmpeg_total_size)}"
            if self._ffmpeg_fps is not None:
                ffmpeg_part += f"/{self._ffmpeg_fps:.1f}fps"
            if self._ffmpeg_speed is not None:
                ffmpeg_part += f"/{self._ffmpeg_speed:.2f}x"
            parts.append(ffmpeg_part)
        if self._ffmpeg_progress_end:
            parts.append("ffmpeg 已报 progress=end，正在收尾/重写文件")
        elif self._stage == "编码收尾":
            parts.append(f"编码收尾已 {now - self._stage_started_at:.0f}s")
        if self._sidecar is not None:
            snapshot = None
            getter = getattr(self._sidecar, "progress_snapshot", None)
            try:
                snapshot = getter() if callable(getter) else None
            except Exception:  # noqa: BLE001
                snapshot = None
            if isinstance(snapshot, dict):
                phase = str(snapshot.get("phase") or "").strip()
                done = snapshot.get("done")
                total = snapshot.get("total")
                if phase:
                    sidecar_part = f"sidecar {phase}"
                    if done is not None and total:
                        sidecar_part += f" {done}/{total}"
                    parts.append(sidecar_part)
        if self._stall_action == "restart_cpu":
            parts.append("已触发硬件编码器→CPU 自动重试")
        parts.append(f"累计 {format_duration(now - self._started_at)}")
        return "，".join(parts)

    def _emit(self, message: str) -> None:
        with self._lock:
            if self._closed:
                return
        try:
            self._logger(f"[导出] {message}")
        except Exception:  # noqa: BLE001 - broken UI signal must not stop telemetry
            pass


_ACTIVE = threading.local()


def active_export_monitor() -> ExportProgressMonitor | None:
    return getattr(_ACTIVE, "monitor", None)


def set_active_export_monitor(monitor: ExportProgressMonitor) -> None:
    _ACTIVE.monitor = monitor


def clear_active_export_monitor(monitor: ExportProgressMonitor | None = None) -> None:
    current = getattr(_ACTIVE, "monitor", None)
    if monitor is None or current is monitor:
        _ACTIVE.monitor = None
