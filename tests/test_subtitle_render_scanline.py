"""Karaoke scan-line（扫字线）model/serialization/painter/GPU contracts."""

from __future__ import annotations

import os
from dataclasses import replace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRectF  # noqa: E402
from PyQt6.QtGui import QImage, QPainter, QPainterPath  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    Style,
    SubtitleStyleScheme,
    effective_karaoke_animation,
    effective_karaoke_scanline,
    rescale_font_sizes,
    style_from_dict,
    style_to_dict,
    style_with_line_animation,
)
from krok_helper.subtitle_render.domain.timing import (  # noqa: E402
    LineAnimationOverride,
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.native.protocol import (  # noqa: E402
    gpu_unsupported_features,
)
from krok_helper.subtitle_render.serialization.timing import (  # noqa: E402
    line_animation_override_from_dict,
    line_animation_override_to_dict,
)
from krok_helper.subtitle_render.engine.painter import paint_frame  # noqa: E402
from krok_helper.subtitle_render.engine.render.elements.horizontal.scanline import (  # noqa: E402
    ScanlineParams,
    _brighten_color_hsv,
    paint_scanline_strip,
)


def _scanline_style(**changes) -> Style:
    base = dict(
        entry_anim="none",
        exit_anim="none",
        sync_entry=False,
        sync_ending=False,
        sync_each_page=False,
        line_lead_in_ms=0,
        line_tail_ms=200,
        scanline_width_px=18,
        scanline_color="#40E0FF",
        scanline_glow_px=6,
    )
    base.update(changes)
    return Style(**base)


def _wiping_track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("歌", 0),
                    TimingChar("詞", 400),
                    TimingChar("測", 800),
                ],
                end_ms=1200,
            )
        ]
    )


# ---------------------------------------------------------------------------
# 模型与序列化
# ---------------------------------------------------------------------------


def test_scanline_modes_resolve_to_base_karaoke_animation() -> None:
    assert (
        effective_karaoke_animation(_scanline_style(karaoke_anim="scanline")) == "none"
    )
    assert (
        effective_karaoke_animation(_scanline_style(karaoke_anim="utopia_scanline"))
        == "utopia"
    )
    assert effective_karaoke_animation(_scanline_style(karaoke_anim="none")) == "none"
    assert (
        effective_karaoke_animation(_scanline_style(karaoke_anim="utopia")) == "utopia"
    )


def test_effective_karaoke_scanline_only_accepts_explicit_modes() -> None:
    assert effective_karaoke_scanline(_scanline_style(karaoke_anim="scanline"))
    assert effective_karaoke_scanline(_scanline_style(karaoke_anim="utopia_scanline"))
    assert not effective_karaoke_scanline(_scanline_style(karaoke_anim="utopia"))
    assert not effective_karaoke_scanline(_scanline_style(karaoke_anim="none"))
    # inherit 的旧推导（入退场含 utopia）不产生扫字线。
    assert not effective_karaoke_scanline(
        _scanline_style(karaoke_anim="inherit", entry_anim="utopia")
    )


def test_per_line_override_controls_scanline_enablement() -> None:
    style = _scanline_style(karaoke_anim="utopia_scanline")
    line = TimingLine(
        chars=[TimingChar("歌", 0)],
        end_ms=500,
        animation_override=LineAnimationOverride(karaoke_anim="none"),
    )
    assert effective_karaoke_scanline(style_with_line_animation(style, line)) is False
    line_keep = TimingLine(chars=[TimingChar("歌", 0)], end_ms=500)
    assert effective_karaoke_scanline(style_with_line_animation(style, line_keep))


def test_scanline_style_fields_round_trip() -> None:
    style = _scanline_style(
        karaoke_anim="utopia_scanline",
        scanline_width_px=33,
        scanline_mode="brighten",
        scanline_color="#123456",
        scanline_brightness_pct=45,
        scanline_glow_px=12,
    )
    restored = style_from_dict(style_to_dict(style))
    assert restored.karaoke_anim == "utopia_scanline"
    assert restored.scanline_width_px == 33
    assert restored.scanline_mode == "brighten"
    assert restored.scanline_color == "#123456"
    assert restored.scanline_brightness_pct == 45
    assert restored.scanline_glow_px == 12
    # 非法模式回落 color，亮度越界钳制在 style 层由参数面板/渲染端兜底。
    assert style_from_dict({"scanline_mode": "wat"}).scanline_mode == "color"


