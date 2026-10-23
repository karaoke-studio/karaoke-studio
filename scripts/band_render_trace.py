"""行背景带全流程诊断脚本（对应用户「走全流程打印」的要求）。

跑的是**真实预览管线**：项目 → 样式（套预设）→ `GpuAsyncSubtitleRenderer`
（与预览面板同一个 worker，含 sidecar 拉起 / configure / style patch / 渲染
/ 回退）→ 拿到帧或回退原因 → 存图 + 统计。

用法：
    set PYTHONPATH=<repo>
    set QT_QPA_PLATFORM=offscreen
    C:\\Python314\\python.exe scripts\\band_render_trace.py ^
        --project "E:\\...\\xxx.yurika" --preset staff_scroll --time 14117

选项：
    --width/--height/--dpr   渲染目标（默认 1920x1080，dpr=1）
    --cpu                    走 Painter 同步渲染做对照
    --line-color "#E01B24"   覆盖谱线颜色
    --out <dir>              输出目录（默认 %TEMP%\\band-trace）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop, QTimer  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

from krok_helper.subtitle_render.domain.band_presets import (  # noqa: E402
    band_preset,
    resolve_band_image,
    resolve_band_lines_image,
)
from krok_helper.subtitle_render.domain.models import style_from_dict  # noqa: E402
from krok_helper.subtitle_render.engine.render.elements.band import (  # noqa: E402
    band_spec,
)
from krok_helper.subtitle_render.engine.render.render_ir import (  # noqa: E402
    build_render_ir,
)
from krok_helper.subtitle_render.sources.sug import load_sug_timing_track  # noqa: E402


def _fmt(value: object) -> str:
    return repr(value)


def print_style(style) -> None:
    print("[1] 样式（行背景带字段）")
    for key in sorted(vars(style)):
        if key.startswith("line_band"):
            print(f"    {key} = {_fmt(getattr(style, key))}")


def print_assets(style) -> None:
    image = str(style.line_band_image or "")
    print("[2] 资源解析")
    base = resolve_band_image(image)
    lines = resolve_band_lines_image(image)
    print(f"    image spec  = {_fmt(image)}")
    print(f"    base image  = {base} (exists={bool(base and base.is_file())})")
    print(f"    lines image = {lines} (exists={bool(lines and lines.is_file())})")
    for label, path in (("base", base), ("lines", lines)):
        if path is not None and path.is_file():
            probe = QImage(str(path))
            print(
                f"    {label} size = {probe.width()}x{probe.height()} "
                f"null={probe.isNull()}"
            )


def print_spec(style) -> None:
    print("[3] band_spec（CPU 绘制口径）")
    spec = band_spec(style)
    if spec is None:
        print("    spec = None（开关关/图不存在 → 整条带子不画）")
        return
    for key in (
        "image_path",
        "cap_left",
        "cap_right",
        "fill_mode",
        "mid_mode",
        "tint",
        "tint_strength_pct",
        "opacity_pct",
        "line_image_path",
        "line_color",
        "edge_kind",
    ):
        print(f"    {key} = {_fmt(getattr(spec, key))}")


def print_ir(track, style, width: int, height: int) -> None:
    print("[4] render IR（GPU 下发口径）")
    ir = build_render_ir(track, style, width=width, height=height, fps=60, dpr=1.0)
    for key in sorted(ir["style"]):
        if key.startswith("line_band"):
            print(f"    {key} = {_fmt(ir['style'][key])}")


def count_color(
    image: QImage, color: QColor, tol: int = 40, step: int = 2
) -> int:
    total = 0
    for y in range(0, image.height(), step):
        for x in range(0, image.width(), step):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() < 60:
                continue
            if (
                abs(pixel.red() - color.red()) <= tol
                and abs(pixel.green() - color.green()) <= tol
                and abs(pixel.blue() - color.blue()) <= tol
            ):
                total += 1
    return total


def flatten(image: QImage, background: str) -> QImage:
    canvas = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(QColor(background))
    painter = QPainter(canvas)
    painter.drawImage(0, 0, image)
    painter.end()
    return canvas


def render_cpu(track, style, t_ms: int, width: int, height: int) -> QImage:
    from krok_helper.subtitle_render.engine.painter import (
        clear_before_layer_cache,
        paint_frame,
    )

    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    clear_before_layer_cache()
    paint_frame(image, track, t_ms, style)
    return image


def render_gpu_worker(
    track,
    style,
    t_ms: int,
    width: int,
    height: int,
    dpr: float,
    duration_ms: int,
    patches: list[dict] | None = None,
    playback_steps: int = 0,
    playback_step_ms: int = 33,
) -> tuple[QImage | None, str]:
    """与预览面板同一条 GPU 管线；返回 (最后一帧, 回退原因)。

    ``patches`` 用来模拟属性面板的连续改值（例如拖谱线颜色滑条）：每一轮
    先 `set_state(..., relayout_scope="paint")` 再 `request`，走 sidecar 的
    style patch 通道。
    """
    from krok_helper.subtitle_render.frontend.preview.preview_async import (
        GpuAsyncSubtitleRenderer,
    )

    renderer = GpuAsyncSubtitleRenderer(width, height)
    result: dict[str, object] = {"image": None, "fallback": "", "mode": ""}
    rounds = list(patches or [])
    state = {"step": 0}

    print("    frame_ready 帧数 / fallback 记录：")

    def finish() -> None:
        loop.quit()

    def advance_or_finish() -> None:
        if result["fallback"]:
            finish()
            return
        if state["step"] < len(rounds):
            patch = rounds[state["step"]]
            state["step"] += 1
            new_style = replace(style, **patch)
            print(f"    patch #{state['step']}: {patch}")
            renderer.set_state(
                track, new_style, duration_ms=duration_ms, relayout_scope="paint"
            )
            renderer.request(t_ms)
            return
        if state["step"] < len(rounds) + playback_steps:
            state["step"] += 1
            next_t = t_ms + (state["step"] - len(rounds)) * playback_step_ms
            print(f"    playback t={next_t}")
            renderer.request(next_t)
            return
        finish()

    def on_frame(image: QImage, frame_t: int) -> None:
        result["image"] = image.copy()
        print(
            f"      frame_ready #{state['step']} t={frame_t} "
            f"{image.width()}x{image.height()}"
        )
        QTimer.singleShot(0, advance_or_finish)

    def on_fallback(reason: str) -> None:
        print(f"      fallback_occurred: {reason}")
        result["fallback"] = str(reason)
        QTimer.singleShot(0, finish)

    def on_mode(mode: str) -> None:
        result["mode"] = str(mode)
        print(f"    backend mode -> {mode}")

    def on_progress(percent: int, stage: str) -> None:
        print(f"    renderProgress {percent}% {stage}")

    loop = QEventLoop()
    renderer.frame_ready.connect(on_frame)
    renderer.fallback_occurred.connect(on_fallback)
    renderer.backendModeChanged.connect(on_mode)
    renderer.renderProgress.connect(on_progress)
    renderer.set_render_target(width, height, dpr)
    renderer.set_state(track, style, duration_ms=duration_ms)
    renderer.request(t_ms)
    QTimer.singleShot(180_000, loop.quit)
    loop.exec()
    stats = {}
    try:
        stats = renderer.stats_snapshot()
    except Exception:  # pragma: no cover - 诊断脚本
        pass
    print(f"    stats = {stats}")
    renderer.stop()
    renderer.deleteLater()
    APP.processEvents()
    return result["image"], result["fallback"] or ""


def main() -> int:
    parser = argparse.ArgumentParser(description="行背景带全流程诊断")
    parser.add_argument("--project", required=True)
    parser.add_argument("--preset", default="staff_scroll")
    parser.add_argument("--time", type=int, default=0)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--dpr", type=float, default=1.0)
    parser.add_argument("--line-color", default="")
    parser.add_argument(
        "--patches",
        default="",
        help="分号分隔的样式覆盖（JSON），模拟面板连续改值，例如 "
        '\'{"line_band_line_color":"#E01B24"};'
        '{"line_band_line_color":"#20C060"}\'',
    )
    parser.add_argument(
        "--frames", type=int, default=0, help="连续播放帧数（每帧 33ms 递增）"
    )
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    out_dir = Path(args.out or (Path(os.environ["TEMP"]) / "band-trace"))
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = json.loads(Path(args.project).read_text(encoding="utf-8"))
    track = load_sug_timing_track(payload["subtitle_path"])
    style = style_from_dict(payload["style"])
    preset = band_preset(args.preset)
    if preset is None:
        print(f"未知预设: {args.preset}")
        return 2
    style = replace(
        style,
        line_band_enabled=True,
        line_band_image=preset.image_spec,
        line_band_cap_left_px=preset.cap_left_px,
        line_band_cap_right_px=preset.cap_right_px,
        line_band_pad_top_px=preset.pad_top_px,
        line_band_pad_bottom_px=preset.pad_bottom_px,
        line_band_pad_left_px=preset.pad_left_px,
        line_band_pad_right_px=preset.pad_right_px,
    )
    if args.line_color:
        style = replace(style, line_band_line_color=args.line_color)

    t_ms = args.time or 0
    print(f"[0] 项目 {Path(args.project).name}  t={t_ms}  {args.width}x{args.height}@{args.dpr}")
    print_style(style)
    print_assets(style)
    print_spec(style)
    print_ir(track, style, args.width, args.height)

    started = time.monotonic()
    patches = []
    if args.patches:
        patches = [json.loads(chunk) for chunk in args.patches.split(";") if chunk.strip()]
    if args.cpu:
        print("[5] Painter 同步渲染")
        image = render_cpu(track, style, t_ms, args.width, args.height)
        fallback = ""
    else:
        print("[5] GPU 预览 worker 渲染（sidecar 全流程）")
        image, fallback = render_gpu_worker(
            track,
            style,
            t_ms,
            args.width,
            args.height,
            args.dpr,
            int(payload.get("duration_ms") or 0),
            patches,
            args.frames,
        )
    elapsed = (time.monotonic() - started) * 1000.0
    print(f"    用时 {elapsed:.0f} ms")

    if image is None or image.isNull():
        print(f"[6] 结果：没有帧（回退原因={fallback or '无'}）")
        return 1

    suffix = "cpu" if args.cpu else "gpu"
    flat = flatten(image, "#E8ECF2")
    flat_path = out_dir / f"{suffix}-{t_ms}-flat.png"
    flat.save(str(flat_path))
    line_color = QColor(args.line_color or "#1A1A1C")
    print(f"[6] 结果：帧 {image.width()}x{image.height()} -> {flat_path}")
    print(f"    近谱线色像素 = {count_color(flat, line_color)}")
    if fallback:
        print(f"    回退原因 = {fallback}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
