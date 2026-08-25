"""Compare the G1 GPU raster core against a real N3 title frame.

The N3 mask is recovered by subtracting the source-video frame from the N3
output frame.  A small translation search intentionally removes layout-anchor
differences: Painter tests own absolute line positioning, while this gate owns
glyph geometry, stroke, and glow raster parity.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from krok_helper.subtitle_render.domain.models import (
    TimingChar,
    TimingLine,
    TimingTrack,
    style_from_dict,
)
from krok_helper.subtitle_render.n3.project_import import load_n3proj
from krok_helper.subtitle_render.native.backend import (
    NativeRendererProcess,
    SharedFrameRingReader,
    resolve_native_renderer_path,
)


def _extract_frame(ffmpeg: Path, video: Path, timestamp_ms: int, output: Path) -> None:
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{timestamp_ms / 1000.0:.6f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-y",
        str(output),
    ]
    subprocess.run(command, check=True)


def _qimage_rgba(image: QImage) -> np.ndarray:
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    bits = converted.constBits()
    bits.setsize(converted.sizeInBytes())
    return np.frombuffer(bits, dtype=np.uint8).reshape(
        converted.height(), converted.bytesPerLine() // 4, 4
    )[:, : converted.width(), :].copy()


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.where(mask)
    if not len(xs):
        raise RuntimeError("reference mask is empty")
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _shift_mask(mask: np.ndarray, dx: int, dy: int) -> np.ndarray:
    shifted = np.zeros_like(mask)
    height, width = mask.shape
    x0, x1 = max(0, dx), min(width, width + dx)
    y0, y1 = max(0, dy), min(height, height + dy)
    source_x0 = max(0, -dx)
    source_y0 = max(0, -dy)
    if x1 > x0 and y1 > y0:
        shifted[y0:y1, x0:x1] = mask[
            source_y0 : source_y0 + (y1 - y0),
            source_x0 : source_x0 + (x1 - x0),
        ]
    return shifted


def _best_translation(
    reference: np.ndarray,
    candidate: np.ndarray,
    radius: int,
) -> tuple[float, int, int]:
    best = (-1.0, 0, 0)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            shifted = _shift_mask(candidate, dx, dy)
            intersection = int(np.count_nonzero(reference & shifted))
            union = int(np.count_nonzero(reference | shifted))
            iou = intersection / union if union else 1.0
            if iou > best[0]:
                best = (iou, dx, dy)
    return best


def _title_track_and_style(project: Path) -> tuple[TimingTrack, object]:
    imported = load_n3proj(project)
    style = style_from_dict(imported.project_data["style"])
    title = style.title_overlay
    if title is None or not title.enabled or not title.text_template.strip():
        raise RuntimeError("N3 project has no enabled title overlay")
    first_line = title.text_template.splitlines()[0]
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(character, index) for index, character in enumerate(first_line)],
                end_ms=max(title.duration_ms, 1),
            )
        ]
    )
    title_style = replace(
        style,
        font_family=title.font_family,
        font_family_latin=title.font_family_latin,
        font_size_px=title.font_size_px,
        font_weight=title.font_weight,
        italic=title.italic,
        letter_spacing_px=title.letter_spacing_px,
        base_color=title.fill.color,
        fill_color=title.fill.color,
        stroke_color=title.stroke.color,
        stroke_width_px=title.stroke_width_px,
        stroke2_enabled=title.stroke2_width_px > 0,
        stroke2_width_px=title.stroke2_width_px,
        decoration_kind=title.decoration_kind,
        shadow_color=title.shadow.color,
        glow_radius_px=title.glow_radius_px,
        glow_before_radius_px=title.glow_radius_px,
        glow_after_radius_px=title.glow_radius_px,
        glow_concentration_level=title.glow_concentration_level,
        line_y_position="top",
        line_horizontal_layout="left",
        horizontal_margin_px=title.offset_x,
        line_y_margin_px=title.offset_y,
        dual_line_layout=False,
        line_lead_in_ms=0,
        line_tail_ms=0,
        font_reference_height=1080,
        title_overlay=None,
        karaoke_colors=None,
    )
    return track, title_style


def _render_gpu(
    renderer_path: Path,
    project: Path,
    width: int,
    height: int,
    timestamp_ms: int,
    force_warp: bool,
) -> QImage:
    track, style = _title_track_and_style(project)
    with NativeRendererProcess(renderer_path, response_timeout_s=30.0) as renderer:
        renderer.configure_gpu(
            track,
            style,
            width=width,
            height=height,
            fps=60,
            force_warp=force_warp,
        )
        event = renderer.render_gpu_frame(timestamp_ms, force_warp=force_warp)
        with SharedFrameRingReader.from_event(event) as reader:
            return reader.read_qimage(event)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--n3-video", type=Path, required=True)
    parser.add_argument("--source-video", type=Path, required=True)
    parser.add_argument("--timestamp-ms", type=int, default=5000)
    parser.add_argument("--crop-height", type=int, default=105)
    parser.add_argument("--threshold", type=int, default=24)
    parser.add_argument("--translation-radius", type=int, default=20)
    parser.add_argument("--min-iou", type=float, default=0.72)
    parser.add_argument("--max-width-delta", type=int, default=4)
    parser.add_argument("--force-warp", action="store_true")
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--renderer", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("build/n3-gpu-reference"))
    args = parser.parse_args()

    # Font/style resolution in build_render_ir requires a live GUI
    # application.  Without it Qt terminates the process before Python can
    # report an exception, leaving only the two ffmpeg-extracted frames.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(sys.argv)
    if app is None:
        raise RuntimeError("QApplication initialization failed")

    ffmpeg_value = args.ffmpeg or shutil.which("ffmpeg")
    if ffmpeg_value is None:
        parser.error("ffmpeg was not found; pass --ffmpeg")
    ffmpeg = Path(ffmpeg_value)
    renderer = args.renderer or resolve_native_renderer_path(root=Path.cwd())
    if renderer is None:
        parser.error("native renderer is not built; pass --renderer")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    n3_frame_path = args.output_dir / "n3.png"
    source_frame_path = args.output_dir / "source.png"
    gpu_overlay_path = args.output_dir / "gpu-overlay.png"
    gpu_composite_path = args.output_dir / "gpu-composite.png"
    _extract_frame(ffmpeg, args.n3_video, args.timestamp_ms, n3_frame_path)
    _extract_frame(ffmpeg, args.source_video, args.timestamp_ms, source_frame_path)

    n3_image = QImage(str(n3_frame_path))
    source_image = QImage(str(source_frame_path))
    if n3_image.isNull() or source_image.isNull() or n3_image.size() != source_image.size():
        raise RuntimeError("N3 and source frames must decode at the same size")
    gpu_image = _render_gpu(
        Path(renderer),
        args.project,
        n3_image.width(),
        n3_image.height(),
        args.timestamp_ms,
        args.force_warp,
    )
    gpu_image.save(str(gpu_overlay_path))
    composite = source_image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(composite)
    painter.drawImage(0, 0, gpu_image)
    painter.end()
    composite.save(str(gpu_composite_path))

    crop_height = min(max(args.crop_height, 1), n3_image.height())
    n3_rgba = _qimage_rgba(n3_image)[:crop_height]
    source_rgba = _qimage_rgba(source_image)[:crop_height]
    gpu_rgba = _qimage_rgba(gpu_image)[:crop_height]
    reference_mask = np.max(
        np.abs(n3_rgba[:, :, :3].astype(np.int16) - source_rgba[:, :, :3].astype(np.int16)),
        axis=2,
    ) > args.threshold
    gpu_mask = gpu_rgba[:, :, 3] > args.threshold
    reference_bbox = _bbox(reference_mask)
    gpu_bbox = _bbox(gpu_mask)
    iou, dx, dy = _best_translation(reference_mask, gpu_mask, args.translation_radius)
    reference_width = reference_bbox[2] - reference_bbox[0] + 1
    gpu_width = gpu_bbox[2] - gpu_bbox[0] + 1
    result = {
        "iou": round(iou, 6),
        "translation": {"x": dx, "y": dy},
        "reference_bbox": reference_bbox,
        "gpu_bbox": gpu_bbox,
        "width_delta": abs(reference_width - gpu_width),
        "threshold": args.threshold,
        "crop_height": crop_height,
        "passed": iou >= args.min_iou
        and abs(reference_width - gpu_width) <= args.max_width_delta,
    }
    (args.output_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
