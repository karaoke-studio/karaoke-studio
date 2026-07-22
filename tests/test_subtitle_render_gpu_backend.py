from __future__ import annotations

import os
import ctypes
from dataclasses import replace
from pathlib import Path
import threading
import time
import uuid

import numpy as np
import pytest
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtGui import QColor, QImage

from krok_helper.subtitle_render.native_backend import (
    NativeRendererError,
    NativeRendererProcess,
    SharedFrameRingReader,
    resolve_native_renderer_path,
)
from krok_helper.subtitle_render.models import (
    GuideSymbol,
    KaraokeColors,
    KaraokeColorState,
    LyricsLayout,
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
from krok_helper.subtitle_render.n3proj_import import load_n3proj
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc


DARK_SPIRAL_N3PROJ = (
    Path.cwd().parent / "songs" / "Dark spiral journey" / "1.n3proj"
)
MEFISTO_N3PROJ = Path.cwd().parent / "songs" / "メフィスト" / "1.n3proj"
RUN_REAL_GPU_AB = os.environ.get("KROK_RUN_REAL_GPU_AB", "0") == "1"


def test_shared_frame_reader_close_tolerates_deleted_qt_wrapper():
    class DeletedSharedMemory:
        def isAttached(self):
            raise RuntimeError("wrapped C/C++ object has been deleted")

    reader = SharedFrameRingReader("deleted-wrapper")
    reader._shared = DeletedSharedMemory()  # noqa: SLF001

    reader.close()
    reader.close()

    assert reader._shared is None  # noqa: SLF001


def _renderer_path() -> Path:
    path = resolve_native_renderer_path(root=Path.cwd())
    if path is None:
        pytest.skip("native subtitle renderer executable is not built")
    return path


def _pixel(slot, x: int, y: int) -> tuple[int, int, int, int]:
    offset = y * slot.stride + x * 4
    return tuple(slot.payload[offset : offset + 4])  # type: ignore[return-value]


def _g1_track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("K", 0),
                    TimingChar("a", 500),
                    TimingChar("歌", 1000),
                ],
                end_ms=1500,
            )
        ]
    )


@pytest.mark.skipif(os.name != "nt", reason="DirectComposition preview is Windows-only")
def test_gpu_g6_direct_composition_child_window_has_zero_readback(qapp, monkeypatch) -> None:
    if QApplication.platformName().lower() != "windows":
        pytest.skip("G6 native HWND smoke requires the Windows Qt platform plugin")
    renderer_path = _renderer_path()
    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_RENDERER", str(renderer_path))

    parent = QWidget()
    parent.resize(320, 180)
    parent.show()
    qapp.processEvents()
    parent_hwnd = int(parent.winId())
    result: dict[str, dict] = {}
    failures: list[BaseException] = []
    presented = threading.Event()
    release = threading.Event()

    def render() -> None:
        try:
            with NativeRendererProcess(response_timeout_s=15.0) as process:
                configured = process.configure_gpu(
                    _g1_track(),
                    Style(font_size_px=48, line_lead_in_ms=0),
                    width=320,
                    height=180,
                    fps=60,
                    force_warp=True,
                )
                event = process.present_gpu_frame(
                    500,
                    parent_hwnd=parent_hwnd,
                    x=0,
                    y=0,
                    width=320,
                    height=180,
                    force_warp=True,
                )
                result.update(configured=configured, event=event)
                presented.set()
                release.wait(timeout=5.0)
                closed = process.close_gpu_preview(force_warp=True)
                result["closed"] = closed
        except BaseException as exc:  # pragma: no cover - surfaced on the GUI thread
            failures.append(exc)

    worker = threading.Thread(target=render, name="g6-native-preview-smoke")
    worker.start()
    deadline = time.monotonic() + 30.0
    while not presented.is_set() and worker.is_alive() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert presented.is_set()
    child_hwnd = int(result["event"]["child_hwnd"])
    user32 = ctypes.windll.user32
    assert user32.IsWindow(child_hwnd)
    assert user32.GetParent(child_hwnd) == parent_hwnd
    child_rect = ctypes.wintypes.RECT()
    assert user32.GetWindowRect(child_hwnd, ctypes.byref(child_rect))
    assert child_rect.right - child_rect.left == 320
    assert child_rect.bottom - child_rect.top == 180
    release.set()
    while worker.is_alive() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    worker.join(timeout=1.0)
    parent.close()
    parent.deleteLater()
    qapp.processEvents()

    assert not worker.is_alive()
    assert failures == []
    assert result["configured"]["native_preview"] is True
    assert result["event"]["event"] == "gpu_frame_presented"
    assert result["event"]["transport"] == "direct_composition"
    assert result["event"]["readback_ms"] == 0.0
    assert float(result["event"]["present_ms"]) >= 0.0
    assert child_hwnd > 0
    assert result["closed"]["event"] == "gpu_preview_closed"
    assert not user32.IsWindow(child_hwnd)


def _g1_style(**changes) -> Style:
    style = Style(
        font_family="Arial",
        font_family_latin="Times New Roman",
        font_size_px=64,
        font_reference_height=360,
        base_color="#FFFFFF",
        fill_color="#FF2030",
        stroke_color="#101010",
        stroke_width_px=3,
        stroke2_enabled=True,
        stroke2_width_px=2,
        decoration_kind="shadow",
        shadow_color="#00FF40",
        line_y_position="center",
        line_horizontal_layout="center",
        line_lead_in_ms=0,
        line_tail_ms=0,
    )
    return replace(style, **changes)


def _g3_ruby_track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("漢", 0), TimingChar("字", 1_000)],
                end_ms=2_000,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="漢字",
                reading="かんじ",
                reading_part_ms=[650, 1_300],
                pos_start_ms=0,
                pos_end_ms=2_000,
                reading_parts=["か", "", "んじ"],
            )
        ],
    )


def _g3_singer_track() -> TimingTrack:
    track = _g3_ruby_track()
    return replace(
        track,
        lines=[
            replace(track.lines[0], singer_id=1, singer_label="主唱")
        ],
    )


def _g3_inline_role_track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("A", 0, role_label="lead"),
                    TimingChar("B", 800, role_label="back"),
                    TimingChar("C", 1_600, role_label="back"),
                ],
                end_ms=2_400,
            )
        ]
    )


def _g3_ruby_anchor_track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("♧", 0, role_label="decor"),
                    TimingChar("項", 500),
                ],
                end_ms=1_200,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="項",
                reading="こう",
                pos_start_ms=500,
                pos_end_ms=1_200,
                reading_parts=["こ", "う"],
                reading_part_ms=[350],
            )
        ],
    )


def _g3_role_ruby_track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("項", 0, role_label="lead")],
                end_ms=1_200,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="項",
                reading="こう",
                pos_start_ms=0,
                pos_end_ms=1_200,
                reading_parts=["こ", "う"],
                reading_part_ms=[600],
            )
        ],
    )


def _g3_fill_track() -> TimingTrack:
    return TimingTrack(
        lines=[TimingLine(chars=[TimingChar("█", 0)], end_ms=1_000)]
    )


def _g4_spin_scene() -> tuple[TimingTrack, Style]:
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("A", 500),
                    TimingChar("漢", 900),
                    TimingChar("字", 1_300),
                    TimingChar("B", 1_700),
                ],
                end_ms=2_100,
                display_start_override_ms=0,
                display_end_override_ms=3_000,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="漢字",
                reading="かんじ",
                reading_parts=["かん", "じ"],
                reading_part_ms=[1_100],
                pos_start_ms=900,
                pos_end_ms=1_700,
            )
        ],
    )
    ruby_state = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FF20E0"),
        stroke=PaintFill(mode="solid", color="#301030"),
        shadow=PaintFill(mode="solid", color="#FF20E0"),
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=72,
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=32,
        ruby_stroke_width_px=2,
        ruby_stroke2_enabled=False,
        ruby_decoration_kind="glow",
        ruby_glow_before_radius_px=6,
        ruby_glow_after_radius_px=6,
        ruby_glow_concentration_level=1,
        ruby_karaoke_colors=KaraokeColors(before=ruby_state, after=ruby_state),
        stroke_width_px=3,
        stroke2_enabled=False,
        decoration_kind="glow",
        glow_before_radius_px=8,
        glow_after_radius_px=8,
        glow_concentration_level=1,
        dual_line_layout=False,
        line_horizontal_layout="center",
        entry_anim="spin_flip",
        entry_lead_ms=1_000,
        exit_anim="spin_flip",
        exit_fade_ms=1_000,
    )
    return track, style


def _g4_utopia_main_scene() -> tuple[TimingTrack, Style]:
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("A", 1_200),
                    TimingChar("夢", 1_700),
                    TimingChar("想", 2_200),
                    TimingChar("B", 2_700),
                ],
                end_ms=3_200,
                display_start_override_ms=0,
                display_end_override_ms=4_400,
            )
        ]
    )
    state = KaraokeColorState(
        text=PaintFill(mode="solid", color="#30D0FF"),
        stroke=PaintFill(mode="solid", color="#202040"),
    )
    return track, _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=72,
        stroke_width_px=3,
        stroke2_enabled=False,
        decoration_kind="glow",
        glow_before_radius_px=8,
        glow_after_radius_px=8,
        glow_concentration_level=1,
        dual_line_layout=False,
        line_horizontal_layout="center",
        karaoke_colors=KaraokeColors(before=state, after=state),
        entry_anim="utopia",
        exit_anim="utopia",
        line_tail_ms=1_000,
    )


def _g4_utopia_ruby_scene() -> tuple[TimingTrack, Style]:
    track, style = _g4_utopia_main_scene()
    track.rubies = [
        RubyAnnotation(
            kanji="夢想",
            reading="ゆめ",
            reading_parts=["ゆ", "め"],
            reading_part_ms=[2_200],
            pos_start_ms=1_700,
            pos_end_ms=2_700,
        )
    ]
    ruby_state = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FF30D0"),
        stroke=PaintFill(mode="solid", color="#301030"),
        shadow=PaintFill(mode="solid", color="#FF30D0"),
    )
    return track, replace(
        style,
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=32,
        ruby_stroke_width_px=2,
        ruby_stroke2_enabled=False,
        ruby_decoration_kind="glow",
        ruby_glow_before_radius_px=6,
        ruby_glow_after_radius_px=6,
        ruby_glow_concentration_level=1,
        ruby_karaoke_colors=KaraokeColors(before=ruby_state, after=ruby_state),
    )
def _alpha_count(payload: bytes) -> int:
    return sum(alpha > 0 for alpha in payload[3::4])


def _alpha_bounds(slot) -> tuple[int, int, int, int]:
    return _payload_alpha_bounds(slot.payload, slot.width, slot.height, slot.stride)


def _payload_alpha_bounds(
    payload: bytes,
    width: int = 640,
    height: int = 360,
    stride: int | None = None,
) -> tuple[int, int, int, int]:
    stride = stride or width * 4
    xs: list[int] = []
    ys: list[int] = []
    for y in range(height):
        row = y * stride
        for x in range(width):
            if payload[row + x * 4 + 3] > 0:
                xs.append(x)
                ys.append(y)
    assert xs and ys
    return min(xs), min(ys), max(xs), max(ys)


def _render_g1_frames(
    renderer: NativeRendererProcess,
    style: Style,
    timestamps_ms: tuple[int, ...],
    *,
    force_warp: bool,
    track: TimingTrack | None = None,
    width: int = 640,
    height: int = 360,
    extra_tracks: list[TimingTrack] | None = None,
    worker_count: int = 1,
    realization_enabled: bool = True,
    shared_resources: bool = False,
) -> tuple[dict, list[bytes]]:
    configured = renderer.configure_gpu(
        track or _g1_track(),
        style,
        width=width,
        height=height,
        fps=60,
        force_warp=force_warp,
        extra_tracks=extra_tracks,
        worker_count=worker_count,
        realization_enabled=realization_enabled,
        shared_resources=shared_resources,
    )
    reader: SharedFrameRingReader | None = None
    frames: list[bytes] = []
    try:
        for frame_index, t_ms in enumerate(timestamps_ms):
            event = renderer.render_gpu_frame(
                t_ms,
                force_warp=force_warp,
                frame_index=frame_index,
            )
            if reader is None:
                reader = SharedFrameRingReader.from_event(event)
                reader.attach()
            image = reader.read_qimage(event).convertToFormat(QImage.Format.Format_RGBA8888)
            bits = image.constBits()
            bits.setsize(image.sizeInBytes())
            frames.append(bytes(bits))
    finally:
        if reader is not None:
            reader.close()
    return configured, frames


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("animation", ["none", "utopia"])
def test_gpu_shared_realizations_preserve_pixels(animation, monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        stroke_width_px=15,
        latin_stroke_width_px=15,
        stroke2_enabled=False,
        decoration_kind="glow",
        glow_radius_px=9,
        glow_before_radius_px=9,
        glow_after_radius_px=9,
        entry_anim=animation,
        exit_anim=animation,
        entry_lead_ms=500,
        exit_fade_ms=500,
    )
    timestamps = (0, 250, 750, 1_250, 1_500)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=60.0) as renderer:
        _, baseline = _render_g1_frames(
            renderer,
            style,
            timestamps,
            force_warp=False,
            worker_count=3,
            realization_enabled=False,
            shared_resources=False,
        )
        configured, shared = _render_g1_frames(
            renderer,
            style,
            timestamps,
            force_warp=False,
            worker_count=3,
            realization_enabled=True,
            shared_resources=True,
        )

    assert configured["shared_resources"] is True
    assert configured["realization_enabled"] is True
    assert configured["realization_count"] > 0
    metrics = []
    for expected, actual in zip(baseline, shared):
        expected_rgba = np.frombuffer(expected, dtype=np.uint8).reshape(-1, 4)
        actual_rgba = np.frombuffer(actual, dtype=np.uint8).reshape(-1, 4)
        visible = (expected_rgba[:, 3] > 2) | (actual_rgba[:, 3] > 2)
        if not np.any(visible):
            metrics.append((0.0, 0.0, 1.0))
            continue
        delta = np.abs(
            expected_rgba[visible].astype(np.int16)
            - actual_rgba[visible].astype(np.int16)
        )
        expected_ink = expected_rgba[:, 3] > 2
        actual_ink = actual_rgba[:, 3] > 2
        union = np.count_nonzero(expected_ink | actual_ink)
        intersection = np.count_nonzero(expected_ink & actual_ink)
        metrics.append(
            (
                float(np.mean(delta)),
                float(np.percentile(delta, 99)),
                intersection / max(union, 1),
            )
        )
    assert max(mean for mean, _p99, _iou in metrics) <= 3.0
    assert min(iou for _mean, _p99, iou in metrics) >= 0.98


@pytest.mark.skipif(
    os.name != "nt" or not RUN_REAL_GPU_AB or not MEFISTO_N3PROJ.is_file(),
    reason="real Mephisto GPU A/B is opt-in",
)
@pytest.mark.parametrize("animation", ["none", "utopia"])
def test_gpu_shared_realizations_preserve_mefisto_pixels(animation, monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    imported = load_n3proj(MEFISTO_N3PROJ).project_data
    track = load_nicokara_lrc(Path(imported["subtitle_path"]))
    style = style_from_dict(imported["style"])

    def wide_scheme(scheme):
        return replace(scheme, stroke_width_px=15, latin_stroke_width_px=15)

    style = replace(
        style,
        stroke_width_px=15,
        latin_stroke_width_px=15,
        entry_anim=animation,
        exit_anim=animation,
        decoration_kind="glow",
        ruby_decoration_kind="glow",
        singer_style_overrides={
            key: wide_scheme(scheme)
            for key, scheme in style.singer_style_overrides.items()
        },
        custom_style_schemes={
            key: wide_scheme(scheme)
            for key, scheme in style.custom_style_schemes.items()
        },
    )
    timestamps = (29_000, 29_500, 30_000, 31_000, 32_000, 33_000)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=300.0) as renderer:
        _, baseline = _render_g1_frames(
            renderer,
            style,
            timestamps,
            force_warp=False,
            width=1920,
            height=1080,
            track=track,
            worker_count=3,
            realization_enabled=False,
            shared_resources=False,
        )
        configured, shared = _render_g1_frames(
            renderer,
            style,
            timestamps,
            force_warp=False,
            width=1920,
            height=1080,
            track=track,
            worker_count=3,
            realization_enabled=True,
            shared_resources=True,
        )

    assert configured["realization_count"] > 0
    for expected, actual in zip(baseline, shared):
        expected_rgba = np.frombuffer(expected, dtype=np.uint8).reshape(-1, 4)
        actual_rgba = np.frombuffer(actual, dtype=np.uint8).reshape(-1, 4)
        visible = (expected_rgba[:, 3] > 2) | (actual_rgba[:, 3] > 2)
        delta = np.abs(
            expected_rgba[visible].astype(np.int16)
            - actual_rgba[visible].astype(np.int16)
        )
        expected_ink = expected_rgba[:, 3] > 2
        actual_ink = actual_rgba[:, 3] > 2
        union = np.count_nonzero(expected_ink | actual_ink)
        intersection = np.count_nonzero(expected_ink & actual_ink)
        assert float(np.mean(delta)) <= 3.0
        assert intersection / max(union, 1) >= 0.98