def test_scanline_pixel_fields_rescale_with_output_height() -> None:
    """扫字线像素字段与字号共用 SizeAndRatio 语义随输出高度换算。"""
    style = _scanline_style(
        karaoke_anim="utopia_scanline",
        font_reference_height=1080,
        scanline_width_px=16,
        scanline_glow_px=8,
        scanline_mode="brighten",
        scanline_brightness_pct=60,
        custom_style_schemes={"主唱": SubtitleStyleScheme(font_size_px=80)},
    )

    up = rescale_font_sizes(style, 2160)
    assert up.font_reference_height == 2160
    assert up.scanline_width_px == 32
    assert up.scanline_glow_px == 16
    # 模式 / 颜色 / 亮度无量纲，不随画布变化。
    assert up.scanline_mode == "brighten"
    assert up.scanline_color == "#40E0FF"
    assert up.scanline_brightness_pct == 60
    # 配色方案不携带扫字线字段，只有常规字体字段换算。
    assert up.custom_style_schemes["主唱"].font_size_px == 160
    # 柔化半径 0 在任何高度下保持 0。
    zero_glow = rescale_font_sizes(replace(style, scanline_glow_px=0), 2160)
    assert zero_glow.scanline_glow_px == 0
    # 往返一致：切回 1080 恢复原值；等高度 no-op 返回同一对象。
    back = rescale_font_sizes(up, 1080)
    assert back.font_reference_height == 1080
    assert back.scanline_width_px == 16
    assert back.scanline_glow_px == 8
    assert rescale_font_sizes(back, 1080) is back


def test_reverse_karaoke_scanline_bakes_per_line() -> None:
    style = _scanline_style(
        karaoke_anim="utopia",
        reverse_karaoke_anim="scanline",
    )
    forward = TimingLine(chars=[TimingChar("歌", 0)], end_ms=500)
    reverse = TimingLine(chars=[TimingChar("歌", 0)], end_ms=500, wipe_reverse=True)
    assert not effective_karaoke_scanline(style_with_line_animation(style, forward))
    assert effective_karaoke_scanline(style_with_line_animation(style, reverse))
    # inherit 沿用正向档位。
    follow = _scanline_style(karaoke_anim="scanline", reverse_karaoke_anim="inherit")
    assert effective_karaoke_scanline(style_with_line_animation(follow, reverse))


def test_scanline_line_override_serialization_round_trip() -> None:
    override = LineAnimationOverride(karaoke_anim="utopia_scanline")
    data = line_animation_override_to_dict(override)
    assert data["karaoke_anim"] == "utopia_scanline"
    restored = line_animation_override_from_dict(
        {"entry_anim": "none", "exit_anim": "none", "karaoke_anim": "scanline"}
    )
    assert restored is not None and restored.karaoke_anim == "scanline"
    legacy = line_animation_override_from_dict(
        {"entry_anim": "none", "exit_anim": "none"}
    )
    assert legacy is not None and legacy.karaoke_anim == "inherit"
    invalid = line_animation_override_from_dict(
        {"entry_anim": "none", "exit_anim": "none", "karaoke_anim": "wat"}
    )
    assert invalid is not None and invalid.karaoke_anim == "inherit"


def test_scanline_modes_do_not_force_gpu_fallback() -> None:
    style = _scanline_style(karaoke_anim="utopia_scanline")
    assert gpu_unsupported_features(_wiping_track(), style) == ()
    plain = _scanline_style(karaoke_anim="scanline")
    assert gpu_unsupported_features(_wiping_track(), plain) == ()


def test_render_only_anim_fields_leave_layout_signature_intact() -> None:
    from krok_helper.subtitle_render.engine.value_signature import (
        lyric_layout_style_signature,
    )

    base = lyric_layout_style_signature(_scanline_style())
    assert base == lyric_layout_style_signature(
        _scanline_style(
            karaoke_anim="scanline",
            reverse_karaoke_anim="utopia_scanline",
            scanline_mode="brighten",
            scanline_width_px=99,
            scanline_color="#123456",
            scanline_brightness_pct=20,
            scanline_glow_px=30,
        )
    )
    # 出入场类型仍参与签名（跨 none 会移动显示窗口，必须重建计划）。
    assert base != lyric_layout_style_signature(_scanline_style(entry_anim="fade"))


