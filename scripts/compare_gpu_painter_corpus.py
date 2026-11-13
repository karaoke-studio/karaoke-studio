"""Compare Direct2D and Painter raw subtitle overlays on the fixed G3 corpus."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import sys
import time
import uuid

import numpy as np
from PyQt6.QtGui import QColor, QFontDatabase, QImage
from PyQt6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from krok_helper.subtitle_render.engine.painter import (  # noqa: E402
    clear_before_layer_cache,
    paint_frame,
)
from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
    RubyAnnotation,
    Style,
    SubtitleStyleScheme,
    TimingChar,
    TimingLine,
    TimingTrack,
    TimingTrackMeta,
    TitleOverlay,
    style_from_dict,
)
from krok_helper.subtitle_render.n3.project_import import load_n3proj  # noqa: E402
from krok_helper.subtitle_render.native.backend import (  # noqa: E402
    NativeRendererProcess,
    SharedFrameRingReader,
)
from krok_helper.subtitle_render.sources.subtitles import load_nicokara_lrc  # noqa: E402


from scripts.subtitle_render_corpus import (
    CorpusScenario, _ordinary_scenario, _tactic_scenario, _heavy_scenario,
    _pattern_image, _real_dark_spiral_scenario,
)


def _rgba_premultiplied(image: QImage) -> np.ndarray:
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
    bits = converted.constBits()
    bits.setsize(converted.sizeInBytes())
    return np.frombuffer(bits, dtype=np.uint8).reshape(
        converted.height(), converted.bytesPerLine() // 4, 4
    )[:, : converted.width(), :].copy()


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.where(mask)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def overlay_diff_metrics(
    painter_image: QImage,
    gpu_image: QImage,
    *,
    alpha_threshold: int = 12,
) -> dict[str, object]:
    painter = _rgba_premultiplied(painter_image)
    gpu = _rgba_premultiplied(gpu_image)
    if painter.shape != gpu.shape:
        raise ValueError(f"image shapes differ: {painter.shape} != {gpu.shape}")
    painter_mask = painter[:, :, 3] > alpha_threshold
    gpu_mask = gpu[:, :, 3] > alpha_threshold
    union = painter_mask | gpu_mask
    intersection = painter_mask & gpu_mask
    union_count = int(np.count_nonzero(union))
    intersection_count = int(np.count_nonzero(intersection))
    diff = np.abs(painter.astype(np.int16) - gpu.astype(np.int16))
    if union_count:
        union_diff = diff[union]
        channel_mae = float(np.mean(union_diff))
        alpha_mae = float(np.mean(union_diff[:, 3]))
        channel_p95 = float(np.percentile(union_diff, 95))
    else:
        channel_mae = alpha_mae = channel_p95 = 0.0
    painter_bbox = _bbox(painter_mask)
    gpu_bbox = _bbox(gpu_mask)
    bbox_edge_delta = (
        max(abs(a - b) for a, b in zip(painter_bbox, gpu_bbox))
        if painter_bbox is not None and gpu_bbox is not None
        else None
    )
    return {
        "alpha_iou": intersection_count / union_count if union_count else 1.0,
        "painter_bbox": painter_bbox,
        "gpu_bbox": gpu_bbox,
        "bbox_edge_delta_px": bbox_edge_delta,
        "union_pixels": union_count,
        "union_channel_mae": channel_mae,
        "union_alpha_mae": alpha_mae,
        "union_channel_p95": channel_p95,
    }


def _render_painter(
    scenario: CorpusScenario, width: int, height: int, t_ms: int
) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    clear_before_layer_cache()
    paint_frame(
        image,
        scenario.track,
        t_ms,
        scenario.style,
        list(scenario.extra_tracks),
    )
    return image


def _frame_passed(scenario: CorpusScenario, metrics: dict[str, object]) -> bool:
    if int(metrics["union_pixels"]) == 0:
        return True
    bbox_delta = metrics["bbox_edge_delta_px"]
    return (
        float(metrics["alpha_iou"]) >= scenario.min_alpha_iou
        and bbox_delta is not None
        and int(bbox_delta) <= scenario.max_bbox_edge_delta_px
        and float(metrics["union_channel_mae"]) <= scenario.max_union_channel_mae
    )


def run_corpus(
    *,
    output_dir: Path,
    width: int,
    height: int,
    force_warp: bool,
    include_real: bool,
    realization_wait_s: float = 0.0,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    texture_path = output_dir / "g3-corpus-pattern.png"
    _pattern_image(texture_path)
    scenarios = [_ordinary_scenario(), _tactic_scenario(), _heavy_scenario(texture_path)]
    real = _real_dark_spiral_scenario() if include_real else None
    if real is not None:
        scenarios.append(real)

    results: list[dict[str, object]] = []
    with NativeRendererProcess(response_timeout_s=30.0) as renderer:
        for scenario in scenarios:
            configured = renderer.configure_gpu(
                scenario.track,
                scenario.style,
                width=width,
                height=height,
                fps=60,
                force_warp=force_warp,
                extra_tracks=list(scenario.extra_tracks),
                prewarm_t_ms=scenario.timestamps_ms[0],
            )
            if realization_wait_s > 0.0:
                deadline = time.monotonic() + realization_wait_s
                while time.monotonic() < deadline:
                    configured = renderer.gpu_diagnostics(force_warp=force_warp)
                    if configured.get("realization_prewarm_complete", True):
                        break
                    time.sleep(0.02)
            frames: list[dict[str, object]] = []
            for frame_index, t_ms in enumerate(scenario.timestamps_ms):
                event = renderer.render_gpu_frame(
                    t_ms,
                    force_warp=force_warp,
                    frame_index=frame_index,
                    shm_key=f"krok_gpu_corpus_{os.getpid()}_{uuid.uuid4().hex}",
                    readback_bands=True,
                )
                with SharedFrameRingReader.from_event(event) as reader:
                    gpu_image = reader.read_qimage(event)
                painter_image = _render_painter(scenario, width, height, t_ms)
                metrics = overlay_diff_metrics(painter_image, gpu_image)
                passed = _frame_passed(scenario, metrics)
                stem = f"{scenario.name}-{t_ms}"
                painter_image.save(str(output_dir / f"{stem}-painter.png"))
                gpu_image.save(str(output_dir / f"{stem}-gpu.png"))
                frames.append({"t_ms": t_ms, "passed": passed, **metrics})
            results.append(
                {
                    "name": scenario.name,
                    "source": scenario.source,
                    "thresholds": {
                        "min_alpha_iou": scenario.min_alpha_iou,
                        "max_bbox_edge_delta_px": scenario.max_bbox_edge_delta_px,
                        "max_union_channel_mae": scenario.max_union_channel_mae,
                    },
                    "cache": {
                        "hits": int(configured["cache_hits"]),
                        "misses": int(configured["cache_misses"]),
                        "bytes": int(configured["estimated_cache_bytes"]),
                    },
                    "passed": all(bool(frame["passed"]) for frame in frames),
                    "frames": frames,
                }
            )
    summary = {
        "schema": 1,
        "backend": "warp" if force_warp else "hardware",
        "width": width,
        "height": height,
        "real_n3_included": real is not None,
        "missing_named_samples": [
            name
            for name in ("TACTIC", "A stain")
            if not (REPO_ROOT.parent / "songs" / name).exists()
        ],
        "passed": all(bool(result["passed"]) for result in results),
        "scenarios": results,
    }
    (output_dir / "result.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("build/gpu-painter-corpus"))
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--warp", action="store_true")
    parser.add_argument("--no-real", action="store_true")
    parser.add_argument("--realization-wait-s", type=float, default=0.0)
    args = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(sys.argv)
    if app is None:
        raise RuntimeError("QApplication initialization failed")
    for font_path in (r"C:\Windows\Fonts\meiryo.ttc", r"C:\Windows\Fonts\times.ttf"):
        if Path(font_path).is_file():
            QFontDatabase.addApplicationFont(font_path)
    summary = run_corpus(
        output_dir=args.output_dir,
        width=max(args.width, 1),
        height=max(args.height, 1),
        force_warp=bool(args.warp),
        include_real=not args.no_real,
        realization_wait_s=max(args.realization_wait_s, 0.0),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
