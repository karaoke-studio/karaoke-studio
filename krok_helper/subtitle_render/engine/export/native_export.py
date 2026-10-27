"""Native sidecar frame source for export experiments.

C6 starts with a narrow adapter: Python still owns ffmpeg, progress, cleanup,
and fallback policy, while the native sidecar renders full RGBA frames in
timestamp ranges.  Strip/band export optimizations remain on the Python path
until the full-frame path is proven stable.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from krok_helper.errors import ExportCancelled
from krok_helper.subtitle_render.domain.timing import TimingTrack
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.engine.export.progress_watchdog import (
    active_export_monitor,
    export_frame_stall_grace_s,
    export_heartbeat_lease_s,
)
from krok_helper.subtitle_render.native.backend import (
    NativeRendererError,
    NativeRendererProcess,
    NativeRendererStallError,
    SharedFrameRingReader,
    SharedFrameSlot,
    heartbeat_lease_s,
)

_DEFAULT_TARGET_CHUNK_BYTES = 128 * 1024 * 1024
_DEFAULT_RING_SLOTS_CAP = 64
_DEFAULT_GPU_EXPORT_REALIZATION_MEMORY_MB = 256
_GPU_REALIZATION_ESTIMATED_BYTES_PER_TASK = 4096
_GPU_REALIZATION_MIN_TASKS = 8192
_GPU_REALIZATION_MAX_TASKS = 262144
# 每帧响应默认 60s：4K + 弱 GPU（含 iGPU）单帧（字形 + 发光）可能远超旧值 15s；
# sidecar 进程退出靠管道 EOF 立即暴露，超时只兜 worker 卡死（如驱动 TDR 挂起）。
_DEFAULT_GPU_EXPORT_RESPONSE_TIMEOUT_S = 60.0
# configure 含每 worker prewarm 整帧渲染，4K 多 worker 弱 GPU 下给足余量。
_DEFAULT_GPU_EXPORT_CONFIGURE_TIMEOUT_S = 180.0
# 显存预检：configure 后 usage×growth 预估帧阶段峰值（glow 池 / 字形增长），
# 超过 budget 则降 worker 重配；单 worker 仍超且逼近 budget 时报错走 CPU 回退。
_DEFAULT_GPU_EXPORT_VRAM_GROWTH = 2.0
# sidecar 应答 shutdown 后还要析构 GPU worker 池（join 线程 + 拆 Direct2D 设备），
# 4 worker / 4K 下 1s 明显不够。等不够就会 TerminateProcess，把 QSharedMemory 的
# 系统信号量永久锁死，本进程 detach 随即无限期阻塞（见 backend.close 的说明）。
_GPU_EXPORT_CLOSE_TIMEOUT_S = 5.0
_GPU_EXPORT_VRAM_NEAR_BUDGET_RATIO = 0.9
_GIB = 1024 * 1024 * 1024
_DEFAULT_GPU_RESTART_ATTEMPTS = 3
GPU_RESTART_RECOVERY_FRAMES = 30


def gpu_restart_attempts() -> int:
    """Per-run GPU sidecar restart allowance (``KROK_SUBTITLE_GPU_RESTART_ATTEMPTS``)."""

    raw = os.environ.get("KROK_SUBTITLE_GPU_RESTART_ATTEMPTS")
    if raw is None or not raw.strip():
        return _DEFAULT_GPU_RESTART_ATTEMPTS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_GPU_RESTART_ATTEMPTS
    return max(0, min(value, 10))


def gpu_max_restarts_per_export() -> int:
    """Absolute per-export restart ceiling (``KROK_SUBTITLE_GPU_MAX_RESTARTS_PER_EXPORT``).

    The adaptive budget is refilled by healthy 30-frame streaks.  Without a
    separate ceiling, a sidecar that crashes every ~30 frames can refill the
    budget forever and turn a long export into thousands of restarts.  This
    ceiling only bounds one export; healthy streaks still refill the
    process-wide budget for later exports.
    """

    default = max(4, gpu_restart_attempts() * 2)
    raw = os.environ.get("KROK_SUBTITLE_GPU_MAX_RESTARTS_PER_EXPORT")
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, min(value, 100))


class GpuRestartBudget:
    """Process-wide adaptive restart allowance for wedged GPU sidecars.

    每次真停滞消耗一次额度；**连续**正常收到 30 帧 GPU 渲染结果后额度
    +1（缓慢恢复到上限，发生停滞/重启时连续计数清零）。额度只限制
    “重启”，不限制首次 GPU 尝试：额度为 0 时下一单仍会先试 GPU，失败
    才回退 CPU。``max_retries == 0`` 表示显式禁用重启（环境变量设 0 时
    不会自行恢复）。
    """

    def __init__(self, max_retries: int) -> None:
        self._max = max(int(max_retries), 0)
        self._remaining = self._max
        self._lock = threading.Lock()

    @property
    def max_retries(self) -> int:
        with self._lock:
            return self._max

    @property
    def remaining(self) -> int:
        with self._lock:
            return self._remaining

    def consume(self) -> bool:
        with self._lock:
            if self._remaining <= 0:
                return False
            self._remaining -= 1
            return True

    def refund(self) -> int:
        """Return an allowance consumed for a retry that was cancelled."""

        with self._lock:
            if self._remaining < self._max:
                self._remaining += 1
            return self._remaining

    def reward_healthy_streak(self) -> int:
        """One sustained healthy GPU frame streak restores one attempt."""

        return self.refund()


_BUDGET_LOCK = threading.Lock()
_GPU_RESTART_BUDGET: GpuRestartBudget | None = None

_GPU_RESTARTABLE_MARKERS = (
    # 真停滞 / 请求写不出去 / 等帧超时
    "request write stalled",
    "frame wait stall",
    "gpu_pool_stalled",
    "pool stalled",
    # 设备级失效：重建 sidecar 通常能恢复
    "device removed",
    "device reset",
    "device hung",
    "dxgi_error_device",
    "0x887a0005",
    "0x887a0006",
    "0x887a0007",
    # sidecar 启动/握手超时或异常退出
    "response timed out",
    "did not become ready",
    "connection closed while process is still running",
    # 进程直接崩溃（TDR / access violation / stack buffer overrun）：
    # 新进程是最可能恢复的路径，不能直接判 CPU。
    "native renderer exited while waiting for",
    "native renderer exited",
    "exit_hex=0xc0000",
    "native renderer pipe write failed for 'gpu_render_frame'",
    'native renderer pipe write failed for "gpu_render_frame"',
    # sidecar 崩溃后帧消费路径暴露出的共享内存/环形槽错误：新 sidecar 会
    # 用新的 shm_key/reader，按续渲处理更划算。
    "failed to attach native shared memory",
    "failed to consume gpu subtitle frame",
    "shared frame",
    "stale shared frame slot",
)


def gpu_export_error_is_restartable(exc: BaseException) -> bool:
    """Whether restarting the GPU sidecar can plausibly recover this failure."""

    if isinstance(exc, NativeRendererStallError):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _GPU_RESTARTABLE_MARKERS)


def gpu_restart_budget() -> GpuRestartBudget:
    global _GPU_RESTART_BUDGET
    with _BUDGET_LOCK:
        if _GPU_RESTART_BUDGET is None:
            _GPU_RESTART_BUDGET = GpuRestartBudget(gpu_restart_attempts())
        return _GPU_RESTART_BUDGET


def reset_gpu_restart_budget() -> None:
    """Test/diagnostic hook: rebuild the process-wide budget from the env."""

    global _GPU_RESTART_BUDGET
    with _BUDGET_LOCK:
        _GPU_RESTART_BUDGET = None


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > minimum else default


def gpu_export_response_timeout_s(realizations: bool) -> float:
    """Per-response sidecar timeout (``KROK_SUBTITLE_GPU_EXPORT_TIMEOUT_S``)."""
    if realizations:
        return _env_float(
            "KROK_SUBTITLE_GPU_EXPORT_TIMEOUT_S", 300.0, minimum=0.0
        )
    return _env_float(
        "KROK_SUBTITLE_GPU_EXPORT_TIMEOUT_S",
        _DEFAULT_GPU_EXPORT_RESPONSE_TIMEOUT_S,
        minimum=0.0,
    )


def gpu_export_configure_timeout_s(realizations: bool) -> float:
    """gpu_configure 专属超时（``KROK_SUBTITLE_GPU_EXPORT_CONFIGURE_TIMEOUT_S``）。"""
    return max(
        gpu_export_response_timeout_s(realizations),
        _env_float(
            "KROK_SUBTITLE_GPU_EXPORT_CONFIGURE_TIMEOUT_S",
            _DEFAULT_GPU_EXPORT_CONFIGURE_TIMEOUT_S,
            minimum=0.0,
        ),
    )


def gpu_export_preflight_enabled() -> bool:
    return os.environ.get(
        "KROK_SUBTITLE_GPU_EXPORT_VRAM_PREFLIGHT", "1"
    ).strip().lower() not in {"0", "false", "no", "off"}


def gpu_export_vram_growth_factor() -> float:
    return max(
        1.0,
        _env_float(
            "KROK_SUBTITLE_GPU_EXPORT_VRAM_GROWTH",
            _DEFAULT_GPU_EXPORT_VRAM_GROWTH,
        ),
    )


def gpu_export_vram_headroom(
    configured: dict[str, object],
) -> tuple[int, int] | None:
    """Extract the usable DXGI segment ``(usage, budget)`` after configure.

    独显通常使用 local segment；iGPU / UMA 共享显存机器可能只上报
    non-local segment。优先 local，其预算无效时回退 non-local，避免 AMD
    APU 在 4K 导出时完全跳过显存护栏。
    """
    if not configured.get("video_memory_info_available"):
        return None
    for prefix in ("local", "non_local"):
        try:
            usage = int(configured.get(f"{prefix}_video_memory_usage_bytes") or 0)
            budget = int(configured.get(f"{prefix}_video_memory_budget_bytes") or 0)
        except (TypeError, ValueError):
            continue
        if budget > 0:
            return max(usage, 0), budget
    return None


def degraded_gpu_worker_candidates(requested: int) -> list[int]:
    """Worker 降级序列：从共享帧传输上限 4 开始逐级递减至 1。

    预检公式是经验估算，逐级下降可避免误判时高配 GPU 从 3 直接掉到 1
    造成数倍导出时长——宁可多一次重配，也要保住并发。
    """
    first = max(1, min(int(requested), 4))
    return list(range(first, 0, -1))


def gpu_worker_limit_without_dynamic_budget(
    configured: dict[str, object], *, width: int, height: int
) -> int:
    """DXGI 动态预算不可用时，按适配器物理显存保守分档。

    非 4K 不预先降级。4K 下高端独显保留并发；小显存独显和
    ``dedicated_video_memory == 0`` 的 UMA/iGPU 才逐级压低。这只是
    QueryVideoMemoryInfo 失效时的后备策略，正常驱动仍以实时 budget 为准。
    """

    if max(int(width), 1) * max(int(height), 1) < 3840 * 2160:
        return 4
    try:
        dedicated = max(int(configured.get("dedicated_video_memory") or 0), 0)
    except (TypeError, ValueError):
        dedicated = 0
    if dedicated >= 8 * _GIB:
        return 4
    if dedicated >= 6 * _GIB:
        return 3
    if dedicated >= 4 * _GIB:
        return 2
    return 1


def gpu_configure_failure_allows_worker_retry(error: BaseException) -> bool:
    """Only retry lower concurrency for failures that can improve with fewer workers.

    In particular, a 180-second protocol timeout must not be repeated for every
    candidate.  Resource/device failures are normally returned immediately and
    are the cases where rebuilding a smaller Direct2D pool is useful.
    """

    message = str(error).lower()
    markers = (
        "out of memory",
        "cannot allocate",
        "failed to allocate",
        "bad_alloc",
        "e_outofmemory",
        "0x8007000e",
        "device removed",
        "device reset",
        "device hung",
        "dxgi_error_device",
        "0x887a0005",
        "0x887a0006",
        "0x887a0007",
    )
    return any(marker in message for marker in markers)


def _configure_gpu_export_with_preflight(
    *,
    renderer_path: str | os.PathLike[str] | None,
    track: TimingTrack,
    style: Style,
    width: int,
    height: int,
    fps: int,
    force_warp: bool,
    extra_tracks: list[TimingTrack] | None,
    worker_count: int,
    realizations: bool,
    shared: bool,
    duration_ms: int | None,
    packed: bool,
    crop_top: int,
    crop_height: int,
    packed_bands: list[tuple[int, int]],
    logger: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[NativeRendererProcess, dict[str, object], str]:
    """启动 sidecar 并 configure，附显存预检与 worker 降级。

    configure 后用 DXGI 上报的进程级显存 usage/budget 判断帧阶段（glow 池、
    字形 realization 会继续增长）是否吃得下：预估峰值超预算则关掉当前
    sidecar、按降级序列用更少 worker 重配；单 worker 仍逼近预算时抛
    :class:`NativeRendererError`，由上层整单回退 CPU Painter 保证导出完成。
    返回 ``(已启动的 renderer, configure 响应, 共享内存 key)``。

    每次起 sidecar / 降级重配前检查 ``should_cancel``：configure 阶段可能
    长达数十秒且没有别的取消探测点，用户点「停止导出」必须能立即生效，
    否则降级重试会把「正在停止导出…」拖成分钟级假死。
    """
    response_timeout_s = gpu_export_response_timeout_s(realizations)
    configure_timeout_s = gpu_export_configure_timeout_s(realizations)
    growth = gpu_export_vram_growth_factor()
    preflight = gpu_export_preflight_enabled()
    candidates = degraded_gpu_worker_candidates(worker_count)
    fallback_worker_limit: int | None = None
    monitor = active_export_monitor()

    def _detach_sidecar() -> None:
        if monitor is not None:
            monitor.set_sidecar(None)

    for attempt_index, attempt_workers in enumerate(candidates):
        # 首次 configure 已确认 DXGI 动态预算不可用时，直接跳过
        # 高于物理显存分档上限的中间并发数，避免重复做昂贵配置。
        if fallback_worker_limit is not None and attempt_workers > fallback_worker_limit:
            continue
        if should_cancel is not None and should_cancel():
            raise ExportCancelled("已停止导出。")
        shm_key = f"krok-gpu-export-{os.getpid()}-{uuid.uuid4().hex}"
        renderer = NativeRendererProcess(
            renderer_path,
            response_timeout_s=response_timeout_s,
            gpu_configure_timeout_s=configure_timeout_s,
            close_timeout_s=_GPU_EXPORT_CLOSE_TIMEOUT_S,
        )
        configure_invoked = False
        if monitor is not None:
            monitor.set_sidecar(renderer)
            monitor.set_stage("GPU 准备", f"{attempt_workers} 个 worker")
        try:
            renderer.start()
            # Query immutable adapter capacity before allocating a worker pool.
            # This prevents a 4-worker probe itself from exhausting a small
            # 4K adapter.  Older test/protocol doubles may not expose this
            # optional query, in which case configure-time DXGI data remains
            # the authority.
            if preflight and fallback_worker_limit is None:
                try:
                    backend_info = renderer.backend_info(force_warp=force_warp)
                except (AttributeError, NativeRendererError):
                    backend_info = {}
                if backend_info:
                    fallback_worker_limit = gpu_worker_limit_without_dynamic_budget(
                        backend_info, width=width, height=height
                    )
                if (
                    fallback_worker_limit is not None
                    and attempt_workers > fallback_worker_limit
                ):
                    if logger is not None:
                        logger(
                            "GPU 自适应并发：按适配器显存将本次导出限制为 "
                            f"{fallback_worker_limit} 个 worker"
                        )
                    renderer.close()
                    _detach_sidecar()
                    continue
            configure_started = time.perf_counter()
            configure_kwargs = {
                "width": width,
                "height": height,
                "fps": fps,
                "force_warp": force_warp,
                "extra_tracks": extra_tracks,
                "worker_count": attempt_workers,
                "realization_enabled": realizations,
                "shared_resources": shared,
                "wait_realizations": realizations,
                "realization_capacity": gpu_export_realization_capacity(),
                "export_crop_top": crop_top if packed else 0,
                "export_crop_height": crop_height if packed and not packed_bands else 0,
                "export_bands": packed_bands,
            }
            if duration_ms is not None:
                configure_kwargs["duration_ms"] = max(int(duration_ms), 0)
            configure_invoked = True
            # configure 是阻塞调用，中途不检查取消；用旁路线程在看用户
            # 点「停止导出」时直接 abort sidecar，让阻塞的协议读立即返回。
            watcher_stop = threading.Event()
            watcher: threading.Thread | None = None
            if should_cancel is not None:
                def _watch_cancel(
                    active_renderer=renderer, stop_event=watcher_stop
                ) -> None:
                    while not stop_event.wait(0.5):
                        try:
                            if should_cancel():
                                _abort_gpu_renderer(active_renderer, "导出已取消")
                                return
                        except Exception:  # noqa: BLE001 - watcher must stay quiet
                            return
                watcher = threading.Thread(
                    target=_watch_cancel,
                    name="subtitle-gpu-cancel-watch",
                    daemon=True,
                )
                watcher.start()
            try:
                configured = dict(
                    renderer.configure_gpu(
                        track,
                        style,
                        **configure_kwargs,
                    )
                )
            finally:
                watcher_stop.set()
                if watcher is not None:
                    watcher.join(timeout=1.0)
            configured["prepare_layout_ms"] = (
                time.perf_counter() - configure_started
            ) * 1000.0
            if should_cancel is not None and should_cancel():
                # 取消可能恰好落在 configure 响应已入队、旁路 watcher 已 abort
                # 之后；此时不能再把 sidecar 交给帧循环重新拉起。
                renderer.close()
                _detach_sidecar()
                raise ExportCancelled("已停止导出。")
        except NativeRendererError as exc:
            renderer.close()
            _detach_sidecar()
            if should_cancel is not None and should_cancel():
                raise ExportCancelled("已停止导出。") from exc
            if (
                configure_invoked
                and attempt_workers > 1
                and gpu_configure_failure_allows_worker_retry(exc)
            ):
                if logger is not None:
                    logger(
                        f"GPU {attempt_workers} worker 配置失败（{exc}），"
                        "降低并发重试"
                    )
                continue
            raise
        except BaseException:
            renderer.close()
            _detach_sidecar()
            raise

        headroom = gpu_export_vram_headroom(configured) if preflight else None
        if preflight and headroom is None:
            fallback_worker_limit = gpu_worker_limit_without_dynamic_budget(
                configured, width=width, height=height
            )
        if headroom is None and attempt_workers > (fallback_worker_limit or 4):
            if logger is not None:
                logger(
                    "GPU 显存预检：DXGI 未提供可用的本地/共享显存预算，"
                    f"按适配器物理显存将 4K 导出降至 "
                    f"{fallback_worker_limit} 个 worker 重新配置"
                )
            renderer.close()
            _detach_sidecar()
            continue
        if headroom is not None:
            usage, budget = headroom
            if usage * growth > budget:
                if attempt_index + 1 < len(candidates):
                    if logger is not None:
                        logger(
                            f"GPU 显存预检：configure 后已用 {usage / 1048576:.0f} MiB"
                            f" / 预算 {budget / 1048576:.0f} MiB，"
                            f"预估帧阶段峰值超预算，降至 "
                            f"{candidates[attempt_index + 1]} 个 worker 重新配置"
                        )
                    renderer.close()
                    _detach_sidecar()
                    continue
                if usage > budget * _GPU_EXPORT_VRAM_NEAR_BUDGET_RATIO:
                    renderer.close()
                    _detach_sidecar()
                    raise NativeRendererError(
                        f"GPU 显存不足：单 worker configure 后已用 "
                        f"{usage / 1048576:.0f} MiB / 预算 "
                        f"{budget / 1048576:.0f} MiB"
                    )
                if logger is not None:
                    logger(
                        f"GPU 显存预检：单 worker 已用 {usage / 1048576:.0f} MiB"
                        f" / 预算 {budget / 1048576:.0f} MiB，余量偏紧，"
                        "仍将尝试 GPU 导出（失败会自动回退 CPU Painter 完成导出）"
                    )
        return renderer, configured, shm_key

    raise NativeRendererError("GPU 显存预检未产生可用配置")  # pragma: no cover


def native_export_timestamps(
    *,
    start_frame: int,
    count: int,
    fps: int,
) -> list[int]:
    """Return export frame timestamps matching the Python renderer cadence."""
    normalized_fps = max(int(fps), 1)
    start = max(int(start_frame), 0)
    frame_count = max(int(count), 0)
    return [int(round((start + index) * 1000 / normalized_fps)) for index in range(frame_count)]


def native_export_chunk_frames(
    *,
    width: int,
    height: int,
    total_frames: int,
    target_bytes: int | None = None,
) -> int:
    """Choose a range size that keeps the shared-memory ring bounded."""
    frame_bytes = max(int(width), 1) * max(int(height), 1) * 4
    try:
        raw_target = (
            int(target_bytes)
            if target_bytes is not None
            else int(os.environ.get("KROK_SUBTITLE_NATIVE_EXPORT_CHUNK_BYTES", _DEFAULT_TARGET_CHUNK_BYTES))
        )
    except ValueError:
        raw_target = _DEFAULT_TARGET_CHUNK_BYTES
    by_bytes = max(raw_target, frame_bytes) // frame_bytes
    capped = min(max(int(by_bytes), 1), _DEFAULT_RING_SLOTS_CAP)
    return max(1, min(capped, max(int(total_frames), 1)))


def native_export_threads() -> int:
    raw = os.environ.get("KROK_SUBTITLE_NATIVE_EXPORT_THREADS") or os.environ.get(
        "KROK_SUBTITLE_NATIVE_THREADS",
        "4",
    )
    try:
        return max(int(raw), 1)
    except ValueError:
        return 4


def gpu_export_realization_capacity() -> int:
    """Return the export realization cap derived from a bounded memory budget."""

    raw = os.environ.get(
        "KROK_SUBTITLE_GPU_EXPORT_REALIZATION_MEMORY_MB",
        str(_DEFAULT_GPU_EXPORT_REALIZATION_MEMORY_MB),
    )
    try:
        budget_mb = max(int(raw), 32)
    except ValueError:
        budget_mb = _DEFAULT_GPU_EXPORT_REALIZATION_MEMORY_MB
    capacity = budget_mb * 1024 * 1024 // _GPU_REALIZATION_ESTIMATED_BYTES_PER_TASK
    return max(
        _GPU_REALIZATION_MIN_TASKS,
        min(int(capacity), _GPU_REALIZATION_MAX_TASKS),
    )


def gpu_export_packed_enabled() -> bool:
    return os.environ.get("KROK_SUBTITLE_GPU_EXPORT_PACKED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def gpu_export_shared_resources_enabled() -> bool:
    return os.environ.get(
        "KROK_SUBTITLE_GPU_EXPORT_SHARED_RESOURCES", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}


def gpu_export_realization_enabled() -> bool:
    return os.environ.get(
        "KROK_SUBTITLE_GPU_EXPORT_REALIZATION", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _wait_for_gpu_export_realizations(
    renderer: NativeRendererProcess,
    configured: dict[str, object],
    *,
    force_warp: bool,
    should_cancel: Callable[[], bool] | None,
    on_progress: Callable[[int, int], None] | None,
) -> dict[str, object]:
    """Wait for export-wide realization prewarm before submitting frame zero."""

    diagnostics = configured
    total = max(int(diagnostics.get("realization_prewarm_tasks", 0) or 0), 0)
    completed = max(int(diagnostics.get("realization_count", 0) or 0), 0)
    if on_progress is not None:
        on_progress(min(completed, total), total)
    while not bool(diagnostics.get("realization_prewarm_complete", True)):
        if should_cancel is not None and should_cancel():
            raise ExportCancelled("已停止导出。")
        time.sleep(0.05)
        diagnostics = renderer.gpu_diagnostics(force_warp=force_warp)
        total = max(
            int(diagnostics.get("realization_prewarm_tasks", total) or total), 0
        )
        completed = max(
            int(diagnostics.get("realization_count", completed) or completed), 0
        )
        if on_progress is not None:
            on_progress(min(completed, total), total)
    if on_progress is not None:
        on_progress(total, total)
    return diagnostics


def shared_slot_rgba_bytes(slot: SharedFrameSlot) -> bytes:
    """Return tightly packed RGBA bytes suitable for ffmpeg rawvideo stdin."""
    row_bytes = max(int(slot.width), 0) * 4
    stride = int(slot.stride)
    height = max(int(slot.height), 0)
    if row_bytes <= 0 or height <= 0:
        return b""
    if stride == row_bytes:
        expected = row_bytes * height
        return bytes(slot.payload[:expected])
    if stride < row_bytes:
        raise NativeRendererError(f"shared frame stride is too small: {stride} < {row_bytes}")
    payload = bytes(slot.payload)
    expected = stride * height
    if len(payload) < expected:
        raise NativeRendererError(f"shared frame payload is truncated: {len(payload)} < {expected}")
    packed = bytearray(row_bytes * height)
    for y in range(height):
        src_start = y * stride
        dst_start = y * row_bytes
        packed[dst_start : dst_start + row_bytes] = payload[src_start : src_start + row_bytes]
    return bytes(packed)


def iter_native_rgba_frames(
    track: TimingTrack,
    style: Style,
    *,
    width: int,
    height: int,
    fps: int,
    total_frames: int,
    duration_ms: int | None = None,
    renderer_path: str | os.PathLike[str] | None = None,
    threads: int | None = None,
    chunk_frames: int | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[bytes]:
    """Yield full-frame RGBA bytes rendered by the native sidecar in order."""
    frame_total = max(int(total_frames), 0)
    if frame_total <= 0:
        return
    timestamps = native_export_timestamps(start_frame=0, count=frame_total, fps=fps)
    for _t_ms, frame in iter_native_rgba_frames_at_times(
        track,
        style,
        timestamps,
        width=width,
        height=height,
        fps=fps,
        duration_ms=duration_ms,
        renderer_path=renderer_path,
        threads=threads,
        chunk_frames=chunk_frames,
        should_cancel=should_cancel,
    ):
        yield frame


def iter_native_rgba_frames_at_times(
    track: TimingTrack,
    style: Style,
    timestamps_ms: list[int],
    *,
    width: int,
    height: int,
    fps: int,
    duration_ms: int | None = None,
    renderer_path: str | os.PathLike[str] | None = None,
    threads: int | None = None,
    chunk_frames: int | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[tuple[int, bytes]]:
    """Yield ``(t_ms, rgba_bytes)`` for explicit timestamps from the sidecar."""
    timestamps_all = [int(t_ms) for t_ms in timestamps_ms]
    frame_total = len(timestamps_all)
    if frame_total <= 0:
        return
    worker_threads = native_export_threads() if threads is None else max(int(threads), 1)
    chunk_size = (
        native_export_chunk_frames(width=width, height=height, total_frames=frame_total)
        if chunk_frames is None
        else max(int(chunk_frames), 1)
    )
    generation = 1
    with NativeRendererProcess(renderer_path, response_timeout_s=5.0, close_timeout_s=1.0) as renderer:
        configure_kwargs = {"width": width, "height": height, "fps": fps}
        if duration_ms is not None:
            configure_kwargs["duration_ms"] = max(int(duration_ms), 0)
        renderer.configure(track, style, **configure_kwargs)
        start_frame = 0
        while start_frame < frame_total:
            if should_cancel is not None and should_cancel():
                raise ExportCancelled("已停止导出。")
            count = min(chunk_size, frame_total - start_frame)
            timestamps = timestamps_all[start_frame : start_frame + count]
            shm_key = f"krok-export-{os.getpid()}-{uuid.uuid4().hex}"
            renderer.start_render_range(
                timestamps,
                generation=generation,
                threads=worker_threads,
                shm_key=shm_key,
                ring_slots=count,
            )
            pending: dict[int, bytes] = {}
            next_emit = 0
            range_done = False
            try:
                while not range_done:
                    if should_cancel is not None and should_cancel():
                        renderer.send_cancel_generation(generation)
                        raise ExportCancelled("已停止导出。")
                    event = renderer.read_event()
                    if int(event.get("generation", generation)) != generation:
                        continue
                    kind = event.get("event")
                    if kind == "frame_ready":
                        with SharedFrameRingReader.from_event(event) as reader:
                            slot = reader.read_frame(event)
                        frame_index = int(event.get("frame_index", slot.frame_index))
                        pending[frame_index] = shared_slot_rgba_bytes(slot)
                        while next_emit in pending:
                            yield timestamps[next_emit], pending.pop(next_emit)
                            next_emit += 1
                    elif kind == "range_done":
                        range_done = True
                    elif kind == "generation_cancelled":
                        continue
                if next_emit != count:
                    raise NativeRendererError(
                        f"native export range ended before all frames were emitted: {next_emit}/{count}"
                    )
            finally:
                generation += 1
            start_frame += count


@contextmanager
def _detach_reader_before_shutdown(
    get_reader: Callable[[], SharedFrameRingReader | None],
):
    """在 sidecar 关闭之前先 detach 共享内存读端。

    ``with A, B:`` 的退出顺序是 B 先于 A。趁 sidecar 还活着、跨进程锁协议还完整
    时 detach，就绕开了「sidecar 先退出 → 系统信号量可能永久被占 → detach 死等」
    这条路；``SharedFrameRingReader.close`` 是幂等的，外层兜底再调一次无副作用。
    """
    try:
        yield
    finally:
        reader = get_reader()
        if reader is not None:
            reader.close()


def _abort_gpu_renderer(renderer: object, reason: str, *, stalled: bool = False) -> None:
    """Best-effort abort for a wedged sidecar (tests may use plain doubles)."""

    abort = getattr(renderer, "abort", None)
    if not callable(abort):
        return
    try:
        abort(reason=reason, stalled=stalled)
        return
    except TypeError:
        pass
    except Exception:  # noqa: BLE001
        return
    try:
        abort(reason=reason)
        return
    except TypeError:
        pass
    except Exception:  # noqa: BLE001
        return
    try:
        abort()
    except Exception:  # noqa: BLE001
        pass


def _await_gpu_frame(
    renderer: object,
    *,
    frame_index: int,
    should_cancel: Callable[[], bool] | None,
    logger: Callable[[str], None] | None = None,
    heartbeat_expected: bool = False,
    before_abort: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Wait for one GPU frame without a wall-clock per-frame timeout.

    Short 0.5s polls keep cancellation responsive and let the export heartbeat
    report "waiting for GPU frame N for Xs".  With a multi-worker pool the main
    loop keeps emitting progress heartbeats while workers render, so a heartbeat
    that stops for one lease means the main loop itself is wedged and the
    sidecar is restarted immediately.  Single-worker exports render
    synchronously on the main loop (no heartbeats during a frame), so they only
    use the generous no-frame backstop; a genuinely long frame must not be
    killed just because no heartbeat can be emitted.
    """

    monitor = active_export_monitor()
    grace_s = export_frame_stall_grace_s(heartbeat_expected)
    lease_s = export_heartbeat_lease_s() if heartbeat_expected else None
    started = time.monotonic()

    def _detach_reader() -> None:
        # 先 detach 共享内存再杀 sidecar，避免跨进程信号量被强杀后永久占用
        # 导致 5s detach 超时/映射泄漏；调用方传入的是幂等 close。
        if before_abort is not None:
            try:
                before_abort()
            except Exception:  # noqa: BLE001 - best effort
                pass

    while True:
        if should_cancel is not None and should_cancel():
            _detach_reader()
            _abort_gpu_renderer(renderer, "导出已取消")
            if monitor is not None:
                monitor.note_repair("导出已取消，正在终止 GPU 字幕渲染器")
            raise ExportCancelled("已停止导出。")
        if lease_s is not None:
            now = time.monotonic()
            age_getter = getattr(renderer, "last_heartbeat_age_s", None)
            try:
                heartbeat_age = (
                    float(age_getter()) if callable(age_getter) else None
                )
            except Exception:  # noqa: BLE001
                heartbeat_age = None
            if heartbeat_age is not None:
                # 只认「本次等待期间没有新心跳」：configure 末尾可能已经
                # 静默了一段时间，不能拿着旧时间戳在第一次轮询就误杀。
                last_heartbeat_at = now - heartbeat_age
                if now - max(started, last_heartbeat_at) >= lease_s:
                    message = (
                        f"GPU 字幕渲染器心跳已停止 {heartbeat_age:.0f}s"
                        "（主循环楔死），正在终止它"
                    )
                    _detach_reader()
                    _abort_gpu_renderer(renderer, "heartbeat stall", stalled=True)
                    if monitor is not None:
                        monitor.note_repair(message)
                    elif logger is not None:
                        logger(message)
                    raise NativeRendererStallError(message)
        try_finish = getattr(renderer, "try_finish_render_gpu_frame", None)
        if callable(try_finish):
            try:
                event = try_finish(0.5)
            except NativeRendererStallError:
                raise
            except NativeRendererError as exc:
                if bool(getattr(renderer, "stall_aborted", False)):
                    raise NativeRendererStallError(str(exc)) from exc
                raise
        else:  # pragma: no cover - very old doubles only
            return renderer.finish_render_gpu_frame()
        if event is not None:
            return event
        elapsed = time.monotonic() - started
        if elapsed >= grace_s:
            message = (
                f"GPU 字幕渲染器已 {elapsed:.0f}s 没有返回第 {frame_index} 帧"
                "（sidecar 无任何帧/心跳，判定楔死），正在终止它"
            )
            _detach_reader()
            _abort_gpu_renderer(renderer, "frame wait stall", stalled=True)
            if monitor is not None:
                monitor.note_repair(message)
            elif logger is not None:
                logger(message)
            raise NativeRendererStallError(message)


