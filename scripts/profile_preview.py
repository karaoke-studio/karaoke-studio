"""真实驱动字幕预览并采集实时帧率 / CPU / GPU，用于诊断掉帧来源。

复刻 main_window 的预览接线（PreviewGraphicsView + TransportBar + 视频原生播放），
加载 .yurika 工程后程序化 play()，跑 Qt 事件循环若干秒，期间：

- 给 paint_frame_to_painter 计时 → 每次字幕层光栅化的真实耗时 + 时间戳；
- 统计 timeChanged（时钟 tick）次数 vs framePainted（真实渲染）次数 → 掉帧比；
- 后台线程按固定间隔采样进程/系统 CPU（psutil）与 GPU util/decoder/显存（nvidia-smi）。

用法：
  python scripts/profile_preview.py [project.yurika] [--start MS] [--measure SEC]
不传 project 默认用 A stain.yurika。不传 --start 自动选歌词最密集区段。
"""
from __future__ import annotations

import argparse
import gc
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")

import psutil  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

from krok_helper.subtitle_render.frontend import preview_graphics as pg  # noqa: E402
from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView  # noqa: E402
from krok_helper.subtitle_render.frontend.preview_view import TransportBar  # noqa: E402
from krok_helper.subtitle_render.models import style_from_dict  # noqa: E402
from krok_helper.subtitle_render.project_store import load_render_project  # noqa: E402
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc  # noqa: E402

DEFAULT_PROJECT = Path(r"D:\カラオケ\songs\A stain\A stain.yurika")


# --------------------------------------------------------------------------- paint timing
PAINTS: list[tuple[float, float, int]] = []  # (wall_perf, dur_ms, t_ms)
_orig_paint = pg.paint_frame_to_painter


def _timed_paint(painter, w, h, track, t_ms, style):
    t0 = time.perf_counter()
    _orig_paint(painter, w, h, track, t_ms, style)
    PAINTS.append((time.perf_counter(), (time.perf_counter() - t0) * 1000.0, t_ms))


pg.paint_frame_to_painter = _timed_paint


# --------------------------------------------------------------------------- video seeks + GC
SEEKS: list[tuple[float, int, int]] = []  # (wall_perf, target_ms, before_pos)
GC_EVENTS: list[tuple[float, float]] = []  # (wall_perf, dur_ms)
_gc_t0 = {"t": 0.0}


def _gc_cb(phase, info):
    if phase == "start":
        _gc_t0["t"] = time.perf_counter()
    else:
        GC_EVENTS.append((time.perf_counter(), (time.perf_counter() - _gc_t0["t"]) * 1000.0))


gc.callbacks.append(_gc_cb)


# --------------------------------------------------------------------------- gpu/cpu sampler
SAMPLES: list[tuple] = []  # (wall_perf, cpu_proc, cpu_sys, gpu_util, gpu_dec, gpu_mem)
_stop = threading.Event()


def _query_gpu() -> tuple[float, float, float]:
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,utilization.decoder,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
        return float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        return (-1.0, -1.0, -1.0)


def _sampler(proc: psutil.Process, interval: float) -> None:
    proc.cpu_percent(None)
    psutil.cpu_percent(None)
    _stop.wait(interval)
    while not _stop.is_set():
        ts = time.perf_counter()
        cpu_proc = proc.cpu_percent(None)
        cpu_sys = psutil.cpu_percent(None)
        gu, gd, gm = _query_gpu()
        SAMPLES.append((ts, cpu_proc, cpu_sys, gu, gd, gm))
        _stop.wait(interval)


# --------------------------------------------------------------------------- helpers
def _line_span(line, lead_in_ms: int, tail_ms: int) -> tuple[int, int]:
    starts = [c.start_ms for c in line.chars if c.start_ms is not None]
    start = min(starts) if starts else line.end_ms
    return start - lead_in_ms, line.end_ms + tail_ms