def _render_painter_oracle(
    style: Style,
    *,
    t_ms: int = 750,
    track: TimingTrack | None = None,
    width: int = 640,
    height: int = 360,
    extra_tracks: list[TimingTrack] | None = None,
) -> bytes:
    from PyQt6.QtGui import QFontDatabase, QImage
    from PyQt6.QtWidgets import QApplication

    from krok_helper.subtitle_render.engine.painter import (
        clear_before_layer_cache,
        paint_frame,
    )

    app = QApplication.instance() or QApplication([])
    assert app is not None
    # Qt's offscreen Windows plugin does not enumerate system fonts by itself.
    # Load deterministic files that DirectWrite resolves to the same families.
    assert QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\times.ttf") >= 0
    assert QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\meiryo.ttc") >= 0
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(0)
    clear_before_layer_cache()
    paint_frame(image, track or _g1_track(), t_ms, style, extra_tracks)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    return bytes(bits)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_horizontal_ruby_gradient_uses_main_line_bounds_by_default(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    transparent = PaintFill(color="#00000000")
    ruby_gradient = PaintFill(
        mode="gradient_horizontal",
        color="#FF0000",
        start_color="#FF0000",
        end_color="#0000FF",
        gradient_stops=[(0, "#FF0000"), (100, "#0000FF")],
    )
    invisible_state = KaraokeColorState(
        text=transparent,
        stroke=transparent,
        stroke2=transparent,
        shadow=transparent,
    )
    ruby_state = KaraokeColorState(
        text=ruby_gradient,
        stroke=transparent,
        stroke2=transparent,
        shadow=transparent,
    )
    line = TimingLine(
        chars=[
            TimingChar("A", 0),
            TimingChar("B", 500),
            TimingChar("C", 1000),
            TimingChar("D", 1500),
        ],
        end_ms=2000,
    )
    track = TimingTrack(
        lines=[line],
        rubies=[
            RubyAnnotation(
                kanji="A",
                reading="M",
                reading_part_ms=[0],
                pos_start_ms=0,
                pos_end_ms=500,
            ),
            RubyAnnotation(
                kanji="D",
                reading="M",
                reading_part_ms=[1500],
                pos_start_ms=1500,
                pos_end_ms=2000,
            ),
        ],
    )
    base_style = Style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=72,
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=40,
        ruby_gap_px=10,
        line_lead_in_ms=0,
        line_y_position="center",
        stroke_width_px=0,
        stroke2_width_px=0,
        decoration_kind="none",
        ruby_stroke_width_px=0,
        ruby_stroke2_width_px=0,
        ruby_decoration_kind="none",
        karaoke_colors=KaraokeColors(
            before=invisible_state,
            after=invisible_state,
        ),
        ruby_karaoke_colors=KaraokeColors(
            before=ruby_state,
            after=ruby_state,
        ),
    )

    def ruby_group_color_bias(frame: bytes) -> tuple[float, float]:
        pixels = memoryview(frame)
        width, height = 640, 360
        visible_columns: list[int] = []
        for x in range(width):
            if any(pixels[(y * width + x) * 4 + 3] for y in range(height)):
                visible_columns.append(x)
        assert visible_columns
        gaps = [
            (visible_columns[index - 1], visible_columns[index])
            for index in range(1, len(visible_columns))
            if visible_columns[index] - visible_columns[index - 1] > 1
        ]
        assert gaps
        split_x = max(gaps, key=lambda gap: gap[1] - gap[0])
        split = (split_x[0] + split_x[1]) // 2

        def bias(left: int, right: int) -> float:
            red = blue = alpha = 0
            for y in range(height):
                for x in range(left, right):
                    offset = (y * width + x) * 4
                    a = pixels[offset + 3]
                    red += pixels[offset] * a
                    blue += pixels[offset + 2] * a
                    alpha += a
            assert alpha > 0
            return (red - blue) / alpha

        return bias(0, split), bias(split, width)

    with NativeRendererProcess(_renderer_path(), response_timeout_s=10.0) as renderer:
        _configured, shared_frames = _render_g1_frames(
            renderer,
            base_style,
            (2000,),
            force_warp=True,
            track=track,
        )
        _configured, local_frames = _render_g1_frames(
            renderer,
            replace(base_style, ruby_horizontal_gradient_with_main=False),
            (2000,),
            force_warp=True,
            track=track,
        )

    shared_left, shared_right = ruby_group_color_bias(shared_frames[0])
    local_left, local_right = ruby_group_color_bias(local_frames[0])
    assert shared_left > 40
    assert shared_right < -40
    assert abs(local_left - local_right) < 20


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_backend_reports_hardware_and_warp_adapters(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=10.0) as renderer:
        hardware = renderer.backend_info()
        warp = renderer.backend_info(force_warp=True)

    if not hardware.get("available"):
        pytest.skip(f"hardware Direct2D adapter is unavailable: {hardware.get('error')}")
    assert hardware["backend"] == "direct2d"
    assert hardware["hardware"] is True
    assert hardware["warp"] is False
    assert hardware["adapter"]
    assert hardware["feature_level"] in {"11_0", "11_1"}
    assert hardware["transparent_surface"] is True
    assert hardware["staging_readback"] is True
    assert hardware["glyphs"] is True

    assert warp["available"] is True
    assert warp["backend"] == "direct2d"
    assert warp["hardware"] is False
    assert warp["warp"] is True
    assert "Microsoft" in warp["adapter"]


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("force_warp", [False, True])
def test_gpu_probe_readback_is_straight_rgba_with_transparent_background(
    monkeypatch,
    force_warp: bool,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=10.0) as renderer:
        if not force_warp and not renderer.backend_info().get("available"):
            pytest.skip("hardware Direct2D adapter is unavailable")
        event = renderer.render_probe(
            width=64,
            height=48,
            force_warp=force_warp,
            draw_glyph=False,
            rgba=(51, 102, 204, 128),
        )
        with SharedFrameRingReader.from_event(event) as reader:
            slot = reader.read_frame(event)

    assert event["backend"] == "direct2d"
    assert event["warp"] is force_warp
    assert event["render_ms"] >= 0.0
    assert event["readback_ms"] >= 0.0
    assert slot.width == 64
    assert slot.height == 48
    assert slot.stride == 64 * 4
    assert slot.pixel_format == "rgba8888"
    assert _pixel(slot, 0, 0) == (0, 0, 0, 0)
    rectangle_pixel = _pixel(slot, 16, 24)
    assert all(abs(actual - expected) <= 1 for actual, expected in zip(rectangle_pixel, (51, 102, 204, 128)))


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_probe_rejects_invalid_dimensions(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=10.0) as renderer:
        with pytest.raises(NativeRendererError, match="dimensions"):
            renderer.render_probe(width=0, height=48)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_preview_readback_copies_shared_slot_directly_to_qimage(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=10.0) as renderer:
        event = renderer.render_probe(
            width=64,
            height=48,
            force_warp=True,
            draw_glyph=False,
            rgba=(51, 102, 204, 128),
        )
        with SharedFrameRingReader.from_event(event) as reader:
            image = reader.read_qimage(event)

    assert image.size().width() == 64
    assert image.size().height() == 48
    assert image.pixelColor(0, 0).getRgb() == (0, 0, 0, 0)
    assert all(
        abs(actual - expected) <= 1
        for actual, expected in zip(image.pixelColor(16, 24).getRgb(), (51, 102, 204, 128))
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_preview_uses_native_premultiplied_bgra_without_checksum(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=10.0) as renderer:
        renderer.configure_gpu(
            _g1_track(),
            _g1_style(),
            width=320,
            height=180,
            fps=60,
            force_warp=True,
        )
        event = renderer.render_gpu_frame(
            750,
            force_warp=True,
            include_checksum=False,
        )
        with SharedFrameRingReader.from_event(event) as reader:
            image = reader.read_qimage(event)

    assert event["pixel_format"] == "bgra8888_premultiplied"
    assert "checksum" not in event
    assert image.format() == QImage.Format.Format_ARGB32_Premultiplied
    assert image.width() == 320
    assert image.height() == 180


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_probe_1000_frames_reuses_device_and_shared_ring(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    shm_key = f"krok_gpu_pytest_{os.getpid()}_{uuid.uuid4().hex}"
    checksums: set[str] = set()
    with NativeRendererProcess(_renderer_path(), response_timeout_s=10.0) as renderer:
        info = renderer.backend_info(force_warp=True)
        assert info["available"] is True
        reader: SharedFrameRingReader | None = None
        try:
            for frame_index in range(1000):
                event = renderer.render_probe(
                    width=64,
                    height=48,
                    force_warp=True,
                    draw_glyph=True,
                    generation=7,
                    frame_index=frame_index,
                    shm_key=shm_key,
                )
                if reader is None:
                    reader = SharedFrameRingReader.from_event(event)
                    reader.attach()
                slot = reader.read_frame(event)
                assert slot.frame_index == frame_index
                checksums.add(str(event["checksum"]))
        finally:
            if reader is not None:
                reader.close()
    assert len(checksums) == 1


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_directwrite_wipe_progresses_monotonically(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured, frames = _render_g1_frames(
            renderer,
            _g1_style(),
            (0, 750, 1500),
            force_warp=True,
        )

    assert configured["event"] == "gpu_configured"
    assert configured["backend"] == "direct2d"
    assert configured["line_count"] == 1
    red_counts = [
        sum(
            1
            for index in range(0, len(payload), 4)
            if payload[index] > 180
            and payload[index + 1] < 100
            and payload[index + 3] > 0
        )
        for payload in frames
    ]
    assert red_counts[0] < red_counts[1] < red_counts[2]
    assert len({_alpha_count(payload) for payload in frames}) == 1


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_main_ruby_progress_modes_follow_painter_and_preserve_empty_pause(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("M", 0),
                    TimingChar("M", 500),
                    TimingChar("M", 1_000),
                    TimingChar("M", 1_500),
                ],
                end_ms=2_000,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="MMMM",
                reading="ab",
                reading_parts=["a", "", "b"],
                reading_part_ms=[100, 1_900],
                pos_start_ms=0,
                pos_end_ms=2_000,
            )
        ],
    )
    before = KaraokeColorState(text=PaintFill(mode="solid", color="#FFFF2020"))
    after = KaraokeColorState(text=PaintFill(mode="solid", color="#FF2040FF"))
    transparent = KaraokeColorState(
        text=PaintFill(mode="solid", color="#00000000"),
        stroke=PaintFill(mode="solid", color="#00000000"),
        stroke2=PaintFill(mode="solid", color="#00000000"),
        shadow=PaintFill(mode="solid", color="#00000000"),
    )
    base_style = _g1_style(
        font_family="Times New Roman",
        font_family_latin="Times New Roman",
        font_size_px=96,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        karaoke_colors=KaraokeColors(before=before, after=after),
        ruby_font_family="Times New Roman",
        ruby_font_family_latin="Times New Roman",
        ruby_font_follow_main=False,
        ruby_font_size_px=24,
        ruby_stroke_width_px=0,
        ruby_stroke2_enabled=False,
        ruby_decoration_kind="none",
        ruby_karaoke_colors=KaraokeColors(before=transparent, after=transparent),
    )

    def after_ratio(payload: bytes, full: bytes) -> float:
        def count(frame: bytes) -> int:
            return sum(
                1
                for index in range(0, len(frame), 4)
                if frame[index + 2] > frame[index] + 40
                and frame[index + 2] > frame[index + 1] + 20
                and frame[index + 3] > 16
            )

        return count(payload) / max(count(full), 1)

    gpu_ratios: dict[str, float] = {}
    painter_ratios: dict[str, float] = {}
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        for mode in ("checkpoint_segments", "reading_units"):
            style = replace(base_style, ruby_main_progress_mode=mode)
            _, gpu = _render_g1_frames(
                renderer, style, (300, 2_000), force_warp=True, track=track
            )
            painter = [
                _render_painter_oracle(style, t_ms=t_ms, track=track)
                for t_ms in (300, 2_000)
            ]
            gpu_ratios[mode] = after_ratio(gpu[0], gpu[1])
            painter_ratios[mode] = after_ratio(painter[0], painter[1])

    for mode in ("checkpoint_segments", "reading_units"):
        assert abs(gpu_ratios[mode] - painter_ratios[mode]) <= 0.08, (
            mode, gpu_ratios, painter_ratios
        )
    assert gpu_ratios["reading_units"] > gpu_ratios["checkpoint_segments"] + 0.08

    explicit_track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("M", 6_220, explicit_start=True, explicit_end=True),
                    TimingChar("M", 6_470, explicit_start=True, explicit_end=True),
                    TimingChar("M", 6_740, explicit_start=True, explicit_end=True),
                    TimingChar("M", 6_920, explicit_start=True),
                    TimingChar("M", 7_265, explicit_end=True),
                ],
                end_ms=7_610,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="MMMMM",
                reading="melody",
                reading_parts=["me", "lo", "d", "y"],
                reading_part_ms=[250, 520, 700],
                pos_start_ms=6_220,
                pos_end_ms=7_610,
            )
        ],
    )
    timestamps = (6_740, 6_920, 7_610)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        for mode in ("checkpoint_segments", "reading_units"):
            explicit_style = replace(base_style, ruby_main_progress_mode=mode)
            _, explicit_gpu_frames = _render_g1_frames(
                renderer,
                explicit_style,
                timestamps,
                force_warp=True,
                track=explicit_track,
            )
            explicit_painter_frames = [
                _render_painter_oracle(
                    explicit_style, t_ms=t_ms, track=explicit_track
                )
                for t_ms in timestamps
            ]
            explicit_gpu_ratios = [
                after_ratio(frame, explicit_gpu_frames[-1])
                for frame in explicit_gpu_frames[:-1]
            ]
            explicit_painter_ratios = [
                after_ratio(frame, explicit_painter_frames[-1])
                for frame in explicit_painter_frames[:-1]
            ]

            assert all(
                abs(gpu - painter) <= 0.08
                for gpu, painter in zip(
                    explicit_gpu_ratios, explicit_painter_ratios
                )
            ), (mode, explicit_gpu_ratios, explicit_painter_ratios)
            assert 0.30 <= explicit_gpu_ratios[0] <= 0.50
            assert 0.50 <= explicit_gpu_ratios[1] <= 0.70


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_overlapping_char_wipe_hands_off_at_following_draw_left(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("W", 0), TimingChar("W", 1_000)],
                end_ms=2_000,
                display_end_override_ms=2_500,
            )
        ]
    )
    before = KaraokeColorState(text=PaintFill(mode="solid", color="#FFFF2020"))
    after = KaraokeColorState(text=PaintFill(mode="solid", color="#FF2040FF"))
    style = _g1_style(
        font_family="Times New Roman",
        font_family_latin="Times New Roman",
        font_size_px=120,
        letter_spacing_px=-55,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        karaoke_colors=KaraokeColors(before=before, after=after),
    )
    timestamps = (1_000, 1_001, 2_000)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    def after_count(payload: bytes) -> int:
        return sum(
            1
            for index in range(0, len(payload), 4)
            if payload[index + 2] > payload[index] + 40
            and payload[index + 2] > payload[index + 1] + 20
            and payload[index + 3] > 16
        )

    gpu_full = after_count(gpu[-1])
    painter_full = after_count(painter[-1])
    assert gpu_full > 0 and painter_full > 0
    for gpu_frame, painter_frame in zip(gpu[:-1], painter[:-1]):
        assert abs(
            after_count(gpu_frame) / gpu_full
            - after_count(painter_frame) / painter_full
        ) <= 0.05
    # The exact hand-off frame retains the first character's adjusted end;
    # the following scanline starts moving on the next sample.
    assert after_count(gpu[0]) <= after_count(gpu[1])
    assert after_count(painter[0]) <= after_count(painter[1])


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_tight_overlap_uses_n3_phase_z_order(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("W", 1_000, role_label="left"),
                    TimingChar("W", 2_000, role_label="right"),
                ],
                end_ms=3_000,
                display_start_override_ms=0,
                display_end_override_ms=4_000,
            )
        ]
    )

    def scheme(color: str) -> SubtitleStyleScheme:
        state = KaraokeColorState(
            text=PaintFill(mode="solid", color=color),
            stroke=PaintFill(mode="solid", color=color),
        )
        return SubtitleStyleScheme(
            font_family="Times New Roman",
            font_family_latin="Times New Roman",
            font_size_px=120,
            stroke_width_px=9,
            decoration_kind="none",
            karaoke_colors=KaraokeColors(before=state, after=state),
        )

    transparent = scheme("#00000000")
    base = _g1_style(
        font_family="Times New Roman",
        font_family_latin="Times New Roman",
        font_size_px=120,
        letter_spacing_px=-80,
        stroke_width_px=9,
        stroke2_enabled=False,
        decoration_kind="none",
        line_horizontal_layout="left",
        horizontal_margin_px=180,
        custom_style_schemes={
            "left": scheme("#FFFF2020"),
            "right": scheme("#FF2040FF"),
        },
    )
    variants = (
        base,
        replace(
            base,
            custom_style_schemes={"left": scheme("#FFFFFFFF"), "right": transparent},
        ),
        replace(
            base,
            custom_style_schemes={"left": transparent, "right": scheme("#FFFFFFFF")},
        ),
    )
    rendered: list[list[bytes]] = []
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        for style in variants:
            _, frames = _render_g1_frames(
                renderer, style, (0, 3_000), force_warp=True, track=track
            )
            rendered.append(frames)

    overlap = [
        index
        for index in range(0, len(rendered[1][0]), 4)
        if rendered[1][0][index + 3] > 160 and rendered[2][0][index + 3] > 160
    ]
    assert overlap

    def ownership(payload: bytes) -> tuple[int, int]:
        left = sum(payload[index] > payload[index + 2] + 30 for index in overlap)
        right = sum(payload[index + 2] > payload[index] + 30 for index in overlap)
        return left, right

    before_left, before_right = ownership(rendered[0][0])
    after_left, after_right = ownership(rendered[0][1])
    assert before_left > before_right
    assert after_right > after_left


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("decoration_kind", ["none", "shadow", "glow"])
def test_gpu_completed_main_wipe_releases_outer_layers(
    monkeypatch, decoration_kind: str
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    before = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFF2020"),
        stroke=PaintFill(mode="solid", color="#FFFF2020"),
        stroke2=PaintFill(mode="solid", color="#FFFF2020"),
        shadow=PaintFill(mode="solid", color="#FFFF2020"),
    )
    after = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FF2040FF"),
        stroke=PaintFill(mode="solid", color="#FF2040FF"),
        stroke2=PaintFill(mode="solid", color="#FF2040FF"),
        shadow=PaintFill(mode="solid", color="#FF2040FF"),
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("\u5186", 0)],
                end_ms=1_000,
                display_end_override_ms=2_000,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=120,
        stroke_width_px=7,
        stroke2_enabled=True,
        stroke2_width_px=7,
        decoration_kind=decoration_kind,
        shadow_offset_x=8,
        shadow_offset_y=7,
        glow_before_radius_px=8,
        glow_after_radius_px=8,
        karaoke_colors=KaraokeColors(before=before, after=after),
        line_tail_ms=1_000,
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer,
            style,
            (1_200,),
            force_warp=True,
            track=track,
        )

    payload = frames[0]
    before_pixels = sum(
        payload[index] > payload[index + 2] + 40
        and payload[index] > payload[index + 1] + 40
        and payload[index + 3] > 16
        for index in range(0, len(payload), 4)
    )
    after_pixels = sum(
        payload[index + 2] > payload[index] + 40
        and payload[index + 2] > payload[index + 1] + 20
        and payload[index + 3] > 16
        for index in range(0, len(payload), 4)
    )
    assert after_pixels > 0
    assert before_pixels == 0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_utopia_keeps_finished_char_body_while_next_char_wipes(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    before = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFF2020"),
        stroke=PaintFill(mode="solid", color="#FFFF2020"),
        stroke2=PaintFill(mode="solid", color="#FFFF2020"),
        shadow=PaintFill(mode="solid", color="#FFFF2020"),
    )
    after = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FF2040FF"),
        stroke=PaintFill(mode="solid", color="#FF2040FF"),
        stroke2=PaintFill(mode="solid", color="#FF2040FF"),
        shadow=PaintFill(mode="solid", color="#FF2040FF"),
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("円", 0), TimingChar("円", 1_000)],
                end_ms=2_000,
                display_start_override_ms=0,
                display_end_override_ms=4_000,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=120,
        stroke_width_px=7,
        stroke2_enabled=True,
        stroke2_width_px=7,
        decoration_kind="none",
        karaoke_colors=KaraokeColors(before=before, after=after),
        entry_anim="utopia",
        exit_anim="utopia",
        line_tail_ms=1_000,
    )

    # 1500ms sits in the wipe delegation window: the first character finished
    # at 1000ms while the second keeps wiping until 2000ms, so the first
    # character's after clip follows the second character's edge. Its own
    # solid body must stay visible instead of being clipped down to the
    # following character's bounds.
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer, style, (1_500,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=1_500, track=track)

    # x < 280 covers only the first character's glyph; the second character
    # (and its utopia entry transform) stays right of it. With the delegation
    # bug the region is empty because the after clip collapses onto the
    # second character's bounds.
    def solid_after_first_char(payload: bytes) -> int:
        count = 0
        for y in range(360):
            row = y * 640 * 4
            for x in range(280):
                offset = row + x * 4
                if (
                    payload[offset + 3] > 200
                    and payload[offset + 2] > payload[offset] + 40
                ):
                    count += 1
        return count

    gpu_solid = solid_after_first_char(frames[0])
    painter_solid = solid_after_first_char(painter)
    assert painter_solid > 1_000
    # Lower bound only: GPU and Painter currently disagree on the utopia
    # glyph scale by ~1.2x, which is a separate parity gap. The delegation
    # bug drops this to ~0.
    assert gpu_solid > painter_solid * 0.5


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("decoration_kind", ["none", "shadow", "glow"])
def test_gpu_utopia_releases_each_completed_character_wipe(
    monkeypatch, decoration_kind: str
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    before = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFF2020"),
        stroke=PaintFill(mode="solid", color="#FFFF2020"),
        stroke2=PaintFill(mode="solid", color="#FFFF2020"),
        shadow=PaintFill(mode="solid", color="#FFFF2020"),
    )
    after = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FF2040FF"),
        stroke=PaintFill(mode="solid", color="#FF2040FF"),
        stroke2=PaintFill(mode="solid", color="#FF2040FF"),
        shadow=PaintFill(mode="solid", color="#FF2040FF"),
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("\u5186", 0),
                    TimingChar("            ", 1_000),
                    TimingChar("\u5186", 2_000),
                ],
                end_ms=3_000,
                display_start_override_ms=0,
                display_end_override_ms=4_000,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=120,
        stroke_width_px=7,
        stroke2_enabled=True,
        stroke2_width_px=7,
        decoration_kind=decoration_kind,
        shadow_offset_x=8,
        shadow_offset_y=7,
        glow_before_radius_px=8,
        glow_after_radius_px=8,
        karaoke_colors=KaraokeColors(before=before, after=after),
        entry_anim="utopia",
        exit_anim="utopia",
        line_tail_ms=1_000,
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer,
            style,
            (1_200,),
            force_warp=True,
            track=track,
        )

    payload = frames[0]
    left_half = [
        payload[y * 640 * 4 + x * 4 : y * 640 * 4 + x * 4 + 4]
        for y in range(360)
        for x in range(320)
    ]
    before_pixels = sum(
        pixel[0] > pixel[2] + 40
        and pixel[0] > pixel[1] + 40
        and pixel[3] > 16
        for pixel in left_half
    )
    after_pixels = sum(
        pixel[2] > pixel[0] + 40
        and pixel[2] > pixel[1] + 20
        and pixel[3] > 16
        for pixel in left_half
    )
    assert after_pixels > 0
    assert before_pixels == 0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("decoration_kind", ["none", "shadow", "glow"])
def test_gpu_completed_ruby_wipe_releases_outer_layers(
    monkeypatch, decoration_kind: str
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    transparent = KaraokeColorState(
        text=PaintFill(mode="solid", color="#00000000"),
        stroke=PaintFill(mode="solid", color="#00000000"),
        stroke2=PaintFill(mode="solid", color="#00000000"),
        shadow=PaintFill(mode="solid", color="#00000000"),
    )
    before = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFF2020"),
        stroke=PaintFill(mode="solid", color="#FFFF2020"),
        stroke2=PaintFill(mode="solid", color="#FFFF2020"),
        shadow=PaintFill(mode="solid", color="#FFFF2020"),
    )
    after = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FF2040FF"),
        stroke=PaintFill(mode="solid", color="#FF2040FF"),
        stroke2=PaintFill(mode="solid", color="#FF2040FF"),
        shadow=PaintFill(mode="solid", color="#FF2040FF"),
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("\u6f22", 0), TimingChar("\u5b57", 1_000)],
                end_ms=2_000,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="\u6f22\u5b57",
                reading="\u304d\u3083\u304b\u3099",
                reading_parts=["\u304d\u3083", "", "\u304b\u3099"],
                reading_part_ms=[650, 1_300],
                pos_start_ms=0,
                pos_end_ms=2_000,
            )
        ],
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=64,
        dual_line_layout=False,
        line_y_position="center",
        line_horizontal_layout="center",
        line_y_margin_px=22,
        line_lead_in_ms=0,
        line_tail_ms=0,
        karaoke_colors=KaraokeColors(before=transparent, after=transparent),
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=28,
        ruby_gap_px=4,
        ruby_stroke_width_px=5,
        ruby_stroke2_enabled=True,
        ruby_stroke2_width_px=5,
        ruby_decoration_kind=decoration_kind,
        ruby_shadow_offset_x=6,
        ruby_shadow_offset_y=5,
        ruby_glow_before_radius_px=6,
        ruby_glow_after_radius_px=6,
        ruby_karaoke_colors=KaraokeColors(before=before, after=after),
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer,
            style,
            (2_000,),
            force_warp=True,
            track=track,
        )

    payload = frames[0]
    before_pixels = sum(
        payload[index] > payload[index + 2] + 40
        and payload[index] > payload[index + 1] + 40
        and payload[index + 3] > 16
        for index in range(0, len(payload), 4)
    )
    after_pixels = sum(
        payload[index + 2] > payload[index] + 40
        and payload[index + 2] > payload[index + 1] + 20
        and payload[index + 3] > 16
        for index in range(0, len(payload), 4)
    )
    assert after_pixels > 0
    assert before_pixels == 0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("decoration_kind", ["none", "shadow", "glow"])
