"""可行性探针：多线程 QPainter 字幕栅格化在 Python GIL 下能否真正并行？

背景（§10.6 / §9.7 B2）：utopia body 是逐帧矢量栅格化（device 分辨率下 ~14ms median），
受**单 worker 线程**所限，而 16 核基本闲置。在投入「多 worker 并行预览」（数周）之前，先实测
**K 个 QThread 各自渲染自己的 QImage 能否把吞吐线性放大**——即 Qt 在栅格化（strokePath /
fillPath / blur）期间是否释放 GIL：
  - 若能 → 跨帧 look-ahead 线程池可行（§10.7 S1）；
  - 若不能 → 只能退回 multiprocess 预览（重）。

glow 走 QGraphicsBlurEffect（需 Qt event dispatcher）→ worker 必须是 QThread 而非
threading.Thread（与异步预览同源的约束）。

只测「纯栅格化吞吐」：每个线程建自己的 QImage + QPainter（不共享 painter）；warmup 串行预热填好
before-layer / glow 缓存，使并行阶段对共享缓存以读命中为主（dict 读在 CPython 下 GIL 原子）。

用法：
  python scripts/bench_parallel_paint.py [project.yurika] [--start MS] [--frames N]
        [--threads 1,2,4,8] [--dpr 1.25] [--no-utopia] [--no-glow]
不传 project 默认 A stain；不传 --start 用 92000。
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time
from dataclasses import replace as _replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")

import psutil  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QThread  # noqa: E402
from PyQt6.QtGui import QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.engine.painter import paint_frame_to_painter  # noqa: E402
from krok_helper.subtitle_render.frontend.preview_async import preview_render_target_size  # noqa: E402
from krok_helper.subtitle_render.models import style_from_dict  # noqa: E402
from krok_helper.subtitle_render.project_store import load_render_project  # noqa: E402
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc  # noqa: E402

DEFAULT_PROJECT = Path(r"D:\カラオケ\songs\A stain\A stain.yurika")


class _RenderThread(QThread):
    """QThread（自带 event dispatcher）→ 渲染分到的一批时间戳，各帧独立 QImage。"""

    def __init__(self, timestamps, render_fn) -> None:
        super().__init__()
        self._timestamps = timestamps
        self._render_fn = render_fn

    def run(self) -> None:  # noqa: D401
        for t in self._timestamps:
            self._render_fn(t)


def _chunk(seq, k):
    """把 seq 尽量均匀切成 k 段（连续切片）。"""
    n = len(seq)
    out = []
    base, rem = divmod(n, k)
    i = 0
    for idx in range(k):
        size = base + (1 if idx < rem else 0)
        out.append(seq[i : i + size])
        i += size
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", nargs="?", default=str(DEFAULT_PROJECT))
    parser.add_argument("--start", type=int, default=92000)
    parser.add_argument("--frames", type=int, default=240, help="测量帧数（按 fps 步进的连续 t）")
    parser.add_argument("--threads", default="1,2,4,8", help="逗号分隔的线程数档位")
    parser.add_argument("--dpr", type=float, default=1.25, help="设备像素比（模拟真实预览窗）")
    parser.add_argument("--warmup", type=int, default=40, help="串行预热帧数（填缓存，不计入）")
    parser.add_argument("--no-utopia", action="store_true")
    parser.add_argument("--no-glow", action="store_true")
    parser.add_argument("--cprofile", action="store_true",
                        help="cProfile 串行渲染，看 Python vs C++ 栅格化耗时占比")
    args = parser.parse_args()

    thread_levels = [int(x) for x in args.threads.split(",") if x.strip()]

    data = load_render_project(Path(args.project))
    style = style_from_dict(data["style"])
    overrides = {}
    if not args.no_utopia:
        overrides.update(entry_anim="utopia", exit_anim="utopia")
    if not args.no_glow:
        overrides.update(decoration_kind="glow")
    if overrides:
        style = _replace(style, **overrides)
    track = load_nicokara_lrc(Path(data["subtitle_path"]))
    screen = data.get("screen", {})
    w = int(screen.get("width", 1920))
    h = int(screen.get("height", 1080))
    fps = int(screen.get("fps", 60))
    frame_ms = 1000.0 / fps

    phys_w, phys_h, dpr = preview_render_target_size(w, h, args.dpr)
    timestamps = [int(args.start + i * frame_ms) for i in range(args.frames)]

    print(f"project   : {args.project}")
    print(f"style     : deco={style.decoration_kind} entry/exit={style.entry_anim}/{style.exit_anim} "
          f"dual={style.dual_line_layout} ruby={'yes' if track.rubies else 'no'}")
    print(f"render    : logical {w}x{h} → physical {phys_w}x{phys_h} (dpr={dpr:.2f})")
    print(f"frames    : {args.frames} (start={args.start}ms, step={frame_ms:.1f}ms), warmup={args.warmup}")
    print(f"cpu cores : {psutil.cpu_count()} logical")
    print(f"threads   : {thread_levels}")
    print("-" * 64)

    app = QApplication(sys.argv)  # noqa: F841 (字体引擎需要)

    def render_one(t_ms: int) -> None:
        image = QImage(phys_w, phys_h, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(0)
        painter = QPainter(image)
        try:
            paint_frame_to_painter(painter, w, h, track, int(t_ms), style)
        finally:
            painter.end()

    # ── warmup（串行，填 before-layer / glow 缓存）────────────────────
    for t in timestamps[: args.warmup]:
        render_one(t)

    # ── cProfile（可选）：判 Python 布局 vs C++ 栅格化谁占主导 ─────────
    if args.cprofile:
        import cProfile  # noqa: PLC0415
        import pstats  # noqa: PLC0415

        prof = cProfile.Profile()
        prof.enable()
        for t in timestamps:
            render_one(t)
        prof.disable()
        st = pstats.Stats(prof)
        st.sort_stats("tottime")
        print("=== cProfile top-25 by tottime（含内置 C 调用，如 strokePath/fillPath）===")
        st.print_stats(25)
        return

    # ── 串行基线（单线程渲染全部 frames）─────────────────────────────
    proc = psutil.Process()
    proc.cpu_percent(None)
    t0 = time.perf_counter()
    for t in timestamps:
        render_one(t)
    serial_wall = time.perf_counter() - t0
    serial_fps = len(timestamps) / serial_wall
    serial_ms = serial_wall / len(timestamps) * 1000.0
    print(f"[serial]  {len(timestamps)} frames in {serial_wall:.2f}s  "
          f"→ {serial_fps:.1f} fps  ({serial_ms:.2f} ms/frame)")
    print("-" * 64)

    # ── 各线程档位 ───────────────────────────────────────────────────
    print(f"{'threads':>7} {'wall(s)':>8} {'fps':>8} {'ms/frame':>9} {'speedup':>8} {'cpu%':>7}")
    for k in thread_levels:
        chunks = [c for c in _chunk(timestamps, k) if c]
        # 并行期间采 CPU
        cpu_samples: list[float] = []
        stop = threading.Event()

        def _sample() -> None:
            proc.cpu_percent(None)
            while not stop.wait(0.1):
                cpu_samples.append(proc.cpu_percent(None))

        sampler = threading.Thread(target=_sample, daemon=True)
        threads = [_RenderThread(c, render_one) for c in chunks]
        sampler.start()
        t0 = time.perf_counter()
        for th in threads:
            th.start()
        for th in threads:
            th.wait()
        wall = time.perf_counter() - t0
        stop.set()
        sampler.join(timeout=1.0)
        kfps = len(timestamps) / wall
        kms = wall / len(timestamps) * 1000.0
        speedup = serial_wall / wall
        cpu = statistics.mean(cpu_samples) if cpu_samples else -1.0
        print(f"{k:>7} {wall:>8.2f} {kfps:>8.1f} {kms:>9.2f} {speedup:>7.2f}x {cpu:>6.0f}%")

    print("-" * 64)
    print("判读：speedup 随线程数接近线性 → Qt 栅格化释放 GIL，跨帧线程池可行（S1）。")
    print("      speedup 卡在 ~1x → GIL 未释放（或 Python 布局占主导），需退 multiprocess 预览。")


if __name__ == "__main__":
    main()