def iter_gpu_rgba_frames(
    track: TimingTrack,
    style: Style,
    *,
    width: int,
    height: int,
    fps: int,
    total_frames: int,
    duration_ms: int | None = None,
    start_t_ms: int = 0,
    start_frame: int | None = None,
    renderer_path: str | os.PathLike[str] | None = None,
    extra_tracks: list[TimingTrack] | None = None,
    force_warp: bool = False,
    worker_count: int = 1,
    shared_resources: bool | None = None,
    realization_enabled: bool | None = None,
    packed_rgba: bool | None = None,
    crop: tuple[int, int] | None = None,
    bands: list[tuple[int, int]] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_prepare_progress: Callable[[int, int], None] | None = None,
    on_diagnostics: Callable[[dict[str, object]], None] | None = None,
    on_frame_diagnostics: Callable[[dict[str, object]], None] | None = None,
    logger: Callable[[str], None] | None = None,
) -> Iterator[bytes | memoryview]:
    """Yield straight RGBA frames from the Direct2D GPU export path.

    Direct2D reads back only the renderer's conservative subtitle bands.  The
    shared-memory reader expands those bands into a transparent QImage before
    converting premultiplied BGRA to ffmpeg's straight RGBA input format.

    One worker keeps the original one-frame-deep two-slot pipeline. The
    opt-in packed path converts full-frame premultiplied BGRA to straight RGBA
    in the sidecar and lends the shared-memory slot directly to ffmpeg without
    constructing a QImage. Hardware
    exports may use up to four independent Direct2D workers; completed frames
    are held in a ring-sized reorder window and yielded strictly in timestamp
    order, so ffmpeg never observes out-of-order input.

    configure 阶段附显存预检：预估峰值超预算时自动降 worker 数重配（见
    :func:`_configure_gpu_export_with_preflight`），``logger`` 接收决策日志。
    """
    from PyQt6.QtGui import QImage

    frame_total = max(int(total_frames), 0)
    if frame_total <= 0:
        return
    slot_count = 2
    packed = gpu_export_packed_enabled() if packed_rgba is None else bool(packed_rgba)
    shared = (
        gpu_export_shared_resources_enabled()
        if shared_resources is None
        else bool(shared_resources)
    )
    realizations = (
        gpu_export_realization_enabled()
        if realization_enabled is None
        else bool(realization_enabled)
    )
    packed_bands: list[tuple[int, int]] = []
    if packed:
        for raw_top, raw_height in bands or []:
            top = max(0, min(int(raw_top), max(int(height) - 1, 0)))
            band_height = max(1, min(int(raw_height), int(height) - top))
            packed_bands.append((top, band_height))
    crop_top, crop_height = crop if packed and crop is not None else (0, height)
    crop_top = max(0, min(int(crop_top), max(int(height) - 1, 0)))
    crop_height = max(1, min(int(crop_height), int(height) - crop_top))
    packed_height = (
        sum(max(int(band_height), 1) for _top, band_height in packed_bands)
        if packed_bands
        else crop_height
    )
    reader: SharedFrameRingReader | None = None

    def _close_reader() -> None:
        if reader is not None:
            reader.close()

    gpu_renderer, configured, shm_key = _configure_gpu_export_with_preflight(
        renderer_path=renderer_path,
        track=track,
        style=style,
        width=width,
        height=height,
        fps=fps,
        force_warp=force_warp,
        extra_tracks=extra_tracks,
        worker_count=worker_count,
        realizations=realizations,
        shared=shared,
        duration_ms=duration_ms,
        packed=packed,
        crop_top=crop_top,
        crop_height=crop_height,
        packed_bands=packed_bands,
        logger=logger,
        should_cancel=should_cancel,
    )
    # 帧阶段也可能卡在「写请求」或「等帧」里；旁路线程让「停止导出」能直接
    # abort sidecar，而不是等 60s 的请求看门狗或下一轮帧检查。
    frame_cancel_stop = threading.Event()
    frame_cancel_thread: threading.Thread | None = None
    if should_cancel is not None:
        def _watch_frame_cancel(
            active_renderer=gpu_renderer, stop_event=frame_cancel_stop
        ) -> None:
            while not stop_event.wait(0.5):
                try:
                    if should_cancel():
                        # 只 abort，不在旁路线程 detach reader：导出线程可能正在
                        # read_qimage/写 packed view，close() 会把 _shared 置空
                        # 造成 AttributeError 或 use-after-unmap。真正需要
                        # detach 的路径由导出线程在 _await 里先 close 再 abort。
                        _abort_gpu_renderer(active_renderer, "导出已取消")
                        return
                except Exception:  # noqa: BLE001 - watcher must stay quiet
                    return

        frame_cancel_thread = threading.Thread(
            target=_watch_frame_cancel,
            name="subtitle-gpu-frame-cancel",
            daemon=True,
        )
        frame_cancel_thread.start()
    try:
        with gpu_renderer as renderer, _detach_reader_before_shutdown(lambda: reader):
            if on_diagnostics is not None:
                on_diagnostics(configured)
            monitor = active_export_monitor()
            if monitor is not None:
                monitor.set_stage("GPU 渲染")
            active_workers = max(
                1, min(int(configured.get("worker_count", 1) or 1), 4)
            )
            frame_offset = int(start_frame) if start_frame is not None else 0
            slot_count = 2 if active_workers == 1 else active_workers

            request_started: dict[int, float] = {}

            def begin_frame(frame_index: int) -> None:
                if start_frame is None:
                    t_ms = max(int(start_t_ms), 0) + int(
                        round(frame_index * 1000 / max(int(fps), 1))
                    )
                else:
                    # 续渲：用绝对帧号算时间戳，避免「取整起点 + 取整偏移」
                    # 在 60/120fps 下出现 ±1ms 的内容漂移。
                    t_ms = int(
                        round(
                            (int(start_frame) + frame_index)
                            * 1000
                            / max(int(fps), 1)
                        )
                    )
                request_started[frame_index] = time.perf_counter()
                renderer.begin_render_gpu_frame(
                    t_ms,
                    force_warp=force_warp,
                    generation=1,
                    frame_index=frame_index,
                    shm_key=shm_key,
                    include_checksum=False,
                    readback_bands=not packed,
                    packed_rgba=packed,
                    packed_height=packed_height if packed else 0,
                    slot_count=slot_count,
                    request_serial=frame_index,
                )

            def record_frame_diagnostics(
                event: dict[str, object],
                frame_index: int,
                *,
                expand_ms: float,
                convert_ms: float,
                bytes_ms: float,
                frame_bytes: int,
                copies_per_frame: int,
            ) -> None:
                if on_frame_diagnostics is None:
                    return
                roundtrip_ms = (
                    time.perf_counter()
                    - request_started.pop(frame_index, time.perf_counter())
                ) * 1000.0
                native_accounted_ms = sum(
                    float(event.get(field, 0.0) or 0.0)
                    for field in (
                        "render_ms",
                        "readback_ms",
                        "native_pack_ms",
                        "shm_copy_ms",
                    )
                )
                on_frame_diagnostics(
                    {
                        "frame_index": frame_index,
                        "t_ms": int(event.get("t_ms", 0) or 0),
                        "worker_index": int(event.get("worker_index", 0) or 0),
                        "native_render_ms": float(event.get("render_ms", 0.0) or 0.0),
                        "animation_layout_ms": float(
                            event.get("animation_layout_ms", 0.0) or 0.0
                        ),
                        "geometry_ms": float(event.get("geometry_ms", 0.0) or 0.0),
                        "stroke_ms": float(event.get("stroke_ms", 0.0) or 0.0),
                        "glow_ms": float(event.get("glow_ms", 0.0) or 0.0),
                        "end_draw_wait_ms": float(
                            event.get("end_draw_wait_ms", 0.0) or 0.0
                        ),
                        "end_draw_glow_source_ms": float(
                            event.get("end_draw_glow_source_ms", 0.0) or 0.0
                        ),
                        "end_draw_ruby_glow_source_ms": float(
                            event.get("end_draw_ruby_glow_source_ms", 0.0) or 0.0
                        ),
                        "end_draw_inline_glow_source_ms": float(
                            event.get("end_draw_inline_glow_source_ms", 0.0) or 0.0
                        ),
                        "end_draw_frame_layers_ms": float(
                            event.get("end_draw_frame_layers_ms", 0.0) or 0.0
                        ),
                        "end_draw_empty_frame_ms": float(
                            event.get("end_draw_empty_frame_ms", 0.0) or 0.0
                        ),
                        "end_draw_count": int(event.get("end_draw_count", 0) or 0),
                        "end_draw_glow_source_count": int(
                            event.get("end_draw_glow_source_count", 0) or 0
                        ),
                        "end_draw_ruby_glow_source_count": int(
                            event.get("end_draw_ruby_glow_source_count", 0) or 0
                        ),
                        "end_draw_inline_glow_source_count": int(
                            event.get("end_draw_inline_glow_source_count", 0) or 0
                        ),
                        "end_draw_frame_layers_count": int(
                            event.get("end_draw_frame_layers_count", 0) or 0
                        ),
                        "end_draw_empty_frame_count": int(
                            event.get("end_draw_empty_frame_count", 0) or 0
                        ),
                        "glow_source_area_px": int(
                            event.get("glow_source_area_px", 0) or 0
                        ),
                        "layer_push": int(event.get("layer_push", 0) or 0),
                        "gpu_wait_ms": float(event.get("gpu_wait_ms", 0.0) or 0.0),
                        "readback_copy_ms": float(
                            event.get("readback_copy_ms", 0.0) or 0.0
                        ),
                        "readback_ms": float(event.get("readback_ms", 0.0) or 0.0),
                        "native_pack_ms": float(
                            event.get("native_pack_ms", 0.0) or 0.0
                        ),
                        "shm_copy_ms": float(event.get("shm_copy_ms", 0.0) or 0.0),
                        "protocol_roundtrip_ms": roundtrip_ms,
                        "protocol_wait_ms": max(
                            roundtrip_ms - native_accounted_ms, 0.0
                        ),
                        "python_expand_ms": expand_ms,
                        "python_convert_ms": convert_ms,
                        "python_bytes_ms": bytes_ms,
                        "bytes_per_frame": frame_bytes,
                        "copies_per_frame": copies_per_frame,
                        "readback_ratio": float(
                            event.get("readback_ratio", 1.0) or 0.0
                        ),
                    }
                )

            def consume_event(event: dict[str, object], frame_index: int) -> bytes:
                nonlocal reader
                if event.get("event") != "gpu_frame_ready":
                    raise NativeRendererError(
                        "GPU export frame was not delivered: "
                        f"{event.get('event', 'unknown')}"
                    )
                if reader is None:
                    reader = SharedFrameRingReader.from_event(event)
                    reader.attach()
                expand_started = time.perf_counter()
                try:
                    image = reader.read_qimage(event)
                    if image.isNull():
                        raise MemoryError("failed to allocate GPU readback QImage")
                    expand_ms = (time.perf_counter() - expand_started) * 1000.0
                    convert_started = time.perf_counter()
                    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
                    if image.isNull():
                        raise MemoryError("failed to allocate converted GPU frame")
                    convert_ms = (time.perf_counter() - convert_started) * 1000.0
                    bytes_started = time.perf_counter()
                    bits = image.constBits()
                    bits.setsize(image.sizeInBytes())
                    frame = bytes(bits)
                    bytes_ms = (time.perf_counter() - bytes_started) * 1000.0
                except NativeRendererError:
                    raise
                except (MemoryError, RuntimeError) as exc:
                    raise NativeRendererError(
                        f"failed to consume GPU subtitle frame {frame_index}: {exc}"
                    ) from exc
                record_frame_diagnostics(
                    event,
                    frame_index,
                    expand_ms=expand_ms,
                    convert_ms=convert_ms,
                    bytes_ms=bytes_ms,
                    frame_bytes=len(frame),
                    copies_per_frame=4,
                )
                return frame

            if active_workers == 1:
                begin_frame(0)
                for frame_index in range(frame_total):
                    if should_cancel is not None and should_cancel():
                        raise ExportCancelled("已停止导出。")
                    if monitor is not None:
                        monitor.begin_gpu_frame(frame_offset + frame_index)
                    try:
                        event = _await_gpu_frame(
                            renderer,
                            frame_index=frame_offset + frame_index,
                            should_cancel=should_cancel,
                            logger=logger,
                            heartbeat_expected=active_workers > 1,
                            before_abort=_close_reader,
                        )
                    finally:
                        if monitor is not None:
                            monitor.end_gpu_frame()
                    if frame_index + 1 < frame_total:
                        begin_frame(frame_index + 1)
                    if packed:
                        if reader is None:
                            reader = SharedFrameRingReader.from_event(event)
                            reader.attach()
                        with reader.borrow_packed_rgba_view(event) as frame:
                            record_frame_diagnostics(
                                event,
                                frame_index,
                                expand_ms=0.0,
                                convert_ms=0.0,
                                bytes_ms=0.0,
                                frame_bytes=len(frame),
                                copies_per_frame=2,
                            )
                            yield frame
                    else:
                        yield consume_event(event, frame_index)
            else:
                # Each slot owns the arithmetic sequence ``slot + n * slots``.
                # A slot is reused only after its previous QImage has been
                # copied, while a one-window reorder bound prevents a fast
                # worker from buffering an unbounded number of 4K frames.
                in_flight: set[int] = set()
                free_slots: set[int] = set()
                next_for_slot = [slot for slot in range(slot_count)]
                pending: dict[int, bytes | dict[str, object]] = {}
                next_emit = 0

                def submit_slot(slot: int) -> None:
                    frame_index = next_for_slot[slot]
                    begin_frame(frame_index)
                    in_flight.add(frame_index)
                    next_for_slot[slot] += slot_count

                for slot in range(min(slot_count, frame_total)):
                    submit_slot(slot)

                while next_emit < frame_total:
                    if should_cancel is not None and should_cancel():
                        raise ExportCancelled("已停止导出。")
                    if monitor is not None:
                        monitor.begin_gpu_frame(frame_offset + next_emit)
                    try:
                        event = _await_gpu_frame(
                            renderer,
                            frame_index=frame_offset + next_emit,
                            should_cancel=should_cancel,
                            logger=logger,
                            heartbeat_expected=active_workers > 1,
                            before_abort=_close_reader,
                        )
                    finally:
                        if monitor is not None:
                            monitor.end_gpu_frame()
                    frame_index = int(event.get("frame_index", -1) or 0)
                    if frame_index not in in_flight or frame_index in pending:
                        raise NativeRendererError(
                            f"unexpected GPU export frame index: {frame_index}"
                        )
                    in_flight.remove(frame_index)
                    slot = frame_index % slot_count
                    if packed:
                        if reader is None:
                            reader = SharedFrameRingReader.from_event(event)
                            reader.attach()
                        # Keep the slot reserved until its borrowed view has
                        # been consumed by ffmpeg. This allows out-of-order GPU
                        # completion without copying packed RGBA in Python or
                        # letting a worker overwrite a still-pending slot.
                        pending[frame_index] = event
                    else:
                        pending[frame_index] = consume_event(event, frame_index)
                        free_slots.add(slot)

                    while next_emit in pending:
                        completed = pending.pop(next_emit)
                        completed_slot = next_emit % slot_count
                        if packed:
                            assert isinstance(completed, dict)
                            assert reader is not None
                            with reader.borrow_packed_rgba_view(completed) as frame:
                                record_frame_diagnostics(
                                    completed,
                                    next_emit,
                                    expand_ms=0.0,
                                    convert_ms=0.0,
                                    bytes_ms=0.0,
                                    frame_bytes=len(frame),
                                    copies_per_frame=2,
                                )
                                yield frame
                            free_slots.add(completed_slot)
                        else:
                            assert isinstance(completed, bytes)
                            yield completed
                        next_emit += 1

                    reorder_limit = next_emit + slot_count
                    for free_slot in sorted(tuple(free_slots)):
                        candidate = next_for_slot[free_slot]
                        if candidate >= frame_total:
                            free_slots.remove(free_slot)
                            continue
                        if candidate >= reorder_limit:
                            continue
                        submit_slot(free_slot)
                        free_slots.remove(free_slot)
            if on_diagnostics is not None:
                # 诊断是可选遥测：帧已经渲完，诊断查询失败绝不能把整单
                # 导出判失败再回退 CPU 重渲一遍。
                try:
                    on_diagnostics(renderer.gpu_diagnostics(force_warp=force_warp))
                except NativeRendererError:
                    pass
    finally:
        frame_cancel_stop.set()
        if frame_cancel_thread is not None:
            frame_cancel_thread.join(timeout=1.0)
        if reader is not None:
            reader.close()
        _monitor = active_export_monitor()
        if _monitor is not None:
            _monitor.set_sidecar(None)
