from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
import uuid

import pytest
from PyQt6.QtGui import QImage

from krok_helper.subtitle_render.native_backend import (
    NativeRendererError,
    NativeRendererProcess,
    SharedFrameRingReader,
    resolve_native_renderer_path,
)
from krok_helper.subtitle_render.models import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
    RubyAnnotation,
    Style,
    SubtitleStyleScheme,
    TimingChar,
    TimingLine,
    TimingTrack,
    style_from_dict,
)
from krok_helper.subtitle_render.n3proj_import import load_n3proj
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc


DARK_SPIRAL_N3PROJ = (
    Path.cwd().parent / "songs" / "Dark spiral journey" / "1.n3proj"
)


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
) -> tuple[dict, list[bytes]]:
    configured = renderer.configure_gpu(
        track or _g1_track(),
        style,
        width=width,
        height=height,
        fps=60,
        force_warp=force_warp,
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


def _render_painter_oracle(
    style: Style,
    *,
    t_ms: int = 750,
    track: TimingTrack | None = None,
    width: int = 640,
    height: int = 360,
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
    paint_frame(image, track or _g1_track(), t_ms, style)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    return bytes(bits)


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
    assert after_alpha_mass > before_alpha_mass * 1.02


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_alignment_uses_visible_ink_bounds(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        renderer.configure_gpu(
            _g1_track(),
            _g1_style(dual_line_layout=False),
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
    assert sum(premultiplied_deltas) / len(premultiplied_deltas) <= 0.05
    assert max_alpha_delta <= 96


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
