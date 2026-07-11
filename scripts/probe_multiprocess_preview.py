"""可行性探针（S2'，§10.7）：多进程预览渲染能否绕开 GIL 把 utopia+glow 填充吞吐抬过实时？

背景：多线程已被 `bench_parallel_paint.py` 证明在 GIL 下无效（8 线程 ~1.07×）。绕开 GIL 唯一
办法是 **multiprocess**（每进程独立解释器/GIL，导出已用，见 `engine/renderer.py`
`_write_frames_multiprocess`）。但多进程要把渲好的帧**跨进程传回**主进程（每帧 ~13MB），
IPC 可能吃掉并行收益。本探针实测三件事：

  1. **纯并行渲染吞吐**（worker 内渲完即弃，不回传）→ 绕开 GIL 的理论上限；
  2. **含 IPC 的净吞吐**（worker 把 RGBA bytes 经 imap 回传主进程）→ 真实可用吞吐；
  3. 两者与**实时基线**（fps，默认 60）比较 → 判定 S2' 值不值得做。

判读：
  - 纯并行吞吐随进程数接近线性，且**含 IPC 净吞吐 > 实时** → S2' 可行（边播边追）。
  - 纯并行能线性、但**含 IPC 净吞吐 ≤ 实时** → IPC 是瓶颈，需共享内存（QSharedMemory/mmap）
    或只回传字幕条带（strip）再评估。
  - 纯并行都上不去 → 渲染本身太重，S2' 也救不了（需 GPU 后端）。

用法：
  python scripts/probe_multiprocess_preview.py [project.yurika] [--start MS] [--frames N]
        [--procs 1,2,4,8] [--dpr 1.25] [--no-utopia] [--no-glow]
不传 project 默认 A stain；不传 --start 用 92000。spawn 进程已用 warmup 批预热（不计入计时）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace as _replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_PROJECT = Path(r"D:\カラオケ\songs\A stain\A stain.yurika")


def _synthetic_track(start_ms: int):
    """无外部工程时的回退：合成多行 + ruby 的卡拉OK 轨道（贴近 utopia+glow+ruby 场景）。"""
    from krok_helper.subtitle_render.models import (
        RubyAnnotation,
        TimingChar,
        TimingLine,
        TimingTrack,
    )

    lines, rubies = [], []
    # (短语, 首段汉字, 注音) —— 给含汉字的行挂 ruby，复刻熟字训/注音的逐帧成本。
    phrases = [
        ("目移りしちゃう", "目", "め"),
        ("あっちこっち", None, None),
        ("欲張がデフォ", "欲張", "よくば"),
        ("乙女心だよ", "乙女心", "おとめごころ"),
    ]
    t = start_ms
    for phrase, kanji, reading in phrases:
        chars, line_start = [], t
        for ch in phrase:
            chars.append(TimingChar(text=ch, start_ms=t))
            t += 260
        lines.append(TimingLine(chars=chars, end_ms=t + 400))
        if kanji:
            rubies.append(
                RubyAnnotation(kanji=kanji, reading=reading, pos_start_ms=line_start, pos_end_ms=t)
            )
        t += 600
    return TimingTrack(lines=lines, rubies=rubies)


# ── worker（spawn：函数必须是 module 级、参数可 pickle；TimingTrack/Style 与导出同样可 pickle）──

_CTX: dict = {}


def _worker_init(track, style, w, h, phys_w, phys_h, dpr) -> None:
    import atexit
    from multiprocessing import shared_memory

    from PyQt6.QtWidgets import QApplication

    _CTX["app"] = QApplication.instance() or QApplication([])
    _CTX.update(track=track, style=style, w=w, h=h, phys_w=phys_w, phys_h=phys_h, dpr=dpr)
    # 每 worker 一块单帧共享内存 slot（复刻 ring buffer：worker 写、main mmap 读）。
    frame_bytes = phys_w * phys_h * 4
    shm = shared_memory.SharedMemory(create=True, size=frame_bytes)
    _CTX["shm"] = shm
    _CTX["frame_bytes"] = frame_bytes
    atexit.register(lambda: (shm.close(), shm.unlink()))


def _render_one(t_ms: int):
    from PyQt6.QtGui import QImage, QPainter

    from krok_helper.subtitle_render.engine.painter import paint_frame_to_painter

    img = QImage(_CTX["phys_w"], _CTX["phys_h"], QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(_CTX["dpr"])
    img.fill(0)
    painter = QPainter(img)
    try:
        paint_frame_to_painter(painter, _CTX["w"], _CTX["h"], _CTX["track"], int(t_ms), _CTX["style"])
    finally:
        painter.end()
    return img


def _render_batch_discard(timestamps) -> int:
    """渲完即弃（纯并行渲染吞吐，无 IPC 回传）。"""
    for t in timestamps:
        _render_one(t)
    return len(timestamps)


def _render_batch_bytes(timestamps) -> list:
    """渲完回传原始 RGBA bytes（含 IPC：每帧 ~phys_w*phys_h*4 字节经 imap 回主进程）。"""
    out = []
    for t in timestamps:
        img = _render_one(t)
        bits = img.constBits()
        bits.setsize(img.sizeInBytes())
        out.append(bytes(bits))
    return out


def _render_batch_shmem(timestamps) -> int:
    """渲完把每帧 memcpy 进本 worker 的单帧 SharedMemory（复刻共享内存 ring 设计的真实地板：
    worker 写 slot、主进程 mmap 读零拷贝）。只回传计数 → 不经 pickle 传 12MB。"""
    shm = _CTX["shm"]
    n = _CTX["frame_bytes"]
    for t in timestamps:
        img = _render_one(t)
        bits = img.constBits()
        bits.setsize(img.sizeInBytes())
        shm.buf[:n] = bytes(bits)  # 写入共享内存 slot（real design：main 端 mmap 读，无需回拷）
    return len(timestamps)


def _chunk(seq, k):
    n = len(seq)
    out, base, rem, i = [], *divmod(n, k), 0
    for idx in range(k):
        size = base + (1 if idx < rem else 0)
        out.append(seq[i : i + size])
        i += size
    return [c for c in out if c]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", nargs="?", default=str(DEFAULT_PROJECT))
    parser.add_argument("--start", type=int, default=92000)
    parser.add_argument("--frames", type=int, default=480)
    parser.add_argument("--procs", default="1,2,4,8")
    parser.add_argument("--dpr", type=float, default=1.25)
    parser.add_argument("--no-utopia", action="store_true")
    parser.add_argument("--no-glow", action="store_true")
    args = parser.parse_args()

    import multiprocessing as mp

    from krok_helper.subtitle_render.engine.painter import paint_frame_to_painter
    from krok_helper.subtitle_render.frontend.preview_async import preview_render_target_size
    from krok_helper.subtitle_render.models import style_from_dict
    from krok_helper.subtitle_render.project_store import load_render_project
    from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtWidgets import QApplication

    import psutil

    proc_levels = [int(x) for x in args.procs.split(",") if x.strip()]

    from krok_helper.subtitle_render.models import Style

    if Path(args.project).exists():
        data = load_render_project(Path(args.project))
        style = style_from_dict(data["style"])
        track = load_nicokara_lrc(Path(data["subtitle_path"]))
        screen = data.get("screen", {})
        w = int(screen.get("width", 1920))
        h = int(screen.get("height", 1080))
        fps = int(screen.get("fps", 60))
    else:
        print(f"[!] 工程不存在：{args.project} → 使用合成 utopia+ruby 轨道（贴近真实场景）")
        track = _synthetic_track(args.start)
        style = Style()
        w, h, fps = 1920, 1080, 60
    overrides = {}
    if not args.no_utopia:
        overrides.update(entry_anim="utopia", exit_anim="utopia")
    if not args.no_glow:
        overrides.update(decoration_kind="glow")
    if overrides:
        style = _replace(style, **overrides)
    frame_ms = 1000.0 / fps
    phys_w, phys_h, dpr = preview_render_target_size(w, h, args.dpr)
    timestamps = [int(args.start + i * frame_ms) for i in range(args.frames)]
    frame_mb = phys_w * phys_h * 4 / (1024 * 1024)

    print(f"project   : {args.project}")
    print(f"style     : deco={style.decoration_kind} entry/exit={style.entry_anim}/{style.exit_anim} "
          f"ruby={'yes' if track.rubies else 'no'}")
    print(f"render    : logical {w}x{h} → physical {phys_w}x{phys_h} (dpr={dpr:.2f}, {frame_mb:.1f} MB/frame)")
    print(f"frames    : {args.frames} (start={args.start}ms, step={frame_ms:.1f}ms)")
    print(f"cpu cores : {psutil.cpu_count()} logical | realtime target = {fps} fps")
    print("-" * 72)

    # ── 主进程内串行基线（= 当前单 worker 预览的产能口径）──────────────
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

    def render_local(t_ms: int) -> None:
        img = QImage(phys_w, phys_h, QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(0)
        p = QPainter(img)
        try:
            paint_frame_to_painter(p, w, h, track, int(t_ms), style)
        finally:
            p.end()

    for t in timestamps[:40]:  # warmup 主进程缓存
        render_local(t)
    t0 = time.perf_counter()
    for t in timestamps:
        render_local(t)
    serial_wall = time.perf_counter() - t0
    serial_fps = len(timestamps) / serial_wall
    print(f"[serial in-proc] {serial_fps:6.1f} fps  ({serial_wall/len(timestamps)*1000:5.2f} ms/frame)  "
          f"{'≥' if serial_fps >= fps else '<'} realtime")
    print("-" * 72)

    initargs = (track, style, w, h, phys_w, phys_h, dpr)
    ctx = mp.get_context("spawn")

    print(f"{'procs':>5} | {'discard':>9} {'spd':>6} | {'pickle-IPC':>11} {'spd':>6} | "
          f"{'shmem-IPC':>10} {'spd':>6} | verdict")
    for k in proc_levels:
        chunks = _chunk(timestamps, k)
        pool = ctx.Pool(k, initializer=_worker_init, initargs=initargs)
        try:
            # warmup：让各 worker spawn + 建 QApplication + 填缓存（不计时）。
            list(pool.imap(_render_batch_discard, _chunk(timestamps[:k * 8], k)))

            # (1) 纯并行渲染（discard，无 IPC）→ 绕开 GIL 的理论上限
            t0 = time.perf_counter()
            list(pool.imap(_render_batch_discard, chunks))
            fps_d = len(timestamps) / (time.perf_counter() - t0)

            # (2) 朴素 IPC：回传 bytes（pickle + 管道传 12MB/帧）
            t0 = time.perf_counter()
            for _blob in pool.imap(_render_batch_bytes, chunks):
                pass
            fps_p = len(timestamps) / (time.perf_counter() - t0)

            # (3) 共享内存 IPC：worker 写 shm slot，只回传计数（复刻 ring buffer 真实地板）
            t0 = time.perf_counter()
            for _n in pool.imap(_render_batch_shmem, chunks):
                pass
            fps_s = len(timestamps) / (time.perf_counter() - t0)
        finally:
            pool.terminate()
            pool.join()

        best = max(fps_p, fps_s)
        verdict = "净吞吐≥实时✓" if best >= fps else ("纯并行≥实时,IPC拖累" if fps_d >= fps else "渲染不够")
        print(f"{k:>5} | {fps_d:>9.1f} {fps_d/serial_fps:>5.2f}x | {fps_p:>11.1f} {fps_p/serial_fps:>5.2f}x | "
              f"{fps_s:>10.1f} {fps_s/serial_fps:>5.2f}x | {verdict}")

    print("-" * 72)
    print("判读：shmem-IPC 是 ring-buffer 设计的真实可用净吞吐。> realtime → S2' 可行（边播边追）；")
    print("      pickle-IPC 崩、shmem-IPC 接近 discard → 证明瓶颈是序列化，共享内存可解。")


if __name__ == "__main__":
    main()