def test_anim_type_delta_window_check() -> None:
    from krok_helper.subtitle_render.frontend.main_window import (
        _ANIM_TYPE_STYLE_FIELDS,
        _RENDER_ONLY_ANIM_STYLE_FIELDS,
        _animation_windows_unchanged,
        _style_delta_only_in,
    )

    base = _scanline_style(entry_anim="fade", exit_anim="fade")
    # 类型等价切换（fade→slide，时长不变）：逐行有效动画时长不变 → 窗口不动。
    changed = replace(base, entry_anim="slide_in", exit_anim="slide_out")
    assert _style_delta_only_in(base, changed, _ANIM_TYPE_STYLE_FIELDS)
    track = _wiping_track()
    assert _animation_windows_unchanged(base, changed, (track,))
    # 跨 none 边界：有效时长 250ms→0，窗口会缩。
    assert not _animation_windows_unchanged(
        base, replace(base, entry_anim="none"), (track,)
    )
    # 唱字/扫字线字段属于渲染专属集合；混入非动画字段则不成立。
    scan = replace(base, karaoke_anim="scanline", scanline_width_px=40)
    assert _style_delta_only_in(base, scan, _RENDER_ONLY_ANIM_STYLE_FIELDS)
    mixed = replace(base, karaoke_anim="scanline", font_size_px=80)
    assert not _style_delta_only_in(base, mixed, _RENDER_ONLY_ANIM_STYLE_FIELDS)


def test_render_ir_paint_scope_reuses_plan_across_scanline_edits() -> None:
    """唱字档位变更走 paint scope：布局计划命中复用，IR 逐行动画仍更新。"""

    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir

    track = _wiping_track()
    base = build_render_ir(
        track, _scanline_style(karaoke_anim="utopia"), width=640, height=360, fps=60
    )
    # 唱字/扫字线字段不在歌词布局签名里：paint scope 应命中上一份计划缓存，
    # 由 _rebind_plan_line_styles 把新动画样式刷进 IR 行。
    reused = build_render_ir(
        track,
        _scanline_style(karaoke_anim="utopia_scanline", scanline_width_px=40),
        width=640,
        height=360,
        fps=60,
        relayout_scope="paint",
    )
    assert [line["scanline"] for line in base["track"]["lines"]] == [False]
    assert [line["karaoke_anim"] for line in base["track"]["lines"]] == ["utopia"]
    assert [line["scanline"] for line in reused["track"]["lines"]] == [True]
    assert [line["karaoke_anim"] for line in reused["track"]["lines"]] == ["utopia"]
    assert reused["style"]["scanline_width_px"] == 40


def test_render_ir_stamps_per_line_scanline_flag() -> None:
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir

    track = _wiping_track()
    for karaoke, expected in (
        ("scanline", True),
        ("utopia_scanline", True),
        ("utopia", False),
        ("none", False),
    ):
        ir = build_render_ir(
            track,
            _scanline_style(karaoke_anim=karaoke),
            width=640,
            height=360,
            fps=60,
        )
        flags = [line["scanline"] for line in ir["track"]["lines"]]
        assert flags == [expected], karaoke
        # 唱字动画本体仍按基础档位下发（GPU 的 Wipe/Utopia 机制复用）。
        assert ir["track"]["lines"][0]["karaoke_anim"] == effective_karaoke_animation(
            _scanline_style(karaoke_anim=karaoke)
        )
        # 参数随样式整包进入 IR，sidecar 解析后即可绘制。
        assert ir["style"]["scanline_width_px"] == 18
        assert ir["style"]["scanline_color"] == "#40E0FF"
        assert ir["style"]["scanline_glow_px"] == 6
        assert ir["style"]["scanline_mode"] == "color"
        assert ir["style"]["scanline_brightness_pct"] == 60


# ---------------------------------------------------------------------------
# CPU Painter
# ---------------------------------------------------------------------------


