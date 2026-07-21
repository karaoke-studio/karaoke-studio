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
from krok_helper.subtitle_render.models import (  # noqa: E402
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
from krok_helper.subtitle_render.n3proj_import import load_n3proj  # noqa: E402
from krok_helper.subtitle_render.native_backend import (  # noqa: E402
    NativeRendererProcess,
    SharedFrameRingReader,
)
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc  # noqa: E402


@dataclass(frozen=True)
class CorpusScenario:
    name: str
    track: TimingTrack
    style: Style
    timestamps_ms: tuple[int, ...]
    extra_tracks: tuple[TimingTrack, ...] = ()
    source: str = "synthetic"
    min_alpha_iou: float = 0.48
    max_bbox_edge_delta_px: int = 18
    max_union_channel_mae: float = 78.0


def _solid_state(text: str, stroke: str, decor: str) -> KaraokeColorState:
    return KaraokeColorState(
        text=PaintFill(mode="solid", color=text),
        stroke=PaintFill(mode="solid", color=stroke),
        stroke2=PaintFill(mode="solid", color="#000000"),
        shadow=PaintFill(mode="solid", color=decor),
    )


def _colors(
    before: str,
    after: str,
    *,
    stroke: str = "#181818",
    before_decor: str = "#FFFFFF",
    after_decor: str = "#2F8BFF",
) -> KaraokeColors:
    return KaraokeColors(
        before=_solid_state(before, stroke, before_decor),
        after=_solid_state(after, stroke, after_decor),
    )


def _base_style(**changes: object) -> Style:
    style = Style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=82,
        font_reference_height=1080,
        letter_spacing_px=2,
        stroke_width_px=4,
        stroke2_enabled=True,
        stroke2_width_px=2,
        decoration_kind="glow",
        glow_radius_px=9,
        glow_before_radius_px=9,
        glow_after_radius_px=9,
        glow_concentration_level=1,
        karaoke_colors=_colors("#FFFFFF", "#2F8BFF"),
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=36,
        ruby_stroke_width_px=2,
        ruby_stroke2_enabled=False,
        ruby_gap_px=5,
        line_y_position="bottom",
        line_y_margin_px=68,
        line_horizontal_layout="asymmetric",
        line_alignments=["left", "right"],
        horizontal_margin_px=52,
        smart_horizontal="none",
        dual_line_layout=True,
        line_gap_px=62,
        line_lead_in_ms=0,
        line_tail_ms=0,
        entry_anim="none",
        exit_anim="none",
        title_overlay=None,
        custom_style_schemes={},
    )
    return replace(style, **changes)


def _ordinary_scenario() -> CorpusScenario:
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("漢", 0), TimingChar("字", 800)],
                end_ms=1_600,
            ),
            TimingLine(
                chars=[
                    TimingChar("R", 500),
                    TimingChar("u", 1_000),
                    TimingChar("b", 1_500),
                    TimingChar("y", 2_000),
                ],
                end_ms=2_500,
            ),
        ],
        rubies=[
            RubyAnnotation(
                kanji="漢字",
                reading="かんじ",
                reading_parts=["か", "", "んじ"],
                reading_part_ms=[550, 1_050],
                pos_start_ms=0,
                pos_end_ms=1_600,
            )
        ],
    )
    return CorpusScenario(
        name="ordinary_dual_ruby_glow",
        track=track,
        style=_base_style(),
        timestamps_ms=(600, 1_250, 2_200),
    )


def _tactic_scenario() -> CorpusScenario:
    roles = ("low", "medium", "high", "medium", "low")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar(text, index * 450, role_label=roles[index])
                    for index, text in enumerate("青白発光字")
                ],
                end_ms=2_500,
            )
        ]
    )
    schemes: dict[str, SubtitleStyleScheme] = {}
    for level, role in enumerate(("low", "medium", "high")):
        schemes[role] = SubtitleStyleScheme(
            font_family="Meiryo",
            font_family_latin="Meiryo",
            font_size_px=92,
            letter_spacing_px=7,
            stroke_width_px=5,
            stroke2_enabled=False,
            decoration_kind="glow",
            glow_before_radius_px=8 + level * 3,
            glow_after_radius_px=8 + level * 3,
            glow_concentration_level=level,
            karaoke_colors=_colors(
                "#FFFFFF",
                "#EAF5FF" if level == 0 else "#3E8DFF",
                stroke="#122348",
                before_decor="#FFFFFF",
                after_decor="#2B72FF",
            ),
        )
    return CorpusScenario(
        name="tactic_like_three_glow_blue_white",
        track=track,
        style=_base_style(
            font_size_px=92,
            letter_spacing_px=7,
            stroke_width_px=5,
            stroke2_enabled=False,
            dual_line_layout=False,
            line_horizontal_layout="center",
            line_y_position="center",
            custom_style_schemes=schemes,
        ),
        timestamps_ms=(250, 1_150, 2_200),
        min_alpha_iou=0.45,
        max_bbox_edge_delta_px=20,
    )


