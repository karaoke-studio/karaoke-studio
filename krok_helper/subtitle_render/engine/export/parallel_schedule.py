"""Resource and concurrency policy for CPU subtitle frame workers."""

from __future__ import annotations

import os

from krok_helper.subtitle_render.engine.export.render_job import RenderJob


_MULTIPROC_AUTO_WORKER_CAP = 8
_MULTIPROC_WORKER_CAP = 16
_MULTIPROC_MIN_FRAMES = 240
_CHUNK_TARGET_BYTES = 64 * 1024 * 1024
_CHUNK_MIN_TARGET_BYTES = 4 * 1024 * 1024
_MULTIPROC_MAX_PENDING_BYTES = 256 * 1024 * 1024
_MULTIPROC_PENDING_HARD_CAP_BYTES = 1024 * 1024 * 1024
_MULTIPROC_SYSTEM_RESERVE_BYTES = 1024 * 1024 * 1024
_MULTIPROC_WORKER_OVERHEAD_BYTES = 64 * 1024 * 1024
_MULTIPROC_MIN_STALL_TIMEOUT_S = 60.0
_MULTIPROC_STALL_BYTES_PER_SLOW_S = 512 * 1024


def _resolve_worker_count(
    total_frames: int, requested_workers: int | None = None
) -> int:
    """Resolve manual, environment, or automatic worker count safely."""
    if requested_workers is not None:
        try:
            count = int(requested_workers)
        except (TypeError, ValueError):
            count = 1
        cap = _MULTIPROC_WORKER_CAP
    else:
        env = os.environ.get("KROK_SUBTITLE_RENDER_WORKERS")
        if env is not None and env.strip():
            try:
                count = int(env)
            except ValueError:
                count = 1
            cap = _MULTIPROC_WORKER_CAP
        else:
            count = os.cpu_count() or 1
            cap = _MULTIPROC_AUTO_WORKER_CAP
    count = max(1, min(count, cap))
    if total_frames < _MULTIPROC_MIN_FRAMES:
        return 1
    return count


