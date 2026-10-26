# -*- coding: utf-8 -*-
"""行背景带（三段式背景条）：几何、预设解析、绘制与 GPU 回落判定。"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRectF  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402

from krok_helper.subtitle_render.domain.band_presets import (  # noqa: E402
    BAND_PRESETS,
    band_preset,
    default_tint_for,
    preset_id_for_image,
    resolve_band_image,
)
from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.engine.painter import paint_frame  # noqa: E402
from krok_helper.subtitle_render.engine.render.elements.band import (  # noqa: E402
    BandSpec,
    band_rect,
    band_spec,
    paint_band,
)
from krok_helper.subtitle_render.native.protocol import (  # noqa: E402
    gpu_unsupported_features,
)


def _style(**overrides) -> Style:
    base = dict(
        font_size_px=48,
        line_band_enabled=True,
        line_band_image="preset:plain_round",
        line_band_fill_mode="three_slice",
        line_band_cap_left_px=64,
        line_band_cap_right_px=64,
        line_band_pad_top_px=10,
        line_band_pad_bottom_px=12,
        line_band_pad_left_px=30,
        line_band_pad_right_px=40,
    )
    base.update(overrides)
    return Style(**base)


def _blank(w: int = 640, h: int = 360) -> QImage:
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#101010"))
    return img


def _track() -> TimingTrack:
    line = TimingLine(
        chars=[TimingChar(text="あい", start_ms=1000)], end_ms=2500
    )
    return TimingTrack(lines=[line])


# ------------------------------------------------------------------ presets
def test_every_preset_resolves_to_an_existing_png():
    for preset in BAND_PRESETS:
        assert preset.preset_id
        assert preset.image_spec == "preset:" + preset.preset_id
        path = resolve_band_image(preset.image_spec)
        assert path is not None and path.is_file(), preset.preset_id
        # 静态预设是 PNG，动图预设（星火/水泡/五线谱）是循环 animated WebP。
        assert path.suffix.lower() in {".png", ".webp"}, preset.preset_id


def test_preset_lookup_and_tint_metadata():
    assert band_preset("nope") is None
    assert preset_id_for_image("preset:stars") == "stars"
    assert preset_id_for_image("C:/x/y.png") is None
    # 只有可调色预设带默认色，其余为空串。
    assert default_tint_for("preset:diagonal") == "#3EC6E0"
    assert default_tint_for("preset:plain_round") == "#2B3A55"
    assert default_tint_for("preset:bubbles") == ""
    assert band_preset("staff_scroll").tintable is False


def test_missing_image_resolves_to_no_spec(tmp_path):
    missing = tmp_path / "nope.png"
    assert band_spec(_style(line_band_image=str(missing))) is None
    assert band_spec(_style(line_band_image="preset:not_a_preset")) is None
    assert band_spec(_style(line_band_enabled=False)) is None


def test_three_slice_cuts_at_the_configured_caps_and_fills_the_hole(qapp, tmp_path):
    """三段式口径（用户 2026-10-24 定）：

    先把图片按端点切成 0/1/2 三份，0 和 2 **直接画**在带子左右两端，中间 1
    填补剩下的空隙。切点就是用户设定的端点，不做任何隐藏夹取。
    """

    # 300x100 的测试图：左 100 红、中 100 绿、右 100 蓝。
    image = QImage(300, 100, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#000000"))
    painter = QPainter(image)
    painter.fillRect(0, 0, 100, 100, QColor("#FF0000"))
    painter.fillRect(100, 0, 100, 100, QColor("#00FF00"))
    painter.fillRect(200, 0, 100, 100, QColor("#0000FF"))
    painter.end()
    path = tmp_path / "slice.png"
    image.save(str(path))

    style = _style(
        line_band_image=str(path),
        line_band_cap_left_px=100,
        line_band_cap_right_px=100,
        line_band_fill_mode="three_slice",
        line_band_mid_mode="repeat",
    )
    spec = band_spec(style)
    assert spec is not None
    assert (spec.cap_left, spec.cap_right) == (100, 100)
    # 中段取图区间就是端点切出来的那一份。
    assert (spec.src_cap_left, spec.src_cap_right) == (100, 100)

    canvas = QImage(500, 100, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(QColor(0, 0, 0, 0))
    target = QPainter(canvas)
    paint_band(target, QRectF(0.0, 0.0, 500.0, 100.0), spec)
    target.end()

    # 带高=图高 → scale=1：左端点 100px 红、右端点 100px 蓝、中间 300px 绿。
    assert canvas.pixelColor(50, 50).red() > 200
    assert canvas.pixelColor(50, 50).green() < 60
    assert canvas.pixelColor(450 - 0, 50).blue() > 200
    for x in (150, 250, 350):
        pixel = canvas.pixelColor(x, 50)
        assert pixel.green() > 200 and pixel.red() < 60, f"x={x} 不是中段绿"

    # 端点调小 → 切点跟着变，中段变成「0..100 之外」的那一块（换图验证）。
    smaller = band_spec(
        replace(style, line_band_cap_left_px=40, line_band_cap_right_px=20)
    )
    assert smaller is not None
    assert (smaller.src_cap_left, smaller.src_cap_right) == (40, 20)

# ----------------------------------------------------------------- geometry
def test_band_rect_uses_four_independent_paddings():
    text = QRectF(100.0, 200.0, 300.0, 60.0)
    style = _style(
        line_band_pad_top_px=7,
        line_band_pad_bottom_px=11,
        line_band_pad_left_px=13,
        line_band_pad_right_px=17,
    )
    rect = band_rect(text, style)
    assert rect.left() == pytest.approx(text.left() - 13)
    assert rect.right() == pytest.approx(text.right() + 17)
    assert rect.top() == pytest.approx(text.top() - 7)
    assert rect.bottom() == pytest.approx(text.bottom() + 11)


def test_band_rect_ruby_reference_moves_only_the_top():
    text = QRectF(50.0, 200.0, 200.0, 60.0)
    without = band_rect(text, _style(line_band_include_ruby=False), ruby_top=150.0)
    with_ruby = band_rect(text, _style(line_band_include_ruby=True), ruby_top=150.0)
    assert with_ruby.top() < without.top()
    assert with_ruby.bottom() == pytest.approx(without.bottom())


def test_band_rect_offset_shifts_both_edges():
    text = QRectF(0.0, 100.0, 100.0, 40.0)
    base = band_rect(text, _style())
    shifted = band_rect(text, _style(line_band_offset_y_px=9))
    assert shifted.top() - base.top() == pytest.approx(9)
    assert shifted.bottom() - base.bottom() == pytest.approx(9)


# ------------------------------------------------------------------ painting
def test_staff_lines_layer_follows_line_color(qapp):
    """谱线层要同时染五线和**高音谱号**，并保留谱号自带的深色描边。"""

    def counts(color: str) -> tuple[int, int]:
        image = QImage(900, 360, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0, 0))
        style = _style(
            line_band_image="preset:staff_scroll",
            line_band_cap_left_px=190,
            line_band_cap_right_px=160,
            line_band_line_color=color,
            line_band_fill_mode="stretch",
        )
        paint_frame(image, _track(), 1800, style)
        xs: list[int] = []
        ys: list[int] = []
        for y in range(image.height()):
            for x in range(image.width()):
                if image.pixelColor(x, y).alpha() > 8:
                    xs.append(x)
                    ys.append(y)
        assert xs
        left, right = min(xs), max(xs)
        # 只看左端点区域：谱号在那里。
        limit = left + int((right - left) * 0.3)
        red = 0
        dark = 0
        for y in range(0, image.height(), 2):
            for x in range(left, limit, 2):
                pixel = image.pixelColor(x, y)
                if pixel.red() > 180 and pixel.green() < 70 and pixel.blue() < 70:
                    red += 1
                elif pixel.alpha() > 180 and pixel.red() < 60 and pixel.green() < 60:
                    dark += 1
        return red, dark

    red, dark = counts("#E01B24")
    assert red > 40, "谱号和谱线都应被染成用户颜色"
    assert dark > 20, "谱号必须保留深色描边"
    assert counts("#000000")[0] == 0


def test_band_edge_glow_grows_beyond_the_band_rect(qapp):
    """发光/投影要长到带子轮廓之外，否则会被不透明白底完全盖住。"""
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(ch, index * 100) for index, ch in enumerate("あいうえおか")],
                end_ms=2_000,
            )
        ]
    )
    canvas = _blank(1200, 400)

    def alpha_bbox(style):
        image = QImage(1200, 400, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0, 0))
        paint_frame(image, track, 1_200, style)
        xs: list[int] = []
        ys: list[int] = []
        for y in range(image.height()):
            for x in range(image.width()):
                if image.pixelColor(x, y).alpha() > 8:
                    xs.append(x)
                    ys.append(y)
        assert xs
        return min(xs), min(ys), max(xs), max(ys)

    base = alpha_bbox(_style(line_band_image="preset:plain_round"))
    glow = alpha_bbox(
        _style(
            line_band_image="preset:plain_round",
            line_band_edge_kind="glow",
            line_band_edge_color="#00FF00",
            line_band_edge_size_px=18,
            line_band_edge_opacity_pct=90,
        )
    )
    # 发光/描边/阴影的尺寸按素材像素给（带子按高度等比缩放），这里只守
    # 「四边都往外长」这个语义；具体像素数随带高缩放。
    assert glow[0] < base[0]
    assert glow[1] < base[1]
    assert glow[2] > base[2]
    assert glow[3] > base[3]


def test_band_is_painted_beside_the_text_and_absent_when_disabled(qapp):
    track = _track()
    enabled = _style()
    disabled = _style(line_band_enabled=False)

    img_on = _blank()
    img_off = _blank()
    paint_frame(img_on, track, 1800, enabled)
    paint_frame(img_off, track, 1800, disabled)

    # 文字在两边渲染完全一致，任何像素差都来自带子。
    diff = 0
    for y in range(0, img_on.height(), 2):
        for x in range(0, img_on.width(), 2):
            if img_on.pixelColor(x, y).rgb() != img_off.pixelColor(x, y).rgb():
                diff += 1
    assert diff > 200, "开启行背景带后应与关闭态有明显像素差"


def test_three_slice_survives_a_band_narrower_than_its_caps(qapp):
    img = _blank(64, 64)
    from PyQt6.QtGui import QPainter

    spec = BandSpec(
        image_path=str(resolve_band_image("preset:staff_scroll")),
        fill_mode="three_slice",
        cap_left=190,
        cap_right=160,
        mid_mode="repeat",
        tint="",
        tint_strength_pct=0,
        opacity_pct=100,
    )
    painter = QPainter(img)
    try:
        paint_band(painter, QRectF(0, 0, 80, 40), spec)  # 端点总宽远大于带子
    finally:
        painter.end()


# ---------------------------------------------------------------- gpu guard
def test_gpu_band_does_not_force_cpu_fallback():
    """行背景带已由 D2D sidecar 原生绘制：开启后不再整帧回落 CPU。"""
    track = _track()
    assert "line_band" not in gpu_unsupported_features(track, _style())
    assert "line_band" not in gpu_unsupported_features(
        track, _style(line_band_enabled=False)
    )


# ------------------------------------------------------------- persistence
BAND_FIELDS = (
    "line_band_enabled",
    "line_band_image",
    "line_band_fill_mode",
    "line_band_cap_left_px",
    "line_band_cap_right_px",
    "line_band_mid_mode",
    "line_band_pad_top_px",
    "line_band_pad_bottom_px",
    "line_band_pad_left_px",
    "line_band_pad_right_px",
    "line_band_include_ruby",
    "line_band_offset_y_px",
    "line_band_opacity_pct",
    "line_band_tint",
    "line_band_tint_strength_pct",
)


def test_preset_middle_tiles_wrap_without_a_visible_seam():
    """中段瓦片必须首尾相接：拼接处的列差不能明显大于内部相邻列差。

    预设素材一旦不可循环，平铺时每个块边界都会出现一条竖线——这是 2026-10-23
    用户实测反馈的「每个预设拼接痕迹都很明显」。渲染端已改成一次取样预拼条，
    这里守住素材本身的自洽性。
    """
    import numpy as np

    for preset in BAND_PRESETS:
        # 只有默认「三段式」的预设才靠中段平铺：花纹类预设默认「裁切（覆盖）」，
        # 整图等比覆盖不平铺，素材不需要自我循环（用户口径 2026-10-24）。
        if preset.default_fill_mode != "three_slice":
            continue
        image = QImage(str(preset.path)).convertToFormat(
            QImage.Format.Format_RGBA8888
        )
        bits = image.constBits()
        bits.setsize(image.sizeInBytes())
        pixels = np.frombuffer(bytes(bits), dtype=np.uint8).reshape(
            image.height(), image.bytesPerLine() // 4, 4
        )[:, : image.width(), :].astype(np.float32)
        left = preset.cap_left_px
        right = preset.cap_right_px
        tile = pixels[:, left : image.width() - right, :]
        seam = float(np.abs(tile[:, 0, :] - tile[:, -1, :]).mean())
        interior = float(np.abs(np.diff(tile, axis=1)).mean())
        assert seam <= max(interior * 8.0, 12.0), (
            f"{preset.preset_id}: 中段首尾列差 {seam:.2f} 远大于内部 "
            f"{interior:.2f}，平铺会出现明显竖缝"
        )


def test_animated_band_gif_picks_frames_by_time(qapp, tmp_path):
    """GIF 行背景要真的动起来：按累积帧时长选帧，CPU 与 sidecar 同契约。"""
    from PIL import Image, ImageDraw

    from krok_helper.subtitle_render.domain.models import Style
    from krok_helper.subtitle_render.engine.render.elements.band import (
        BAND_ANIM_MAX_FRAMES,
        animated_band_frames,
        band_spec,
        paint_band,
    )

    frames = []
    for color in ((255, 40, 40, 255), (40, 255, 80, 255), (40, 120, 255, 255)):
        frame = Image.new("RGBA", (480, 60), (0, 0, 0, 0))
        ImageDraw.Draw(frame).rectangle([0, 10, 480, 50], fill=color)
        frames.append(frame)
    gif_path = tmp_path / "band.gif"
    frames[0].save(
        str(gif_path), save_all=True, append_images=frames[1:],
        duration=100, loop=0, disposal=2,
    )

    animated = animated_band_frames(str(gif_path))
    assert animated is not None
    decoded, delays = animated
    assert len(decoded) == 3
    assert len(decoded) <= BAND_ANIM_MAX_FRAMES
    assert delays == (100, 100, 100)

    style = Style(
        line_band_enabled=True,
        line_band_image=str(gif_path),
        line_band_fill_mode="cover",
    )
    spec = band_spec(style)
    sampled = []
    for t_ms in (0, 150, 250):
        canvas = QImage(240, 60, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(QColor(0, 0, 0, 0))
        painter = QPainter(canvas)
        try:
            paint_band(painter, QRectF(0, 0, 240, 60), spec, t_ms)
        finally:
            painter.end()
        sampled.append(canvas.pixelColor(120, 30).name())
    assert len(set(sampled)) >= 2, sampled


def _transparent_pixels(image: QImage) -> int:
    import numpy as np

    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    alpha = np.frombuffer(bytes(bits), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine() // 4, 4
    )[:, : image.width(), 3]
    return int((alpha < 255).sum())


def _translucent_pixels(image: QImage) -> int:
    import numpy as np

    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    alpha = np.frombuffer(bytes(bits), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine() // 4, 4
    )[:, : image.width(), 3]
    return int(((alpha > 0) & (alpha < 255)).sum())


def test_shipped_animated_presets_loop_on_the_first_frame():
    """随包动图预设必须**首尾严格同帧**、60 帧 × 20ms，而且带真半透明。

    2026-10-24 用户实测「循环不完整、第一帧不是最后一帧」。当时素材是 GIF：
    每帧单独配调色板，个别帧的透明下标没落到任何像素上——解码出来整帧不透明，
    每个循环闪一块实心色板；首帧用全局调色板、末帧用局部调色板，同样的内容也
    会量化出不同像素。现在动图预设改成 animated WebP（8 bit alpha），这条守住
    「首尾同帧 + 时长 + 半透明确实是半透明（不是被压成 0/255）」。
    """
    from krok_helper.subtitle_render.engine.render.elements.band import (
        animated_band_frames,
    )

    animated_presets = [
        preset for preset in BAND_PRESETS
        if preset.path.suffix.lower() == ".webp"
    ]
    assert animated_presets, "至少要有星火/水泡/五线谱三个动图预设"
    for preset in animated_presets:
        animated = animated_band_frames(str(preset.path))
        assert animated is not None, preset.preset_id
        frames, delays = animated
        assert len(frames) == 60, preset.preset_id
        assert set(delays) == {20}, preset.preset_id
        assert sum(delays) == 1200, preset.preset_id
        # 首尾严格同帧（像素级，透明区也一致）
        assert frames[0] == frames[-1], preset.preset_id
        # 真的在动（不能整段静止）
        assert frames[0] != frames[1], preset.preset_id
        for index, frame in enumerate(frames):
            assert _transparent_pixels(frame) > 0, (
                f"{preset.preset_id}[{index}] 整帧不透明，播放时会闪一块色板"
            )
        if preset.preset_id == "bubbles":
            # 水泡的淡填充必须是真半透明，不能退化成 0/255
            assert _translucent_pixels(frames[0]) > 1000, preset.preset_id


def test_band_fields_survive_project_roundtrip():
    from krok_helper.subtitle_render.domain.models import (
        style_from_dict,
        style_to_dict,
    )

    style = _style(
        line_band_fill_mode="cover",
        line_band_mid_mode="stretch",
        line_band_include_ruby=True,
        line_band_offset_y_px=-7,
        line_band_opacity_pct=42,
        line_band_tint="#123456",
        line_band_tint_strength_pct=61,
    )
    restored = style_from_dict(style_to_dict(style))
    for name in BAND_FIELDS:
        assert getattr(restored, name) == getattr(style, name), name


def test_invalid_band_enum_falls_back_to_defaults():
    from krok_helper.subtitle_render.domain.models import style_from_dict

    restored = style_from_dict(
        {"line_band_fill_mode": "bogus", "line_band_mid_mode": "bogus"}
    )
    assert restored.line_band_fill_mode == Style().line_band_fill_mode
    assert restored.line_band_mid_mode == Style().line_band_mid_mode

def test_native_mid_strip_is_drawn_from_the_strip_not_the_source_bitmap():
    """sidecar 中段必须画预拼条本身。

    预拼条已按设计边界裁剪；若误传源 bitmap，会从 x=0 取样，把谱号等端点
    图案在中段开头再画一遍（用户实测「左侧谱号出现两遍」的根因）。
    """
    source = Path(
        "native/subtitle_renderer/src/backends/direct2d/d2d_backend_render.cpp"
    ).read_text(encoding="utf-8")
    marker = "} else if (midStrip) {"
    start = source.index(marker)
    branch = source[start:start + 1200]
    assert "drawBandPiece(midStrip," in branch
    assert "drawBandPiece(bitmap," not in branch

@pytest.mark.parametrize("kind", ["shadow", "glow", "stroke"])
@pytest.mark.parametrize("fill_mode", ["stretch", "three_slice", "cover", "repeat"])
def test_painter_effects_follow_displayed_gif_alpha(qapp, tmp_path, kind, fill_mode):
    import numpy as np
    from PIL import Image, ImageDraw

    frames = []
    for x in (12, 65, None):
        frame = Image.new("RGBA", (100, 64), (0, 0, 0, 0))
        if x is not None:
            ImageDraw.Draw(frame).rectangle((x, 15, x + 18, 45), fill="white")
        frames.append(frame)
    path = tmp_path / "alpha.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=400, disposal=2, loop=0)
    spec = band_spec(_style(
        line_band_image=str(path), line_band_fill_mode=fill_mode,
        line_band_cap_left_px=10, line_band_cap_right_px=10,
        line_band_mid_mode="stretch", line_band_shadow_distance_px=0,
        **{f"line_band_{kind}_enabled": True, f"line_band_{kind}_size_px": 6,
           f"line_band_{kind}_color": "#FF0000"},
    ))
    masks = []
    for t in (0, 400, 800, 1200):
        canvas = QImage(300, 180, QImage.Format.Format_RGBA8888)
        canvas.fill(0)
        painter = QPainter(canvas)
        try:
            paint_band(painter, QRectF(30, 30, 240, 100), spec, t)
        finally:
            painter.end()
        bits = canvas.constBits()
        bits.setsize(canvas.sizeInBytes())
        rgba = np.frombuffer(bytes(bits), np.uint8).reshape(180, 300, 4).astype(int)
        masks.append((rgba[:, :, 0] > rgba[:, :, 1] + 30) & (rgba[:, :, 3] > 10))
    assert masks[0].sum() > 20 and masks[1].sum() > 20
    assert not masks[2].any()
    assert not np.array_equal(masks[0], masks[1])
    assert np.array_equal(masks[0], masks[3])


def test_band_effect_keeps_faint_alpha_and_color_opacity(qapp):
    from krok_helper.subtitle_render.engine.render.elements.band import BandEffect, _edge_source

    image = QImage(40, 40, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(255, 255, 255, 32))
    spec = band_spec(_style())
    edge, pad = _edge_source(spec, image, BandEffect("stroke", "#80FF0000", 2))
    # Do not binarize faint alpha away; the effect color's alpha must also apply.
    assert 14 <= edge.pixelColor(20 + pad, 20 + pad).alpha() <= 18


@pytest.mark.parametrize("alignment", ["left", "center", "right"])
@pytest.mark.parametrize("offset", [-40, 0, 100])
def test_band_covers_inline_volume_bounds(qapp, monkeypatch, alignment, offset):
    from krok_helper.subtitle_render.engine import painter as engine
    captured = []
    monkeypatch.setattr(engine, "_paint_band", lambda painter, rect, spec, t: captured.append(QRectF(rect)))
    style = _style(
        volume_enabled=True, volume_appearance_mode="custom",
        volume_size=80, volume_column_count=4, volume_column_width=18,
        volume_offset_x=offset, volume_duration_ms=2000,
        line_horizontal_layout=alignment,
    )
    track = _track()
    image = _blank()
    paint_frame(image, track, 1800, style)
    assert captured
    full = captured[-1]
    captured.clear()
    paint_frame(image, track, 1800, replace(style, volume_enabled=False))
    assert captured
    assert full.width() > captured[-1].width()


@pytest.mark.parametrize("enabled,host", [(False, None), (True, False), (True, True)])
def test_inline_content_width_respects_module_and_host(qapp, enabled, host):
    from krok_helper.subtitle_render.engine.render.elements.horizontal.positioning import line_total_width
    from krok_helper.subtitle_render.engine.render.elements.signal import inline_volume_geometry
    track = _track()
    line = track.lines[0]
    line.volume_head_override = host
    style = _style(volume_enabled=enabled, volume_appearance_mode="custom")
    text = line_total_width(line, style)
    content = line_total_width(line, style, track=track)
    inline = inline_volume_geometry(track, line, style)
    if enabled and host:
        assert inline is not None
        assert content == round(text + inline[1].group_width)
    else:
        assert inline is None
        assert content == text