def test_scanline_brighten_raises_hsv_value_without_washing_out_hue() -> None:
    from PyQt6.QtGui import QColor

    source = QColor("#804020")
    raised = QColor(_brighten_color_hsv(source.name(), 0.5))
    source_h, source_s, source_v, _ = source.getHsvF()
    raised_h, raised_s, raised_v, _ = raised.getHsvF()
    assert raised_h == pytest.approx(source_h, abs=0.01)
    assert raised_s == pytest.approx(source_s, abs=0.01)
    assert raised_v > source_v
    assert raised != QColor("#BFA090")  # 旧的半透明白色叠加结果


def test_scanline_soft_radius_consumes_the_hard_core() -> None:
    sharp = ScanlineParams(width_px=20, color="#FFFFFF", glow_px=0)
    softened = ScanlineParams(width_px=20, color="#FFFFFF", glow_px=6)
    fully_soft = ScanlineParams(width_px=20, color="#FFFFFF", glow_px=10)
    assert sharp.solid_half_width == 10
    assert softened.solid_half_width == 4
    assert fully_soft.solid_half_width == 0


def test_scanline_softness_stays_inside_glyph_geometry() -> None:
    image = QImage(120, 60, QImage.Format.Format_RGBA8888)
    image.fill(0)
    path = QPainterPath()
    path.addRect(QRectF(30, 10, 40, 40))
    painter = QPainter(image)
    try:
        paint_scanline_strip(
            painter,
            path,
            path.boundingRect(),
            front=50,
            params=ScanlineParams(width_px=20, color="#FFFFFF", glow_px=10),
            style=_scanline_style(stroke_width_px=0, stroke2_width_px=0),
        )
    finally:
        painter.end()
    pixels = np.frombuffer(image.bits().asstring(image.sizeInBytes()), dtype=np.uint8)
    alpha = pixels.reshape(60, 120, 4)[:, :, 3]
    assert np.count_nonzero(alpha[:, :30]) == 0
    assert np.count_nonzero(alpha[:, 70:]) == 0
    assert np.count_nonzero(alpha[:, 30:70]) > 0


def test_scanline_is_empty_while_front_crosses_a_transparent_glyph_gap() -> None:
    image = QImage(120, 60, QImage.Format.Format_RGBA8888)
    image.fill(0)
    path = QPainterPath()
    path.addRect(QRectF(10, 10, 20, 40))
    path.addRect(QRectF(70, 10, 20, 40))
    painter = QPainter(image)
    try:
        paint_scanline_strip(
            painter,
            path,
            path.boundingRect(),
            front=50,
            params=ScanlineParams(width_px=10, color="#FFFFFF", glow_px=5),
            style=_scanline_style(stroke_width_px=0, stroke2_width_px=0),
        )
    finally:
        painter.end()
    pixels = np.frombuffer(image.bits().asstring(image.sizeInBytes()), dtype=np.uint8)
    assert np.count_nonzero(pixels.reshape(60, 120, 4)[:, :, 3]) == 0


def _frame_bytes(track: TimingTrack, style: Style, t_ms: int) -> bytes:
    image = QImage(800, 450, QImage.Format.Format_RGBA8888)
    image.fill(0)
    paint_frame(image, track, t_ms, style)
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    return bytes(bits)


def _visible_diff(left: bytes, right: bytes) -> int:
    a = np.frombuffer(left, dtype=np.uint8).reshape(-1, 4).astype(np.int16)
    b = np.frombuffer(right, dtype=np.uint8).reshape(-1, 4).astype(np.int16)
    return int(np.count_nonzero(np.abs(a - b).max(axis=1) > 2))


def test_painter_scanline_highlights_the_moving_front(qapp) -> None:
    track = _wiping_track()
    base = _scanline_style(karaoke_anim="none")
    scanline = _scanline_style(karaoke_anim="scanline")

    # 走字进行中：锋面高亮带带来可见差异。
    mid_base = _frame_bytes(track, base, 500)
    mid_scan = _frame_bytes(track, scanline, 500)
    assert _visible_diff(mid_base, mid_scan) > 50

    # 未开始与已唱完：锋面不存在，两档画面一致。
    early_base = _frame_bytes(track, base, 0)
    early_scan = _frame_bytes(track, scanline, 0)
    assert _visible_diff(early_base, early_scan) == 0
    late_base = _frame_bytes(track, base, 1200)
    late_scan = _frame_bytes(track, scanline, 1200)
    assert _visible_diff(late_base, late_scan) == 0


