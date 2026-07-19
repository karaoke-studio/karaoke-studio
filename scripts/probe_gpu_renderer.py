"""Run the G0 Direct2D renderer stability and staging-readback probe."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import statistics
import sys
import time
import uuid

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from krok_helper.subtitle_render.native_backend import (  # noqa: E402
    NativeRendererProcess,
    SharedFrameRingReader,
)


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _working_set_bytes(pid: int) -> int:
    if os.name != "nt":
        return 0
    process_query_information = 0x0400
    process_vm_read = 0x0010
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    handle = kernel32.OpenProcess(process_query_information | process_vm_read, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")
    try:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
        return int(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


def _pixel(payload: bytes, stride: int, x: int, y: int) -> tuple[int, int, int, int]:
    offset = y * stride + x * 4
    return tuple(payload[offset : offset + 4])  # type: ignore[return-value]


def run_probe(*, frames: int, width: int, height: int, force_warp: bool, max_growth_mb: float) -> dict:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    shm_key = f"krok_gpu_probe_{os.getpid()}_{uuid.uuid4().hex}"
    render_times: list[float] = []
    readback_times: list[float] = []
    checksums: set[str] = set()
    reader: SharedFrameRingReader | None = None
    with NativeRendererProcess(response_timeout_s=15.0, close_timeout_s=2.0) as renderer:
        info = renderer.backend_info(force_warp=force_warp)
        if not info.get("available"):
            raise RuntimeError(str(info.get("error") or "Direct2D backend unavailable"))
        pid = renderer.process_id
        if pid is None:
            raise RuntimeError("native renderer process is not running")
        baseline_working_set = 0
        start = time.perf_counter()
        try:
            for frame_index in range(frames):
                event = renderer.render_probe(
                    width=width,
                    height=height,
                    force_warp=force_warp,
                    draw_glyph=True,
                    generation=1,
                    frame_index=frame_index,
                    shm_key=shm_key,
                )
                if reader is None:
                    reader = SharedFrameRingReader.from_event(event)
                    reader.attach()
                slot = reader.read_frame(event)
                if slot.frame_index != frame_index:
                    raise AssertionError((slot.frame_index, frame_index))
                if _pixel(slot.payload, slot.stride, 0, 0) != (0, 0, 0, 0):
                    raise AssertionError("transparent probe background was not preserved")
                checksums.add(str(event["checksum"]))
                render_times.append(float(event["render_ms"]))
                readback_times.append(float(event["readback_ms"]))
                if frame_index == min(24, frames - 1):
                    baseline_working_set = _working_set_bytes(pid)
        finally:
            if reader is not None:
                reader.close()
        elapsed = time.perf_counter() - start
        final_working_set = _working_set_bytes(pid)
    if len(checksums) != 1:
        raise AssertionError(f"GPU probe output was not deterministic: {len(checksums)} checksums")
    growth_bytes = max(0, final_working_set - baseline_working_set)
    if growth_bytes > int(max_growth_mb * 1024 * 1024):
        raise AssertionError(
            f"sidecar working set grew by {growth_bytes / 1024 / 1024:.2f} MiB "
            f"after warmup (limit {max_growth_mb:.2f} MiB)"
        )
    return {
        "backend": info["backend"],
        "adapter": info["adapter"],
        "feature_level": info["feature_level"],
        "warp": bool(info["warp"]),
        "frames": frames,
        "width": width,
        "height": height,
        "elapsed_ms": round(elapsed * 1000.0, 3),
        "fps": round(frames / elapsed, 3) if elapsed > 0 else 0.0,
        "render_mean_ms": round(statistics.fmean(render_times), 4),
        "render_p95_ms": round(sorted(render_times)[min(len(render_times) - 1, int(len(render_times) * 0.95))], 4),
        "readback_mean_ms": round(statistics.fmean(readback_times), 4),
        "readback_p95_ms": round(sorted(readback_times)[min(len(readback_times) - 1, int(len(readback_times) * 0.95))], 4),
        "working_set_growth_mb": round(growth_bytes / 1024 / 1024, 3),
        "checksum": next(iter(checksums)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="G0 Direct2D GPU renderer stability probe")
    parser.add_argument("--frames", type=int, default=1000)
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--height", type=int, default=144)
    parser.add_argument("--warp", action="store_true", help="force Microsoft WARP")
    parser.add_argument("--both", action="store_true", help="run hardware then WARP")
    parser.add_argument("--max-growth-mb", type=float, default=64.0)
    args = parser.parse_args()
    modes = [False, True] if args.both else [bool(args.warp)]
    for force_warp in modes:
        result = run_probe(
            frames=max(1, args.frames),
            width=max(1, args.width),
            height=max(1, args.height),
            force_warp=force_warp,
            max_growth_mb=max(0.0, args.max_growth_mb),
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