def _pattern_image(path: Path) -> None:
    image = QImage(12, 12, QImage.Format.Format_RGBA8888)
    palette = ("#FF3050", "#30FF80", "#3070FF", "#FFE040")
    for y in range(image.height()):
        for x in range(image.width()):
            image.setPixelColor(x, y, QColor(palette[(x // 6) + 2 * (y // 6)]))
    if not image.save(str(path)):
        raise RuntimeError(f"failed to write corpus texture: {path}")


def _heavy_scenario(texture_path: Path) -> CorpusScenario:
    gradient = PaintFill(
        mode="gradient_vertical",
        gradient_stops=[(0, "#FF3050"), (45, "#30FF80"), (100, "#3070FF")],
    )
    image_fill = PaintFill(
        mode="image", image_path=str(texture_path), image_scale_pct=175
    )

    def scheme(fill: PaintFill, decor: str, size: int) -> SubtitleStyleScheme:
        before = KaraokeColorState(
            text=fill,
            stroke=PaintFill(mode="solid", color="#161616"),
            shadow=PaintFill(mode="solid", color=decor),
        )
        after = replace(before)
        return SubtitleStyleScheme(
            font_family="Meiryo",
            font_family_latin="Meiryo",
            font_size_px=size,
            stroke_width_px=4,
            stroke2_enabled=False,
            decoration_kind="glow",
            glow_before_radius_px=10,
            glow_after_radius_px=7,
            glow_concentration_level=2,
            karaoke_colors=KaraokeColors(before=before, after=after),
        )

    main = TimingTrack(
        meta=TimingTrackMeta(title="GPU 彩色字幕", artist="Painter Oracle"),
        lines=[
            TimingLine(
                chars=[
                    TimingChar("重", 0, role_label="gradient"),
                    TimingChar("彩", 600, role_label="image"),
                    TimingChar("G", 1_200, role_label="gradient"),
                    TimingChar("P", 1_700, role_label="image"),
                    TimingChar("U", 2_200, role_label="gradient"),
                ],
                end_ms=2_800,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="重彩",
                reading="じゅうさい",
                reading_parts=["じゅう", "さい"],
                reading_part_ms=[1_100],
                pos_start_ms=0,
                pos_end_ms=1_200,
            )
        ],
    )
    extra = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text, 0, role_label="extra") for text in "副字幕"],
                end_ms=2_800,
            )
        ]
    )
    title = TitleOverlay(
        enabled=True,
        text_template="{title}\n{artist}",
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=42,
        fill=PaintFill(mode="solid", color="#FFFFFF"),
        stroke=PaintFill(mode="solid", color="#101010"),
        stroke_width_px=2,
        stroke2_width_px=0,
        decoration_kind="glow",
        shadow=PaintFill(mode="solid", color="#3060FF"),
        glow_radius_px=6,
        glow_concentration_level=1,
        anchor="top_left",
        align="left",
        offset_x=40,
        offset_y=30,
        line_gap_px=8,
        layout_index=None,
        show_mode="whole",
        fade_in_ms=0,
        fade_out_ms=0,
    )
    style = _base_style(
        dual_line_layout=False,
        line_horizontal_layout="center",
        line_y_position="bottom",
        title_overlay=title,
        custom_style_schemes={
            "gradient": scheme(gradient, "#FF40A0", 88),
            "image": scheme(image_fill, "#3070FF", 76),
            "extra": scheme(PaintFill(mode="solid", color="#40FF80"), "#40FF80", 54),
        },
    )
    return CorpusScenario(
        name="g3_heavy_roles_fills_title_multisource",
        track=main,
        style=style,
        timestamps_ms=(350, 1_450, 2_500),
        extra_tracks=(extra,),
        min_alpha_iou=0.38,
        max_bbox_edge_delta_px=28,
        max_union_channel_mae=95.0,
    )


def _real_dark_spiral_scenario() -> CorpusScenario | None:
    project_path = REPO_ROOT.parent / "songs" / "Dark spiral journey" / "1.n3proj"
    lrc_path = project_path.with_name("Dark spiral journey.lrc")
    if not project_path.is_file() or not lrc_path.is_file():
        return None
    imported = load_n3proj(project_path)
    source_track = load_nicokara_lrc(lrc_path)
    if len(source_track.lines) <= 3:
        return None
    line = replace(source_track.lines[3], layout_index=0)
    start_ms = line.chars[0].start_ms
    end_ms = line.end_ms or start_ms
    track = TimingTrack(
        meta=source_track.meta,
        lines=[line],
        rubies=[
            ruby
            for ruby in source_track.rubies
            if ruby.pos_start_ms < end_ms and ruby.pos_end_ms > start_ms
        ],
    )
    imported_style = style_from_dict(imported.project_data["style"])
    style = replace(
        imported_style,
        font_family="Meiryo",
        font_family_latin="Meiryo",
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        title_overlay=None,
        custom_style_schemes={},
        singer_style_overrides={},
        dual_line_layout=False,
        line_y_position="center",
        line_horizontal_layout="center",
        smart_horizontal="none",
        line_alignments=["center"],
        line_lead_in_ms=0,
        line_tail_ms=0,
        entry_anim="none",
        exit_anim="none",
        lit_enabled=False,
        viewport_scale_pct=100,
        viewport_rotation_deg=0,
        viewport_offset_x=0,
        viewport_offset_y=0,
    )
    return CorpusScenario(
        name="dark_spiral_real_n3_common_slice",
        track=track,
        style=style,
        timestamps_ms=(24_300, 24_900, 25_500),
        source=str(project_path),
        min_alpha_iou=0.38,
        max_bbox_edge_delta_px=20,
        max_union_channel_mae=100.0,
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
