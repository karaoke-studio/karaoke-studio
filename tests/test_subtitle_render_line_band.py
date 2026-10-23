# -*- coding: utf-8 -*-
"""行背景带（三段式背景条）：几何、预设解析、绘制与 GPU 回落判定。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRectF  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402

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
        assert path.suffix.lower() == ".png"


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


def test_display_caps_can_shrink_below_the_preset_design_caps():
    """显示端点可自由调小，但中段仍从素材设计边界切——不会重复端点图案。

    用户实测：左端点拖到 60 时谱号出现两遍（中段从 60 开始把谱号又铺一
    次）。修复口径是显示端点与源图裁切边界解耦：显示 60/40，源图仍是
    190/160，谱号只出现在端点里（被裁窄），中段不含谱号。
    """
    from krok_helper.subtitle_render.engine.render.elements.band import (
        _mid_tile,
        band_source,
    )

    preset = band_preset("staff_scroll")
    spec = band_spec(
        _style(
            line_band_image="preset:staff_scroll",
            line_band_cap_left_px=60,
            line_band_cap_right_px=40,
        )
    )
    assert spec is not None
    assert (spec.cap_left, spec.cap_right) == (60, 40)
    assert (spec.src_cap_left, spec.src_cap_right) == (
        preset.cap_left_px,
        preset.cap_right_px,
    )
    # 中段瓦片起点在素材设计边界：瓦片宽度 = 图宽 - 设计端点之和，且左缘
    # 那 40 列只有 5 条 3px 谱线（谱号墨迹不在其中）。
    image = band_source(spec)
    tile = _mid_tile(image, spec.src_cap_left, spec.src_cap_right)
    assert tile.width() == image.width() - preset.cap_left_px - preset.cap_right_px
    opaque = sum(
        1
        for x in range(min(40, tile.width()))
        for y in range(tile.height())
        if tile.pixelColor(x, y).alpha() > 200
    )
    assert opaque < 40 * tile.height() * 0.2
    # 自定义图没有设计边界：源图边界跟随显示端点。
    custom = band_spec(
        _style(
            line_band_image=str(preset.path),
            line_band_cap_left_px=60,
            line_band_cap_right_px=40,
        )
    )
    assert custom is not None
    assert (custom.cap_left, custom.cap_right) == (60, 40)
    assert (custom.src_cap_left, custom.src_cap_right) == (60, 40)


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
    assert glow[0] < base[0] - 6
    assert glow[1] < base[1] - 6
    assert glow[2] > base[2] + 6
    assert glow[3] > base[3] + 6


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