def _busiest_start_ms(track, style, total_ms: int, window_ms: int) -> int:
    spans = [_line_span(l, style.line_lead_in_ms, style.line_tail_ms) for l in track.lines]
    char_counts = [sum(1 for c in l.chars if c.text.strip()) for l in track.lines]
    best_t, best_score = 0, -1
    for t in range(0, max(total_ms - window_ms, 1), 1000):
        score = 0
        for (s, e), n in zip(spans, char_counts):
            if s < t + window_ms and e > t:
                score += n
        if score > best_score:
            best_score, best_t = score, t
    return best_t


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", nargs="?", default=str(DEFAULT_PROJECT))
    parser.add_argument("--start", type=int, default=-1, help="起始 ms，-1=自动选最密集区段")
    parser.add_argument("--measure", type=float, default=15.0, help="测量秒数")
    parser.add_argument("--warmup", type=float, default=2.5, help="预热秒数（不计入统计）")
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--no-subtitle", action="store_true", help="不画字幕，测纯视频呈现节奏（A/B）")
    parser.add_argument("--utopia", action="store_true", help="覆盖样式：entry/exit=utopia")
    parser.add_argument("--glow", action="store_true", help="覆盖样式：decoration_kind=glow")
    args = parser.parse_args()

    data = load_render_project(Path(args.project))
    style = style_from_dict(data["style"])
    from dataclasses import replace as _replace  # noqa: PLC0415

    overrides = {}
    if args.utopia:
        overrides.update(entry_anim="utopia", exit_anim="utopia")
    if args.glow:
        overrides.update(decoration_kind="glow")
    if overrides:
        style = _replace(style, **overrides)
    track = load_nicokara_lrc(Path(data["subtitle_path"]))
    screen = data.get("screen", {})
    w = int(screen.get("width", 1920))
    h = int(screen.get("height", 1080))
    fps = int(screen.get("fps", 60))
    video = Path(data["video_path"])
    total_ms = max((l.end_ms for l in track.lines), default=240000)

    window_ms = int(args.measure * 1000)
    start_ms = args.start if args.start >= 0 else _busiest_start_ms(track, style, total_ms, window_ms)

    print(f"project        : {args.project}")
    print(f"video          : {video.name}  ({w}x{h}@{fps})")
    print(f"lines/chars    : {len(track.lines)} lines, {sum(len(l.chars) for l in track.lines)} chars")
    print(f"style          : font={style.font_family} {style.font_size_px}px stroke={style.stroke_width_px} "
          f"deco={style.decoration_kind} dual_line={style.dual_line_layout} ruby={'yes' if track.rubies else 'no'} "
          f"entry/exit={style.entry_anim}/{style.exit_anim} lit={style.lit_enabled}")
    print(f"measure window : start={start_ms}ms ({start_ms/1000:.1f}s), warmup={args.warmup}s, measure={args.measure}s")
    print(f"cpu cores      : {psutil.cpu_count()} logical")
    print("-" * 72)

    app = QApplication(sys.argv)

    view = PreviewGraphicsView()
    view.set_output_size(w, h)
    view.set_style(style)
    view.set_track(None if args.no_subtitle else track)
    view.set_video_source(video)

    # 包裹视频播放器 setPosition：统计播放中触发的 seek（_sync_video_position 漂移校正）。
    vp = view._video_player  # noqa: SLF001
    if vp is not None:
        _orig_setpos = vp.setPosition

        def _wrapped_setpos(ms, _vp=vp, _o=_orig_setpos):
            SEEKS.append((time.perf_counter(), int(ms), _vp.position()))
            return _o(ms)

        vp.setPosition = _wrapped_setpos

    # 直接计量「呈现到屏幕的视频帧」节奏（用户实际看到的视频流畅度）。
    VFRAMES: list[float] = []
    try:
        sink = view._video_item.videoSink()  # noqa: SLF001
        sink.videoFrameChanged.connect(lambda _f: VFRAMES.append(time.perf_counter()))
    except Exception as exc:  # pragma: no cover
        print(f"(video sink hook 失败: {exc})")
        VFRAMES = []

    transport = TransportBar()
    transport.set_preview_fps(fps)
    transport.set_duration(total_ms)
    transport.set_audio_source(video)

    tick_count = {"n": 0}
    transport.timeChanged.connect(view.set_time)
    transport.timeChanged.connect(lambda _v: tick_count.__setitem__("n", tick_count["n"] + 1))
    transport.playbackStateChanged.connect(view.set_playing)
    view.framePainted.connect(transport.note_preview_frame_painted)

    container = QWidget()
    container.setWindowTitle("preview profile")
    lay = QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(view, 1)
    lay.addWidget(transport)
    container.resize(960, 600)
    container.show()

    proc = psutil.Process()
    sampler = threading.Thread(target=_sampler, args=(proc, args.sample_interval), daemon=True)

    measure_state = {"t0": 0.0, "tick0": 0, "paint0": 0, "seek0": 0, "gc0": 0, "vf0": 0}

    def begin_measure():
        measure_state["t0"] = time.perf_counter()
        measure_state["tick0"] = tick_count["n"]
        measure_state["paint0"] = len(PAINTS)
        measure_state["seek0"] = len(SEEKS)
        measure_state["gc0"] = len(GC_EVENTS)
        measure_state["vf0"] = len(VFRAMES)
        sampler.start()

    def finish():
        _stop.set()
        t_elapsed = time.perf_counter() - measure_state["t0"]
        ticks = tick_count["n"] - measure_state["tick0"]
        paints = PAINTS[measure_state["paint0"]:]
        seeks = SEEKS[measure_state["seek0"]:]
        gcs = GC_EVENTS[measure_state["gc0"]:]
        vframes = VFRAMES[measure_state["vf0"]:]
        _report(t_elapsed, ticks, paints, fps, seeks, gcs, args.no_subtitle, vframes)
        app.quit()

    # 播放 → 预热结束开始测量 → 测量结束出报告
    transport.set_time(start_ms)
    QTimer.singleShot(200, transport.play)
    QTimer.singleShot(int(args.warmup * 1000), begin_measure)
    QTimer.singleShot(int((args.warmup + args.measure) * 1000), finish)

    app.exec()