def test_painter_utopia_scanline_varies_with_width(qapp) -> None:
    track = _wiping_track()
    narrow = _frame_bytes(
        track,
        _scanline_style(karaoke_anim="utopia_scanline", scanline_width_px=6),
        500,
    )
    wide = _frame_bytes(
        track,
        _scanline_style(karaoke_anim="utopia_scanline", scanline_width_px=60),
        500,
    )
    # 更粗的高亮带覆盖更多文字，两帧可见差异。
    assert _visible_diff(narrow, wide) > 50


def test_painter_scanline_brighten_mode_lifts_existing_colors(qapp) -> None:
    track = _wiping_track()
    base = _frame_bytes(track, _scanline_style(karaoke_anim="none"), 500)
    color = _frame_bytes(track, _scanline_style(karaoke_anim="scanline"), 500)
    brighten = _frame_bytes(
        track,
        _scanline_style(
            karaoke_anim="scanline",
            scanline_mode="brighten",
            scanline_brightness_pct=70,
        ),
        500,
    )
    # 底色发光相对基准有提亮差异，且与单独颜色的画面不同。
    assert _visible_diff(base, brighten) > 50
    assert _visible_diff(color, brighten) > 50
    # 亮度 0 = 不提亮：与基准一致。
    zero = _frame_bytes(
        track,
        _scanline_style(
            karaoke_anim="scanline",
            scanline_mode="brighten",
            scanline_brightness_pct=0,
        ),
        500,
    )
    assert _visible_diff(base, zero) == 0


def test_painter_reverse_line_scanline_follows_reverse_front(qapp) -> None:
    from krok_helper.subtitle_render.domain.timing import normalize_reversed_wipe_lines

    # 整行严格逆序（[5]歌[3]词[>1]）在加载入口镜像理顺并打反向标记。
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("歌", 5), TimingChar("詞", 3)],
                end_ms=1,
            )
        ]
    )
    normalize_reversed_wipe_lines(track)
    assert track.lines[0].wipe_reverse
    base = _scanline_style(karaoke_anim="none", reverse_karaoke_anim="inherit")
    reverse = _scanline_style(karaoke_anim="none", reverse_karaoke_anim="scanline")
    base_frame = _frame_bytes(track, base, 3)
    reverse_frame = _frame_bytes(track, reverse, 3)
    assert _visible_diff(base_frame, reverse_frame) > 30


def test_painter_scanline_covers_ruby_front(qapp) -> None:
    from krok_helper.subtitle_render.domain.timing import RubyAnnotation

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("歌", 0), TimingChar("詞", 600)],
                end_ms=1200,
            )
        ],
        rubies=[
            RubyAnnotation(
                kanji="歌",
                reading="うた",
                reading_parts=["う", "た"],
                reading_part_ms=[300],
                pos_start_ms=0,
                pos_end_ms=600,
            ),
            RubyAnnotation(
                kanji="詞",
                reading="し",
                reading_parts=["し"],
                reading_part_ms=[],
                pos_start_ms=600,
                pos_end_ms=1200,
            ),
        ],
    )
    base = _scanline_style(karaoke_anim="none", font_size_px=48)
    scanline = _scanline_style(karaoke_anim="scanline", font_size_px=48)
    mid_base = _frame_bytes(track, base, 300)
    mid_scan = _frame_bytes(track, scanline, 300)
    assert _visible_diff(mid_base, mid_scan) > 20


# ---------------------------------------------------------------------------
# GPU sidecar（Direct2D）
# ---------------------------------------------------------------------------

_IS_WINDOWS = os.name == "nt"