def test_gpu_utopia_releases_each_completed_ruby_unit_wipe(
    monkeypatch, decoration_kind: str
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    transparent = KaraokeColorState(
        text=PaintFill(mode="solid", color="#00000000"),
        stroke=PaintFill(mode="solid", color="#00000000"),
        stroke2=PaintFill(mode="solid", color="#00000000"),
        shadow=PaintFill(mode="solid", color="#00000000"),
    )
    before = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFF2020"),
        stroke=PaintFill(mode="solid", color="#FFFF2020"),
        stroke2=PaintFill(mode="solid", color="#FFFF2020"),
        shadow=PaintFill(mode="solid", color="#FFFF2020"),
    )
    after = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FF2040FF"),
        stroke=PaintFill(mode="solid", color="#FF2040FF"),
        stroke2=PaintFill(mode="solid", color="#FF2040FF"),
        shadow=PaintFill(mode="solid", color="#FF2040FF"),
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("A", 1_200),
                    TimingChar("\u6f22", 1_700),
                    TimingChar("\u5b57", 2_200),
                    TimingChar("B", 2_700),
                ],
                end_ms=3_200,
                display_start_override_ms=0,
                display_end_override_ms=4_400,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="\u6f22\u5b57",
                reading="\u304b\u306a",
                reading_parts=["\u304b", "\u306a"],
                reading_part_ms=[500],
                pos_start_ms=1_700,
                pos_end_ms=2_700,
            )
        ],
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=72,
        karaoke_colors=KaraokeColors(before=transparent, after=transparent),
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=64,
        ruby_stroke_width_px=7,
        ruby_stroke2_enabled=True,
        ruby_stroke2_width_px=7,
        ruby_decoration_kind=decoration_kind,
        ruby_shadow_offset_x=8,
        ruby_shadow_offset_y=7,
        ruby_glow_before_radius_px=8,
        ruby_glow_after_radius_px=8,
        ruby_karaoke_colors=KaraokeColors(before=before, after=after),
        entry_anim="utopia",
        exit_anim="utopia",
        line_tail_ms=1_000,
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer,
            style,
            (2_300,),
            force_warp=True,
            track=track,
        )

    payload = frames[0]
    left_half = [
        payload[y * 640 * 4 + x * 4 : y * 640 * 4 + x * 4 + 4]
        for y in range(360)
        for x in range(320)
    ]
    before_pixels = sum(
        pixel[0] > pixel[2] + 40
        and pixel[0] > pixel[1] + 40
        and pixel[3] > 16
        for pixel in left_half
    )
    after_pixels = sum(
        pixel[2] > pixel[0] + 40
        and pixel[2] > pixel[1] + 20
        and pixel[3] > 16
        for pixel in left_half
    )
    assert after_pixels > 0
    assert before_pixels == 0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_layers_outer_stroke_stroke_and_body(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, body = _render_g1_frames(
            renderer,
            _g1_style(stroke_width_px=0, stroke2_enabled=False, stroke2_width_px=0),
            (750,),
            force_warp=True,
        )
        _, stroke = _render_g1_frames(
            renderer,
            _g1_style(stroke_width_px=5, stroke2_enabled=False, stroke2_width_px=0),
            (750,),
            force_warp=True,
        )
        _, stroke2 = _render_g1_frames(
            renderer,
            _g1_style(stroke_width_px=5, stroke2_enabled=True, stroke2_width_px=5),
            (750,),
            force_warp=True,
        )

    assert _alpha_count(body[0]) < _alpha_count(stroke[0]) < _alpha_count(stroke2[0])


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_repeated_configure_hits_geometry_layout_cache(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        first = renderer.configure_gpu(
            _g1_track(),
            _g1_style(),
            width=640,
            height=360,
            fps=60,
            force_warp=True,
        )
        second = renderer.configure_gpu(
            _g1_track(),
            _g1_style(),
            width=640,
            height=360,
            fps=60,
            force_warp=True,
        )

    assert first["cache_hits"] == 0
    assert first["cache_misses"] == 1
    assert second["cache_hits"] == 1
    assert second["cache_misses"] == 1
    assert second["cached_lines"] == 1
    assert second["cached_chars"] == 3
    assert second["cached_geometries"] >= 3
    assert second["estimated_cache_bytes"] > 0
    assert second["configure_ms"] < first["configure_ms"]


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_preview_worker_pool_bounds_in_flight_and_tags_out_of_order_frames(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    shm_key = f"test-gpu-preview-pool-{uuid.uuid4().hex}"
    with NativeRendererProcess(_renderer_path(), response_timeout_s=30.0) as renderer:
        configured = renderer.configure_gpu(
            _g1_track(),
            _g1_style(),
            width=640,
            height=360,
            fps=60,
            worker_count=2,
        )
        assert configured["worker_count"] == 2
        for frame_index, t_ms in enumerate((500, 1000)):
            renderer.begin_render_gpu_frame(
                t_ms,
                generation=7,
                frame_index=frame_index,
                request_serial=100 + frame_index,
                shm_key=shm_key,
                slot_count=2,
                include_checksum=False,
            )
        events = [
            renderer.finish_render_gpu_frame(),
            renderer.finish_render_gpu_frame(),
        ]
        diagnostics = renderer.gpu_diagnostics()
        pooled_frames: dict[int, bytes] = {}
        for event in events:
            with SharedFrameRingReader.from_event(event) as reader:
                pooled_frames[int(event["t_ms"])] = bytes(reader.read_frame(event).payload)

        renderer.configure_gpu(
            _g1_track(),
            _g1_style(),
            width=640,
            height=360,
            fps=60,
            worker_count=1,
        )
        serial_frames: dict[int, bytes] = {}
        for frame_index, t_ms in enumerate((500, 1000)):
            event = renderer.render_gpu_frame(
                t_ms,
                generation=8,
                frame_index=frame_index,
                shm_key=f"{shm_key}-serial",
                include_checksum=False,
            )
            with SharedFrameRingReader.from_event(event) as reader:
                serial_frames[t_ms] = bytes(reader.read_frame(event).payload)

    assert {event["request_serial"] for event in events} == {100, 101}
    assert {event["frame_index"] for event in events} == {0, 1}
    assert all(event["generation"] == 7 for event in events)
    assert all(event["worker_index"] in {0, 1} for event in events)
    assert diagnostics["worker_count"] == 2
    assert diagnostics["in_flight"] == 0
    assert diagnostics["max_in_flight"] <= 2
    assert pooled_frames == serial_frames


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_glow_scratch_reuse_is_worker_history_independent(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar(char, index * 100)
                    for index, char in enumerate("WIDE GLOW SOURCE")
                ],
                end_ms=700,
                display_start_override_ms=0,
                display_end_override_ms=750,
            ),
            TimingLine(
                chars=[TimingChar("I", 800), TimingChar("I", 1_100)],
                end_ms=1_400,
                display_start_override_ms=800,
                display_end_override_ms=1_600,
            ),
        ]
    )
    style = _g1_style(
        decoration_kind="glow",
        glow_radius_px=18,
        line_horizontal_layout="right",
    )
    times = (500, 1_000)
    shm_key = f"test-gpu-glow-history-{uuid.uuid4().hex}"

    with NativeRendererProcess(_renderer_path(), response_timeout_s=30.0) as renderer:
        renderer.configure_gpu(
            track, style, width=640, height=360, fps=60, worker_count=1,
            realization_enabled=False,
        )
        serial = {}
        for frame_index, t_ms in enumerate(times):
            event = renderer.render_gpu_frame(
                t_ms,
                frame_index=frame_index,
                shm_key=f"{shm_key}-serial",
                include_checksum=False,
            )
            with SharedFrameRingReader.from_event(event) as reader:
                serial[t_ms] = bytes(reader.read_frame(event).payload)

        renderer.configure_gpu(
            track, style, width=640, height=360, fps=60, worker_count=2,
            realization_enabled=False,
        )
        for frame_index, t_ms in enumerate(times):
            renderer.begin_render_gpu_frame(
                t_ms,
                frame_index=frame_index,
                shm_key=f"{shm_key}-pooled",
                slot_count=2,
                include_checksum=False,
            )
        pooled = {}
        for _ in times:
            event = renderer.finish_render_gpu_frame()
            with SharedFrameRingReader.from_event(event) as reader:
                pooled[int(event["t_ms"])] = bytes(reader.read_frame(event).payload)

    assert pooled == serial


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_diagnostics_report_cache_and_dxgi_memory_without_rendering(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured = renderer.configure_gpu(
            _g1_track(),
            _g1_style(),
            width=640,
            height=360,
            fps=60,
            force_warp=True,
        )
        diagnostics = renderer.gpu_diagnostics(force_warp=True)

    assert diagnostics["event"] == "gpu_diagnostics"
    assert diagnostics["cache_hits"] == configured["cache_hits"]
    assert diagnostics["cache_misses"] == configured["cache_misses"]
    assert diagnostics["estimated_cache_bytes"] == configured["estimated_cache_bytes"]
    assert diagnostics["video_memory_info_available"] is True
    for segment in ("local", "non_local"):
        usage = diagnostics[f"{segment}_video_memory_usage_bytes"]
        budget = diagnostics[f"{segment}_video_memory_budget_bytes"]
        assert usage >= 0
        assert budget >= usage


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_frame_diagnostics_expose_resource_timing_and_glow_counters(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        decoration_kind="glow",
        glow_radius_px=10,
        glow_before_radius_px=10,
        glow_after_radius_px=10,
        glow_concentration_level=1,
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured = renderer.configure_gpu(
            _g1_track(), style, width=640, height=360, fps=60, force_warp=True
        )
        event = renderer.render_gpu_frame(750, force_warp=True)
        diagnostics = renderer.gpu_diagnostics(force_warp=True)

    assert configured["counters_enabled"] is True
    assert configured["geometry_created_stable"] > 0
    assert event["counters_enabled"] is True
    assert event["brush_created"] > 0
    assert event["geometry_created_stable"] == 0
    assert event["geometry_created_dynamic"] == 0
    assert event["stroke_draw"] > 0
    assert event["stroke2_draw"] > 0
    assert event["glow_source_area_px"] > 0
    assert event["layer_push"] > 0
    for field in (
        "animation_layout_ms",
        "geometry_ms",
        "stroke_ms",
        "glow_ms",
        "end_draw_wait_ms",
        "end_draw_glow_source_ms",
        "end_draw_ruby_glow_source_ms",
        "end_draw_inline_glow_source_ms",
        "end_draw_frame_layers_ms",
        "end_draw_empty_frame_ms",
        "gpu_wait_ms",
        "readback_copy_ms",
        "shm_copy_ms",
    ):
        assert event[field] >= 0.0
    assert event["end_draw_count"] == sum(
        event[field]
        for field in (
            "end_draw_glow_source_count",
            "end_draw_ruby_glow_source_count",
            "end_draw_inline_glow_source_count",
            "end_draw_frame_layers_count",
            "end_draw_empty_frame_count",
        )
    )
    assert event["end_draw_wait_ms"] == pytest.approx(
        sum(
            event[field]
            for field in (
                "end_draw_glow_source_ms",
                "end_draw_ruby_glow_source_ms",
                "end_draw_inline_glow_source_ms",
                "end_draw_frame_layers_ms",
                "end_draw_empty_frame_ms",
            )
        )
    )
    assert diagnostics["frames_rendered"] == 1
    assert diagnostics["brush_created"] == event["brush_created"]
    assert diagnostics["glow_source_area_px"] == event["glow_source_area_px"]


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_frame_counters_can_be_disabled_without_disabling_timing(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("KROK_GPU_COUNTERS", "0")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured = renderer.configure_gpu(
            _g1_track(), _g1_style(), width=640, height=360, fps=60, force_warp=True
        )
        event = renderer.render_gpu_frame(750, force_warp=True)
        diagnostics = renderer.gpu_diagnostics(force_warp=True)

    assert configured["counters_enabled"] is False
    assert event["counters_enabled"] is False
    assert diagnostics["counters_enabled"] is False
    for field in (
        "brush_created",
        "geometry_created_stable",
        "geometry_created_dynamic",
        "stroke_draw",
        "stroke2_draw",
        "glow_source_area_px",
        "layer_push",
    ):
        assert event[field] == 0
        assert diagnostics[field] == 0
    assert event["render_ms"] >= 0.0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_brush_cache_eliminates_steady_frame_resource_creation(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.delenv("KROK_GPU_RESOURCE_CACHE", raising=False)
    style = _g1_style(
        decoration_kind="glow",
        glow_before_radius_px=8,
        glow_after_radius_px=8,
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        renderer.configure_gpu(
            _g1_track(), style, width=640, height=360, fps=60, force_warp=True
        )
        warmup = renderer.render_gpu_frame(750, force_warp=True)
        steady = renderer.render_gpu_frame(750, force_warp=True, frame_index=1)
        diagnostics = renderer.gpu_diagnostics(force_warp=True)

    assert diagnostics["resource_cache_enabled"] is True
    assert warmup["brush_created"] > 0
    assert steady["brush_created"] == 0
    assert steady["geometry_created_stable"] == 0
    assert diagnostics["brush_cache_hits"] > 0
    assert diagnostics["brush_cache_misses"] == warmup["brush_created"]
    assert 0 < diagnostics["brush_cache_size"] <= diagnostics["brush_cache_capacity"]
    assert diagnostics["brush_cache_evictions"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_brush_cache_can_be_disabled_for_ab_comparison(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("KROK_GPU_RESOURCE_CACHE", "0")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        renderer.configure_gpu(
            _g1_track(), _g1_style(), width=640, height=360, fps=60, force_warp=True
        )
        first = renderer.render_gpu_frame(750, force_warp=True)
        second = renderer.render_gpu_frame(750, force_warp=True, frame_index=1)
        diagnostics = renderer.gpu_diagnostics(force_warp=True)

    assert diagnostics["resource_cache_enabled"] is False
    assert first["brush_created"] > 0
    assert second["brush_created"] == first["brush_created"]
    assert diagnostics["brush_cache_size"] == 0
    assert diagnostics["brush_cache_hits"] == 0
    assert diagnostics["brush_cache_misses"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize(
    "changed_style",
    [
        _g1_style(font_size_px=108),
        _g1_style(font_family="Meiryo"),
        _g1_style(stroke_width_px=14),
        _g1_style(fill_color="#12AB34"),
    ],
    ids=("size", "font", "stroke", "color"),
)
def test_gpu_brush_cache_invalidates_on_style_changes(monkeypatch, changed_style) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        renderer.configure_gpu(
            _g1_track(), _g1_style(), width=640, height=360, fps=60, force_warp=True
        )
        renderer.render_gpu_frame(750, force_warp=True)
        before = renderer.gpu_diagnostics(force_warp=True)
        renderer.configure_gpu(
            _g1_track(), changed_style, width=640, height=360, fps=60,
            force_warp=True,
        )
        after_configure = renderer.gpu_diagnostics(force_warp=True)
        changed = renderer.render_gpu_frame(750, force_warp=True, frame_index=1)

    assert before["brush_cache_size"] > 0
    assert after_configure["brush_cache_size"] == 0
    assert after_configure["brush_cache_invalidations"] == (
        before["brush_cache_invalidations"] + 1
    )
    assert changed["brush_created"] > 0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("resource_kind", ("gradient", "image", "role"))
def test_gpu_brush_cache_invalidates_paint_and_role_resources(
    monkeypatch, tmp_path, resource_kind
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    def state(fill: PaintFill) -> KaraokeColorState:
        return KaraokeColorState(
            text=fill,
            stroke=PaintFill(mode="solid", color="#202020"),
            stroke2=PaintFill(mode="solid", color="#000000"),
            shadow=PaintFill(mode="solid", color="#000000"),
        )

    track = _g1_track()
    if resource_kind == "gradient":
        first_fill = PaintFill(
            mode="gradient_horizontal",
            gradient_stops=[(0, "#FF0000"), (100, "#0000FF")],
        )
        second_fill = replace(
            first_fill,
            gradient_stops=[(0, "#00FF00"), (100, "#FF00FF")],
        )
        first = _g1_style(
            karaoke_colors=KaraokeColors(
                before=state(first_fill), after=state(first_fill)
            )
        )
        second = replace(
            first,
            karaoke_colors=KaraokeColors(
                before=state(second_fill), after=state(second_fill)
            ),
        )
    elif resource_kind == "image":
        paths = [tmp_path / "red.png", tmp_path / "blue.png"]
        for path, color in zip(paths, ("#FFFF0000", "#FF0000FF")):
            image = QImage(8, 8, QImage.Format.Format_ARGB32)
            image.fill(QColor(color))
            assert image.save(str(path))
        fills = [PaintFill(mode="image", image_path=str(path)) for path in paths]
        first = _g1_style(
            karaoke_colors=KaraokeColors(
                before=state(fills[0]), after=state(fills[0])
            )
        )
        second = replace(
            first,
            karaoke_colors=KaraokeColors(
                before=state(fills[1]), after=state(fills[1])
            ),
        )
    else:
        track = TimingTrack(
            lines=[
                TimingLine(
                    chars=[
                        TimingChar("角", 0, role_label="角色"),
                        TimingChar("色", 750, role_label="角色"),
                    ],
                    end_ms=1500,
                )
            ]
        )
        first_scheme = SubtitleStyleScheme(fill_color="#FF0000")
        second_scheme = replace(first_scheme, fill_color="#00FF00")
        first = _g1_style(custom_style_schemes={"角色": first_scheme})
        second = replace(first, custom_style_schemes={"角色": second_scheme})

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        renderer.configure_gpu(
            track, first, width=640, height=360, fps=60, force_warp=True
        )
        first_frame = renderer.render_gpu_frame(750, force_warp=True)
        before = renderer.gpu_diagnostics(force_warp=True)
        renderer.configure_gpu(
            track, second, width=640, height=360, fps=60, force_warp=True
        )
        after_configure = renderer.gpu_diagnostics(force_warp=True)
        second_frame = renderer.render_gpu_frame(
            750, force_warp=True, frame_index=1
        )

    assert before["brush_cache_size"] > 0
    assert after_configure["brush_cache_size"] == 0
    assert after_configure["brush_cache_invalidations"] == (
        before["brush_cache_invalidations"] + 1
    )
    assert second_frame["brush_created"] > 0
    assert second_frame["checksum"] != first_frame["checksum"]


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_brush_cache_is_bounded_and_reports_evictions(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    role_count = 520
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("A", index * 2, role_label=f"role-{index}")
                    for index in range(role_count)
                ],
                end_ms=role_count * 2 + 1_000,
            )
        ]
    )
    schemes = {
        f"role-{index}": SubtitleStyleScheme(fill_color=f"#{index + 1:06X}")
        for index in range(role_count)
    }
    style = _g1_style(
        font_size_px=8,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        custom_style_schemes=schemes,
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=30.0) as renderer:
        renderer.configure_gpu(
            track, style, width=640, height=360, fps=60, force_warp=True
        )
        for frame_index, t_ms in enumerate(range(0, role_count * 2 + 1, 40)):
            renderer.render_gpu_frame(
                t_ms, force_warp=True, frame_index=frame_index
            )
        diagnostics = renderer.gpu_diagnostics(force_warp=True)

    assert diagnostics["brush_cache_size"] == diagnostics["brush_cache_capacity"]
    assert diagnostics["brush_cache_capacity"] == 512
    assert diagnostics["brush_cache_evictions"] > 0


def _wait_for_realization_prewarm(
    renderer: NativeRendererProcess,
    *,
    force_warp: bool = False,
    timeout_s: float = 10.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    diagnostics = renderer.gpu_diagnostics(force_warp=force_warp)
    while (
        not diagnostics.get("realization_prewarm_complete", True)
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
        diagnostics = renderer.gpu_diagnostics(force_warp=force_warp)
    assert diagnostics["realization_prewarm_complete"] is True
    return diagnostics


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_realization_prewarms_off_frame_and_hits_on_steady_frame(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.delenv("KROK_GPU_REALIZATION", raising=False)
    style = _g1_style(
        stroke_width_px=14,
        stroke2_width_px=7,
        decoration_kind="none",
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured = renderer.configure_gpu(
            _g1_track(), style, width=640, height=360, fps=60,
            force_warp=False, prewarm_t_ms=750,
        )
        diagnostics = _wait_for_realization_prewarm(renderer)
        frame = renderer.render_gpu_frame(750, force_warp=False)

    assert configured["realization_enabled"] is True
    assert configured["realization_supported"] is True
    assert diagnostics["realization_count"] > 0
    assert diagnostics["realization_prewarm_skipped"] == 0
    assert frame["realization_hit"] > 0
    assert frame["realization_miss"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_realization_accepts_export_capacity_override(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(stroke_width_px=14, stroke2_width_px=7)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured = renderer.configure_gpu(
            _g1_track(),
            style,
            width=640,
            height=360,
            fps=60,
            force_warp=False,
            realization_capacity=65_536,
        )

    assert configured["realization_capacity"] == 65_536


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_realization_can_be_disabled_per_configuration(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.delenv("KROK_GPU_REALIZATION", raising=False)
    style = _g1_style(
        stroke_width_px=14,
        stroke2_width_px=7,
        decoration_kind="none",
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured = renderer.configure_gpu(
            _g1_track(),
            style,
            width=640,
            height=360,
            fps=60,
            force_warp=False,
            prewarm_t_ms=750,
            worker_count=2,
            realization_enabled=False,
        )
        frame = renderer.render_gpu_frame(750, force_warp=False)

    assert configured["realization_enabled"] is False
    assert configured["worker_count"] == 2
    assert configured["realization_prewarm_complete"] is True
    assert configured["realization_prewarm_tasks"] == 0
    assert configured["realization_count"] == 0
    assert frame["realization_hit"] == 0
    assert frame["realization_miss"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_realization_finishes_during_continuous_60fps_playback(monkeypatch) -> None:
    """Prewarm must make progress without a 100ms paused/idle window."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.delenv("KROK_GPU_REALIZATION", raising=False)
    style = _g1_style(
        stroke_width_px=14,
        stroke2_width_px=7,
        decoration_kind="none",
    )
    recent_hits: list[int] = []
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        renderer.configure_gpu(
            _g1_track(), style, width=640, height=360, fps=60,
            force_warp=False, prewarm_t_ms=750, worker_count=1,
        )
        next_frame_at = time.monotonic()
        for frame_index in range(120):
            frame = renderer.render_gpu_frame(
                750,
                force_warp=False,
                generation=1,
                frame_index=frame_index,
            )
            if frame_index >= 90:
                recent_hits.append(int(frame["realization_hit"]))
            next_frame_at += 1.0 / 60.0
            remaining = next_frame_at - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        diagnostics = renderer.gpu_diagnostics()

    assert diagnostics["realization_prewarm_tasks"] > 0
    assert diagnostics["realization_prewarm_complete"] is True
    assert diagnostics["realization_count"] > 0
    assert any(hit > 0 for hit in recent_hits)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_realization_rapid_style_churn_discards_stale_prewarm(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.delenv("KROK_GPU_REALIZATION", raising=False)
    track = _g1_track()
    wide_style = _g1_style(
        stroke_width_px=14,
        latin_stroke_width_px=14,
        stroke2_width_px=7,
        latin_stroke2_width_px=7,
        decoration_kind="none",
    )
    fine_style = replace(
        wide_style,
        stroke_width_px=5,
        latin_stroke_width_px=5,
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        renderer.configure_gpu(
            track, wide_style, width=640, height=360, fps=60,
            force_warp=False, prewarm_t_ms=750,
        )
        time.sleep(0.11)
        for stroke_width in (15, 16, 17):
            renderer.configure_gpu(
                track,
                replace(
                    wide_style,
                    stroke_width_px=stroke_width,
                    latin_stroke_width_px=stroke_width,
                ),
                width=640,
                height=360,
                fps=60,
                force_warp=False,
                prewarm_t_ms=750,
            )
        renderer.configure_gpu(
            track, fine_style, width=640, height=360, fps=60,
            force_warp=False, prewarm_t_ms=750,
        )
        diagnostics = _wait_for_realization_prewarm(renderer)
        time.sleep(0.05)
        frame = renderer.render_gpu_frame(750, force_warp=False)

    assert diagnostics["realization_count"] == 0
    assert frame["realization_hit"] == 0
    assert frame["realization_miss"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_realization_threshold_preserves_fine_stroke_pixels(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        stroke_width_px=5,
        latin_stroke_width_px=5,
        stroke2_enabled=True,
        stroke2_width_px=7,
        latin_stroke2_width_px=7,
        decoration_kind="none",
    )

    def render(enabled: bool) -> tuple[dict, dict]:
        monkeypatch.setenv("KROK_GPU_REALIZATION", "1" if enabled else "0")
        with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
            configured = renderer.configure_gpu(
                _g1_track(), style, width=640, height=360, fps=60,
                force_warp=False, prewarm_t_ms=750,
            )
            if enabled:
                configured = _wait_for_realization_prewarm(renderer)
            frame = renderer.render_gpu_frame(750, force_warp=False)
        return configured, frame

    enabled_diagnostics, enabled_frame = render(True)
    _, disabled_frame = render(False)

    assert enabled_diagnostics["realization_count"] == 0
    assert enabled_frame["realization_hit"] == 0
    assert enabled_frame["checksum"] == disabled_frame["checksum"]


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_realization_is_disabled_for_warp_by_default(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.delenv("KROK_GPU_REALIZATION_WARP", raising=False)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured = renderer.configure_gpu(
            _g1_track(), _g1_style(stroke_width_px=14),
            width=640, height=360, fps=60, force_warp=True,
        )
        frame = renderer.render_gpu_frame(750, force_warp=True)

    assert configured["realization_enabled"] is False
    assert configured["realization_count"] == 0
    assert frame["realization_hit"] == 0
    assert frame["realization_miss"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("stroke_width", [14, 30, 50])
def test_gpu_realization_wide_stroke_pixels_stay_within_bounded_diff(
    monkeypatch,
    stroke_width: int,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        stroke_width_px=stroke_width,
        latin_stroke_width_px=stroke_width,
        stroke2_width_px=7,
        latin_stroke2_width_px=7,
        decoration_kind="none",
    )

    def render(enabled: bool) -> bytes:
        monkeypatch.setenv("KROK_GPU_REALIZATION", "1" if enabled else "0")
        with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
            renderer.configure_gpu(
                _g1_track(), style, width=640, height=360, fps=60,
                force_warp=False, prewarm_t_ms=750,
            )
            if enabled:
                _wait_for_realization_prewarm(renderer)
            event = renderer.render_gpu_frame(750, force_warp=False)
            reader = SharedFrameRingReader.from_event(event)
            reader.attach()
            try:
                image = reader.read_qimage(event).convertToFormat(
                    QImage.Format.Format_RGBA8888
                )
                bits = image.constBits()
                bits.setsize(image.sizeInBytes())
                return bytes(bits)
            finally:
                reader.close()

    realized = render(True)
    fallback = render(False)
    realized_bounds = _payload_alpha_bounds(realized)
    fallback_bounds = _payload_alpha_bounds(fallback)
    assert all(
        abs(actual - expected) <= 2
        for actual, expected in zip(realized_bounds, fallback_bounds)
    )
    channel_deltas = [abs(a - b) for a, b in zip(realized, fallback)]
    mean_channel_delta = sum(channel_deltas) / len(channel_deltas)
    large_channel_delta_count = sum(delta > 8 for delta in channel_deltas)
    assert mean_channel_delta < 0.2, (stroke_width, mean_channel_delta)
    assert large_channel_delta_count < 3_000, (
        stroke_width,
        large_channel_delta_count,
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_realization_utopia_dynamic_units_fall_back_without_visual_jump(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track, base_style = _g4_utopia_main_scene()
    style = replace(
        base_style,
        stroke_width_px=14,
        latin_stroke_width_px=14,
        stroke2_enabled=True,
        stroke2_width_px=7,
        latin_stroke2_width_px=7,
        decoration_kind="none",
    )
    timestamps = (100, 350, 700, 1_250, 2_400, 3_600)

    def render(enabled: bool) -> tuple[list[dict], list[bytes]]:
        monkeypatch.setenv("KROK_GPU_REALIZATION", "1" if enabled else "0")
        events: list[dict] = []
        payloads: list[bytes] = []
        with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
            renderer.configure_gpu(
                track, style, width=640, height=360, fps=60,
                force_warp=False, prewarm_t_ms=350,
            )
            if enabled:
                _wait_for_realization_prewarm(renderer)
            reader: SharedFrameRingReader | None = None
            try:
                for frame_index, t_ms in enumerate(timestamps):
                    event = renderer.render_gpu_frame(
                        t_ms, force_warp=False, frame_index=frame_index
                    )
                    events.append(event)
                    if reader is None:
                        reader = SharedFrameRingReader.from_event(event)
                        reader.attach()
                    image = reader.read_qimage(event).convertToFormat(
                        QImage.Format.Format_RGBA8888
                    )
                    bits = image.constBits()
                    bits.setsize(image.sizeInBytes())
                    payloads.append(bytes(bits))
            finally:
                if reader is not None:
                    reader.close()
        return events, payloads

    realized_events, realized = render(True)
    _, fallback = render(False)

    assert all(event["realization_miss"] == 0 for event in realized_events)
    assert any(event["realization_hit"] > 0 for event in realized_events)
    for t_ms, realized_frame, fallback_frame in zip(timestamps, realized, fallback):
        deltas = [abs(a - b) for a, b in zip(realized_frame, fallback_frame)]
        assert sum(deltas) / len(deltas) < 1.2
        if any(realized_frame[3::4]) and any(fallback_frame[3::4]):
            actual_bounds = _payload_alpha_bounds(realized_frame)
            expected_bounds = _payload_alpha_bounds(fallback_frame)
            assert all(
                abs(actual - expected) <= 2
                for actual, expected in zip(
                    actual_bounds,
                    expected_bounds,
                )
            ), (t_ms, actual_bounds, expected_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_n3_glow_concentration_adds_blur_passes(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    low = _g1_style(
        decoration_kind="glow",
        glow_radius_px=10,
        glow_before_radius_px=10,
        glow_after_radius_px=10,
        glow_concentration_level=0,
    )
    high = replace(low, glow_concentration_level=2)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, low_frames = _render_g1_frames(renderer, low, (750,), force_warp=True)
        _, high_frames = _render_g1_frames(renderer, high, (750,), force_warp=True)

    low_alpha = low_frames[0][3::4]
    high_alpha = high_frames[0][3::4]
    assert sum(high_alpha) > sum(low_alpha)
    assert _alpha_count(high_frames[0]) >= _alpha_count(low_frames[0])
    assert any(
        payload[index + 1] > payload[index] + 20 and payload[index + 3] > 0
        for payload in high_frames
        for index in range(0, len(payload), 4)
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("concentration", [0, 1, 2])
def test_gpu_glow_dirty_scratch_matches_full_surface_for_wide_utopia_ruby(
    monkeypatch,
    concentration: int,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("KROK_GPU_REALIZATION", "0")
    track, base_style = _g4_utopia_ruby_scene()
    style = replace(
        base_style,
        stroke_width_px=50,
        latin_stroke_width_px=50,
        stroke2_enabled=True,
        stroke2_width_px=7,
        latin_stroke2_enabled=True,
        latin_stroke2_width_px=7,
        glow_before_radius_px=12,
        glow_after_radius_px=12,
        glow_concentration_level=concentration,
        ruby_stroke_width_px=10,
        ruby_stroke2_enabled=True,
        ruby_stroke2_width_px=5,
        ruby_glow_before_radius_px=8,
        ruby_glow_after_radius_px=8,
        ruby_glow_concentration_level=concentration,
    )
    timestamps = (100, 350, 700, 1_250, 2_400, 3_600)

    def render(enabled: bool) -> tuple[list[dict], list[bytes]]:
        monkeypatch.setenv(
            "KROK_GPU_GLOW_DIRTY_RECT", "1" if enabled else "0"
        )
        events: list[dict] = []
        payloads: list[bytes] = []
        with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
            renderer.configure_gpu(
                track, style, width=640, height=360, fps=60,
                force_warp=False,
            )
            reader: SharedFrameRingReader | None = None
            try:
                for frame_index, t_ms in enumerate(timestamps):
                    event = renderer.render_gpu_frame(
                        t_ms, force_warp=False, frame_index=frame_index
                    )
                    events.append(event)
                    if reader is None:
                        reader = SharedFrameRingReader.from_event(event)
                        reader.attach()
                    image = reader.read_qimage(event).convertToFormat(
                        QImage.Format.Format_RGBA8888
                    )
                    bits = image.constBits()
                    bits.setsize(image.sizeInBytes())
                    payloads.append(bytes(bits))
            finally:
                if reader is not None:
                    reader.close()
        return events, payloads

    dirty_events, dirty_frames = render(True)
    full_events, full_frames = render(False)

    assert sum(event["glow_source_area_px"] for event in dirty_events) < (
        sum(event["glow_source_area_px"] for event in full_events) * 0.75
    )
    for t_ms, dirty, full in zip(timestamps, dirty_frames, full_frames):
        dirty_bounds = _payload_alpha_bounds(dirty)
        full_bounds = _payload_alpha_bounds(full)
        assert all(
            abs(actual - expected) <= 2
            for actual, expected in zip(
                dirty_bounds,
                full_bounds,
            )
        ), (t_ms, dirty_bounds, full_bounds)
        deltas = [abs(a - b) for a, b in zip(dirty, full)]
        mean_delta = sum(deltas) / len(deltas)
        large_delta_count = sum(delta > 8 for delta in deltas)
        assert mean_delta < 0.5, (t_ms, mean_delta)
        assert large_delta_count < 5_000, (t_ms, large_delta_count)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_glow_dirty_rect_switch_is_zero_cost_when_glow_is_disabled(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("KROK_GPU_REALIZATION", "0")
    style = _g1_style(
        stroke_width_px=14,
        stroke2_width_px=7,
        decoration_kind="none",
    )

    def render(enabled: bool) -> tuple[dict, dict]:
        monkeypatch.setenv(
            "KROK_GPU_GLOW_DIRTY_RECT", "1" if enabled else "0"
        )
        with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
            configured = renderer.configure_gpu(
                _g1_track(), style, width=640, height=360, fps=60,
                force_warp=False,
            )
            frame = renderer.render_gpu_frame(750, force_warp=False)
        return configured, frame

    dirty_config, dirty_frame = render(True)
    full_config, full_frame = render(False)

    assert dirty_config["glow_dirty_rect_enabled"] is True
    assert full_config["glow_dirty_rect_enabled"] is False
    assert dirty_frame["glow_source_area_px"] == 0
    assert full_frame["glow_source_area_px"] == 0
    assert dirty_frame["checksum"] == full_frame["checksum"]


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_ruby_has_independent_geometry_and_wipe(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        font_family="Meiryo",
        base_color="#FFFFFF",
        fill_color="#FFFFFF",
        ruby_color="#FF2030",
        ruby_font_size_px=36,
        ruby_gap_px=4,
        ruby_stroke_width_px=3,
        ruby_stroke2_enabled=False,
        decoration_kind="shadow",
        shadow_color="#00000000",
        shadow_offset_x=0,
        shadow_offset_y=0,
        dual_line_layout=False,
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured, frames = _render_g1_frames(
            renderer,
            style,
            (0, 700, 1_200, 2_000),
            force_warp=True,
            track=_g3_ruby_track(),
        )

    red_counts = [
        sum(
            payload[index] > 180
            and payload[index + 1] < 100
            and payload[index + 3] > 0
            for index in range(0, len(payload), 4)
        )
        for payload in frames
    ]
    assert configured["cached_rubies"] == 1
    assert configured["cached_chars"] == 5
    assert red_counts[0] < red_counts[1] == red_counts[2] < red_counts[3]
    assert len({_alpha_count(payload) for payload in frames}) == 1


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_next_line_ruby_is_not_cached_at_previous_line_end(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("笑", 1_000), TimingChar(" ", 1_900)],
                end_ms=2_000,
            ),
            TimingLine(chars=[TimingChar("笑", 2_000)], end_ms=2_500),
        ],
        rubies=[
            RubyAnnotation(
                kanji="笑",
                reading="わら",
                pos_start_ms=1_000,
                pos_end_ms=1_500,
            ),
            RubyAnnotation(
                kanji="笑",
                reading="わら",
                pos_start_ms=2_000,
                pos_end_ms=2_100,
            ),
        ],
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured = renderer.configure_gpu(
            track,
            _g1_style(dual_line_layout=False),
            width=640,
            height=360,
            fps=60,
            force_warp=True,
        )

    # Each source ruby belongs to exactly one line.  The second ruby starts at
    # the first line's exclusive end and must not be cached against both lines.
    assert configured["cached_rubies"] == 2


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize(
    "direction_changes",
    [{}, {"right_to_left": True}, {"vertical": True}],
)
def test_gpu_g4_shared_timing_span_uses_painter_resolved_intervals(
    monkeypatch,
    direction_changes: dict[str, object],
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    span = {
        "source_span_start_ms": 0,
        "source_span_end_ms": 3_000,
        "source_span_count": 3,
    }
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("W", 0, source_span_index=0, **span),
                    TimingChar(" ", 1_000, source_span_index=1, **span),
                    TimingChar("M", 2_000, source_span_index=2, **span),
                ],
                end_ms=3_000,
            )
        ]
    )
    before = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFF2020"),
        stroke=PaintFill(mode="solid", color="#00000000"),
        shadow=PaintFill(mode="solid", color="#00000000"),
    )
    after = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FF2040FF")
    )
    style = _g1_style(
        font_family="Times New Roman",
        font_family_latin="Times New Roman",
        font_size_px=82,
        dual_line_layout=False,
        line_horizontal_layout="center",
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        karaoke_colors=KaraokeColors(before=before, after=after),
        **direction_changes,
    )
    from krok_helper.subtitle_render.engine.painter import (
        resolved_char_intervals_for_line,
    )

    intervals = resolved_char_intervals_for_line(track.lines[0], style)
    if style.vertical:
        assert intervals == [(0, 1_000), (1_000, 2_000), (2_000, 3_000)]
    else:
        assert intervals != [(0, 1_000), (1_000, 2_000), (2_000, 3_000)]
    timestamps = tuple(
        sorted(
            {
                0,
                500,
                intervals[0][1] - 1,
                intervals[0][1] + 1,
                intervals[1][1] - 1,
                intervals[1][1] + 1,
                3_000,
            }
        )
    )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )

    def blue_progress(payload: bytes) -> int:
        return sum(
            max(int(payload[index + 2]) - int(payload[index]), 0)
            for index in range(0, len(payload), 4)
        )

    gpu_full = blue_progress(gpu[-1])
    painter_full = blue_progress(painter[-1])
    for t_ms, gpu_frame, painter_frame in zip(timestamps, gpu, painter):
        gpu_ratio = blue_progress(gpu_frame) / gpu_full
        painter_ratio = blue_progress(painter_frame) / painter_full
        assert abs(gpu_ratio - painter_ratio) <= 0.06, (
            direction_changes,
            t_ms,
            intervals,
            gpu_ratio,
            painter_ratio,
        )
    assert all(
        abs(actual - expected) <= 12
        for actual, expected in zip(
            _payload_alpha_bounds(gpu[-1]),
            _payload_alpha_bounds(painter[-1]),
        )
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("mode", ["prefix", "replacement", "inline"])
@pytest.mark.parametrize(
    "direction_changes",
    [{}, {"right_to_left": True}, {"vertical": True}],
)
def test_gpu_g4_guide_symbols_follow_painter_vector_glyphs(
    monkeypatch,
    mode: str,
    direction_changes: dict[str, object],
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    symbol = GuideSymbol(
        path_commands=(
            ("M", 100.0, 0.0),
            ("L", 500.0, -820.0),
            ("L", 900.0, 0.0),
            ("Z",),
        ),
        duration_ms=400,
        count=2,
        role_labels=("lead", None),
        replacement_prefix=(("3", "2") if mode == "replacement" else ()),
    )
    if mode == "prefix":
        line = TimingLine(
            chars=[TimingChar(" ", 1_200)],
            end_ms=1_600,
            guide_symbol=symbol,
        )
        timestamps = (400, 600, 800, 1_000, 1_600)
    elif mode == "replacement":
        line = TimingLine(
            chars=[
                TimingChar("3", 400),
                TimingChar("2", 800),
                TimingChar(" ", 1_200),
            ],
            end_ms=1_600,
            guide_symbol=symbol,
        )
        timestamps = (400, 600, 800, 1_000, 1_600)
    else:
        line = TimingLine(
            chars=[TimingChar("*", 400, role_label="lead")],
            end_ms=800,
            inline_guide_symbols={0: replace(symbol, count=1)},
        )
        timestamps = (400, 600, 800)
    before = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFF2020"),
        stroke=PaintFill(mode="solid", color="#00000000"),
        shadow=PaintFill(mode="solid", color="#00000000"),
    )
    after = KaraokeColorState(text=PaintFill(mode="solid", color="#FF2040FF"))
    role_before = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFFE020"),
        stroke=PaintFill(mode="solid", color="#00000000"),
        shadow=PaintFill(mode="solid", color="#00000000"),
    )
    role_after = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FF8040FF"),
        stroke=PaintFill(mode="solid", color="#00000000"),
        shadow=PaintFill(mode="solid", color="#00000000"),
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=80,
        dual_line_layout=False,
        line_horizontal_layout="center",
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        karaoke_colors=KaraokeColors(before=before, after=after),
        custom_style_schemes={
            "lead": SubtitleStyleScheme(
                font_size_px=80,
                karaoke_colors=KaraokeColors(
                    before=role_before,
                    after=role_after,
                ),
            )
        },
        **direction_changes,
    )
    track = TimingTrack(lines=[line])
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )

    def blue_progress(payload: bytes) -> int:
        return sum(
            max(int(payload[index + 2]) - int(payload[index]), 0)
            for index in range(0, len(payload), 4)
        )

    gpu_full = blue_progress(gpu[-1])
    painter_full = blue_progress(painter[-1])
    assert gpu_full > 0 and painter_full > 0
    for t_ms, gpu_frame, painter_frame in zip(timestamps, gpu, painter):
        gpu_ratio = blue_progress(gpu_frame) / gpu_full
        painter_ratio = blue_progress(painter_frame) / painter_full
        assert abs(gpu_ratio - painter_ratio) <= 0.06, (
            mode,
            direction_changes,
            t_ms,
            gpu_ratio,
            painter_ratio,
        )
    if not style.vertical:
        def role_after_pixels(payload: bytes) -> int:
            return sum(
                payload[index] > 90
                and payload[index + 2] > 180
                and payload[index + 3] > 0
                for index in range(0, len(payload), 4)
            )

        assert role_after_pixels(gpu[-1]) > 0
        assert role_after_pixels(painter[-1]) > 0
    gpu_bounds = _payload_alpha_bounds(gpu[-1])
    painter_bounds = _payload_alpha_bounds(painter[-1])
    assert all(
        abs(actual - expected) <= 12
        for actual, expected in zip(
            gpu_bounds,
            painter_bounds,
        )
    ), (mode, direction_changes, gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_vertical_title_remains_in_screen_coordinates(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        meta=TimingTrackMeta(title="Vertical", artist="Title"),
        lines=[TimingLine(chars=[TimingChar("Later", 4_000)], end_ms=5_000)],
    )
    title = TitleOverlay(
        enabled=True,
        text_template="{title} / {artist}",
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=42,
        fill=PaintFill(mode="solid", color="#40FF60"),
        stroke=PaintFill(mode="solid", color="#102010"),
        stroke_width_px=2,
        anchor="top_left",
        align="left",
        offset_x=41,
        offset_y=33,
        show_mode="head",
        duration_ms=2_000,
        fade_in_ms=0,
        fade_out_ms=0,
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        vertical=True,
        title_overlay=title,
    )
    painter = _render_painter_oracle(style, t_ms=1_000, track=track)
    flat_style = replace(style, vertical=False)
    flat_painter = _render_painter_oracle(flat_style, t_ms=1_000, track=track)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, (1_000,), force_warp=True, track=track
        )
        _, flat_gpu = _render_g1_frames(
            renderer, flat_style, (1_000,), force_warp=True, track=track
        )
    assert painter == flat_painter
    assert gpu[0] == flat_gpu[0]
    gpu_bounds = _payload_alpha_bounds(gpu[0])
    painter_bounds = _payload_alpha_bounds(painter)
    assert all(
        abs(actual - expected) <= 24
        for actual, expected in zip(gpu_bounds, painter_bounds)
    ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_vertical_inline_role_styles_follow_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    def role_scheme(color: str, size: int) -> SubtitleStyleScheme:
        state = KaraokeColorState(text=PaintFill(mode="solid", color=color))
        return SubtitleStyleScheme(
            font_family="Meiryo",
            font_family_latin="Meiryo",
            font_size_px=size,
            stroke_width_px=0,
            decoration_kind="none",
            karaoke_colors=KaraokeColors(before=state, after=state),
        )

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("A", 0, role_label="large"),
                    TimingChar("漢", 500, role_label="small"),
                    TimingChar("B", 1_000, role_label="large"),
                ],
                end_ms=1_500,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=60,
        vertical=True,
        dual_line_layout=False,
        line_y_position="center",
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        custom_style_schemes={
            "large": role_scheme("#FF2020", 76),
            "small": role_scheme("#20FF40", 46),
        },
    )
    plain_track = replace(
        track,
        lines=[
            replace(
                track.lines[0],
                chars=[replace(ch, role_label=None) for ch in track.lines[0].chars],
            )
        ],
    )
    painter = _render_painter_oracle(style, t_ms=1_500, track=track)
    plain_painter = _render_painter_oracle(style, t_ms=1_500, track=plain_track)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, (1_500,), force_warp=True, track=track
        )
        _, plain_gpu = _render_g1_frames(
            renderer, style, (1_500,), force_warp=True, track=plain_track
        )
    assert painter == plain_painter
    assert gpu[0] == plain_gpu[0]
    gpu_bounds = _payload_alpha_bounds(gpu[0])
    painter_bounds = _payload_alpha_bounds(painter)
    assert all(
        abs(actual - expected) <= 12
        for actual, expected in zip(gpu_bounds, painter_bounds)
    ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_rtl_inline_roles_and_volume_signal_follow_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("W", 4_000, role_label="red"),
                    TimingChar("i", 4_200, role_label="green"),
                    TimingChar("M", 4_400, role_label="red"),
                ],
                end_ms=5_000,
            )
        ]
    )

    def role_scheme(color: str, size: int) -> SubtitleStyleScheme:
        state = KaraokeColorState(text=PaintFill(mode="solid", color=color))
        return SubtitleStyleScheme(
            font_family="Meiryo",
            font_family_latin="Meiryo",
            font_size_px=size,
            stroke_width_px=0,
            decoration_kind="none",
            karaoke_colors=KaraokeColors(before=state, after=state),
        )

    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=64,
        right_to_left=True,
        dual_line_layout=False,
        line_horizontal_layout="center",
        line_lead_in_ms=500,
        line_tail_ms=500,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        custom_style_schemes={
            "red": role_scheme("#FF2020", 74),
            "green": role_scheme("#20FF40", 48),
        },
        lit_enabled=True,
        lit_style="volume",
        signals_duration_ms=4_000,
        lit_waiting_time_ms=0,
        lit_time_offset_ms=0,
        lit_stroke_width=2,
        volume_size=42,
        volume_column_width=12,
        volume_column_count=4,
        volume_column_spacing=3,
    )
    timestamps = (500, 2_500, 4_500)
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    for t_ms, gpu_frame, painter_frame in zip(timestamps, gpu, painter):
        gpu_bounds = _payload_alpha_bounds(gpu_frame)
        painter_bounds = _payload_alpha_bounds(painter_frame)
        assert all(
            abs(actual - expected) <= 14
            for actual, expected in zip(gpu_bounds, painter_bounds)
        ), (t_ms, gpu_bounds, painter_bounds, configured)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_rtl_vertical_combination_follows_vertical_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("縦", 0),
                    TimingChar("書", 500),
                    TimingChar("き", 1_000),
                ],
                end_ms=1_500,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=64,
        vertical=True,
        right_to_left=True,
        dual_line_layout=False,
        line_y_position="center",
        decoration_kind="glow",
        glow_before_radius_px=7,
        glow_after_radius_px=7,
        glow_concentration_level=1,
    )
    timestamps = (250, 750, 1_500)
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    for gpu_frame, painter_frame in zip(gpu, painter):
        assert all(
            abs(actual - expected) <= 12
            for actual, expected in zip(
                _payload_alpha_bounds(gpu_frame),
                _payload_alpha_bounds(painter_frame),
            )
        )
    for index in range(len(timestamps)):
        assert abs(
            sum(gpu[index][3::4]) / sum(gpu[-1][3::4])
            - sum(painter[index][3::4]) / sum(painter[-1][3::4])
        ) <= 0.09


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("direction_changes", [{"vertical": True}, {"right_to_left": True}])
@pytest.mark.parametrize(
    "entry_anim,exit_anim",
    [("fade", "fade"), ("slide_in", "slide_out"), ("rise", "rise")],
)
def test_gpu_g4_directional_basic_line_animations_follow_painter(
    monkeypatch,
    direction_changes: dict[str, object],
    entry_anim: str,
    exit_anim: str,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("動", 1_000), TimingChar("画", 1_500)],
                end_ms=2_000,
                display_start_override_ms=0,
                display_end_override_ms=3_000,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=64,
        dual_line_layout=False,
        line_horizontal_layout="center",
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        entry_anim=entry_anim,
        entry_lead_ms=1_000,
        exit_anim=exit_anim,
        exit_fade_ms=1_000,
        **direction_changes,
    )
    timestamps = (500, 1_000, 2_500)
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    for index in (0, 2):
        assert abs(
            sum(gpu[index][3::4]) / sum(gpu[1][3::4])
            - sum(painter[index][3::4]) / sum(painter[1][3::4])
        ) <= 0.08
        gpu_bounds = _payload_alpha_bounds(gpu[index])
        painter_bounds = _payload_alpha_bounds(painter[index])
        assert all(
            abs(actual - expected) <= 14
            for actual, expected in zip(gpu_bounds, painter_bounds)
        ), (direction_changes, entry_anim, index, gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("direction_changes", [{"vertical": True}, {"right_to_left": True}])
@pytest.mark.parametrize("animation", ["char_fade", "spin_flip", "utopia"])
def test_gpu_g4_directional_character_animations_follow_painter(
    monkeypatch,
    direction_changes: dict[str, object],
    animation: str,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("A", 1_000),
                    TimingChar("漢", 1_300),
                    TimingChar("字", 1_600),
                    TimingChar("B", 1_900),
                ],
                end_ms=2_100,
                display_start_override_ms=0,
                display_end_override_ms=3_000,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=64,
        dual_line_layout=False,
        line_horizontal_layout="center",
        stroke_width_px=2,
        stroke2_enabled=False,
        decoration_kind="glow",
        glow_before_radius_px=6,
        glow_after_radius_px=6,
        glow_concentration_level=1,
        entry_anim=animation,
        entry_lead_ms=1_000,
        exit_anim=animation,
        exit_fade_ms=1_000,
        **direction_changes,
    )
    timestamps = (250, 750, 1_500, 2_500)
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    gpu_full = sum(gpu[2][3::4])
    painter_full = sum(painter[2][3::4])
    assert gpu_full > 0 and painter_full > 0
    for t_ms, gpu_frame, painter_frame in zip(timestamps, gpu, painter):
        gpu_alpha = sum(gpu_frame[3::4])
        painter_alpha = sum(painter_frame[3::4])
        assert abs(gpu_alpha / gpu_full - painter_alpha / painter_full) <= 0.16
        if gpu_alpha == 0 or painter_alpha == 0:
            assert gpu_alpha == painter_alpha == 0
            continue
        gpu_bounds = _payload_alpha_bounds(gpu_frame)
        painter_bounds = _payload_alpha_bounds(painter_frame)
        assert all(
            abs(actual - expected) <= 20
            for actual, expected in zip(gpu_bounds, painter_bounds)
        ), (direction_changes, animation, t_ms, gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_per_row_offsets_and_alignments_follow_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    def singer_scheme(color: str) -> SubtitleStyleScheme:
        state = KaraokeColorState(text=PaintFill(mode="solid", color=color))
        return SubtitleStyleScheme(
            font_family="Meiryo",
            font_family_latin="Meiryo",
            font_size_px=62,
            stroke_width_px=0,
            decoration_kind="none",
            karaoke_colors=KaraokeColors(before=state, after=state),
        )

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(ch, 0) for ch in "LEFT"],
                end_ms=1_000,
                singer_id=1,
            ),
            TimingLine(
                chars=[TimingChar(ch, 0) for ch in "RIGHT"],
                end_ms=1_000,
                singer_id=2,
            ),
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=62,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=True,
        line_horizontal_layout="per_row",
        line_alignments=["left", "right"],
        line_y_position="center",
        line_gap_px=26,
        row1_align="left",
        row1_offset_x=57,
        row1_offset_y=-18,
        row2_align="right",
        row2_offset_x=-73,
        row2_offset_y=24,
        singer_style_overrides={
            1: singer_scheme("#FF2020"),
            2: singer_scheme("#20FF40"),
        },
    )
    painter = _render_painter_oracle(style, t_ms=500, track=track)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, (500,), force_warp=True, track=track
        )

    def color_bounds(payload: bytes, *, green: bool) -> tuple[int, int, int, int]:
        points: list[tuple[int, int]] = []
        for index in range(0, len(payload), 4):
            red, value_green, _blue, alpha = payload[index : index + 4]
            matches = value_green > red + 80 if green else red > value_green + 80
            if alpha > 0 and matches:
                pixel = index // 4
                points.append((pixel % 640, pixel // 640))
        assert points
        return (
            min(x for x, _ in points),
            min(y for _, y in points),
            max(x for x, _ in points),
            max(y for _, y in points),
        )

    for green in (False, True):
        gpu_bounds = color_bounds(gpu[0], green=green)
        painter_bounds = color_bounds(painter, green=green)
        assert all(
            abs(actual - expected) <= 8
            for actual, expected in zip(gpu_bounds, painter_bounds)
        ), (green, gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_per_row_alignment_includes_volume_signal_union(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("Signal", 4_000)], end_ms=5_000)]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=60,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        line_horizontal_layout="per_row",
        row1_align="left",
        row1_offset_x=43,
        row1_offset_y=-16,
        line_lead_in_ms=500,
        line_tail_ms=500,
        lit_enabled=True,
        lit_style="volume",
        signals_duration_ms=4_000,
        lit_waiting_time_ms=0,
        lit_time_offset_ms=0,
        lit_stroke_width=2,
        volume_size=42,
        volume_column_width=12,
        volume_column_count=4,
        volume_column_spacing=3,
    )
    timestamps = (500, 2_500, 3_999)
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )

    for gpu_frame, painter_frame in zip(gpu, painter):
        gpu_bounds = _payload_alpha_bounds(gpu_frame)
        painter_bounds = _payload_alpha_bounds(painter_frame)
        assert all(
            abs(actual - expected) <= 12
            for actual, expected in zip(gpu_bounds, painter_bounds)
        ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_line_layout_override_geometry_and_ruby_follow_painter(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("漢", 0), TimingChar("字", 1_000)],
                end_ms=2_000,
                layout_index=1,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="漢字",
                reading="かんじ",
                pos_start_ms=0,
                pos_end_ms=2_000,
            )
        ],
    )
    layout = LyricsLayout(
        name="GPU layout",
        line_y_position="top",
        line_y_margin_px=34,
        line_gap_px=21,
        smart_horizontal="none",
        horizontal_margin_px=71,
        line_alignments=["right"],
        letter_spacing_px=11,
        allow_biting=True,
        ruby_interval_px=14,
        ruby_alignment="center",
        ruby_gap_px=9,
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=68,
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=28,
        dual_line_layout=True,
        line_horizontal_layout="asymmetric",
        line_alignments=["left", "right"],
        line_y_position="bottom",
        line_y_margin_px=80,
        layouts=[layout],
    )
    timestamps = (500, 1_500)
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )

    for gpu_frame, painter_frame in zip(gpu, painter):
        gpu_bounds = _payload_alpha_bounds(gpu_frame)
        painter_bounds = _payload_alpha_bounds(painter_frame)
        assert all(
            abs(actual - expected) <= 10
            for actual, expected in zip(gpu_bounds, painter_bounds)
        ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_n3_smart_horizontal_uses_painter_page_geometry(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("LEFT", 1_000)], end_ms=2_000),
            TimingLine(chars=[TimingChar("R", 1_500)], end_ms=2_500),
        ]
    )
    style = _g1_style(
        layout_semantics="n3_1074",
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=52,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=True,
        line_horizontal_layout="asymmetric",
        line_alignments=["left", "right"],
        smart_horizontal="equal_margins",
        horizontal_margin_px=24,
        line_lead_in_ms=1_000,
        line_tail_ms=500,
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, (1_600,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=1_600, track=track)

    gpu_bounds = _payload_alpha_bounds(gpu[0])
    painter_bounds = _payload_alpha_bounds(painter)
    assert all(
        abs(actual - expected) <= 12
        for actual, expected in zip(gpu_bounds, painter_bounds)
    ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_n3_smart_horizontal_measures_inline_role_fonts(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("L", 0, role_label="head")],
                end_ms=2_000,
                display_start_override_ms=0,
                display_end_override_ms=2_000,
            ),
            TimingLine(
                chars=[
                    TimingChar("W", 500, role_label="wide"),
                    TimingChar("W", 1_000, role_label="wide"),
                ],
                end_ms=2_000,
                display_start_override_ms=0,
                display_end_override_ms=2_000,
            ),
        ]
    )
    style = _g1_style(
        layout_semantics="n3_1074",
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=36,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=True,
        line_horizontal_layout="asymmetric",
        line_alignments=["left", "right"],
        smart_horizontal="equal_margins",
        horizontal_margin_px=24,
        line_lead_in_ms=0,
        line_tail_ms=0,
        custom_style_schemes={
            "head": SubtitleStyleScheme(
                font_family="Arial",
                font_family_latin="Arial",
                font_size_px=84,
                stroke_width_px=0,
            ),
            "wide": SubtitleStyleScheme(
                font_family="Arial",
                font_family_latin="Arial",
                font_size_px=112,
                stroke_width_px=0,
            )
        },
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, (1_500,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=1_500, track=track)

    gpu_bounds = _payload_alpha_bounds(gpu[0])
    painter_bounds = _payload_alpha_bounds(painter)
    assert all(
        abs(actual - expected) <= 16
        for actual, expected in zip(gpu_bounds, painter_bounds)
    ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_n3_center_lane_uses_main_text_anchor_like_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    lines = [
        TimingLine(
            chars=[TimingChar("L", 0)],
            end_ms=500,
            display_start_override_ms=0,
            display_end_override_ms=500,
        ),
        TimingLine(
            chars=[TimingChar("I", 1_000)],
            end_ms=2_000,
            display_start_override_ms=1_000,
            display_end_override_ms=2_000,
        ),
        TimingLine(
            chars=[TimingChar("R", 3_000)],
            end_ms=3_500,
            display_start_override_ms=3_000,
            display_end_override_ms=3_500,
        ),
    ]
    track = TimingTrack(
        lines=lines,
        rubies=[
            RubyAnnotation(
                kanji="I", reading="WWWWWW", pos_start_ms=1_000, pos_end_ms=2_000
            )
        ],
    )
    style = _g1_style(
        layout_semantics="n3_1074",
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=60,
        ruby_font_family="Arial",
        ruby_font_family_latin="Arial",
        ruby_font_size_px=60,
        ruby_alignment="center",
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=True,
        line_horizontal_layout="asymmetric",
        line_alignments=["left", "center", "right"],
        smart_horizontal="none",
        line_lead_in_ms=0,
        line_tail_ms=0,
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, (1_500,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=1_500, track=track)

    gpu_bounds = _payload_alpha_bounds(gpu[0])
    painter_bounds = _payload_alpha_bounds(painter)
    assert all(
        abs(actual - expected) <= 16
        for actual, expected in zip(gpu_bounds, painter_bounds)
    ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_n3_top_margin_ignores_ruby_height_like_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    line = TimingLine(
        chars=[TimingChar("A", 0)],
        end_ms=2_000,
        display_start_override_ms=0,
        display_end_override_ms=2_000,
    )
    track = TimingTrack(
        lines=[line],
        rubies=[
            RubyAnnotation(
                kanji="A",
                reading="WWWW",
                pos_start_ms=0,
                pos_end_ms=2_000,
            )
        ],
    )
    style = _g1_style(
        layout_semantics="n3_1074",
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=64,
        ruby_font_family="Arial",
        ruby_font_family_latin="Arial",
        ruby_font_size_px=42,
        ruby_gap_px=9,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        line_horizontal_layout="center",
        line_y_position="top",
        line_y_margin_px=47,
        line_lead_in_ms=0,
        line_tail_ms=0,
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, (1_000,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=1_000, track=track)

    gpu_bounds = _payload_alpha_bounds(gpu[0])
    painter_bounds = _payload_alpha_bounds(painter)
    assert all(
        abs(actual - expected) <= 16
        for actual, expected in zip(gpu_bounds, painter_bounds)
    ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_n3_adjacent_ruby_interference_shifts_following_text(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    line = TimingLine(
        chars=[
            TimingChar("A", 0),
            TimingChar("B", 1_000),
            TimingChar("C", 2_000),
        ],
        end_ms=3_000,
        display_start_override_ms=0,
        display_end_override_ms=3_000,
    )
    track = TimingTrack(
        lines=[line],
        rubies=[
            RubyAnnotation(
                kanji=char,
                reading="WWWW",
                pos_start_ms=start,
                pos_end_ms=start + 1_000,
            )
            for char, start in (("A", 0), ("B", 1_000), ("C", 2_000))
        ],
    )
    style = _g1_style(
        layout_semantics="n3_1074",
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=54,
        ruby_font_family="Arial",
        ruby_font_family_latin="Arial",
        ruby_font_size_px=28,
        ruby_alignment="center",
        ruby_interval_px=8,
        letter_spacing_px=-70,
        stroke_width_px=0,
        stroke2_enabled=False,
        ruby_stroke_width_px=0,
        ruby_stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        line_horizontal_layout="center",
        line_y_position="center",
        line_lead_in_ms=0,
        line_tail_ms=0,
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, (1_500,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=1_500, track=track)

    gpu_bounds = _payload_alpha_bounds(gpu[0])
    painter_bounds = _payload_alpha_bounds(painter)
    assert all(
        abs(actual - expected) <= 16
        for actual, expected in zip(gpu_bounds, painter_bounds)
    ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize(
    ("ruby_alignment", "ruby_interval_px", "ruby_gap_px"),
    [
        ("auto", 0, 4),
        ("center", 8, 4),
        ("equal_space", -4, -4),
    ],
)
def test_gpu_g3_ruby_layout_is_bounded_by_painter_oracle(
    monkeypatch,
    ruby_alignment: str,
    ruby_interval_px: int,
    ruby_gap_px: int,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        font_family="Meiryo",
        base_color="#FFFFFF",
        fill_color="#FFFFFF",
        ruby_color="#FF2030",
        ruby_font_size_px=36,
        ruby_gap_px=ruby_gap_px,
        ruby_interval_px=ruby_interval_px,
        ruby_alignment=ruby_alignment,
        ruby_stroke_width_px=3,
        ruby_stroke2_enabled=False,
        decoration_kind="shadow",
        shadow_color="#00000000",
        shadow_offset_x=0,
        shadow_offset_y=0,
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer,
            style,
            (1_000,),
            force_warp=True,
            track=_g3_ruby_track(),
        )
    painter = _render_painter_oracle(style, t_ms=1_000, track=_g3_ruby_track())

    gpu_bounds = _payload_alpha_bounds(gpu[0])
    painter_bounds = _payload_alpha_bounds(painter)
    assert all(
        abs(actual - expected) <= 5
        for actual, expected in zip(gpu_bounds, painter_bounds)
    ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.skipif(
    not DARK_SPIRAL_N3PROJ.is_file(),
    reason="Dark Spiral N3 reference project is unavailable",
)
def test_gpu_g3_real_n3_ruby_frame_is_bounded_by_painter_oracle(monkeypatch) -> None:
    """Gate real N3 ruby segmentation while isolating later G3 features."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    project = load_n3proj(DARK_SPIRAL_N3PROJ)
    source_track = load_nicokara_lrc(
        DARK_SPIRAL_N3PROJ.with_name("Dark spiral journey.lrc")
    )
    line = source_track.lines[3]
    line_start_ms = line.chars[0].start_ms
    line_end_ms = line.end_ms or line_start_ms
    track = TimingTrack(
        meta=source_track.meta,
        lines=[line],
        rubies=[
            ruby
            for ruby in source_track.rubies
            if ruby.pos_start_ms < line_end_ms and ruby.pos_end_ms > line_start_ms
        ],
    )

    def solid(fill):
        return replace(fill, mode="solid", gradient_stops=[])

    def solid_state(state):
        return replace(
            state,
            text=solid(state.text),
            stroke=solid(state.stroke),
            stroke2=solid(state.stroke2),
            shadow=solid(state.shadow),
        )

    def solid_colors(colors):
        return replace(
            colors,
            before=solid_state(colors.before),
            after=solid_state(colors.after),
        )

    imported_style = style_from_dict(project.project_data["style"])
    style = replace(
        imported_style,
        # The reference project's UD font is not installed on every test host.
        # Meiryo keeps DirectWrite and QPainter on the same physical font while
        # retaining the real N3 text, ruby parts, timings, sizes and effects.
        font_family="Meiryo",
        font_family_latin="Meiryo",
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        karaoke_colors=solid_colors(imported_style.karaoke_colors),
        ruby_karaoke_colors=solid_colors(imported_style.ruby_karaoke_colors),
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
    )
    t_ms = 24_900
    width, height = 1_920, 1_080
    with NativeRendererProcess(_renderer_path(), response_timeout_s=20.0) as renderer:
        configured, gpu = _render_g1_frames(
            renderer,
            style,
            (t_ms,),
            force_warp=False,
            track=track,
            width=width,
            height=height,
        )
    painter = _render_painter_oracle(
        style,
        t_ms=t_ms,
        track=track,
        width=width,
        height=height,
    )

    assert configured["cached_rubies"] == 5
    gpu_bounds = _payload_alpha_bounds(gpu[0], width, height)
    painter_bounds = _payload_alpha_bounds(painter, width, height)
    assert all(
        abs(actual - expected) <= 7
        for actual, expected in zip(gpu_bounds, painter_bounds)
    ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_singer_override_matches_painter_geometry(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    base = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=54,
        ruby_font_size_px=28,
        ruby_gap_px=4,
        stroke_width_px=2,
        stroke2_enabled=False,
        decoration_kind="shadow",
        shadow_color="#00000000",
        shadow_offset_x=0,
        shadow_offset_y=0,
        dual_line_layout=False,
    )
    singer_colors = KaraokeColors(
        before=KaraokeColorState(
            text=PaintFill(mode="solid", color="#00FF40"),
            stroke=PaintFill(mode="solid", color="#101010"),
        ),
        after=KaraokeColorState(
            text=PaintFill(mode="solid", color="#FF2040"),
            stroke=PaintFill(mode="solid", color="#202020"),
        ),
    )
    style = replace(
        base,
        singer_style_overrides={
            1: SubtitleStyleScheme(
                font_family="Meiryo",
                font_family_latin="Meiryo",
                font_size_px=84,
                stroke_width_px=5,
                ruby_font_size_px=42,
                ruby_stroke_width_px=3,
                ruby_gap_px=7,
                karaoke_colors=singer_colors,
                ruby_karaoke_colors=singer_colors,
            )
        },
    )
    track = _g3_singer_track()
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        base_configured, base_frames = _render_g1_frames(
            renderer,
            base,
            (1_000,),
            force_warp=True,
            track=track,
        )
        singer_configured, singer_frames = _render_g1_frames(
            renderer,
            style,
            (1_000,),
            force_warp=True,
            track=track,
        )
    painter = _render_painter_oracle(style, t_ms=1_000, track=track)

    assert singer_configured["cache_misses"] == base_configured["cache_misses"] + 1
    base_bounds = _payload_alpha_bounds(base_frames[0])
    singer_bounds = _payload_alpha_bounds(singer_frames[0])
    painter_bounds = _payload_alpha_bounds(painter)
    assert singer_bounds[2] - singer_bounds[0] > base_bounds[2] - base_bounds[0]
    singer_width = singer_bounds[2] - singer_bounds[0]
    painter_width = painter_bounds[2] - painter_bounds[0]
    singer_height = singer_bounds[3] - singer_bounds[1]
    painter_height = painter_bounds[3] - painter_bounds[1]
    assert abs(singer_width - painter_width) <= 3
    assert abs(singer_height - painter_height) <= 12
    assert abs(
        (singer_bounds[0] + singer_bounds[2]) / 2
        - (painter_bounds[0] + painter_bounds[2]) / 2
    ) <= 3
    # DirectWrite's N3 outline origin and Qt's font-engine origin differ for
    # this Meiryo weight; retain N3 geometry while bounding Painter placement.
    assert abs(
        (singer_bounds[1] + singer_bounds[3]) / 2
        - (painter_bounds[1] + painter_bounds[3]) / 2
    ) <= 14
    assert any(
        payload[index + 1] > payload[index] + 40 and payload[index + 3] > 0
        for payload in singer_frames
        for index in range(0, len(payload), 4)
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_composites_active_lines_from_multiple_subtitle_sources(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    def role_scheme(color: str) -> SubtitleStyleScheme:
        colors = KaraokeColors(
            before=KaraokeColorState(text=PaintFill(mode="solid", color=color)),
            after=KaraokeColorState(text=PaintFill(mode="solid", color=color)),
        )
        return SubtitleStyleScheme(
            font_family="Meiryo",
            font_family_latin="Meiryo",
            font_size_px=92,
            stroke_width_px=0,
            decoration_kind="none",
            karaoke_colors=colors,
        )

    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=92,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        custom_style_schemes={
            "primary": role_scheme("#FF2020"),
            "extra": role_scheme("#20FF40"),
        },
    )
    primary = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(ch, 0, role_label="primary") for ch in "WWWW"],
                end_ms=1_000,
            )
        ]
    )
    extra = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(ch, 0, role_label="extra") for ch in "II"],
                end_ms=1_000,
            )
        ]
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured, frames = _render_g1_frames(
            renderer,
            style,
            (500,),
            force_warp=True,
            track=primary,
            extra_tracks=[extra],
        )
    painter = _render_painter_oracle(
        style,
        t_ms=500,
        track=primary,
        extra_tracks=[extra],
    )

    assert configured["line_count"] == 2
    gpu = frames[0]
    assert any(
        gpu[index] > gpu[index + 1] + 80 and gpu[index + 3] > 0
        for index in range(0, len(gpu), 4)
    )
    assert any(
        gpu[index + 1] > gpu[index] + 80 and gpu[index + 3] > 0
        for index in range(0, len(gpu), 4)
    )
    gpu_bounds = _payload_alpha_bounds(gpu)
    painter_bounds = _payload_alpha_bounds(painter)
    assert all(
        abs(actual - expected) <= 12
        for actual, expected in zip(gpu_bounds, painter_bounds)
    ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_title_overlay_matches_painter_window_fade_and_anchor(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        meta=TimingTrackMeta(title="星空", artist="歌手"),
        lines=[TimingLine(chars=[TimingChar("尾", 3_000)], end_ms=4_000)],
    )
    title = TitleOverlay(
        enabled=True,
        text_template="{title} / {artist}",
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=48,
        font_weight=700,
        fill=PaintFill(mode="solid", color="#40FF60"),
        stroke=PaintFill(mode="solid", color="#102010"),
        stroke_width_px=3,
        stroke2_width_px=0,
        decoration_kind="shadow",
        shadow=PaintFill(mode="solid", color="#00000000"),
        shadow_offset_x=0,
        shadow_offset_y=0,
        anchor="top_left",
        align="left",
        offset_x=37,
        offset_y=29,
        layout_index=None,
        show_mode="head",
        head_offset_ms=0,
        duration_ms=2_000,
        fade_in_ms=500,
        fade_out_ms=500,
    )
    style = _g1_style(
        line_lead_in_ms=0,
        line_tail_ms=0,
        custom_style_schemes={},
        title_overlay=title,
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured, frames = _render_g1_frames(
            renderer,
            style,
            (0, 250, 1_000, 2_001),
            force_warp=True,
            track=track,
        )
    painter = _render_painter_oracle(style, t_ms=1_000, track=track)

    assert configured["line_count"] == 2
    assert _alpha_count(frames[0]) == 0
    assert _alpha_count(frames[3]) == 0
    assert 0 < sum(frames[1][3::4]) < sum(frames[2][3::4])
    gpu_bounds = _payload_alpha_bounds(frames[2])
    painter_bounds = _payload_alpha_bounds(painter)
    assert all(
        abs(actual - expected) <= 12
        for actual, expected in zip(gpu_bounds, painter_bounds)
    ), (gpu_bounds, painter_bounds)
    assert any(
        frames[2][index + 1] > frames[2][index] + 50
        and frames[2][index + 3] > 0
        for index in range(0, len(frames[2]), 4)
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_multiline_title_role_styles_match_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    def role_scheme(color: str, size: int) -> SubtitleStyleScheme:
        colors = KaraokeColors(
            before=KaraokeColorState(text=PaintFill(mode="solid", color=color)),
            after=KaraokeColorState(text=PaintFill(mode="solid", color=color)),
        )
        return SubtitleStyleScheme(
            font_family="Meiryo",
            font_family_latin="Meiryo",
            font_size_px=size,
            stroke_width_px=0,
            decoration_kind="none",
            karaoke_colors=colors,
        )

    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("尾", 3_000)], end_ms=4_000)]
    )
    title = TitleOverlay(
        enabled=True,
        text_template="AB\n日C",
        char_role_labels=[["red", "green"], [None, "red"]],
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=44,
        fill=PaintFill(mode="solid", color="#E8E8E8"),
        stroke_width_px=0,
        stroke2_width_px=0,
        decoration_kind="shadow",
        shadow=PaintFill(mode="solid", color="#00000000"),
        shadow_offset_x=0,
        shadow_offset_y=0,
        anchor="top_left",
        align="left",
        offset_x=32,
        offset_y=24,
        line_gap_px=11,
        layout_index=None,
        show_mode="whole",
        fade_in_ms=0,
        fade_out_ms=0,
    )
    style = _g1_style(
        line_lead_in_ms=0,
        line_tail_ms=0,
        custom_style_schemes={
            "red": role_scheme("#FF2020", 52),
            "green": role_scheme("#20FF40", 38),
        },
        title_overlay=title,
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer, style, (1_000,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=1_000, track=track)
    gpu = frames[0]

    assert any(
        gpu[index] > gpu[index + 1] + 70 and gpu[index + 3] > 0
        for index in range(0, len(gpu), 4)
    )
    assert any(
        gpu[index + 1] > gpu[index] + 70 and gpu[index + 3] > 0
        for index in range(0, len(gpu), 4)
    )
    gpu_bounds = _payload_alpha_bounds(gpu)
    painter_bounds = _payload_alpha_bounds(painter)
    assert all(
        abs(actual - expected) <= 24
        for actual, expected in zip(gpu_bounds, painter_bounds)
    ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_applies_each_source_track_offset_like_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        font_family="Meiryo",
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
    )
    primary = TimingTrack(
        meta=TimingTrackMeta(offset_ms=1_000),
        lines=[TimingLine(chars=[TimingChar("主", 0)], end_ms=800)],
    )
    extra = TimingTrack(
        meta=TimingTrackMeta(offset_ms=2_000),
        lines=[TimingLine(chars=[TimingChar("副", 0)], end_ms=800)],
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer,
            style,
            (500, 1_500, 2_500),
            force_warp=True,
            track=primary,
            extra_tracks=[extra],
        )

    assert _alpha_count(frames[0]) == 0
    assert _alpha_count(frames[1]) > 0
    assert _alpha_count(frames[2]) > 0
    assert frames[1] != frames[2]


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_banded_readback_reconstructs_full_frame_exactly(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        meta=TimingTrackMeta(title="Band"),
        lines=[TimingLine(chars=[TimingChar("歌词", 0)], end_ms=1_500)],
    )
    title = TitleOverlay(
        enabled=True,
        text_template="{title}",
        font_family="Meiryo",
        font_size_px=42,
        anchor="top_left",
        offset_x=30,
        offset_y=24,
        layout_index=None,
        show_mode="head",
        duration_ms=1_500,
        fade_in_ms=0,
        fade_out_ms=0,
    )
    style = _g1_style(
        font_family="Meiryo",
        line_y_position="bottom",
        line_lead_in_ms=0,
        line_tail_ms=0,
        glow_radius_px=10,
        glow_before_radius_px=10,
        glow_after_radius_px=10,
        dual_line_layout=False,
        custom_style_schemes={},
        title_overlay=title,
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        renderer.configure_gpu(
            track,
            style,
            width=640,
            height=360,
            fps=60,
            force_warp=True,
        )
        full_event = renderer.render_gpu_frame(
            750,
            force_warp=True,
            shm_key=f"krok-gpu-full-{uuid.uuid4().hex}",
        )
        with SharedFrameRingReader.from_event(full_event) as reader:
            full_slot = reader.read_frame(full_event)
        band_event = renderer.render_gpu_frame(
            750,
            force_warp=True,
            shm_key=f"krok-gpu-bands-{uuid.uuid4().hex}",
            readback_bands=True,
        )
        with SharedFrameRingReader.from_event(band_event) as reader:
            band_slot = reader.read_frame(band_event)
        empty_event = renderer.render_gpu_frame(
            2_500,
            force_warp=True,
            shm_key=f"krok-gpu-empty-bands-{uuid.uuid4().hex}",
            readback_bands=True,
        )
        with SharedFrameRingReader.from_event(empty_event) as reader:
            empty_image = reader.read_qimage(empty_event)

    assert band_event["pixel_format"] == "bgra8888_premultiplied_bands"
    assert len(band_event["bands"]) == 2
    assert band_event["payload_bytes"] < full_event["payload_bytes"] * 0.7
    assert band_event["readback_ratio"] < 0.7
    assert band_slot.pixel_format == "bgra8888_premultiplied"
    assert band_slot.payload == full_slot.payload
    assert empty_event["bands"] == []
    assert empty_event["payload_bytes"] == 0
    empty_bits = empty_image.constBits()
    empty_bits.setsize(empty_image.sizeInBytes())
    assert not any(bytes(empty_bits))


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_export_packed_rgba_matches_qimage_unpremultiply(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = _g1_track()
    style = _g1_style(
        base_color="#80FF40",
        fill_color="#2040FF",
        decoration_kind="glow",
        glow_radius_px=6,
    )
    shm_key = f"krok-gpu-packed-{uuid.uuid4().hex}"
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        renderer.configure_gpu(
            track,
            style,
            width=160,
            height=90,
            fps=60,
            force_warp=True,
            realization_enabled=False,
        )
        full_event = renderer.render_gpu_frame(
            750,
            force_warp=True,
            shm_key=shm_key,
        )
        with SharedFrameRingReader.from_event(full_event) as reader:
            expected_image = reader.read_qimage(full_event).convertToFormat(
                QImage.Format.Format_RGBA8888
            )
            expected_bits = expected_image.constBits()
            expected_bits.setsize(expected_image.sizeInBytes())
            full_expected = bytes(expected_bits)

        crop_top = 20
        crop_height = 40
        row_bytes = 160 * 4
        expected = full_expected[
            crop_top * row_bytes : (crop_top + crop_height) * row_bytes
        ]
        renderer.configure_gpu(
            track,
            style,
            width=160,
            height=90,
            fps=60,
            force_warp=True,
            realization_enabled=False,
            export_crop_top=crop_top,
            export_crop_height=crop_height,
        )

        packed_event = renderer.render_gpu_frame(
            750,
            force_warp=True,
            shm_key=shm_key,
            packed_rgba=True,
            packed_height=crop_height,
            include_checksum=False,
        )
        with SharedFrameRingReader.from_event(packed_event) as reader:
            with reader.borrow_packed_rgba_view(packed_event) as view:
                actual = bytes(view)

        export_bands = [(10, 10), (60, 10)]
        renderer.configure_gpu(
            track,
            style,
            width=160,
            height=90,
            fps=60,
            force_warp=True,
            realization_enabled=False,
            export_bands=export_bands,
        )
        bands_event = renderer.render_gpu_frame(
            750,
            force_warp=True,
            shm_key=shm_key,
            packed_rgba=True,
            packed_height=20,
            include_checksum=False,
        )
        with SharedFrameRingReader.from_event(bands_event) as reader:
            with reader.borrow_packed_rgba_view(bands_event) as view:
                actual_bands = bytes(view)

    assert packed_event["pixel_format"] == "rgba8888"
    assert packed_event["native_pack_ms"] >= 0.0
    assert packed_event["shm_copy_ms"] == 0.0
    assert actual == expected
    assert actual_bands == b"".join(
        full_expected[top * row_bytes : (top + height) * row_bytes]
        for top, height in export_bands
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_uses_painter_resolved_display_overrides(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("A", 1_000)],
                end_ms=1_500,
                display_start_override_ms=200,
                display_end_override_ms=400,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        line_lead_in_ms=900,
        line_tail_ms=900,
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer,
            style,
            (199, 300, 1_501, 2_000),
            force_warp=True,
            track=track,
        )

    assert _alpha_count(frames[0]) == 0
    assert _alpha_count(frames[1]) > 0
    assert _alpha_count(frames[2]) == 0
    assert _alpha_count(frames[3]) == 0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize(
    ("entry_anim", "exit_anim", "axis"),
    [
        ("fade", "fade", None),
        ("slide_in", "slide_out", "x"),
        ("rise", "rise", "y"),
    ],
)
def test_gpu_g4_basic_line_animations_follow_painter(
    monkeypatch, entry_anim: str, exit_anim: str, axis: str | None
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("动画", 1_000)],
                end_ms=2_000,
                display_start_override_ms=0,
                display_end_override_ms=3_000,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        line_horizontal_layout="center",
        entry_anim=entry_anim,
        entry_lead_ms=1_000,
        exit_anim=exit_anim,
        exit_fade_ms=1_000,
    )
    timestamps = (500, 1_000, 2_500, 3_000)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    assert _alpha_count(gpu[3]) == _alpha_count(painter[3]) == 0
    for animated_index in (0, 2):
        gpu_ratio = sum(gpu[animated_index][3::4]) / sum(gpu[1][3::4])
        painter_ratio = sum(painter[animated_index][3::4]) / sum(painter[1][3::4])
        assert abs(gpu_ratio - painter_ratio) <= 0.06

    if axis is not None:
        coordinate = 0 if axis == "x" else 1

        def center(payload: bytes) -> float:
            bounds = _payload_alpha_bounds(payload)
            return (bounds[coordinate] + bounds[coordinate + 2]) / 2.0

        for animated_index in (0, 2):
            gpu_shift = center(gpu[animated_index]) - center(gpu[1])
            painter_shift = center(painter[animated_index]) - center(painter[1])
            assert abs(gpu_shift - painter_shift) <= 3.0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_char_fade_staggers_main_ruby_and_glow_like_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("A", 500),
                    TimingChar("漢", 900),
                    TimingChar("字", 1_300),
                    TimingChar("B", 1_700),
                ],
                end_ms=2_100,
                display_start_override_ms=0,
                display_end_override_ms=3_000,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="漢字",
                reading="かんじ",
                reading_parts=["かん", "じ"],
                reading_part_ms=[1_100],
                pos_start_ms=900,
                pos_end_ms=1_700,
            )
        ],
    )
    ruby_state = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FF20E0"),
        stroke=PaintFill(mode="solid", color="#301030"),
        shadow=PaintFill(mode="solid", color="#FF20E0"),
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=72,
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=32,
        ruby_stroke_width_px=2,
        ruby_stroke2_enabled=False,
        ruby_decoration_kind="glow",
        ruby_glow_before_radius_px=6,
        ruby_glow_after_radius_px=6,
        ruby_glow_concentration_level=1,
        ruby_karaoke_colors=KaraokeColors(before=ruby_state, after=ruby_state),
        stroke_width_px=3,
        stroke2_enabled=False,
        decoration_kind="glow",
        glow_before_radius_px=8,
        glow_after_radius_px=8,
        glow_concentration_level=1,
        dual_line_layout=False,
        line_horizontal_layout="center",
        entry_anim="char_fade",
        entry_lead_ms=1_000,
        exit_anim="char_fade",
        exit_fade_ms=1_000,
    )
    timestamps = (100, 350, 600, 2_750, 2_900, 3_000)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    assert _alpha_count(gpu[-1]) == _alpha_count(painter[-1]) == 0
    full_gpu_alpha = sum(gpu[2][3::4])
    full_painter_alpha = sum(painter[2][3::4])
    for index in (0, 1, 3, 4):
        gpu_ratio = sum(gpu[index][3::4]) / full_gpu_alpha
        painter_ratio = sum(painter[index][3::4]) / full_painter_alpha
        assert abs(gpu_ratio - painter_ratio) <= 0.09

    def magenta_count(payload: bytes) -> int:
        return sum(
            payload[index] > payload[index + 1] + 50
            and payload[index + 2] > payload[index + 1] + 50
            and payload[index + 3] > 8
            for index in range(0, len(payload), 4)
        )

    assert magenta_count(gpu[0]) == magenta_count(painter[0]) == 0
    assert magenta_count(gpu[1]) > 0 and magenta_count(painter[1]) > 0
    assert magenta_count(gpu[4]) == magenta_count(painter[4]) == 0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_spin_flip_transforms_all_character_layers_like_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track, style = _g4_spin_scene()
    timestamps = (100, 350, 600, 2_750, 2_900, 3_000)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
        _, fade_only = _render_g1_frames(
            renderer,
            replace(style, entry_anim="char_fade", exit_anim="char_fade"),
            timestamps,
            force_warp=True,
            track=track,
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    assert _alpha_count(gpu[-1]) == _alpha_count(painter[-1]) == 0
    assert gpu[0] != fade_only[0]
    assert gpu[1] != fade_only[1]
    assert gpu[3] != fade_only[3]
    full_gpu_alpha = sum(gpu[2][3::4])
    full_painter_alpha = sum(painter[2][3::4])
    for index in (0, 1, 3, 4):
        gpu_ratio = sum(gpu[index][3::4]) / full_gpu_alpha
        painter_ratio = sum(painter[index][3::4]) / full_painter_alpha
        assert abs(gpu_ratio - painter_ratio) <= 0.12
        gpu_bounds = _payload_alpha_bounds(gpu[index])
        painter_bounds = _payload_alpha_bounds(painter[index])
        # The N3-compatible dynamic path strokes the already-transformed base
        # geometry.  Its edge therefore keeps a stable device-space width
        # while Painter's baked outline narrows with the spin matrix.
        assert all(
            abs(actual - expected) <= 52
            for actual, expected in zip(gpu_bounds, painter_bounds)
        ), (index, gpu_bounds, painter_bounds)

    # At the settled point all residual transforms are identity.
    settled_gpu_bounds = _payload_alpha_bounds(gpu[2])
    settled_painter_bounds = _payload_alpha_bounds(painter[2])
    assert all(
        abs(actual - expected) <= 8
        for actual, expected in zip(settled_gpu_bounds, settled_painter_bounds)
    )

    # Painter bakes the shadow (including its stroke silhouette and offset)
    # before applying the per-character affine transform.
    shadow_style = replace(
        style,
        decoration_kind="shadow",
        shadow_offset_x=12,
        shadow_offset_y=10,
        ruby_decoration_kind="shadow",
        ruby_shadow_offset_x=6,
        ruby_shadow_offset_y=5,
    )
    shadow_timestamps = (100, 350, 2_750, 2_900)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, shadow_gpu = _render_g1_frames(
            renderer,
            shadow_style,
            shadow_timestamps,
            force_warp=True,
            track=track,
        )
    shadow_painter = [
        _render_painter_oracle(shadow_style, t_ms=t_ms, track=track)
        for t_ms in shadow_timestamps
    ]
    for gpu_frame, painter_frame in zip(shadow_gpu, shadow_painter):
        assert all(
            abs(actual - expected) <= 52
            for actual, expected in zip(
                _payload_alpha_bounds(gpu_frame),
                _payload_alpha_bounds(painter_frame),
            )
        ), (_payload_alpha_bounds(gpu_frame), _payload_alpha_bounds(painter_frame))


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_char_drip_matches_painter_right_edge_shear(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("A", 1_000),
                    TimingChar("B", 1_300),
                    TimingChar("C", 1_600),
                ],
                end_ms=1_900,
                display_start_override_ms=0,
                display_end_override_ms=3_000,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=72,
        dual_line_layout=False,
        line_horizontal_layout="center",
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="glow",
        glow_before_radius_px=8,
        glow_after_radius_px=8,
        glow_concentration_level=1,
        entry_anim="char_drip",
        entry_lead_ms=1_000,
        exit_anim="char_drip",
        exit_fade_ms=1_000,
    )
    timestamps = (125, 600, 2_875)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
        _, fade_only = _render_g1_frames(
            renderer,
            replace(style, entry_anim="char_fade", exit_anim="char_fade"),
            timestamps,
            force_warp=True,
            track=track,
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    assert gpu[0] != fade_only[0]
    assert gpu[2] != fade_only[2]
    for index in (0, 1, 2):
        assert all(
            abs(actual - expected) <= 18
            for actual, expected in zip(
                _payload_alpha_bounds(gpu[index]),
                _payload_alpha_bounds(painter[index]),
            )
        ), (index, _payload_alpha_bounds(gpu[index]), _payload_alpha_bounds(painter[index]))


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize(
    ("animation", "t_ms"),
    [("utopia", 350), ("spin_flip", 125), ("char_drip", 350)],
)
def test_gpu_dynamic_overflow_band_readback_matches_full_frame(
    animation: str, t_ms: int, monkeypatch
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track, base_style = _g4_spin_scene()
    style = replace(
        base_style,
        stroke_width_px=15,
        stroke2_enabled=True,
        stroke2_width_px=3,
        ruby_stroke_width_px=4,
        ruby_stroke2_enabled=True,
        ruby_stroke2_width_px=2,
        entry_anim=animation,
        exit_anim=animation,
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        renderer.configure_gpu(
            track, style, width=640, height=360, fps=60, force_warp=True
        )
        full_event = renderer.render_gpu_frame(
            t_ms,
            force_warp=True,
            shm_key=f"krok-gpu-dynamic-full-{uuid.uuid4().hex}",
        )
        with SharedFrameRingReader.from_event(full_event) as reader:
            full_frame = bytes(reader.read_frame(full_event).payload)
        band_event = renderer.render_gpu_frame(
            t_ms,
            force_warp=True,
            shm_key=f"krok-gpu-dynamic-band-{uuid.uuid4().hex}",
            readback_bands=True,
        )
        with SharedFrameRingReader.from_event(band_event) as reader:
            band_frame = bytes(reader.read_frame(band_event).payload)

    assert _alpha_count(full_frame) > 0
    assert band_event["pixel_format"] == "bgra8888_premultiplied_bands"
    if animation != "char_drip":
        assert band_event["readback_ratio"] < 1.0
    assert band_frame == full_frame


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("animation", ["utopia", "spin_flip", "char_drip"])
def test_gpu_dynamic_actions_use_direct_stroke_without_expanded_frame_geometry(
    animation: str, monkeypatch
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track, base_style = _g4_spin_scene()
    style = replace(
        base_style,
        stroke_width_px=15,
        stroke2_enabled=True,
        stroke2_width_px=3,
        ruby_stroke_width_px=4,
        ruby_stroke2_enabled=True,
        ruby_stroke2_width_px=2,
        entry_anim=animation,
        exit_anim=animation,
    )

    def render(direct_stroke: bool) -> tuple[dict, bytes]:
        monkeypatch.setenv(
            "KROK_GPU_DYNAMIC_DIRECT_STROKE", "1" if direct_stroke else "0"
        )
        with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
            renderer.configure_gpu(
                track, style, width=640, height=360, fps=60, force_warp=True
            )
            event = renderer.render_gpu_frame(350, force_warp=True)
            with SharedFrameRingReader.from_event(event) as reader:
                payload = bytes(reader.read_frame(event).payload)
        return event, payload

    direct_event, direct_frame = render(True)
    legacy_event, legacy_frame = render(False)

    assert _alpha_count(direct_frame) > 0
    assert direct_event["stroke_draw"] > 0
    assert direct_event["stroke2_draw"] > 0
    assert direct_event["geometry_created_dynamic"] < legacy_event[
        "geometry_created_dynamic"
    ]
    # Constant-width N3 edges intentionally differ during a non-identity
    # transform, but the visible silhouette must remain in the same region.
    assert all(
        abs(actual - expected) <= 52
        for actual, expected in zip(
            _payload_alpha_bounds(direct_frame),
            _payload_alpha_bounds(legacy_frame),
        )
    ), (
        animation,
        _payload_alpha_bounds(direct_frame),
        _payload_alpha_bounds(legacy_frame),
        direct_event["geometry_created_dynamic"],
        legacy_event["geometry_created_dynamic"],
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("animation", ["utopia", "spin_flip", "char_drip"])
def test_gpu_dynamic_direct_stroke_preserves_alpha_fill_body_protection(
    animation: str, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    image_path = tmp_path / "dynamic-alpha-fill.png"
    source = QImage(1, 1, QImage.Format.Format_RGBA8888)
    source.fill(QColor(255, 255, 255, 128))
    assert source.save(str(image_path))
    image_fill = PaintFill(
        mode="image", image_path=str(image_path), image_scale_pct=100
    )
    state = KaraokeColorState(
        text=image_fill,
        stroke=PaintFill(mode="solid", color="#FF0000"),
    )
    track, base_style = _g4_spin_scene()
    style = replace(
        base_style,
        stroke_width_px=15,
        stroke2_enabled=False,
        decoration_kind="none",
        karaoke_colors=KaraokeColors(before=state, after=state),
        entry_anim=animation,
        exit_anim=animation,
    )

    def render(direct_stroke: bool) -> bytes:
        monkeypatch.setenv(
            "KROK_GPU_DYNAMIC_DIRECT_STROKE", "1" if direct_stroke else "0"
        )
        with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
            _, frames = _render_g1_frames(
                renderer, style, (350,), force_warp=True, track=track
            )
        return frames[0]

    # The optimized path deliberately keeps the transformed outside-only
    # outline for translucent/image glyph bodies. Independent D2D processes
    # can differ by a few antialiasing values, so gate the visible result and
    # silhouette instead of requiring byte identity.
    direct = np.frombuffer(render(True), dtype=np.uint8).reshape(-1, 4)
    legacy = np.frombuffer(render(False), dtype=np.uint8).reshape(-1, 4)
    visible = (direct[:, 3] > 2) | (legacy[:, 3] > 2)
    delta = np.abs(
        direct[visible].astype(np.int16) - legacy[visible].astype(np.int16)
    )
    direct_ink = direct[:, 3] > 2
    legacy_ink = legacy[:, 3] > 2
    union = np.count_nonzero(direct_ink | legacy_ink)
    intersection = np.count_nonzero(direct_ink & legacy_ink)
    assert float(np.mean(delta)) <= 1.0
    assert float(np.percentile(delta, 99)) <= 16
    assert intersection / max(union, 1) >= 0.995


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("effect", ["char_fade", "spin_flip"])
@pytest.mark.parametrize("utopia_phase", ["entry", "exit"])
def test_gpu_g4_utopia_does_not_override_opposite_character_transition(
    monkeypatch,
    effect: str,
    utopia_phase: str,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track, base = _g4_utopia_main_scene()
    base = replace(
        base,
        stroke_width_px=0,
        decoration_kind="none",
        glow_before_radius_px=0,
        glow_after_radius_px=0,
    )
    if utopia_phase == "entry":
        style = replace(base, entry_anim="utopia", exit_anim=effect)
        reference = replace(base, entry_anim="utopia", exit_anim="none")
        t_ms = 4_000
    else:
        style = replace(base, entry_anim=effect, exit_anim="utopia")
        reference = replace(base, entry_anim="none", exit_anim="utopia")
        t_ms = 200

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, (t_ms,), force_warp=True, track=track
        )
        _, gpu_reference = _render_g1_frames(
            renderer, reference, (t_ms,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=t_ms, track=track)
    painter_reference = _render_painter_oracle(
        reference, t_ms=t_ms, track=track
    )

    assert gpu[0] != gpu_reference[0]
    gpu_ratio = sum(gpu[0][3::4]) / max(sum(gpu_reference[0][3::4]), 1)
    painter_ratio = sum(painter[3::4]) / max(sum(painter_reference[3::4]), 1)
    assert abs(gpu_ratio - painter_ratio) <= 0.15


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_utopia_main_intro_wipe_and_outro_follow_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track, style = _g4_utopia_main_scene()
    timestamps = (100, 350, 700, 1_250, 2_400, 3_600, 4_300)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    assert _alpha_count(gpu[-1]) == _alpha_count(painter[-1]) == 0
    settled_gpu_alpha = sum(gpu[2][3::4])
    settled_painter_alpha = sum(painter[2][3::4])
    for index in range(len(timestamps) - 1):
        gpu_ratio = sum(gpu[index][3::4]) / settled_gpu_alpha
        painter_ratio = sum(painter[index][3::4]) / settled_painter_alpha
        assert abs(gpu_ratio - painter_ratio) <= 0.14, (
            timestamps[index], gpu_ratio, painter_ratio
        )
        assert all(
            abs(actual - expected) <= 14
            for actual, expected in zip(
                _payload_alpha_bounds(gpu[index]),
                _payload_alpha_bounds(painter[index]),
            )
        ), (
            timestamps[index],
            _payload_alpha_bounds(gpu[index]),
            _payload_alpha_bounds(painter[index]),
        )

    shadow_style = replace(
        style,
        decoration_kind="shadow",
        shadow_offset_x=12,
        shadow_offset_y=10,
        ruby_decoration_kind="shadow",
        ruby_shadow_offset_x=6,
        ruby_shadow_offset_y=5,
    )
    shadow_timestamps = (350, 2_300, 3_600)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, shadow_gpu = _render_g1_frames(
            renderer,
            shadow_style,
            shadow_timestamps,
            force_warp=True,
            track=track,
        )
    shadow_painter = [
        _render_painter_oracle(shadow_style, t_ms=t_ms, track=track)
        for t_ms in shadow_timestamps
    ]
    for gpu_frame, painter_frame in zip(shadow_gpu, shadow_painter):
        assert all(
            abs(actual - expected) <= 12
            for actual, expected in zip(
                _payload_alpha_bounds(gpu_frame),
                _payload_alpha_bounds(painter_frame),
            )
        ), (_payload_alpha_bounds(gpu_frame), _payload_alpha_bounds(painter_frame))


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_utopia_karaoke_bounce_is_independent_and_legacy_compatible(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track, legacy_style = _g4_utopia_main_scene()
    explicit_style = replace(legacy_style, karaoke_anim="utopia")
    disabled_style = replace(legacy_style, karaoke_anim="none")
    standalone_style = replace(
        legacy_style,
        entry_anim="none",
        exit_anim="none",
        karaoke_anim="utopia",
    )
    timestamps = (350, 1_350, 3_600)

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, legacy_gpu = _render_g1_frames(
            renderer, legacy_style, timestamps, force_warp=True, track=track
        )
        _, explicit_gpu = _render_g1_frames(
            renderer, explicit_style, timestamps, force_warp=True, track=track
        )
        _, disabled_gpu = _render_g1_frames(
            renderer, disabled_style, (1_350,), force_warp=True, track=track
        )
        _, standalone_gpu = _render_g1_frames(
            renderer, standalone_style, (1_350,), force_warp=True, track=track
        )

    legacy_painter = [
        _render_painter_oracle(legacy_style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]
    explicit_painter = [
        _render_painter_oracle(explicit_style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]
    disabled_painter = _render_painter_oracle(
        disabled_style, t_ms=1_350, track=track
    )
    standalone_painter = _render_painter_oracle(
        standalone_style, t_ms=1_350, track=track
    )

    assert legacy_gpu == explicit_gpu
    assert legacy_painter == explicit_painter
    assert legacy_gpu[1] == standalone_gpu[0]
    assert legacy_painter[1] == standalone_painter
    assert legacy_gpu[1] != disabled_gpu[0]
    assert legacy_painter[1] != disabled_painter

    ruby_track, ruby_legacy_style = _g4_utopia_ruby_scene()
    ruby_standalone_style = replace(
        ruby_legacy_style,
        entry_anim="none",
        exit_anim="none",
        karaoke_anim="utopia",
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, ruby_legacy_gpu = _render_g1_frames(
            renderer,
            ruby_legacy_style,
            (1_800,),
            force_warp=True,
            track=ruby_track,
        )
        _, ruby_standalone_gpu = _render_g1_frames(
            renderer,
            ruby_standalone_style,
            (1_800,),
            force_warp=True,
            track=ruby_track,
        )
    assert ruby_legacy_gpu == ruby_standalone_gpu
    assert _render_painter_oracle(
        ruby_legacy_style, t_ms=1_800, track=ruby_track
    ) == _render_painter_oracle(
        ruby_standalone_style, t_ms=1_800, track=ruby_track
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_utopia_ruby_units_and_group_outro_follow_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track, style = _g4_utopia_ruby_scene()
    timestamps = (350, 700, 1_800, 2_300, 3_300, 3_600, 4_300)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    assert _alpha_count(gpu[-1]) == _alpha_count(painter[-1]) == 0
    full_gpu_alpha = sum(gpu[1][3::4])
    full_painter_alpha = sum(painter[1][3::4])
    for index in range(len(timestamps) - 1):
        gpu_ratio = sum(gpu[index][3::4]) / full_gpu_alpha
        painter_ratio = sum(painter[index][3::4]) / full_painter_alpha
        assert abs(gpu_ratio - painter_ratio) <= 0.15, (
            timestamps[index], gpu_ratio, painter_ratio
        )
        assert all(
            abs(actual - expected) <= 16
            for actual, expected in zip(
                _payload_alpha_bounds(gpu[index]),
                _payload_alpha_bounds(painter[index]),
            )
        ), (
            timestamps[index],
            _payload_alpha_bounds(gpu[index]),
            _payload_alpha_bounds(painter[index]),
        )

    shadow_style = replace(
        style,
        decoration_kind="shadow",
        shadow_offset_x=12,
        shadow_offset_y=10,
        ruby_decoration_kind="shadow",
        ruby_shadow_offset_x=6,
        ruby_shadow_offset_y=5,
    )
    shadow_timestamps = (350, 2_300, 3_600)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, shadow_gpu = _render_g1_frames(
            renderer,
            shadow_style,
            shadow_timestamps,
            force_warp=True,
            track=track,
        )
    shadow_painter = [
        _render_painter_oracle(shadow_style, t_ms=t_ms, track=track)
        for t_ms in shadow_timestamps
    ]
    for gpu_frame, painter_frame in zip(shadow_gpu, shadow_painter):
        assert all(
            abs(actual - expected) <= 14
            for actual, expected in zip(
                _payload_alpha_bounds(gpu_frame),
                _payload_alpha_bounds(painter_frame),
            )
        ), (_payload_alpha_bounds(gpu_frame), _payload_alpha_bounds(painter_frame))


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_volume_signal_timing_union_layout_and_colors_follow_painter(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("Signal", 4_000)],
                end_ms=5_000,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=64,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        line_horizontal_layout="center",
        line_lead_in_ms=500,
        line_tail_ms=500,
        lit_enabled=True,
        lit_style="volume",
        signals_duration_ms=4_000,
        lit_waiting_time_ms=0,
        lit_time_offset_ms=0,
        lit_opacity_pct=80,
        lit_stroke_width=3,
        volume_size=52,
        volume_column_width=14,
        volume_column_count=4,
        volume_column_spacing=3,
        volume_align=1,
        volume_ratio=3.0,
        volume_offset_x=-8,
        volume_offset_y=4,
        volume_fill_color="#F8F8F8",
        volume_stroke_color="#1040FF",
        volume_overlay_fill_color="#1040FF",
        volume_overlay_stroke_color="#FFFFFF",
        volume_flash_times=3,
        volume_flash_duration_ratio=1.0,
        volume_transition_ratio_pct=67,
    )
    timestamps = (0, 500, 1_000, 2_500, 3_000, 3_500, 3_999, 4_500)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    for t_ms, gpu_frame, painter_frame in zip(timestamps, gpu, painter):
        assert all(
            abs(actual - expected) <= 12
            for actual, expected in zip(
                _payload_alpha_bounds(gpu_frame),
                _payload_alpha_bounds(painter_frame),
            )
        ), (
            t_ms,
            _payload_alpha_bounds(gpu_frame),
            _payload_alpha_bounds(painter_frame),
        )
    # The flash phase disappears at 500 ms in both implementations, then the
    # overlay advances left-to-right during the final fill phase.
    assert sum(gpu[1][3::4]) < sum(gpu[0][3::4])
    assert sum(painter[1][3::4]) < sum(painter[0][3::4])
    gpu_text_alpha = sum(gpu[1][3::4])
    painter_text_alpha = sum(painter[1][3::4])
    for index in (0, 2, 4, 5, 6):
        gpu_signal_alpha = sum(gpu[index][3::4]) - gpu_text_alpha
        painter_signal_alpha = sum(painter[index][3::4]) - painter_text_alpha
        assert abs(gpu_signal_alpha / painter_signal_alpha - 1.0) <= 0.05

    def blue_pixels(payload: bytes) -> int:
        return sum(
            payload[index + 2] > payload[index] + 40
            and payload[index + 2] > payload[index + 1] + 20
            and payload[index + 3] > 16
            for index in range(0, len(payload), 4)
        )

    assert blue_pixels(gpu[4]) < blue_pixels(gpu[6])
    assert blue_pixels(painter[4]) < blue_pixels(painter[6])


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("lit_style", ["circle", "square", "rounded"])
@pytest.mark.parametrize("transition_mode", ["fade", "slide"])
def test_gpu_g4_shape_signal_geometry_and_extinguish_transition_follow_painter(
    monkeypatch, lit_style: str, transition_mode: str
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("Signal", 4_000)], end_ms=5_000)]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=64,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        line_horizontal_layout="center",
        line_lead_in_ms=500,
        line_tail_ms=500,
        lit_enabled=True,
        lit_style=lit_style,
        lit_number=4,
        lit_size=34,
        lit_offset_x=-6,
        lit_offset_y=-12,
        lit_tracking=5,
        lit_fill_color="#2040FF",
        lit_stroke_color="#FFFFFF",
        lit_stroke_width=3,
        lit_stroke_soften=2,
        lit_opacity_pct=85,
        lit_edge_brightness_pct=70,
        lit_shadow=True,
        signals_duration_ms=4_000,
        lit_waiting_time_ms=0,
        lit_time_offset_ms=0,
        lit_transition_mode=transition_mode,
        lit_transition_ratio_pct=67,
        lit_transition_angle_deg=35,
        lit_transition_distance=28,
    )
    timestamps = (0, 500, 1_000, 1_500, 2_000, 2_500, 3_000, 3_500, 4_000)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    for t_ms, gpu_frame, painter_frame in zip(timestamps, gpu, painter):
        assert all(
            abs(actual - expected) <= 13
            for actual, expected in zip(
                _payload_alpha_bounds(gpu_frame),
                _payload_alpha_bounds(painter_frame),
            )
        ), (
            lit_style,
            transition_mode,
            t_ms,
            _payload_alpha_bounds(gpu_frame),
            _payload_alpha_bounds(painter_frame),
        )

    def blue_pixels(payload: bytes) -> int:
        return sum(
            payload[index + 2] > payload[index] + 50
            and payload[index + 2] > payload[index + 1] + 20
            and payload[index + 3] > 16
            for index in range(0, len(payload), 4)
        )

    gpu_counts = [blue_pixels(frame) for frame in gpu[:-1]]
    painter_counts = [blue_pixels(frame) for frame in painter[:-1]]
    assert gpu_counts[0] > gpu_counts[-1] > 0
    assert painter_counts[0] > painter_counts[-1] > 0
    assert blue_pixels(gpu[-1]) == blue_pixels(painter[-1]) == 0
    gpu_text_alpha = sum(gpu[-1][3::4])
    painter_text_alpha = sum(painter[-1][3::4])
    gpu_full_signal = sum(gpu[0][3::4]) - gpu_text_alpha
    painter_full_signal = sum(painter[0][3::4]) - painter_text_alpha
    assert gpu_full_signal > 0 and painter_full_signal > 0
    for index in range(1, len(timestamps) - 1):
        gpu_ratio = (sum(gpu[index][3::4]) - gpu_text_alpha) / gpu_full_signal
        painter_ratio = (
            sum(painter[index][3::4]) - painter_text_alpha
        ) / painter_full_signal
        assert abs(gpu_ratio - painter_ratio) <= 0.08, (
            lit_style,
            transition_mode,
            timestamps[index],
            gpu_ratio,
            painter_ratio,
        )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("decoration_kind", ["shadow", "glow"])
def test_gpu_g4_rtl_main_layout_and_right_to_left_wipe_follow_painter(
    monkeypatch, decoration_kind: str
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("W", 0),
                    TimingChar("i", 500),
                    TimingChar("M", 1_000),
                    TimingChar(".", 1_500),
                ],
                end_ms=2_000,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=72,
        right_to_left=True,
        dual_line_layout=False,
        line_y_position="center",
        line_horizontal_layout="center",
        line_lead_in_ms=0,
        line_tail_ms=0,
        stroke_width_px=3,
        stroke2_enabled=True,
        stroke2_width_px=2,
        decoration_kind=decoration_kind,
        shadow_offset_x=7,
        shadow_offset_y=6,
        glow_before_radius_px=8,
        glow_after_radius_px=8,
        glow_concentration_level=1,
    )
    timestamps = (0, 250, 500, 750, 1_000, 1_250, 1_500, 1_750, 2_000)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    def after_pixels(payload: bytes) -> int:
        return sum(
            payload[index] > payload[index + 1] + 60
            and payload[index] > payload[index + 2] + 30
            and payload[index + 3] > 16
            for index in range(0, len(payload), 4)
        )

    for gpu_frame, painter_frame in zip(gpu, painter):
        assert all(
            abs(actual - expected) <= 10
            for actual, expected in zip(
                _payload_alpha_bounds(gpu_frame),
                _payload_alpha_bounds(painter_frame),
            )
        )
    gpu_full = after_pixels(gpu[-1])
    painter_full = after_pixels(painter[-1])
    assert gpu_full > 0 and painter_full > 0
    for gpu_frame, painter_frame in zip(gpu, painter):
        assert abs(
            after_pixels(gpu_frame) / gpu_full
            - after_pixels(painter_frame) / painter_full
        ) <= 0.08


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_rtl_ruby_reverses_visual_units_and_keeps_empty_timing_pause(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("\u6f22", 0), TimingChar("\u5b57", 1_000)],
                end_ms=2_000,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="\u6f22\u5b57",
                reading="\u304d\u3083\u304b\u3099",
                reading_parts=["\u304d\u3083", "", "\u304b\u3099"],
                reading_part_ms=[650, 1_300],
                pos_start_ms=0,
                pos_end_ms=2_000,
            )
        ],
    )
    transparent = KaraokeColorState(
        text=PaintFill(mode="solid", color="#00000000"),
        stroke=PaintFill(mode="solid", color="#00000000"),
        shadow=PaintFill(mode="solid", color="#00000000"),
    )
    ruby_before = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFF2020")
    )
    ruby_after = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FF2040FF")
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=64,
        right_to_left=True,
        dual_line_layout=False,
        line_y_position="center",
        line_horizontal_layout="center",
        line_y_margin_px=22,
        line_lead_in_ms=0,
        line_tail_ms=0,
        karaoke_colors=KaraokeColors(before=transparent, after=transparent),
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=28,
        ruby_gap_px=4,
        ruby_stroke_width_px=0,
        ruby_stroke2_enabled=False,
        ruby_decoration_kind="none",
        ruby_karaoke_colors=KaraokeColors(before=ruby_before, after=ruby_after),
    )
    timestamps = (0, 300, 800, 1_200, 1_600, 2_000)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    def blue_positions(payload: bytes) -> list[int]:
        return [
            (index // 4) % 640
            for index in range(0, len(payload), 4)
            if payload[index + 2] > payload[index] + 40
            and payload[index + 2] > payload[index + 1] + 20
            and payload[index + 3] > 16
        ]

    for gpu_frame, painter_frame in zip(gpu, painter):
        gpu_bounds = _payload_alpha_bounds(gpu_frame)
        painter_bounds = _payload_alpha_bounds(painter_frame)
        assert all(
            abs(actual - expected) <= 5
            for actual, expected in zip(
                gpu_bounds,
                painter_bounds,
            )
        ), (gpu_bounds, painter_bounds)
    gpu_full = len(blue_positions(gpu[-1]))
    painter_full = len(blue_positions(painter[-1]))
    assert gpu_full > 0 and painter_full > 0
    for gpu_frame, painter_frame in zip(gpu, painter):
        gpu_positions = blue_positions(gpu_frame)
        painter_positions = blue_positions(painter_frame)
        assert abs(
            len(gpu_positions) / gpu_full
            - len(painter_positions) / painter_full
        ) <= 0.12
        if gpu_positions and painter_positions:
            assert abs(min(gpu_positions) - min(painter_positions)) <= 10
            assert abs(max(gpu_positions) - max(painter_positions)) <= 10
    assert len(blue_positions(gpu[2])) == len(blue_positions(gpu[3]))
    assert len(blue_positions(painter[2])) == len(blue_positions(painter[3]))
    # The first logical reading unit is visually placed on the right in RTL.
    gpu_initial = blue_positions(gpu[1])
    painter_initial = blue_positions(painter[1])
    assert sum(gpu_initial) / len(gpu_initial) > 320
    assert sum(painter_initial) / len(painter_initial) > 320


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize(
    ("decoration_kind", "bounds_tolerance"), [("shadow", 6), ("glow", 7)]
)
def test_gpu_g4_rtl_ruby_shadow_and_glow_follow_painter(
    monkeypatch, decoration_kind: str, bounds_tolerance: int
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("\u6f22", 0), TimingChar("\u5b57", 1_000)],
                end_ms=2_000,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="\u6f22\u5b57",
                reading="\u304d\u3083\u304b\u3099",
                reading_parts=["\u304d\u3083", "\u304b\u3099"],
                reading_part_ms=[1_000],
                pos_start_ms=0,
                pos_end_ms=2_000,
            )
        ],
    )
    transparent = KaraokeColorState(
        text=PaintFill(mode="solid", color="#00000000"),
        stroke=PaintFill(mode="solid", color="#00000000"),
        shadow=PaintFill(mode="solid", color="#00000000"),
    )
    ruby_state = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFFFFFF"),
        stroke=PaintFill(mode="solid", color="#101010"),
        shadow=PaintFill(mode="solid", color="#20FF40"),
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=64,
        right_to_left=True,
        dual_line_layout=False,
        line_y_position="center",
        line_horizontal_layout="center",
        line_lead_in_ms=0,
        line_tail_ms=0,
        karaoke_colors=KaraokeColors(before=transparent, after=transparent),
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=28,
        ruby_gap_px=4,
        ruby_stroke_width_px=2,
        ruby_stroke2_enabled=True,
        ruby_stroke2_width_px=1,
        ruby_decoration_kind=decoration_kind,
        ruby_shadow_offset_x=7,
        ruby_shadow_offset_y=9,
        ruby_glow_before_radius_px=8,
        ruby_glow_after_radius_px=8,
        ruby_glow_concentration_level=1,
        ruby_karaoke_colors=KaraokeColors(before=ruby_state, after=ruby_state),
    )
    timestamps = (0, 500, 1_500, 2_000)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    for gpu_frame, painter_frame in zip(gpu, painter):
        assert all(
            abs(actual - expected) <= bounds_tolerance
            for actual, expected in zip(
                _payload_alpha_bounds(gpu_frame),
                _payload_alpha_bounds(painter_frame),
            )
        ), (
            decoration_kind,
            _payload_alpha_bounds(gpu_frame),
            _payload_alpha_bounds(painter_frame),
        )
    gpu_full = sum(gpu[-1][3::4])
    painter_full = sum(painter[-1][3::4])
    for gpu_frame, painter_frame in zip(gpu, painter):
        assert abs(
            sum(gpu_frame[3::4]) / gpu_full
            - sum(painter_frame[3::4]) / painter_full
        ) <= 0.03


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_vertical_ruby_geometry_and_empty_timing_slot_follow_painter(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("\u6f22", 0), TimingChar("\u5b57", 1_000)],
                end_ms=2_000,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="\u6f22\u5b57",
                reading="\u304b\u3093\u3058",
                reading_parts=["\u304b", "", "\u3093\u3058"],
                reading_part_ms=[650, 1_300],
                pos_start_ms=0,
                pos_end_ms=2_000,
            )
        ],
    )
    transparent = KaraokeColorState(
        text=PaintFill(mode="solid", color="#00000000"),
        stroke=PaintFill(mode="solid", color="#00000000"),
        shadow=PaintFill(mode="solid", color="#00000000"),
    )
    ruby_before = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFF2020")
    )
    ruby_after = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FF2040FF")
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=64,
        vertical=True,
        dual_line_layout=False,
        line_y_position="center",
        line_y_margin_px=22,
        line_lead_in_ms=0,
        line_tail_ms=0,
        karaoke_colors=KaraokeColors(before=transparent, after=transparent),
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=28,
        ruby_gap_px=4,
        ruby_stroke_width_px=0,
        ruby_stroke2_enabled=False,
        ruby_decoration_kind="none",
        ruby_karaoke_colors=KaraokeColors(before=ruby_before, after=ruby_after),
    )
    timestamps = (0, 300, 800, 1_200, 1_600, 2_000)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    def blue_pixels(payload: bytes) -> int:
        return sum(
            payload[index + 2] > payload[index] + 40
            and payload[index + 2] > payload[index + 1] + 20
            and payload[index + 3] > 16
            for index in range(0, len(payload), 4)
        )

    for gpu_frame, painter_frame in zip(gpu, painter):
        assert all(
            abs(actual - expected) <= 10
            for actual, expected in zip(
                _payload_alpha_bounds(gpu_frame),
                _payload_alpha_bounds(painter_frame),
            )
        )
    gpu_full = blue_pixels(gpu[-1])
    painter_full = blue_pixels(painter[-1])
    assert gpu_full > 0 and painter_full > 0
    for gpu_frame, painter_frame in zip(gpu, painter):
        assert abs(
            blue_pixels(gpu_frame) / gpu_full
            - blue_pixels(painter_frame) / painter_full
        ) <= 0.05
    # The empty middle reading part still owns its interval, so neither oracle
    # advances the visible wipe between these two samples.
    assert blue_pixels(gpu[2]) == blue_pixels(gpu[3])
    assert blue_pixels(painter[2]) == blue_pixels(painter[3])


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize(
    ("decoration_kind", "bounds_tolerance", "trajectory_tolerance"),
    [("shadow", 2, 0.02), ("glow", 8, 0.03)],
)
def test_gpu_g4_vertical_ruby_shadow_and_glow_follow_painter(
    monkeypatch,
    decoration_kind: str,
    bounds_tolerance: int,
    trajectory_tolerance: float,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("\u6f22", 0), TimingChar("\u5b57", 1_000)],
                end_ms=2_000,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="\u6f22\u5b57",
                reading="\u304b\u3093\u3058",
                reading_parts=["\u304b\u3093", "\u3058"],
                reading_part_ms=[1_000],
                pos_start_ms=0,
                pos_end_ms=2_000,
            )
        ],
    )
    transparent = KaraokeColorState(
        text=PaintFill(mode="solid", color="#00000000"),
        stroke=PaintFill(mode="solid", color="#00000000"),
        shadow=PaintFill(mode="solid", color="#00000000"),
    )
    ruby_state = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFFFFFF"),
        stroke=PaintFill(mode="solid", color="#101010"),
        shadow=PaintFill(mode="solid", color="#20FF40"),
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=64,
        vertical=True,
        dual_line_layout=False,
        line_y_position="center",
        line_y_margin_px=22,
        line_lead_in_ms=0,
        line_tail_ms=0,
        karaoke_colors=KaraokeColors(before=transparent, after=transparent),
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=28,
        ruby_gap_px=4,
        ruby_stroke_width_px=2,
        ruby_stroke2_enabled=True,
        ruby_stroke2_width_px=1,
        ruby_decoration_kind=decoration_kind,
        ruby_shadow_offset_x=7,
        ruby_shadow_offset_y=9,
        ruby_glow_before_radius_px=8,
        ruby_glow_after_radius_px=8,
        ruby_glow_concentration_level=1,
        ruby_karaoke_colors=KaraokeColors(before=ruby_state, after=ruby_state),
    )
    timestamps = (0, 500, 1_500, 2_000)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    for gpu_frame, painter_frame in zip(gpu, painter):
        assert all(
            abs(actual - expected) <= bounds_tolerance
            for actual, expected in zip(
                _payload_alpha_bounds(gpu_frame),
                _payload_alpha_bounds(painter_frame),
            )
        )
    gpu_full = sum(gpu[-1][3::4])
    painter_full = sum(painter[-1][3::4])
    assert gpu_full > 0 and painter_full > 0
    for gpu_frame, painter_frame in zip(gpu, painter):
        assert abs(
            sum(gpu_frame[3::4]) / gpu_full
            - sum(painter_frame[3::4]) / painter_full
        ) <= trajectory_tolerance


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("line_y_position", ["top", "center", "bottom"])
def test_gpu_g4_vertical_main_glyph_orientation_wipe_and_shadow_follow_painter(
    monkeypatch, line_y_position: str
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("縦", 0),
                    TimingChar("A", 500),
                    TimingChar("ー", 1_000),
                    TimingChar("。", 1_500),
                ],
                end_ms=2_000,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=64,
        vertical=True,
        dual_line_layout=False,
        line_y_position=line_y_position,
        line_y_margin_px=22,
        line_lead_in_ms=0,
        line_tail_ms=0,
        stroke_width_px=3,
        stroke2_enabled=True,
        stroke2_width_px=2,
        decoration_kind="shadow",
        shadow_offset_x=7,
        shadow_offset_y=6,
    )
    timestamps = (0, 250, 750, 1_250, 1_750, 2_000)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    for t_ms, gpu_frame, painter_frame in zip(timestamps, gpu, painter):
        assert all(
            abs(actual - expected) <= 3
            for actual, expected in zip(
                _payload_alpha_bounds(gpu_frame),
                _payload_alpha_bounds(painter_frame),
            )
        ), (
            line_y_position,
            t_ms,
            _payload_alpha_bounds(gpu_frame),
            _payload_alpha_bounds(painter_frame),
        )
        gpu_alpha = sum(gpu_frame[3::4])
        painter_alpha = sum(painter_frame[3::4])
        assert abs(gpu_alpha / painter_alpha - 1.0) <= 0.015


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_vertical_dual_columns_flow_right_to_left_like_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(char, 0) for char in "右列"],
                end_ms=1_000,
            ),
            TimingLine(
                chars=[TimingChar(char, 0) for char in "左列"],
                end_ms=1_000,
            ),
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=60,
        vertical=True,
        dual_line_layout=True,
        line_y_position="center",
        line_y_margin_px=28,
        line_gap_px=24,
        line_lead_in_ms=0,
        line_tail_ms=0,
        stroke_width_px=2,
        stroke2_enabled=False,
        decoration_kind="shadow",
        shadow_offset_x=5,
        shadow_offset_y=4,
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, (500,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=500, track=track)

    assert all(
        abs(actual - expected) <= 4
        for actual, expected in zip(
            _payload_alpha_bounds(gpu[0]), _payload_alpha_bounds(painter)
        )
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_vertical_glow_before_after_clip_follows_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("光", 0),
                    TimingChar("ー", 600),
                    TimingChar("彩", 1_200),
                ],
                end_ms=1_800,
            )
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=68,
        vertical=True,
        dual_line_layout=False,
        line_y_position="center",
        line_lead_in_ms=0,
        line_tail_ms=0,
        stroke_width_px=3,
        stroke2_enabled=True,
        stroke2_width_px=2,
        decoration_kind="glow",
        glow_before_radius_px=10,
        glow_after_radius_px=10,
        glow_concentration_level=1,
    )
    timestamps = (0, 300, 900, 1_500, 1_800)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True, track=track
        )
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=track)
        for t_ms in timestamps
    ]

    for t_ms, gpu_frame, painter_frame in zip(timestamps, gpu, painter):
        assert all(
            abs(actual - expected) <= 12
            for actual, expected in zip(
                _payload_alpha_bounds(gpu_frame),
                _payload_alpha_bounds(painter_frame),
            )
        ), (
            t_ms,
            _payload_alpha_bounds(gpu_frame),
            _payload_alpha_bounds(painter_frame),
        )
    full_gpu = sum(gpu[-1][3::4])
    full_painter = sum(painter[-1][3::4])
    for index in range(len(timestamps) - 1):
        gpu_ratio = sum(gpu[index][3::4]) / full_gpu
        painter_ratio = sum(painter[index][3::4]) / full_painter
        assert abs(gpu_ratio - painter_ratio) <= 0.09


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_dual_lane_alignments_follow_painter_schedule(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    def role_scheme(color: str) -> SubtitleStyleScheme:
        colors = KaraokeColors(
            before=KaraokeColorState(text=PaintFill(mode="solid", color=color)),
            after=KaraokeColorState(text=PaintFill(mode="solid", color=color)),
        )
        return SubtitleStyleScheme(
            font_family="Meiryo",
            font_family_latin="Meiryo",
            font_size_px=70,
            stroke_width_px=0,
            decoration_kind="none",
            karaoke_colors=colors,
        )

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(ch, 0, role_label="left") for ch in "AAA"],
                end_ms=1_000,
            ),
            TimingLine(
                chars=[TimingChar(ch, 0, role_label="right") for ch in "BBBB"],
                end_ms=1_000,
            ),
        ]
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=70,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=True,
        line_horizontal_layout="asymmetric",
        line_alignments=["left", "right"],
        horizontal_margin_px=44,
        smart_horizontal="none",
        custom_style_schemes={
            "left": role_scheme("#FF2020"),
            "right": role_scheme("#20FF40"),
        },
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer, style, (500,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=500, track=track)

    def color_bounds(payload: bytes, *, green: bool) -> tuple[int, int]:
        xs = []
        for index in range(0, len(payload), 4):
            red, value_green, _blue, alpha = payload[index : index + 4]
            matches_color = (
                value_green > red + 80 if green else red > value_green + 80
            )
            if alpha > 0 and matches_color:
                xs.append((index // 4) % 640)
        assert xs
        return min(xs), max(xs)

    for green in (False, True):
        gpu_bounds = color_bounds(frames[0], green=green)
        painter_bounds = color_bounds(painter, green=green)
        assert all(
            abs(actual - expected) <= 8
            for actual, expected in zip(gpu_bounds, painter_bounds)
        ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_inline_role_fonts_sizes_colors_and_strokes_match_painter(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    def colors(before: str, after: str, stroke: str) -> KaraokeColors:
        return KaraokeColors(
            before=KaraokeColorState(
                text=PaintFill(mode="solid", color=before),
                stroke=PaintFill(mode="solid", color=stroke),
            ),
            after=KaraokeColorState(
                text=PaintFill(mode="solid", color=after),
                stroke=PaintFill(mode="solid", color=stroke),
            ),
        )

    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=60,
        stroke_width_px=2,
        stroke2_enabled=False,
        decoration_kind="shadow",
        shadow_color="#00000000",
        shadow_offset_x=0,
        shadow_offset_y=0,
        dual_line_layout=False,
        custom_style_schemes={
            "lead": SubtitleStyleScheme(
                font_family="Meiryo",
                font_family_latin="Meiryo",
                font_size_px=92,
                latin_font_size_px=92,
                stroke_width_px=6,
                stroke2_enabled=False,
                karaoke_colors=colors("#2040FF", "#FF2040", "#101010"),
            ),
            "back": SubtitleStyleScheme(
                font_family="Times New Roman",
                font_family_latin="Times New Roman",
                font_size_px=50,
                latin_font_size_px=50,
                stroke_width_px=3,
                stroke2_enabled=False,
                karaoke_colors=colors("#20FF40", "#FFE020", "#202020"),
            ),
        },
    )
    track = _g3_inline_role_track()
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured, frames = _render_g1_frames(
            renderer,
            style,
            (1_200,),
            force_warp=True,
            track=track,
        )
    painter = _render_painter_oracle(style, t_ms=1_200, track=track)

    gpu_bounds = _payload_alpha_bounds(frames[0])
    painter_bounds = _payload_alpha_bounds(painter)
    assert configured["cached_chars"] == 3
    assert configured["cached_styles"] == 4
    assert abs(
        (gpu_bounds[2] - gpu_bounds[0])
        - (painter_bounds[2] - painter_bounds[0])
    ) <= 7
    assert abs(
        (gpu_bounds[3] - gpu_bounds[1])
        - (painter_bounds[3] - painter_bounds[1])
    ) <= 8
    payload = frames[0]
    assert any(
        payload[index] > payload[index + 1] + 50 and payload[index + 3] > 0
        for index in range(0, len(payload), 4)
    )
    assert any(
        payload[index + 1] > payload[index] + 50 and payload[index + 3] > 0
        for index in range(0, len(payload), 4)
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_inline_roles_use_independent_n3_glow(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    def glow_colors(color: str) -> KaraokeColors:
        state = KaraokeColorState(
            text=PaintFill(mode="solid", color="#FFFFFF"),
            stroke=PaintFill(mode="solid", color="#202020"),
            shadow=PaintFill(mode="solid", color=color),
        )
        return KaraokeColors(before=state, after=state)

    schemes = {
        "lead": SubtitleStyleScheme(
            font_family="Meiryo",
            font_family_latin="Meiryo",
            font_size_px=82,
            latin_font_size_px=82,
            stroke_width_px=4,
            decoration_kind="glow",
            glow_before_radius_px=10,
            glow_after_radius_px=10,
            glow_concentration_level=2,
            karaoke_colors=glow_colors("#00FF40"),
        ),
        "back": SubtitleStyleScheme(
            font_family="Times New Roman",
            font_family_latin="Times New Roman",
            font_size_px=52,
            latin_font_size_px=52,
            stroke_width_px=3,
            decoration_kind="glow",
            glow_before_radius_px=3,
            glow_after_radius_px=6,
            glow_concentration_level=0,
            karaoke_colors=glow_colors("#2040FF"),
        ),
    }
    glow_style = _g1_style(
        font_family="Meiryo",
        decoration_kind="shadow",
        shadow_color="#00000000",
        shadow_offset_x=0,
        shadow_offset_y=0,
        dual_line_layout=False,
        custom_style_schemes=schemes,
    )
    no_glow_style = replace(
        glow_style,
        custom_style_schemes={
            name: replace(scheme, decoration_kind="none")
            for name, scheme in schemes.items()
        },
    )
    track = _g3_inline_role_track()
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, no_glow_frames = _render_g1_frames(
            renderer,
            no_glow_style,
            (1_200,),
            force_warp=True,
            track=track,
        )
        _, glow_frames = _render_g1_frames(
            renderer,
            glow_style,
            (1_200,),
            force_warp=True,
            track=track,
        )

    assert sum(glow_frames[0][3::4]) > sum(no_glow_frames[0][3::4]) * 1.05
    payload = glow_frames[0]
    assert any(
        payload[index + 1] > payload[index] + 30
        and payload[index + 1] > payload[index + 2] + 30
        and payload[index + 3] > 0
        for index in range(0, len(payload), 4)
    )
    assert any(
        payload[index + 2] > payload[index] + 30
        and payload[index + 2] > payload[index + 1] + 30
        and payload[index + 3] > 0
        for index in range(0, len(payload), 4)
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_affects_ruby_anchor_matches_painter_direction(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    white = KaraokeColors(
        before=KaraokeColorState(text=PaintFill(color="#FFFFFF")),
        after=KaraokeColorState(text=PaintFill(color="#FFFFFF")),
    )
    red = KaraokeColors(
        before=KaraokeColorState(text=PaintFill(color="#FF0000")),
        after=KaraokeColorState(text=PaintFill(color="#FF0000")),
    )
    base = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=72,
        ruby_font_size_px=30,
        ruby_gap_px=4,
        stroke_width_px=0,
        ruby_stroke_width_px=0,
        decoration_kind="shadow",
        shadow_color="#00000000",
        shadow_offset_x=0,
        shadow_offset_y=0,
        dual_line_layout=False,
        karaoke_colors=white,
        ruby_karaoke_colors=red,
    )
    ignored = replace(
        base,
        custom_style_schemes={
            "decor": SubtitleStyleScheme(
                font_family="Meiryo",
                font_size_px=190,
                affects_ruby_anchor=False,
                karaoke_colors=white,
            )
        },
    )
    included = replace(
        ignored,
        custom_style_schemes={
            "decor": replace(
                ignored.custom_style_schemes["decor"],
                affects_ruby_anchor=True,
            )
        },
    )
    track = _g3_ruby_anchor_track()
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu_ignored = _render_g1_frames(
            renderer, ignored, (700,), force_warp=True, track=track
        )
        _, gpu_included = _render_g1_frames(
            renderer, included, (700,), force_warp=True, track=track
        )
    painter_ignored = _render_painter_oracle(ignored, t_ms=700, track=track)
    painter_included = _render_painter_oracle(included, t_ms=700, track=track)

    def ruby_top(payload: bytes) -> int:
        ys = [
            index // (640 * 4)
            for index in range(0, len(payload), 4)
            if payload[index] > 180
            and payload[index + 1] < 80
            and payload[index + 2] < 80
            and payload[index + 3] > 0
        ]
        assert ys
        return min(ys)

    gpu_delta = ruby_top(gpu_ignored[0]) - ruby_top(gpu_included[0])
    painter_delta = ruby_top(painter_ignored) - ruby_top(painter_included)
    assert gpu_delta > 20
    assert painter_delta > 20
    assert abs(gpu_delta - painter_delta) <= 6


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_role_ruby_font_colors_and_outline_match_painter(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    def solid_colors(color: str, stroke: str = "#000000") -> KaraokeColors:
        state = KaraokeColorState(
            text=PaintFill(mode="solid", color=color),
            stroke=PaintFill(mode="solid", color=stroke),
        )
        return KaraokeColors(before=state, after=state)

    style = _g1_style(
        font_family="Meiryo",
        font_size_px=72,
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Arial",
        ruby_font_size_px=24,
        ruby_gap_px=4,
        ruby_stroke_width_px=1,
        ruby_stroke2_enabled=False,
        decoration_kind="shadow",
        shadow_color="#00000000",
        shadow_offset_x=0,
        shadow_offset_y=0,
        dual_line_layout=False,
        karaoke_colors=solid_colors("#FFFFFF"),
        ruby_karaoke_colors=solid_colors("#FF2020"),
        custom_style_schemes={
            "lead": SubtitleStyleScheme(
                font_family="Meiryo",
                font_size_px=72,
                karaoke_colors=solid_colors("#2040FF"),
                ruby_font_family="Meiryo",
                ruby_font_family_latin="Times New Roman",
                ruby_font_size_px=46,
                ruby_latin_font_size_px=40,
                ruby_latin_font_weight=700,
                ruby_gap_px=7,
                ruby_stroke_width_px=5,
                ruby_stroke2_enabled=False,
                ruby_karaoke_colors=solid_colors("#20FF40", "#101010"),
            )
        },
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("A", 0, role_label="lead")],
                end_ms=1_200,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="A",
                reading="abc",
                pos_start_ms=0,
                pos_end_ms=1_200,
                reading_parts=["a", "b", "c"],
                reading_part_ms=[400, 800],
            )
        ],
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer, style, (1_000,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=1_000, track=track)

    def green_bounds(payload: bytes) -> tuple[int, int, int, int]:
        xs: list[int] = []
        ys: list[int] = []
        for index in range(0, len(payload), 4):
            if (
                payload[index + 1] > 160
                and payload[index + 1] > payload[index] + 70
                and payload[index + 1] > payload[index + 2] + 70
                and payload[index + 3] > 0
            ):
                pixel = index // 4
                xs.append(pixel % 640)
                ys.append(pixel // 640)
        assert xs and ys
        return min(xs), min(ys), max(xs), max(ys)

    gpu_bounds = green_bounds(frames[0])
    painter_bounds = green_bounds(painter)
    assert abs((gpu_bounds[2] - gpu_bounds[0]) - (painter_bounds[2] - painter_bounds[0])) <= 8
    assert abs((gpu_bounds[3] - gpu_bounds[1]) - (painter_bounds[3] - painter_bounds[1])) <= 8
    assert abs(gpu_bounds[1] - painter_bounds[1]) <= 12
    assert not any(
        frames[0][index] > frames[0][index + 1] + 80
        and frames[0][index + 1] < 100
        and frames[0][index + 3] > 0
        for index in range(0, len(frames[0]), 4)
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_role_ruby_uses_independent_n3_glow(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    ruby_state = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFFFF"),
        stroke=PaintFill(mode="solid", color="#202020"),
        shadow=PaintFill(mode="solid", color="#FF20E0"),
    )
    role = SubtitleStyleScheme(
        font_family="Meiryo",
        font_size_px=72,
        ruby_font_family="Meiryo",
        ruby_font_size_px=42,
        ruby_stroke_width_px=3,
        ruby_stroke2_enabled=False,
        ruby_decoration_kind="glow",
        ruby_glow_before_radius_px=11,
        ruby_glow_after_radius_px=6,
        ruby_glow_concentration_level=2,
        ruby_karaoke_colors=KaraokeColors(before=ruby_state, after=ruby_state),
    )
    glow_style = _g1_style(
        font_family="Meiryo",
        decoration_kind="shadow",
        shadow_color="#00000000",
        shadow_offset_x=0,
        shadow_offset_y=0,
        dual_line_layout=False,
        custom_style_schemes={"lead": role},
    )
    no_glow_style = replace(
        glow_style,
        custom_style_schemes={"lead": replace(role, ruby_decoration_kind="none")},
    )
    track = _g3_role_ruby_track()
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, no_glow = _render_g1_frames(
            renderer, no_glow_style, (300,), force_warp=True, track=track
        )
        _, glow = _render_g1_frames(
            renderer, glow_style, (300,), force_warp=True, track=track
        )

    assert sum(glow[0][3::4]) > sum(no_glow[0][3::4]) * 1.12
    assert any(
        glow[0][index] > glow[0][index + 1] + 20
        and glow[0][index + 2] > glow[0][index + 1] + 20
        and glow[0][index + 3] > 0
        for index in range(0, len(glow[0]), 4)
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_vertical_gradient_tracks_painter_fill_direction(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    fill = PaintFill(
        mode="gradient_vertical",
        color="#FF0000",
        gradient_stops=[(0, "#FF0000"), (50, "#20FF20"), (100, "#0000FF")],
    )
    state = KaraokeColorState(text=fill)
    style = _g1_style(
        font_family="Meiryo",
        font_size_px=140,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        karaoke_colors=KaraokeColors(before=state, after=state),
    )
    track = _g3_fill_track()
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer, style, (700,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=700, track=track)

    def row_colors(payload: bytes) -> list[tuple[float, float, float]]:
        bounds = _payload_alpha_bounds(payload)
        rows: list[tuple[float, float, float]] = []
        for y in range(bounds[1], bounds[3] + 1):
            pixels = [
                payload[y * 640 * 4 + x * 4 : y * 640 * 4 + x * 4 + 4]
                for x in range(bounds[0], bounds[2] + 1)
                if payload[y * 640 * 4 + x * 4 + 3] >= 240
            ]
            if pixels:
                rows.append(tuple(sum(pixel[channel] for pixel in pixels) / len(pixels) for channel in range(3)))
        assert len(rows) > 20
        return rows

    gpu_rows = row_colors(frames[0])
    painter_rows = row_colors(painter)
    for rows in (gpu_rows, painter_rows):
        top = rows[len(rows) // 8]
        middle = rows[len(rows) // 2]
        bottom = rows[len(rows) * 7 // 8]
        assert top[0] > top[2] + 80
        assert middle[1] > middle[0] + 50 and middle[1] > middle[2] + 50
        assert bottom[2] > bottom[0] + 80
    for ratio in (0.125, 0.5, 0.875):
        gpu = gpu_rows[min(int(len(gpu_rows) * ratio), len(gpu_rows) - 1)]
        cpu = painter_rows[min(int(len(painter_rows) * ratio), len(painter_rows) - 1)]
        assert max(abs(gpu[channel] - cpu[channel]) for channel in range(3)) <= 42


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_vertical_gradient_is_not_retargeted_by_matching_ruby_glow(
    monkeypatch,
) -> None:
    """A ruby brush must not mutate an already-held main-text brush.

    N3 projects can use the same vertical gradient for the sung body and glow,
    with ruby colors following the main text.  The brush cache used to return
    one mutable D2D brush for both the main and ruby bounds; preparing the ruby
    glow then moved the main gradient into the ruby box and clamped the body to
    one end color.
    """
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    gradient = PaintFill(
        mode="gradient_vertical",
        color="#FF0000",
        gradient_stops=[(0, "#FF0000"), (50, "#20FF20"), (100, "#0000FF")],
    )
    transparent = PaintFill(mode="solid", color="#00000000")
    state = KaraokeColorState(
        text=gradient,
        stroke=transparent,
        stroke2=transparent,
        shadow=gradient,
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=140,
        dual_line_layout=False,
        line_y_position="center",
        line_horizontal_layout="center",
        line_lead_in_ms=0,
        line_tail_ms=0,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="glow",
        glow_before_radius_px=4,
        glow_after_radius_px=4,
        karaoke_colors=KaraokeColors(before=state, after=state),
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=48,
        ruby_gap_px=5,
        ruby_stroke_width_px=0,
        ruby_stroke2_enabled=False,
        ruby_decoration_kind="glow",
        ruby_glow_before_radius_px=4,
        ruby_glow_after_radius_px=4,
        ruby_colors_follow_main=True,
        ruby_karaoke_colors=KaraokeColors(before=state, after=state),
        layout_semantics="n3_1074",
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("\u6f22", 0), TimingChar("\u5b57", 500)],
                end_ms=1_000,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="\u6f22\u5b57",
                reading="\u304b\u3093\u3058",
                reading_part_ms=[0, 500],
                pos_start_ms=0,
                pos_end_ms=1_000,
                reading_parts=["\u304b\u3093", "\u3058"],
            )
        ],
    )

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer, style, (1_000,), force_warp=True, track=track
        )

    payload = frames[0]
    bounds = _payload_alpha_bounds(payload)
    main_top = (bounds[1] + bounds[3]) // 2
    opaque_main_pixels = [
        payload[y * 640 * 4 + x * 4 : y * 640 * 4 + x * 4 + 4]
        for y in range(main_top, bounds[3] + 1)
        for x in range(bounds[0], bounds[2] + 1)
        if payload[y * 640 * 4 + x * 4 + 3] >= 240
    ]
    red_pixels = sum(
        pixel[0] > pixel[1] + 80 and pixel[0] > pixel[2] + 80
        for pixel in opaque_main_pixels
    )
    green_pixels = sum(
        pixel[1] > pixel[0] + 50 and pixel[1] > pixel[2] + 50
        for pixel in opaque_main_pixels
    )
    assert red_pixels > 100
    assert green_pixels > 1_000


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_split_vertical_preserves_painter_hard_bands(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    colors = ((255, 255, 255), (255, 32, 32), (32, 64, 255))
    fill = PaintFill(
        mode="split_vertical",
        color="#FFFFFF",
        split_stops=[
            (0, "#FFFFFF"),
            (30, "#FF2020"),
            (65, "#2040FF"),
            (100, "#2040FF"),
        ],
    )
    state = KaraokeColorState(text=fill)
    style = _g1_style(
        font_family="Meiryo",
        font_size_px=140,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        karaoke_colors=KaraokeColors(before=state, after=state),
    )
    track = _g3_fill_track()
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer, style, (700,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=700, track=track)

    def band_rows(payload: bytes) -> list[int]:
        bounds = _payload_alpha_bounds(payload)
        rows: list[int] = []
        for y in range(bounds[1], bounds[3] + 1):
            pixels = [
                tuple(payload[y * 640 * 4 + x * 4 + channel] for channel in range(3))
                for x in range(bounds[0], bounds[2] + 1)
                if payload[y * 640 * 4 + x * 4 + 3] >= 240
            ]
            if not pixels:
                continue
            mean = tuple(sum(pixel[channel] for pixel in pixels) / len(pixels) for channel in range(3))
            rows.append(min(range(3), key=lambda index: sum((mean[c] - colors[index][c]) ** 2 for c in range(3))))
        assert len(rows) > 20
        return rows

    gpu_rows = band_rows(frames[0])
    painter_rows = band_rows(painter)
    gpu_transitions = [
        index for index in range(1, len(gpu_rows))
        if gpu_rows[index] != gpu_rows[index - 1]
    ]
    painter_transitions = [
        index for index in range(1, len(painter_rows))
        if painter_rows[index] != painter_rows[index - 1]
    ]
    # Painter's one-pixel hard-band texture and N3's bitmap brush both wrap
    # outside the shared fill rectangle. Direct2D must begin in the same tail
    # band and hit the three visible boundaries at the same scanlines.
    assert gpu_rows[0] == painter_rows[0] == 2
    assert len(gpu_transitions) >= 3
    assert len(painter_transitions) >= 3
    assert gpu_transitions[:3] == pytest.approx(painter_transitions[:3], abs=1)
    assert [gpu_rows[index] for index in gpu_transitions[:3]] == [0, 1, 2]
    assert [painter_rows[index] for index in painter_transitions[:3]] == [0, 1, 2]


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_image_fill_wrap_scale_and_canvas_anchor_match_painter(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pattern_path = tmp_path / "gpu-fill-pattern.png"
    pattern = QImage(8, 8, QImage.Format.Format_RGBA8888)
    palette = ("#FF2020", "#20FF40", "#2040FF", "#FFE020")
    for y in range(pattern.height()):
        for x in range(pattern.width()):
            pattern.setPixelColor(x, y, QColor(palette[(x // 4) + 2 * (y // 4)]))
    assert pattern.save(str(pattern_path))

    def render(scale_pct: int) -> tuple[list[bytes], bytes]:
        fill = PaintFill(
            mode="image",
            color="#FFFFFF",
            image_path=str(pattern_path),
            image_scale_pct=scale_pct,
        )
        state = KaraokeColorState(text=fill)
        style = _g1_style(
            font_family="Meiryo",
            font_size_px=140,
            stroke_width_px=0,
            stroke2_enabled=False,
            decoration_kind="none",
            dual_line_layout=False,
            karaoke_colors=KaraokeColors(before=state, after=state),
        )
        track = _g3_fill_track()
        with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
            _, frames = _render_g1_frames(
                renderer, style, (100, 900), force_warp=True, track=track
            )
        return frames, _render_painter_oracle(style, t_ms=900, track=track)

    frames_100, painter_100 = render(100)
    frames_200, painter_200 = render(200)
    assert frames_100[0] == frames_100[1]
    assert frames_200[0] == frames_200[1]
    assert frames_100[0] != frames_200[0]

    def overlapping_diffs(gpu: bytes, painter: bytes) -> list[int]:
        diffs: list[int] = []
        for index in range(0, len(gpu), 4):
            if gpu[index + 3] >= 250 and painter[index + 3] >= 250:
                diffs.append(max(abs(gpu[index + c] - painter[index + c]) for c in range(3)))
        assert len(diffs) > 2_000
        return sorted(diffs)

    for gpu, painter in ((frames_100[1], painter_100), (frames_200[1], painter_200)):
        diffs = overlapping_diffs(gpu, painter)
        assert diffs[len(diffs) // 2] <= 12
        assert diffs[int(len(diffs) * 0.90)] <= 70


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_alpha_image_fill_protects_body_from_primary_stroke(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    image_path = tmp_path / "gpu-alpha-fill.png"
    source = QImage(1, 1, QImage.Format.Format_RGBA8888)
    source.fill(0)
    source.setPixelColor(0, 0, QColor(255, 255, 255, 128))
    assert source.save(str(image_path))
    image_fill = PaintFill(
        mode="image", image_path=str(image_path), image_scale_pct=100
    )
    state = KaraokeColorState(
        text=image_fill,
        stroke=PaintFill(mode="solid", color="#FF0000"),
    )
    style = _g1_style(
        font_family="Meiryo",
        font_size_px=140,
        stroke_width_px=12,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        karaoke_colors=KaraokeColors(before=state, after=state),
    )
    track = _g3_fill_track()
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer, style, (100,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=100, track=track)
    bounds = _payload_alpha_bounds(painter)
    center_y = (bounds[1] + bounds[3]) // 2
    outer_index = (center_y * 640 + bounds[0] + 2) * 4
    inside_index = (center_y * 640 + bounds[0] + 8) * 4
    gpu_outer = tuple(frames[0][outer_index : outer_index + 4])
    gpu_inside = tuple(frames[0][inside_index : inside_index + 4])
    painter_inside = tuple(painter[inside_index : inside_index + 4])

    assert gpu_outer[0] > gpu_outer[1] + 150
    assert abs(gpu_inside[0] - gpu_inside[1]) <= 5
    assert abs(painter_inside[0] - painter_inside[1]) <= 5


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_image_fill_file_signature_invalidates_scene_cache(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    image_path = tmp_path / "gpu-hot-image.png"

    def save(width: int, color: str) -> None:
        image = QImage(width, 4, QImage.Format.Format_RGBA8888)
        image.fill(QColor(color))
        assert image.save(str(image_path))

    save(4, "#FF2020")
    fill = PaintFill(mode="image", image_path=str(image_path), image_scale_pct=100)
    state = KaraokeColorState(text=fill)
    style = _g1_style(
        font_family="Meiryo",
        font_size_px=140,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        karaoke_colors=KaraokeColors(before=state, after=state),
    )
    track = _g3_fill_track()
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        first_configured, first = _render_g1_frames(
            renderer, style, (100,), force_warp=True, track=track
        )
        save(5, "#2040FF")
        second_configured, second = _render_g1_frames(
            renderer, style, (100,), force_warp=True, track=track
        )

    assert second_configured["cache_misses"] == first_configured["cache_misses"] + 1
    assert first[0] != second[0]
    assert any(
        first[0][index] > first[0][index + 2] + 100 and first[0][index + 3] > 0
        for index in range(0, len(first[0]), 4)
    )
    assert any(
        second[0][index + 2] > second[0][index] + 100 and second[0][index + 3] > 0
        for index in range(0, len(second[0]), 4)
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_role_main_and_ruby_shadows_match_painter_direction(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    def colors(shadow: str) -> KaraokeColors:
        state = KaraokeColorState(
            text=PaintFill(mode="solid", color="#FFFFFF"),
            stroke=PaintFill(mode="solid", color="#202020"),
            shadow=PaintFill(mode="solid", color=shadow),
        )
        return KaraokeColors(before=state, after=state)

    style = _g1_style(
        font_family="Meiryo",
        font_size_px=84,
        ruby_font_family="Meiryo",
        ruby_font_size_px=34,
        stroke_width_px=2,
        stroke2_enabled=False,
        decoration_kind="none",
        dual_line_layout=False,
        custom_style_schemes={
            "lead": SubtitleStyleScheme(
                font_family="Meiryo",
                font_size_px=84,
                stroke_width_px=2,
                stroke2_enabled=False,
                decoration_kind="shadow",
                shadow_offset_x=14,
                shadow_offset_y=9,
                karaoke_colors=colors("#20FF40"),
                ruby_font_family="Meiryo",
                ruby_font_size_px=34,
                ruby_stroke_width_px=1,
                ruby_stroke2_enabled=False,
                ruby_decoration_kind="shadow",
                ruby_shadow_offset_x=-10,
                ruby_shadow_offset_y=6,
                ruby_karaoke_colors=colors("#FF20E0"),
            )
        },
    )
    track = _g3_role_ruby_track()
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer, style, (300,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=300, track=track)

    def color_bounds(payload: bytes, kind: str) -> tuple[int, int, int, int]:
        xs: list[int] = []
        ys: list[int] = []
        for index in range(0, len(payload), 4):
            red, green, blue, alpha = payload[index : index + 4]
            visible = alpha > 0 and (
                (green > red + 50 and green > blue + 50)
                if kind == "green"
                else (red > green + 50 and blue > green + 50)
            )
            if visible:
                pixel = index // 4
                xs.append(pixel % 640)
                ys.append(pixel // 640)
        assert xs and ys
        return min(xs), min(ys), max(xs), max(ys)

    for kind in ("green", "magenta"):
        gpu_bounds = color_bounds(frames[0], kind)
        painter_bounds = color_bounds(painter, kind)
        assert abs(
            (gpu_bounds[0] + gpu_bounds[2]) / 2
            - (painter_bounds[0] + painter_bounds[2]) / 2
        ) <= 12
        assert abs(
            (gpu_bounds[1] + gpu_bounds[3]) / 2
            - (painter_bounds[1] + painter_bounds[3]) / 2
        ) <= 14


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_shadow_wipe_splits_source_before_offset(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    before = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFFFF"),
        shadow=PaintFill(mode="solid", color="#20FF40"),
    )
    after = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFFFF"),
        shadow=PaintFill(mode="solid", color="#2040FF"),
    )
    style = _g1_style(
        font_family="Meiryo",
        font_size_px=140,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="shadow",
        shadow_offset_x=18,
        shadow_offset_y=8,
        dual_line_layout=False,
        karaoke_colors=KaraokeColors(before=before, after=after),
    )
    track = _g3_fill_track()
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer, style, (500,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=500, track=track)

    def colored_x(payload: bytes, blue: bool) -> tuple[int, int]:
        xs = []
        for index in range(0, len(payload), 4):
            red, green, value_blue, alpha = payload[index : index + 4]
            match = alpha > 0 and (
                value_blue > green + 60 and value_blue > red + 20
                if blue
                else green > value_blue + 60 and green > red + 60
            )
            if match:
                xs.append((index // 4) % 640)
        assert xs
        return min(xs), max(xs)

    for blue in (False, True):
        gpu = colored_x(frames[0], blue)
        cpu = colored_x(painter, blue)
        assert abs(gpu[0] - cpu[0]) <= 5
        assert abs(gpu[1] - cpu[1]) <= 5


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_ruby_uses_n3_multi_pass_glow(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    base = _g1_style(
        font_family="Meiryo",
        base_color="#FFFFFF",
        fill_color="#FFFFFF",
        ruby_color="#FF2030",
        ruby_font_size_px=36,
        ruby_gap_px=4,
        ruby_stroke_width_px=3,
        ruby_stroke2_enabled=False,
        decoration_kind="shadow",
        ruby_decoration_kind="glow",
        shadow_color="#00FF40",
        ruby_glow_before_radius_px=8,
        ruby_glow_after_radius_px=8,
        ruby_glow_concentration_level=0,
    )
    high = replace(base, ruby_glow_concentration_level=2)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, low_frames = _render_g1_frames(
            renderer,
            base,
            (1_000,),
            force_warp=True,
            track=_g3_ruby_track(),
        )
        _, high_frames = _render_g1_frames(
            renderer,
            high,
            (1_000,),
            force_warp=True,
            track=_g3_ruby_track(),
        )

    low_alpha = low_frames[0][3::4]
    high_alpha = high_frames[0][3::4]
    assert sum(high_alpha) > sum(low_alpha)
    assert _alpha_count(high_frames[0]) >= _alpha_count(low_frames[0])
    assert any(
        payload[index + 1] > payload[index] + 20 and payload[index + 3] > 0
        for payload in high_frames
        for index in range(0, len(payload), 4)
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g3_ruby_uses_independent_before_and_after_glow_radii(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        font_family="Meiryo",
        decoration_kind="shadow",
        shadow_color="#00000000",
        shadow_offset_x=0,
        shadow_offset_y=0,
        ruby_font_size_px=36,
        ruby_stroke_width_px=3,
        ruby_stroke2_enabled=False,
        ruby_decoration_kind="glow",
        ruby_glow_before_radius_px=2,
        ruby_glow_after_radius_px=12,
        ruby_glow_concentration_level=1,
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, frames = _render_g1_frames(
            renderer,
            style,
            (0, 1_999),
            force_warp=True,
            track=_g3_ruby_track(),
        )

    before_alpha_mass = sum(frames[0][3::4])
    after_alpha_mass = sum(frames[1][3::4])
    # Integer-half N3 edge placement makes the 3 px ruby stroke one physical
    # pixel narrower than the prior float-half approximation. The larger after
    # radius must still increase the total glow mass measurably.
    assert after_alpha_mass > before_alpha_mass * 1.01


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_ruby_before_glow_keeps_full_leading_halo(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    transparent = KaraokeColorState(
        text=PaintFill(mode="solid", color="#00000000"),
        stroke=PaintFill(mode="solid", color="#00000000"),
        shadow=PaintFill(mode="solid", color="#00000000"),
    )
    before = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFFFFFF"),
        stroke=PaintFill(mode="solid", color="#FF101010"),
        shadow=PaintFill(mode="solid", color="#FF2040FF"),
    )
    after = replace(before, shadow=PaintFill(mode="solid", color="#FFFF2040"))
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("星", 1_000)],
                end_ms=2_000,
                display_start_override_ms=0,
                display_end_override_ms=2_500,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="星",
                reading="ほし",
                reading_parts=["ほ", "し"],
                reading_part_ms=[1_500],
                pos_start_ms=1_000,
                pos_end_ms=2_000,
            )
        ],
    )
    style = _g1_style(
        font_family="Meiryo",
        font_size_px=96,
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        karaoke_colors=KaraokeColors(before=transparent, after=transparent),
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=42,
        ruby_stroke_width_px=2,
        ruby_stroke2_enabled=False,
        ruby_decoration_kind="glow",
        ruby_glow_before_radius_px=12,
        ruby_glow_after_radius_px=12,
        ruby_glow_concentration_level=1,
        ruby_karaoke_colors=KaraokeColors(before=before, after=after),
        dual_line_layout=False,
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, (500,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=500, track=track)

    def blue_profile(payload: bytes) -> tuple[int, int, int, int]:
        xs: list[int] = []
        alpha_mass = 0
        for index in range(0, len(payload), 4):
            red, green, blue, alpha = payload[index : index + 4]
            if blue > red + 30 and blue > green + 15 and alpha > 0:
                xs.append((index // 4) % 640)
                alpha_mass += alpha
        assert xs
        return min(xs), max(xs), len(xs), alpha_mass

    gpu_profile = blue_profile(gpu[0])
    painter_profile = blue_profile(painter)
    assert abs(gpu_profile[0] - painter_profile[0]) <= 8
    assert abs(gpu_profile[1] - painter_profile[1]) <= 8
    assert gpu_profile[2] >= painter_profile[2] * 0.85
    assert gpu_profile[3] >= painter_profile[3] * 0.90


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_utopia_before_glow_uses_character_local_wipe_scope(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track, style = _g4_utopia_main_scene()
    before = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFFFFFFF"),
        stroke=PaintFill(mode="solid", color="#FF101010"),
        shadow=PaintFill(mode="solid", color="#FF2040FF"),
    )
    after = replace(before, shadow=PaintFill(mode="solid", color="#FFFF2040"))
    style = replace(
        style,
        glow_before_radius_px=12,
        glow_after_radius_px=12,
        karaoke_colors=KaraokeColors(before=before, after=after),
    )
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, (350,), force_warp=True, track=track
        )
    painter = _render_painter_oracle(style, t_ms=350, track=track)

    def blue_bounds(payload: bytes) -> tuple[int, int, int, int]:
        xs: list[int] = []
        ys: list[int] = []
        for index in range(0, len(payload), 4):
            red, green, blue, alpha = payload[index : index + 4]
            if blue > red + 30 and blue > green + 15 and alpha > 0:
                pixel = index // 4
                xs.append(pixel % 640)
                ys.append(pixel // 640)
        assert xs and ys
        return min(xs), min(ys), max(xs), max(ys)

    gpu_bounds = blue_bounds(gpu[0])
    painter_bounds = blue_bounds(painter)
    assert abs(gpu_bounds[0] - painter_bounds[0]) <= 8
    assert abs(gpu_bounds[2] - painter_bounds[2]) <= 12


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_alignment_uses_visible_ink_bounds(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        renderer.configure_gpu(
            _g1_track(),
            _g1_style(dual_line_layout=False, decoration_kind="none"),
            width=640,
            height=360,
            fps=60,
            force_warp=True,
        )
        event = renderer.render_gpu_frame(750, force_warp=True)
        with SharedFrameRingReader.from_event(event) as reader:
            slot = reader.read_frame(event)

    left, top, right, bottom = _alpha_bounds(slot)
    assert abs((left + right) / 2.0 - slot.width / 2.0) <= 2.0
    assert abs((top + bottom) / 2.0 - slot.height / 2.0) <= 2.0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_hardware_and_warp_are_pixel_bounded(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        info = renderer.backend_info()
        if not info.get("available"):
            pytest.skip("hardware Direct2D adapter is unavailable")
        _, hardware = _render_g1_frames(renderer, _g1_style(), (750,), force_warp=False)
        _, warp = _render_g1_frames(renderer, _g1_style(), (750,), force_warp=True)

    hardware_alpha = hardware[0][3::4]
    warp_alpha = warp[0][3::4]
    differing_alpha = sum(a != b for a, b in zip(hardware_alpha, warp_alpha))
    max_alpha_delta = max(abs(a - b) for a, b in zip(hardware_alpha, warp_alpha))
    premultiplied_deltas = [
        abs(
            hardware[0][index + channel] * hardware[0][index + 3] // 255
            - warp[0][index + channel] * warp[0][index + 3] // 255
        )
        for index in range(0, len(hardware[0]), 4)
        for channel in range(3)
    ]
    assert differing_alpha <= len(hardware_alpha) * 0.01
    # Direct2D hardware and WARP use slightly different edge antialiasing at a
    # small number of pixels. Gate the aggregate premultiplied image error,
    # which is stable and meaningful even where straight RGB has tiny alpha.
    assert sum(abs(a - b) for a, b in zip(hardware_alpha, warp_alpha)) / len(hardware_alpha) <= 0.05
    assert sum(premultiplied_deltas) / len(premultiplied_deltas) <= 0.06
    assert max_alpha_delta <= 96


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize(
    "viewport_changes,bounds_tolerance",
    [
        ({"viewport_offset_x": 90, "viewport_offset_y": 40}, 3),
        ({"viewport_scale_pct": 150, "viewport_align": "top_left"}, 4),
        ({"viewport_rotation_deg": 30}, 5),
        (
            {
                "viewport_scale_pct": 125,
                "viewport_rotation_deg": -20,
                "viewport_offset_x": -35,
                "viewport_offset_y": 24,
                "viewport_align": "bottom_right",
            },
            6,
        ),
    ],
)
def test_gpu_g4_viewport_transform_matches_painter(
    monkeypatch,
    viewport_changes: dict[str, object],
    bounds_tolerance: int,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        decoration_kind="glow",
        shadow_color="#00FF40",
        glow_radius_px=8,
        glow_before_radius_px=8,
        glow_after_radius_px=8,
        glow_concentration_level=1,
        **viewport_changes,
    )
    timestamps = (250, 750, 1_500)
    painter = [
        _render_painter_oracle(style, t_ms=t_ms)
        for t_ms in timestamps
    ]
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer, style, timestamps, force_warp=True
        )

    for gpu_frame, painter_frame in zip(gpu, painter):
        gpu_bounds = _payload_alpha_bounds(gpu_frame)
        painter_bounds = _payload_alpha_bounds(painter_frame)
        assert all(
            abs(actual - expected) <= bounds_tolerance
            for actual, expected in zip(gpu_bounds, painter_bounds)
        ), (viewport_changes, gpu_bounds, painter_bounds)
    gpu_full = sum(gpu[-1][3::4])
    painter_full = sum(painter[-1][3::4])
    for gpu_frame, painter_frame in zip(gpu, painter):
        assert abs(
            sum(gpu_frame[3::4]) / gpu_full
            - sum(painter_frame[3::4]) / painter_full
        ) <= 0.04


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize(
    "direction_changes",
    [{}, {"vertical": True}, {"right_to_left": True}],
)
def test_gpu_g4_viewport_transform_preserves_ruby_direction_layout(
    monkeypatch,
    direction_changes: dict[str, object],
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=64,
        dual_line_layout=False,
        decoration_kind="shadow",
        shadow_offset_x=7,
        shadow_offset_y=9,
        ruby_font_family="Meiryo",
        ruby_font_family_latin="Meiryo",
        ruby_font_follow_main=False,
        ruby_font_size_px=28,
        ruby_gap_px=4,
        ruby_stroke_width_px=2,
        ruby_stroke2_enabled=True,
        ruby_stroke2_width_px=1,
        ruby_decoration_kind="glow",
        ruby_glow_before_radius_px=7,
        ruby_glow_after_radius_px=7,
        ruby_glow_concentration_level=1,
        viewport_scale_pct=115,
        viewport_rotation_deg=12,
        viewport_offset_x=18,
        viewport_offset_y=-11,
        viewport_align="center",
        **direction_changes,
    )
    timestamps = (500, 1_500)
    painter = [
        _render_painter_oracle(style, t_ms=t_ms, track=_g3_ruby_track())
        for t_ms in timestamps
    ]
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu = _render_g1_frames(
            renderer,
            style,
            timestamps,
            force_warp=True,
            track=_g3_ruby_track(),
        )

    for gpu_frame, painter_frame in zip(gpu, painter):
        assert all(
            abs(actual - expected) <= 15
            for actual, expected in zip(
                _payload_alpha_bounds(gpu_frame),
                _payload_alpha_bounds(painter_frame),
            )
        ), (
            direction_changes,
            _payload_alpha_bounds(gpu_frame),
            _payload_alpha_bounds(painter_frame),
        )
    assert abs(
        sum(gpu[0][3::4]) / sum(gpu[-1][3::4])
        - sum(painter[0][3::4]) / sum(painter[-1][3::4])
    ) <= 0.05


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g4_viewport_transform_applies_to_title_and_volume_signal(
    monkeypatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    track = TimingTrack(
        meta=TimingTrackMeta(title="GPU", artist="CPU"),
        lines=[TimingLine(chars=[TimingChar("Signal", 4_000)], end_ms=5_000)],
    )
    title = TitleOverlay(
        enabled=True,
        text_template="{title} / {artist}",
        font_family="Meiryo",
        font_family_latin="Meiryo",
        font_size_px=40,
        fill=PaintFill(mode="solid", color="#40FF60"),
        stroke=PaintFill(mode="solid", color="#102010"),
        stroke_width_px=2,
        anchor="top_left",
        align="left",
        offset_x=37,
        offset_y=29,
        show_mode="head",
        duration_ms=2_000,
        fade_in_ms=0,
        fade_out_ms=0,
    )
    style = _g1_style(
        font_family="Meiryo",
        font_family_latin="Meiryo",
        title_overlay=title,
        dual_line_layout=False,
        line_lead_in_ms=500,
        line_tail_ms=500,
        lit_enabled=True,
        lit_style="volume",
        signals_duration_ms=4_000,
        lit_waiting_time_ms=0,
        lit_time_offset_ms=0,
        lit_opacity_pct=80,
        lit_stroke_width=2,
        volume_size=42,
        volume_column_width=12,
        volume_column_count=4,
        volume_column_spacing=3,
        viewport_scale_pct=110,
        viewport_rotation_deg=-10,
        viewport_offset_x=-16,
        viewport_offset_y=12,
        viewport_align="center",
    )
    scenarios = [
        (replace(style, lit_enabled=False), 1_000),
        (replace(style, title_overlay=None), 3_000),
    ]
    painter = [
        _render_painter_oracle(layer_style, t_ms=t_ms, track=track)
        for layer_style, t_ms in scenarios
    ]
    gpu: list[bytes] = []
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        for layer_style, t_ms in scenarios:
            _, frames = _render_g1_frames(
                renderer, layer_style, (t_ms,), force_warp=True, track=track
            )
            gpu.append(frames[0])

    for gpu_frame, painter_frame in zip(gpu, painter):
        gpu_bounds = _payload_alpha_bounds(gpu_frame)
        painter_bounds = _payload_alpha_bounds(painter_frame)
        assert all(
            abs(actual - expected) <= 15
            for actual, expected in zip(gpu_bounds, painter_bounds)
        ), (gpu_bounds, painter_bounds)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_basic_frame_matches_python_painter_within_bounded_diff(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        font_family="Meiryo",
        shadow_color="#00000000",
        shadow_offset_x=0,
        shadow_offset_y=0,
    )
    python_payload = _render_painter_oracle(style)

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu_frames = _render_g1_frames(renderer, style, (750,), force_warp=True)
    gpu_payload = gpu_frames[0]

    assert _alpha_count(python_payload) > 0
    assert _alpha_count(gpu_payload) > 0
    python_slot = type(
        "PainterFrame",
        (),
        {"payload": python_payload, "width": 640, "height": 360, "stride": 640 * 4},
    )()
    gpu_slot = type(
        "GpuFrame",
        (),
        {"payload": gpu_payload, "width": 640, "height": 360, "stride": 640 * 4},
    )()
    python_bounds = _alpha_bounds(python_slot)
    gpu_bounds = _alpha_bounds(gpu_slot)
    assert all(abs(a - b) <= 2 for a, b in zip(python_bounds, gpu_bounds)), (
        python_bounds,
        gpu_bounds,
    )

    channel_deltas = [abs(a - b) for a, b in zip(python_payload, gpu_payload)]
    assert sum(channel_deltas) / len(channel_deltas) < 1.0
    assert sum(delta > 8 for delta in channel_deltas) < 10_000


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_n3_glow_matches_python_painter_within_bounded_diff(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        font_family="Meiryo",
        decoration_kind="glow",
        shadow_color="#00FF40",
        glow_radius_px=8,
        glow_before_radius_px=8,
        glow_after_radius_px=8,
        glow_concentration_level=1,
    )
    python_payload = _render_painter_oracle(style)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu_frames = _render_g1_frames(renderer, style, (750,), force_warp=True)
    gpu_payload = gpu_frames[0]

    python_slot = type(
        "PainterGlowFrame",
        (),
        {"payload": python_payload, "width": 640, "height": 360, "stride": 640 * 4},
    )()
    gpu_slot = type(
        "GpuGlowFrame",
        (),
        {"payload": gpu_payload, "width": 640, "height": 360, "stride": 640 * 4},
    )()
    assert all(
        abs(a - b) <= 2
        for a, b in zip(_alpha_bounds(python_slot), _alpha_bounds(gpu_slot))
    )
    channel_deltas = [abs(a - b) for a, b in zip(python_payload, gpu_payload)]
    # Exact DirectWrite design bearings produce a slightly different glow edge
    # than QPainter's font-engine approximation; geometry and aggregate pixels
    # remain tightly bounded.
    assert sum(channel_deltas) / len(channel_deltas) < 1.3
    assert sum(delta > 8 for delta in channel_deltas) < 15_000


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_export_pipeline_matches_serial_frames(monkeypatch) -> None:
    """G7: the two-slot pipelined export yields the same bytes as serial renders."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.engine.native_export import iter_gpu_rgba_frames

    track = _g1_track()
    style = _g1_style(
        decoration_kind="glow",
        glow_before_radius_px=6,
        glow_after_radius_px=6,
        glow_concentration_level=1,
    )
    width, height, fps, total = 320, 180, 30, 8

    pipelined = list(
        iter_gpu_rgba_frames(
            track,
            style,
            width=width,
            height=height,
            fps=fps,
            total_frames=total,
            renderer_path=_renderer_path(),
            force_warp=True,
        )
    )

    serial: list[bytes] = []
    shm_key = f"test-gpu-serial-{uuid.uuid4().hex}"
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        renderer.configure_gpu(
            track, style, width=width, height=height, fps=fps, force_warp=True
        )
        reader = None
        try:
            for frame_index in range(total):
                event = renderer.render_gpu_frame(
                    int(round(frame_index * 1000 / fps)),
                    force_warp=True,
                    generation=1,
                    frame_index=frame_index,
                    shm_key=shm_key,
                    include_checksum=False,
                    readback_bands=True,
                )
                if reader is None:
                    reader = SharedFrameRingReader.from_event(event)
                    reader.attach()
                image = reader.read_qimage(event).convertToFormat(
                    QImage.Format.Format_RGBA8888
                )
                bits = image.constBits()
                bits.setsize(image.sizeInBytes())
                serial.append(bytes(bits))
        finally:
            if reader is not None:
                reader.close()

    assert len(pipelined) == total
    assert sum(frame[3::4].count(0) < len(frame) // 4 for frame in pipelined) > 0
    assert pipelined == serial