def _report(elapsed_s, ticks, paints, target_fps, seeks=None, gcs=None, no_subtitle=False, vframes=None) -> None:
    seeks = seeks or []
    gcs = gcs or []
    vframes = vframes or []
    print(f"\n=== 测量结果（{elapsed_s:.1f}s 实测{'，纯视频/无字幕' if no_subtitle else ''}）===")

    # 真正呈现到屏幕的视频帧（用户看到的视频流畅度，独立于字幕层）
    if vframes and len(vframes) > 1:
        vfps = len(vframes) / elapsed_s if elapsed_s > 0 else 0
        vgaps = [(b - a) * 1000.0 for a, b in zip(vframes, vframes[1:])]
        vbudget = 1000.0 / target_fps
        vstall = sum(1 for g in vgaps if g > 1.5 * vbudget)
        print(f"【视频帧呈现】{len(vframes)} 帧 ({vfps:.1f}fps，目标 {target_fps})；"
              f"间隔 median={statistics.median(vgaps):.1f}ms p95={_pct(vgaps,0.95):.1f}ms max={max(vgaps):.1f}ms；"
              f"卡顿(>{1.5*vbudget:.0f}ms) {vstall}/{len(vgaps)} ({100*vstall/len(vgaps):.1f}%)")
    elif not no_subtitle:
        print("【视频帧呈现】无 videoFrameChanged 采样")
    n_paint = len(paints)
    avg_fps = n_paint / elapsed_s if elapsed_s > 0 else 0
    tick_fps = ticks / elapsed_s if elapsed_s > 0 else 0
    print(f"时钟 tick      : {ticks}  ({tick_fps:.1f}/s)  ← 预览时钟推进次数")
    print(f"实际渲染帧     : {n_paint}  ({avg_fps:.1f}/s)  ← 字幕层真实 paint 次数")
    print(f"目标帧率       : {target_fps}/s")
    if ticks:
        print(f"掉帧比         : {100*(1 - n_paint/max(ticks,1)):.1f}%  (paints/ticks={n_paint}/{ticks})")

    durs = [d for _, d, _ in paints]
    if durs:
        print(f"\n单帧字幕光栅化耗时 (paint_frame_to_painter)：")
        print(f"  mean={statistics.mean(durs):.2f}ms  median={statistics.median(durs):.2f}ms  "
              f"p95={_pct(durs,0.95):.2f}ms  max={max(durs):.2f}ms  min={min(durs):.2f}ms")
        budget = 1000.0 / target_fps
        over = sum(1 for d in durs if d > budget)
        print(f"  单帧预算={budget:.2f}ms（{target_fps}fps）；超预算帧数 {over}/{len(durs)} ({100*over/len(durs):.1f}%)")
        print(f"  若纯光栅化受限的理论上限 ≈ {1000.0/max(statistics.mean(durs),0.001):.0f}fps")

    # 帧间隔（真实渲染节奏）+ 最大卡顿时刻 vs seek 对齐
    ts = [t for t, _, _ in paints]
    if len(ts) > 1:
        gaps = [(b - a) * 1000.0 for a, b in zip(ts, ts[1:])]
        budget = 1000.0 / target_fps
        stalls = sum(1 for g in gaps if g > 1.5 * budget)
        print(f"\n渲染帧间隔：mean={statistics.mean(gaps):.2f}ms  median={statistics.median(gaps):.2f}ms  "
              f"p95={_pct(gaps,0.95):.2f}ms  max={max(gaps):.2f}ms")
        print(f"  卡顿帧（间隔 > 1.5×预算 {1.5*budget:.1f}ms）：{stalls}/{len(gaps)} ({100*stalls/len(gaps):.1f}%)")
        # top-5 最大间隔，看附近是否有 video seek
        seek_ts = [t for t, _, _ in seeks]
        idx_sorted = sorted(range(len(gaps)), key=lambda i: gaps[i], reverse=True)[:5]
        print("  top-5 卡顿（间隔ms @相对秒；最近 seek 距离ms）：")
        for i in idx_sorted:
            gap_wall = ts[i + 1]
            near = min((abs(gap_wall - st) * 1000.0 for st in seek_ts), default=float("inf"))
            near_s = f"{near:.0f}" if near != float("inf") else "—"
            print(f"    {gaps[i]:.1f}ms @ {ts[i]-ts[0]:.1f}s   最近seek={near_s}ms")

    # 时钟平滑度：走字动画的视觉流畅取决于 t_ms 是否平滑单调推进（而非 paint 次数）。
    tms = [tm for _, _, tm in paints]
    if len(tms) > 1:
        dt_tms = [b - a for a, b in zip(tms, tms[1:])]
        gaps_wall = [(b - a) * 1000.0 for a, b in zip(ts, ts[1:])]
        backward = sum(1 for d in dt_tms if d < 0)
        same = sum(1 for d in dt_tms if d == 0)
        # 理想：dt_tms ≈ dt_wall（每毫秒墙钟推进 1ms 时间）。偏差大 = 走字忽快忽慢。
        err = [abs(dtm - gw) for dtm, gw in zip(dt_tms, gaps_wall)]
        big_jump = sum(1 for d in dt_tms if d > 40)
        print(f"\n时钟(t_ms)推进平滑度（走字流畅度的真正决定项）：")
        print(f"  相邻 paint 的 Δt_ms：mean={statistics.mean(dt_tms):.1f}ms median={statistics.median(dt_tms):.1f}ms "
              f"max={max(dt_tms)}ms min={min(dt_tms)}ms")
        print(f"  后退跳变(Δ<0)：{backward} 次；停滞(Δ=0，同一时刻重复 paint)：{same} 次 "
              f"({100*same/len(dt_tms):.0f}%)；大前跳(Δ>40ms)：{big_jump} 次")
        print(f"  |Δt_ms − Δ墙钟| 偏差：mean={statistics.mean(err):.1f}ms p95={_pct(err,0.95):.1f}ms max={max(err):.1f}ms")

    # video seek（播放中漂移校正 → 解码 stall）
    print(f"\n视频 seek（播放中 _sync_video_position 校正）：{len(seeks)} 次"
          f"（{len(seeks)/max(elapsed_s,0.001):.1f}/s）")
    if seeks:
        deltas = [abs(tgt - before) for _, tgt, before in seeks]
        print(f"  漂移量 mean={statistics.mean(deltas):.0f}ms max={max(deltas):.0f}ms（>80ms 即触发 setPosition）")

    # GC 停顿
    if gcs:
        durs_gc = [d for _, d in gcs]
        print(f"\nPython GC：{len(gcs)} 次，单次 mean={statistics.mean(durs_gc):.2f}ms max={max(durs_gc):.2f}ms")

    # 每秒 FPS 桶
    if ts:
        t0 = ts[0]
        buckets: dict[int, int] = {}
        for t in ts:
            buckets[int(t - t0)] = buckets.get(int(t - t0), 0) + 1
        line = "  ".join(f"{s}s:{buckets.get(s,0)}" for s in range(int(elapsed_s) + 1))
        print(f"\n每秒渲染帧数：{line}")

    # CPU / GPU
    if SAMPLES:
        cpu_proc = [s[1] for s in SAMPLES]
        cpu_sys = [s[2] for s in SAMPLES]
        gpu_u = [s[3] for s in SAMPLES if s[3] >= 0]
        gpu_d = [s[4] for s in SAMPLES if s[4] >= 0]
        gpu_m = [s[5] for s in SAMPLES if s[5] >= 0]
        cores = psutil.cpu_count() or 1
        print(f"\nCPU：进程 mean={statistics.mean(cpu_proc):.0f}% max={max(cpu_proc):.0f}% "
              f"(≈{statistics.mean(cpu_proc)/cores:.0f}%/{max(cpu_proc)/cores:.0f}% 归一化到全核)；"
              f"系统 mean={statistics.mean(cpu_sys):.0f}% max={max(cpu_sys):.0f}%")
        if gpu_u:
            print(f"GPU：util mean={statistics.mean(gpu_u):.0f}% max={max(gpu_u):.0f}%；"
                  f"decoder mean={statistics.mean(gpu_d):.0f}% max={max(gpu_d):.0f}%；"
                  f"显存 mean={statistics.mean(gpu_m):.0f}MB")
        else:
            print("GPU：nvidia-smi 采样失败")


if __name__ == "__main__":
    main()