@pytest.mark.skipif(not _IS_WINDOWS, reason="Direct2D GPU backend is Windows-only")
def test_gpu_scanline_paints_front_band(monkeypatch) -> None:
    pytest.importorskip("PyQt6.QtWidgets")
    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
        SharedFrameRingReader,
        resolve_native_renderer_path,
    )

    renderer_path = resolve_native_renderer_path()
    if renderer_path is None:
        pytest.skip("native renderer sidecar not built")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    track = _wiping_track()

    def gpu_frames(style: Style) -> list[bytes]:
        with NativeRendererProcess(renderer_path, response_timeout_s=60.0) as renderer:
            renderer.configure_gpu(
                track,
                style,
                width=640,
                height=360,
                fps=60,
                force_warp=True,
            )
            frames: list[bytes] = []
            reader: SharedFrameRingReader | None = None
            try:
                for index, t_ms in enumerate((0, 500, 1200)):
                    event = renderer.render_gpu_frame(
                        t_ms, force_warp=True, frame_index=index
                    )
                    if reader is None:
                        reader = SharedFrameRingReader.from_event(event)
                        reader.attach()
                    image = reader.read_qimage(event).convertToFormat(
                        QImage.Format.Format_RGBA8888
                    )
                    bits = image.constBits()
                    bits.setsize(image.sizeInBytes())
                    frames.append(bytes(bits))
            finally:
                if reader is not None:
                    reader.close()
            return frames

    base = gpu_frames(_scanline_style(karaoke_anim="none"))
    scan = gpu_frames(_scanline_style(karaoke_anim="scanline"))
    # 走字中帧有高亮带差异；起止帧（未开始/已完成）保持一致。
    assert _visible_diff(base[0], scan[0]) == 0
    assert _visible_diff(base[1], scan[1]) > 50
    assert _visible_diff(base[2], scan[2]) == 0


@pytest.mark.skipif(not _IS_WINDOWS, reason="Direct2D GPU backend is Windows-only")
def test_gpu_scanline_brighten_mode(monkeypatch) -> None:
    pytest.importorskip("PyQt6.QtWidgets")
    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
        SharedFrameRingReader,
        resolve_native_renderer_path,
    )

    renderer_path = resolve_native_renderer_path()
    if renderer_path is None:
        pytest.skip("native renderer sidecar not built")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    track = _wiping_track()

    def gpu_frame(style: Style, t_ms: int) -> bytes:
        with NativeRendererProcess(renderer_path, response_timeout_s=60.0) as renderer:
            renderer.configure_gpu(
                track, style, width=640, height=360, fps=60, force_warp=True
            )
            event = renderer.render_gpu_frame(t_ms, force_warp=True, frame_index=0)
            reader = SharedFrameRingReader.from_event(event)
            try:
                reader.attach()
                image = reader.read_qimage(event).convertToFormat(
                    QImage.Format.Format_RGBA8888
                )
                bits = image.constBits()
                bits.setsize(image.sizeInBytes())
                return bytes(bits)
            finally:
                reader.close()

    base = gpu_frame(_scanline_style(karaoke_anim="none"), 500)
    brighten = gpu_frame(
        _scanline_style(
            karaoke_anim="scanline",
            scanline_mode="brighten",
            scanline_brightness_pct=70,
        ),
        500,
    )
    zero = gpu_frame(
        _scanline_style(
            karaoke_anim="scanline",
            scanline_mode="brighten",
            scanline_brightness_pct=0,
        ),
        500,
    )
    assert _visible_diff(base, brighten) > 50
    assert _visible_diff(base, zero) == 0


@pytest.mark.skipif(not _IS_WINDOWS, reason="Direct2D GPU backend is Windows-only")
def test_gpu_utopia_scanline_paints_front_band(monkeypatch) -> None:
    pytest.importorskip("PyQt6.QtWidgets")
    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
        SharedFrameRingReader,
        resolve_native_renderer_path,
    )

    renderer_path = resolve_native_renderer_path()
    if renderer_path is None:
        pytest.skip("native renderer sidecar not built")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    track = _wiping_track()

    def gpu_frames(style: Style, t_ms: int) -> bytes:
        with NativeRendererProcess(renderer_path, response_timeout_s=60.0) as renderer:
            renderer.configure_gpu(
                track, style, width=640, height=360, fps=60, force_warp=True
            )
            event = renderer.render_gpu_frame(t_ms, force_warp=True, frame_index=0)
            reader = SharedFrameRingReader.from_event(event)
            try:
                reader.attach()
                image = reader.read_qimage(event).convertToFormat(
                    QImage.Format.Format_RGBA8888
                )
                bits = image.constBits()
                bits.setsize(image.sizeInBytes())
                return bytes(bits)
            finally:
                reader.close()

    base = gpu_frames(_scanline_style(karaoke_anim="utopia"), 500)
    scan = gpu_frames(_scanline_style(karaoke_anim="utopia_scanline"), 500)
    assert _visible_diff(base, scan) > 50