def _resolve_chunk_size(job: RenderJob, render_h: int, total_frames: int, worker_count: int) -> int:
    """每个 worker 任务的帧数：按目标字节封顶（控内存/IPC），且每 worker 至少几块以均衡。

    chunk 目标随 worker 数缩放：``在飞内存上限 / (worker+2)``，夹在 4~64MiB——
    固定 64MiB 时 256MiB 窗口只能容纳 ~4 块，8 worker 的高核机器会有约一半
    worker 无任务可做（并行度 8→4）。缩放后 1080p 全幅 8 worker 每块 ~24MiB、
    窗口 10，喂满全部 worker。
    """
    frame_bytes = max(job.width * render_h * 4, 1)
    target_bytes = max(
        _CHUNK_MIN_TARGET_BYTES,
        min(
            _CHUNK_TARGET_BYTES,
            _MULTIPROC_MAX_PENDING_BYTES // max(worker_count + 2, 1),
        ),
    )
    by_bytes = max(1, target_bytes // frame_bytes)
    by_balance = max(1, total_frames // (worker_count * 4))
    return max(1, min(by_bytes, by_balance))


# A3 内存护栏：imap 会把全部任务一次性派发、已完成结果在主进程无界积压，
# 4K 全幅帧（31.6MiB/帧）下渲染快于编码时几分钟即可吃掉数 GB 内存。
# 改用 apply_async + 有界在飞窗口：同时未消费的 chunk 结果条数与字节数都封顶。


def _resolve_pending_memory_budget(available_memory_bytes: int | None) -> int:
    """在飞结果预算：256MiB 保守下限，有余量时用可用内存的 1/8。

    上限 1GiB，足以喂满 16 个 4K 全幅 worker（约 506MiB），
    又不会因高内存机器而允许主进程无节制积压。
    """

    if available_memory_bytes is None or available_memory_bytes <= 0:
        return _MULTIPROC_MAX_PENDING_BYTES
    return max(
        _MULTIPROC_MAX_PENDING_BYTES,
        min(
            _MULTIPROC_PENDING_HARD_CAP_BYTES,
            int(available_memory_bytes) // 8,
        ),
    )


def _resolve_pending_window(
    worker_count: int,
    chunk: int,
    frame_bytes: int,
    *,
    pending_budget_bytes: int = _MULTIPROC_MAX_PENDING_BYTES,
) -> int:
    """同时在飞（已派发未消费）的 chunk 数上限。

    调用方需先经 :func:`_resolve_effective_worker_count` 把 worker 数压回
    预算内（worker ≤ budget/chunk），此后窗口 = min(worker+2, 字节上限)
    恒 ≥ worker 数——既不饿死 worker，也绝不突破内存预算。
    """
    chunk_bytes = max(chunk * max(frame_bytes, 1), 1)
    by_bytes = max(1, max(int(pending_budget_bytes), 1) // chunk_bytes)
    return max(1, min(worker_count + 2, by_bytes))


def _resolve_effective_worker_count(
    worker_count: int,
    chunk: int,
    frame_bytes: int,
    *,
    available_memory_bytes: int | None = None,
    pending_budget_bytes: int = _MULTIPROC_MAX_PENDING_BYTES,
) -> int:
    """按主进程在飞结果和系统可用内存限制 worker 数。

    单帧巨大时，worker 数同时受动态在飞结果预算和估算的
    worker 峰值内存限制。高内存机器的在飞预算可从 256MiB
    扩到 1GiB，因此手动 16 worker 的 4K 不会因固定上限被错压成 8；
    低内存机器仍会降 worker 防止 OOM。
    """
    chunk_bytes = max(chunk * max(frame_bytes, 1), 1)
    by_bytes = max(1, max(int(pending_budget_bytes), 1) // chunk_bytes)
    resolved = max(1, min(worker_count, by_bytes))
    if available_memory_bytes is None or available_memory_bytes <= 0:
        return resolved

    # worker 常驻 QImage + empty_frame；渲染 chunk 时同时存在 bytearray、
    # 返回 bytes 及 multiprocessing 序列化副本。额外保留 QApplication /
    # 字体缓存的经验上限，避免 4K 全幅回退一次拉起 8 个大进程。
    per_worker_peak = (
        2 * max(frame_bytes, 1)
        + 3 * chunk_bytes
        + _MULTIPROC_WORKER_OVERHEAD_BYTES
    )
    # available 已排除当前已占用内存；再保留至少 1GiB 与在飞
    # 结果窗口。高内存机器不会触发降级，低内存/UMA 压力机器降并发。
    worker_budget = max(
        int(available_memory_bytes)
        - _MULTIPROC_SYSTEM_RESERVE_BYTES
        - max(int(pending_budget_bytes), 1),
        0,
    )
    by_system_memory = max(1, worker_budget // max(per_worker_peak, 1))
    return max(1, min(resolved, by_system_memory))


def _available_system_memory_bytes() -> int | None:
    """返回当前可用物理内存；查询失败时保留旧的并发策略。"""

    try:
        import psutil

        available = int(psutil.virtual_memory().available)
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        return None
    return available if available > 0 else None


def _resolve_stall_timeout_s(chunk_frames: int, frame_bytes: int) -> float:
    """单 chunk 结果的等待上限：超时视为 worker 已异常（如被系统 OOM 终止）。

    按数据量而非帧数估算——条带/多带让单帧更小、帧数更多，按帧数线性放大
    会等到数十分钟：基础 60s + 每 512KiB 给 1s（4K 全幅 33MiB ≈ 125s）。
    环境变量 KROK_SUBTITLE_RENDER_STALL_TIMEOUT_S 覆盖。
    """
    raw = os.environ.get("KROK_SUBTITLE_RENDER_STALL_TIMEOUT_S")
    if raw and raw.strip():
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    chunk_bytes = max(int(chunk_frames), 1) * max(int(frame_bytes), 1)
    return _MULTIPROC_MIN_STALL_TIMEOUT_S + chunk_bytes / _MULTIPROC_STALL_BYTES_PER_SLOW_S
