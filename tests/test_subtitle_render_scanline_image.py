"""自定义图片扫字线形态（scanline_shape == "image"）契约与渲染行为。

2026-10 新增的第四种扫字线形态：用户图片（含动图 GIF/WebP）叠加在走字
锋面上，可调偏移、可关普通高亮带、可叠「取色层级」（实色上色/描边/装饰
的排列组合）；素材按绝对路径存盘、工程目录回退、缺失回退普通高亮；
不参与 utopia 缩放与布局防重叠统计。
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRectF  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402

from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    Style,
    normalize_scanline_image_layers,
    style_from_dict,
    style_to_dict,
    style_with_output_scanline,
)
from krok_helper.subtitle_render.domain.timing import (  # noqa: E402
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.engine.painter import paint_frame  # noqa: E402
from krok_helper.subtitle_render.engine.render.elements.horizontal.scanline import (  # noqa: E402
    scanline_params_for_style,
)
from krok_helper.subtitle_render.engine.render.elements.scanline_image import (  # noqa: E402
    clear_scanline_image_caches,
    paint_scanline_image,
    resolve_scanline_image_path,
    scanline_image_available,
    scanline_image_frame,
    scanline_image_search_roots,
    set_scanline_image_search_roots,
)
from krok_helper.subtitle_render.engine.value_signature import (  # noqa: E402
    lyric_layout_style_signature,
)
from krok_helper.subtitle_render.native.protocol import (  # noqa: E402
    gpu_unsupported_features,
)


def _image_style(**changes) -> Style:
    base = dict(
        entry_anim="none",
        exit_anim="none",
        sync_entry=False,
        sync_ending=False,
        sync_each_page=False,
        line_lead_in_ms=0,
        line_tail_ms=200,
        scanline_enabled=True,
        scanline_width_px=18,
        scanline_color="#40E0FF",
        scanline_glow_px=6,
        scanline_shape="image",
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


def _marker_png(path: Path, size: int = 24, color=(255, 60, 60, 255)) -> Path:
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle(
        [0, 0, size - 1, size - 1], fill=color
    )
    image.save(str(path))
    return path


def _marker_gif(path: Path) -> Path:
    from PIL import Image, ImageDraw

    frames = []
    for color in ((255, 40, 40, 255), (40, 255, 80, 255), (40, 120, 255, 255)):
        frame = Image.new("RGBA", (24, 24), (0, 0, 0, 0))
        ImageDraw.Draw(frame).rectangle([0, 0, 23, 23], fill=color)
        frames.append(frame)
    frames[0].save(
        str(path), save_all=True, append_images=frames[1:],
        duration=100, loop=0, disposal=2,
    )
    return path


@pytest.fixture(autouse=True)
def _clean_scanline_image_state():
    clear_scanline_image_caches()
    set_scanline_image_search_roots([])
    yield
    set_scanline_image_search_roots([])
    clear_scanline_image_caches()


def _frame_bytes(track: TimingTrack, style: Style, t_ms: int) -> bytes:
    image = QImage(1280, 720, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    paint_frame(image, track, t_ms, style)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    return bytes(bits)


def _visible_diff(left: bytes, right: bytes) -> int:
    import numpy as np

    a = np.frombuffer(left, dtype=np.uint8).reshape(-1, 4)
    b = np.frombuffer(right, dtype=np.uint8).reshape(-1, 4)
    return int((np.abs(a.astype(int) - b.astype(int)).max(axis=1) > 8).sum())


def _image_pixels(
    image: QImage,
    target: QColor,
    tolerance: int = 24,
    alpha_min: int = 128,
):
    """收集接近目标色的像素（含包围盒）。

    ARGB32 在内存里按小端 B,G,R,A 排布，通道下标注意别对调；
    ``alpha_min`` 用于收集发光晕这类半透明像素。
    """

    import numpy as np

    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    array = np.frombuffer(bytes(bits), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine() // 4, 4
    )[:, : image.width(), :]
    alpha = array[:, :, 3]
    blue, green, red = array[:, :, 0], array[:, :, 1], array[:, :, 2]
    mask = alpha >= alpha_min
    for channel, value in zip(
        (blue, green, red), (target.blue(), target.green(), target.red())
    ):
        mask &= np.abs(channel.astype(int) - value) <= tolerance
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return 0, None
    return len(xs), (
        int(xs.min()),
        int(ys.min()),
        int(xs.max()),
        int(ys.max()),
    )


# ---------------------------------------------------------------------------
# 模型与序列化
# ---------------------------------------------------------------------------


def test_scanline_image_style_fields_round_trip() -> None:
    style = _image_style(
        scanline_image_path="D:\\assets\\mark.webp",
        scanline_image_band_enabled=False,
        scanline_image_offset_x_px=-60,
        scanline_image_offset_y_px=35,
        scanline_image_color_layers="stroke,decor",
        scanline_image_scale_pct=150,
        scanline_image_tint_strength_pct=30,
    )
    restored = style_from_dict(style_to_dict(style))
    assert restored.scanline_shape == "image"
    assert restored.scanline_image_path == "D:\\assets\\mark.webp"
    assert restored.scanline_image_band_enabled is False
    assert restored.scanline_image_offset_x_px == -60
    assert restored.scanline_image_offset_y_px == 35
    assert restored.scanline_image_color_layers == "stroke,decor"
    assert restored.scanline_image_scale_pct == 150
    assert restored.scanline_image_tint_strength_pct == 30
    # 新字段默认值：旧工程读入不受影响。
    fresh = style_from_dict({"scanline_shape": "image"})
    assert fresh.scanline_image_path == ""
    assert fresh.scanline_image_band_enabled is True
    assert fresh.scanline_image_offset_x_px == 0
    assert fresh.scanline_image_offset_y_px == 0
    assert fresh.scanline_image_color_layers == ""
    assert fresh.scanline_image_scale_pct == 100
    assert fresh.scanline_image_tint_strength_pct == 50


def test_normalize_scanline_image_layers_combinations() -> None:
    # 任意组合、乱序去重、未知 token 丢弃、空 → 原样绘制。
    assert normalize_scanline_image_layers("decor,solid,solid") == "solid,decor"
    assert normalize_scanline_image_layers("stroke") == "stroke"
    assert (
        normalize_scanline_image_layers("decor,solid,stroke,bogus")
        == "solid,stroke,decor"
    )
    assert normalize_scanline_image_layers("") == ""
    assert normalize_scanline_image_layers(None) == ""
    assert normalize_scanline_image_layers(42) == ""


def test_style_from_dict_validates_image_fields() -> None:
    restored = style_from_dict(
        {
            "scanline_shape": "image",
            "scanline_image_path": 7,
            "scanline_image_band_enabled": "off",
            "scanline_image_offset_x_px": 9999,
            "scanline_image_offset_y_px": -9999,
            "scanline_image_color_layers": "bogus",
        }
    )
    assert restored.scanline_image_path == ""
    assert restored.scanline_image_band_enabled is True
    assert restored.scanline_image_offset_x_px == 2000
    assert restored.scanline_image_offset_y_px == -2000
    assert restored.scanline_image_color_layers == ""
    # 非法形态回落普通高亮，不把未知字符串带进渲染端。
    assert style_from_dict({"scanline_shape": "wat"}).scanline_shape == "band"


def test_style_with_output_scanline_scales_image_offsets() -> None:
    style = _image_style(
        scanline_image_offset_x_px=-108,
        scanline_image_offset_y_px=54,
    )
    scaled = style_with_output_scanline(style, 2160)
    assert scaled.scanline_image_offset_x_px == -216
    assert scaled.scanline_image_offset_y_px == 108
    # 1080 输出原样返回同一对象。
    assert style_with_output_scanline(style, 1080) is style


# ---------------------------------------------------------------------------
# 参数解析与高亮带闸门
# ---------------------------------------------------------------------------


def test_scanline_image_params_resolution(tmp_path) -> None:
    marker = _marker_png(tmp_path / "mark.png")
    present = _image_style(
        scanline_image_path=str(marker),
        scanline_image_band_enabled=False,
        scanline_image_color_layers="solid,decor",
        scanline_image_offset_x_px=-10,
        scanline_image_offset_y_px=20,
    )
    params = scanline_params_for_style(present)
    assert params.shape == "image"
    assert params.is_column_shape
    assert params.image_path == str(marker)
    assert params.image_band_enabled is False
    assert params.image_layers == ("solid", "decor")
    assert params.image_offset_x_px == -10.0
    assert params.image_offset_y_px == 20.0
    # 图片可用 + 高亮关闭 → 普通高亮带抑制。
    assert params.suppress_strip is True
    # 高亮开启：永不抑制。
    assert (
        scanline_params_for_style(
            replace(present, scanline_image_band_enabled=True)
        ).suppress_strip
        is False
    )
    # 素材缺失 + 高亮关闭 → 回退普通高亮（不抑制）。
    missing = scanline_params_for_style(
        replace(present, scanline_image_path=str(tmp_path / "gone.png"))
    )
    assert missing.image_layers == ("solid", "decor")
    assert missing.suppress_strip is False
    # 未勾任何取色层级 → 原样绘制。
    assert (
        scanline_params_for_style(
            replace(present, scanline_image_color_layers="bogus")
        ).image_layers
        == ()
    )


def test_scanline_image_shape_normalizes_to_band() -> None:
    params = scanline_params_for_style(
        _image_style(karaoke_anim="utopia", scanline_shape="bubble")
    )
    assert params.shape == "bubble"
    # 非法形态回落 band。
    fallback = scanline_params_for_style(
        Style(scanline_shape="statue")
    )
    assert fallback.shape == "band"


# ---------------------------------------------------------------------------
# 路径回退
# ---------------------------------------------------------------------------


def test_resolve_scanline_image_path_fallbacks(tmp_path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    moved = _marker_png(project_dir / "mark.webp", color=(10, 200, 220, 255))
    broken = str(tmp_path / "usb" / "mark.webp")

    # 无搜索根：绝对路径失效 → None。
    assert resolve_scanline_image_path("") is None
    assert resolve_scanline_image_path(broken) is None
    # 登记工程目录后按 basename 命中。
    set_scanline_image_search_roots([project_dir])
    assert resolve_scanline_image_path(broken) == str(moved)
    # 工程目录里也没有的文件名仍为 None。
    assert resolve_scanline_image_path(str(tmp_path / "nope.webp")) is None
    # 绝对路径存在时优先于搜索根。
    direct = _marker_png(tmp_path / "direct.png")
    assert resolve_scanline_image_path(str(direct)) == str(direct)
    assert scanline_image_available(str(direct))
    assert not scanline_image_available(str(direct) + ".missing")


def test_session_registers_search_roots(tmp_path) -> None:
    from krok_helper.subtitle_render.project.session import (
        SubtitleProjectSession,
    )

    session = SubtitleProjectSession()
    session.adopt_project_identity(
        path=tmp_path / "song.yurika", disk_revision=1
    )
    assert scanline_image_search_roots() == [tmp_path]
    session.adopt_project_identity(path=None, disk_revision=1)
    assert scanline_image_search_roots() == []


# ---------------------------------------------------------------------------
# 帧解码（动图逐帧）
# ---------------------------------------------------------------------------


def test_scanline_image_frame_picks_animated_frames(tmp_path) -> None:
    gif = _marker_gif(tmp_path / "mark.gif")
    first = scanline_image_frame(str(gif), 0)
    assert first is not None
    assert first.pixelColor(12, 12).name().upper() == "#FF2828"
    assert scanline_image_frame(str(gif), 150).pixelColor(12, 12).name().upper() == "#28FF50"
    assert scanline_image_frame(str(gif), 250).pixelColor(12, 12).name().upper() == "#2878FF"
    # 循环：总时长 300ms 后回到第一帧。
    assert scanline_image_frame(str(gif), 310).pixelColor(12, 12).name().upper() == "#FF2828"


def test_scanline_image_frame_static_passthrough(tmp_path) -> None:
    marker = _marker_png(tmp_path / "mark.png")
    frame = scanline_image_frame(str(marker), 12345)
    assert frame is not None
    assert frame.pixelColor(12, 12).alpha() == 255


# ---------------------------------------------------------------------------
# 绘制：位置/偏移/取色层级
# ---------------------------------------------------------------------------


def _paint_to_canvas(params, style, **kwargs) -> QImage:
    from krok_helper.subtitle_render.engine.style.style_semantics import (
        effective_karaoke_colors,
    )

    canvas = QImage(400, 240, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(0)
    painter = QPainter(canvas)
    try:
        paint_scanline_image(
            painter,
            params=params,
            style=style,
            shape_colors=kwargs.get(
                "shape_colors", effective_karaoke_colors(style)
            ),
            shape_style=kwargs.get("shape_style", style),
            anchor_x=kwargs.get("anchor_x", 200.0),
            anchor_y=kwargs.get("anchor_y", 120.0),
            gradient_line=kwargs.get("gradient_line", QRectF(0, 0, 400, 240)),
            opacity=1.0,
            canvas_scale=kwargs.get("canvas_scale", 1.0),
        )
    finally:
        painter.end()
    return canvas


def _params_of(style: Style):
    return scanline_params_for_style(style)


def test_paint_scanline_image_centers_on_anchor_and_applies_offsets(
    tmp_path,
) -> None:
    marker = _marker_png(tmp_path / "mark.png", size=24)

    centered = _paint_to_canvas(
        _params_of(_image_style(scanline_image_path=str(marker))),
        _image_style(scanline_image_path=str(marker)),
    )
    count, box = _image_pixels(centered, QColor("#FF3C3C"))
    assert count > 100
    cx = (box[0] + box[2]) / 2.0
    cy = (box[1] + box[3]) / 2.0
    assert cx == pytest.approx(200.0, abs=2.0)
    assert cy == pytest.approx(120.0, abs=2.0)

    shifted_style = _image_style(
        scanline_image_path=str(marker),
        scanline_image_offset_x_px=-30,
        scanline_image_offset_y_px=25,
    )
    shifted = _paint_to_canvas(_params_of(shifted_style), shifted_style)
    _, box2 = _image_pixels(shifted, QColor("#FF3C3C"))
    cx2 = (box2[0] + box2[2]) / 2.0
    cy2 = (box2[1] + box2[3]) / 2.0
    assert cx2 == pytest.approx(170.0, abs=2.0)
    # Y 正值向上（屏幕 Y 轴向下，+25 → 中心上移到 95）。
    assert cy2 == pytest.approx(95.0, abs=2.0)


def test_paint_scanline_image_scale_pct(tmp_path) -> None:
    """尺寸百分比：200% 时包围盒尺寸翻倍。"""

    marker = _marker_png(tmp_path / "mark.png", size=24)
    style = _image_style(
        scanline_image_path=str(marker), scanline_image_scale_pct=200
    )
    scaled = _paint_to_canvas(_params_of(style), style)
    _, box = _image_pixels(scaled, QColor("#FF3C3C"))
    assert box is not None
    assert box[2] - box[0] == pytest.approx(46, abs=4)


def test_paint_scanline_image_scales_with_canvas(tmp_path) -> None:
    marker = _marker_png(tmp_path / "mark.png", size=24)
    style = _image_style(scanline_image_path=str(marker))
    base = _paint_to_canvas(_params_of(style), style, canvas_scale=1.0)
    _, box = _image_pixels(base, QColor("#FF3C3C"))
    assert box[2] - box[0] == pytest.approx(23, abs=2)
    doubled = _paint_to_canvas(_params_of(style), style, canvas_scale=2.0)
    _, box2 = _image_pixels(doubled, QColor("#FF3C3C"))
    assert box2[2] - box2[0] == pytest.approx(47, abs=3)


def test_paint_scanline_image_layers(tmp_path) -> None:
    # 480px 图：效果比值 = 480/1080 ≈ 0.444（描边 8 → ceil 4px、
    # 发光 10 → ceil 5px），保证新换算下描边/发光仍然可测。
    marker = _marker_png(tmp_path / "big.png", size=480)

    plain_style = _image_style(scanline_image_path=str(marker))
    plain = _paint_to_canvas(
        _params_of(plain_style), plain_style, canvas_scale=240 / 1080
    )
    count, plain_box = _image_pixels(plain, QColor("#FF3C3C"))
    assert count > 100

    # 1) 实色上色（100% = 纯目标色剪影，alpha 保留）。
    tint_style = _image_style(
        scanline_image_path=str(marker),
        scanline_color="#00E5FF",
        scanline_image_tint_strength_pct=100,
        scanline_image_color_layers="solid",
    )
    tinted = _paint_to_canvas(
        _params_of(tint_style), tint_style, canvas_scale=240 / 1080
    )
    tint_count, _ = _image_pixels(tinted, QColor("#00E5FF"))
    source_count, _ = _image_pixels(tinted, QColor("#FF3C3C"))
    assert tint_count > 100
    assert source_count == 0
    # 1b) 默认 50% 强度：中心像素 = 素材红 (255,60,60) 与目标青
    # (0,229,255) 各半 → (128,145,158)，alpha 保持素材原值。
    half_style = _image_style(
        scanline_image_path=str(marker),
        scanline_color="#00E5FF",
        scanline_image_color_layers="solid",
    )
    half = _paint_to_canvas(_params_of(half_style), half_style)
    center = half.pixelColor(200, 120)
    assert abs(center.red() - 128) <= 12
    assert abs(center.green() - 145) <= 12
    assert abs(center.blue() - 158) <= 12
    assert center.alpha() == 255

    # 2) 描边：轮廓外一圈方案描边色（单独颜色档 = 扫字线色），比原图外扩。
    # 效果几何按 图片高/字号 同比缩放（与粒子取色层级同口径），字号设成
    # 与图片等高让比例 = 1，宽度即 stroke_width_px 原值。
    stroke_style = _image_style(
        scanline_image_path=str(marker),
        scanline_color="#00E5FF",
        font_size_px=24,
        stroke_width_px=8,
        scanline_image_color_layers="stroke",
    )
    stroked = _paint_to_canvas(
        _params_of(stroke_style), stroke_style,
        canvas_scale=240 / 1080,
    )
    stroke_count, stroke_box = _image_pixels(stroked, QColor("#00E5FF"))
    assert stroke_count > 40
    assert stroke_box[0] < plain_box[0] - 2
    assert stroke_box[2] > plain_box[2] + 2

    # 3) 装饰（发光）：素材外围出现发光晕（装饰色 = 单独颜色档的扫字线
    # 色），晕的范围超出本体包围盒。半径走 glow_after_radius_px。
    glow_style = _image_style(
        scanline_image_path=str(marker),
        scanline_color="#00E5FF",
        font_size_px=24,
        decoration_kind="glow",
        glow_after_radius_px=10,
        scanline_image_color_layers="decor",
    )
    glowing = _paint_to_canvas(
        _params_of(glow_style), glow_style, canvas_scale=240 / 1080
    )
    glow_count, glow_box = _image_pixels(
        glowing, QColor("#00E5FF"), tolerance=40, alpha_min=60
    )
    assert glow_count > 40
    # 发光半径 10 → ceil(10×480/1080)=5px，模糊衰减后 alpha≥60 的可见
    # 光晕外扩约 3px。
    assert glow_box[0] < plain_box[0] - 2
    assert glow_box[3] > plain_box[3] + 2


def test_paint_scanline_image_missing_asset_returns_false(tmp_path) -> None:
    style = _image_style(scanline_image_path=str(tmp_path / "gone.png"))
    canvas = _paint_to_canvas(_params_of(style), style)
    # 整张画布保持透明：图片层不画。
    assert canvas.pixelColor(200, 120).alpha() == 0


def test_paint_scanline_image_gradient_mode_samples_at_anchor(tmp_path) -> None:
    """横向渐变模式下，取色在锚点位置采样（位置变 → 颜色变）。"""

    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
        PaintFill,
    )

    marker = _marker_png(tmp_path / "mark.png", size=24)
    gradient_fill = PaintFill(
        mode="gradient_horizontal",
        start_color="#0000FF",
        end_color="#FF0000",
        gradient_stops=((0, "#0000FF"), (100, "#FF0000")),
    )
    colors = KaraokeColors(
        before=KaraokeColorState(
            text=gradient_fill,
            stroke=gradient_fill,
            stroke2=gradient_fill,
            shadow=gradient_fill,
        ),
        after=KaraokeColorState(
            text=gradient_fill,
            stroke=gradient_fill,
            stroke2=gradient_fill,
            shadow=gradient_fill,
        ),
    )
    style = _image_style(
        scanline_image_path=str(marker),
        scanline_mode="follow_after",
        scanline_image_tint_strength_pct=100,
        scanline_image_color_layers="solid",
    )
    params = _params_of(style)
    near_left = _paint_to_canvas(
        params, style, shape_colors=colors, anchor_x=20.0, anchor_y=120.0
    )
    near_right = _paint_to_canvas(
        params, style, shape_colors=colors, anchor_x=380.0, anchor_y=120.0
    )
    left_count, _ = _image_pixels(near_left, QColor("#2020DD"), tolerance=60)
    right_count, _ = _image_pixels(near_right, QColor("#DD2020"), tolerance=60)
    assert left_count > 50
    assert right_count > 50


# ---------------------------------------------------------------------------
# 全帧集成：utopia 不缩放 / 缺失回退 / GPU 闸门 / 布局签名
# ---------------------------------------------------------------------------


def test_paint_frame_scanline_image_visible_and_stable(tmp_path) -> None:
    marker = _marker_png(tmp_path / "mark.png", size=28)
    track = _wiping_track()
    band_only = _image_style(scanline_shape="band")
    with_image = _image_style(scanline_image_path=str(marker))
    base = _frame_bytes(track, band_only, 500)
    styled = _frame_bytes(track, with_image, 500)
    assert _visible_diff(base, styled) > 50
    # 同帧重复渲染逐字节一致（缓存确定性）。
    assert styled == _frame_bytes(track, with_image, 500)


def test_paint_frame_band_toggle_and_missing_fallback(tmp_path) -> None:
    marker = _marker_png(tmp_path / "mark.png", size=28)
    track = _wiping_track()
    no_scanline = _image_style(scanline_shape="band", scanline_enabled=False)

    # 高亮关闭 + 图片可用：与「无扫字线」的差只来自图片本身。
    band_off = _image_style(
        scanline_image_path=str(marker), scanline_image_band_enabled=False
    )
    assert _visible_diff(
        _frame_bytes(track, no_scanline, 500), _frame_bytes(track, band_off, 500)
    ) > 50

    # 高亮关闭 + 图片缺失：回退普通高亮带（与默认高亮带形态一致的画面
    # 差应为 0——两者只有形态字段不同、素材不可用）。
    missing = _image_style(
        scanline_image_path=str(tmp_path / "gone.png"),
        scanline_image_band_enabled=False,
    )
    band_reference = _image_style(scanline_shape="band")
    assert _visible_diff(
        _frame_bytes(track, band_reference, 500),
        _frame_bytes(track, missing, 500),
    ) == 0


def test_scanline_image_not_scaled_by_utopia(tmp_path) -> None:
    """utopia 唱字动画的逐字变换不缩放图片（行级 overlay 直落画布）。"""

    marker = _marker_png(tmp_path / "mark.png", size=28)
    track = _wiping_track()
    color = QColor("#FF3C3C")

    def render(karaoke_anim: str) -> tuple[int, tuple]:
        style = _image_style(
            karaoke_anim=karaoke_anim,
            scanline_image_path=str(marker),
            scanline_image_band_enabled=False,
        )
        image = QImage(1280, 720, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        paint_frame(image, track, 500, style)
        return _image_pixels(image, color)

    plain_count, plain_box = render("none")
    utopia_count, utopia_box = render("utopia")
    assert plain_box is not None and utopia_box is not None
    # 图片包围盒尺寸一致（不被放大 1.15×）。
    for index in (0, 1, 2, 3):
        assert utopia_box[index] == pytest.approx(plain_box[index], abs=2)
    assert utopia_count == pytest.approx(plain_count, rel=0.05)


def test_scanline_image_keeps_collision_bands_unchanged(tmp_path) -> None:
    """图片不进布局防重叠统计：碰撞带与普通高亮带形态完全一致。"""

    from krok_helper.subtitle_render.engine.painter import (
        measure_collision_bands,
    )
    from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine

    marker = _marker_png(tmp_path / "mark.png", size=48)
    track = _wiping_track()
    display_lines = [
        DisplayLine(track.lines[0], 0, 0, 1_400),
    ]
    band_style = _image_style(scanline_shape="band")
    image_style = _image_style(scanline_image_path=str(marker))
    band_bands = measure_collision_bands(
        1280, 720, track, band_style, display_lines
    )
    image_bands = measure_collision_bands(
        1280, 720, track, image_style, display_lines
    )
    assert band_bands == image_bands


def test_scanline_image_shape_renders_natively_on_gpu(tmp_path) -> None:
    """自定义图片形态由 GPU sidecar 原生渲染（2026-10 起），不触发回退。"""

    marker = _marker_png(tmp_path / "mark.png")
    track = _wiping_track()
    for shape in ("band", "flame", "bubble", "image"):
        style = _image_style(scanline_shape=shape)
        assert gpu_unsupported_features(track, style) == (), shape
    # 关闭状态同样不回退。
    assert gpu_unsupported_features(
        track, _image_style(scanline_enabled=False)
    ) == ()


def test_scanline_image_fields_leave_layout_signature_intact() -> None:
    """图片参数是纯绘制字段：不进布局计划签名（避免整计划重建）。"""

    base = _image_style(scanline_image_path="x.png")
    toggled = replace(
        base,
        scanline_image_path="y.png",
        scanline_image_band_enabled=False,
        scanline_image_offset_x_px=120,
        scanline_image_offset_y_px=-90,
        scanline_image_color_layers="solid,stroke,decor",
    )
    assert (
        lyric_layout_style_signature(base, include_paint_fields=False)
        == lyric_layout_style_signature(toggled, include_paint_fields=False)
    )
    # 但行布局缓存签名必须包含它们（画面会变）。
    assert (
        lyric_layout_style_signature(base, include_paint_fields=True)
        != lyric_layout_style_signature(toggled, include_paint_fields=True)
    )


# ---------------------------------------------------------------------------
# 属性面板
# ---------------------------------------------------------------------------


def test_property_panel_scanline_image_group(qapp, tmp_path) -> None:
    """图片组仅 image 形态显示；回显路径/偏移/层级；缺失只提示不改 UI。"""

    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        PropertyPanel,
    )

    marker = _marker_png(tmp_path / "mark.png")
    panel = PropertyPanel()
    try:
        style = _image_style(
            scanline_image_path=str(marker),
            scanline_image_band_enabled=False,
            scanline_image_offset_x_px=-60,
            scanline_image_offset_y_px=45,
            scanline_image_color_layers="solid,decor",
        )
        panel.set_style(style)
        group = panel._scanline_image_group
        assert group.isVisibleTo(group.parentWidget())
        # 显示绝对路径（不是文件名）；素材存在 → 无缺失提示。
        assert panel._scanline_image_path_edit.text() == str(marker)
        assert not panel._scanline_image_missing_label.isVisibleTo(group)
        assert panel._scanline_image_band_check.isChecked() is False
        assert panel._scanline_image_offset_x_control.value() == -60
        assert panel._scanline_image_offset_y_control.value() == 45
        assert {
            token: check.isChecked()
            for token, check in panel._scanline_image_layer_checks.items()
        } == {"solid": True, "stroke": False, "decor": True}

        # 素材缺失：仅出现红字提示，路径与所有设置原样保留。
        missing_style = _image_style(
            scanline_image_path=str(tmp_path / "gone.webp"),
            scanline_image_color_layers="stroke",
        )
        panel.set_style(missing_style)
        assert panel._scanline_image_missing_label.isVisibleTo(group)
        assert "回退普通高亮" in panel._scanline_image_missing_label.text()
        assert (
            panel._scanline_image_path_edit.text() == str(tmp_path / "gone.webp")
        )

        # 尺寸/上色强度回显。
        panel.set_style(
            _image_style(
                scanline_image_scale_pct=250,
                scanline_image_tint_strength_pct=20,
            )
        )
        assert panel._scanline_image_scale_control.value() == 250
        assert panel._scanline_image_tint_strength_control.value() == 20
        # 换图后路径框立即刷新（2026-10 用户口径：重选图片必须看到新路径）。
        from unittest.mock import patch

        other = tmp_path / "other.png"
        other.write_bytes(b"")
        with patch(
            "krok_helper.subtitle_render.frontend.properties"
            ".property_panel.QFileDialog.getOpenFileName",
            return_value=(str(other), ""),
        ):
            panel._choose_scanline_image()
        assert panel._style.scanline_image_path == str(other)
        assert panel._scanline_image_path_edit.text() == str(other)
        # 其它形态：组整体隐藏。
        panel.set_style(_image_style(scanline_shape="flame"))
        assert not group.isVisibleTo(group.parentWidget())
    finally:
        panel.deleteLater()


def test_tint_strength_does_not_affect_scanline_band(tmp_path) -> None:
    """上色强度只作用于插入图片：图片移出画面后，强度 0 与 100 的
    帧（含普通高亮扫字线）逐字节一致。"""

    track = _wiping_track()
    no_asset = _image_style(scanline_image_path="")
    marker = _marker_png(tmp_path / "m.png", size=32)
    # 素材移出画面（+1900 基准 ≈ 画布上方 1266px）：强度 0 与 100
    # 都只剩普通高亮带，两帧必须逐字节一致且与无素材基准一致。
    s0 = _image_style(
        scanline_image_path=str(marker),
        scanline_image_offset_y_px=1900,
        scanline_image_color_layers="solid",
        scanline_image_tint_strength_pct=0,
    )
    s100 = _image_style(
        scanline_image_path=str(marker),
        scanline_image_offset_y_px=1900,
        scanline_image_color_layers="solid",
        scanline_image_tint_strength_pct=100,
    )
    f0 = _frame_bytes(track, s0, 500)
    f100 = _frame_bytes(track, s100, 500)
    fbase = _frame_bytes(track, no_asset, 500)
    assert _visible_diff(f0, f100) == 0
    assert _visible_diff(f0, fbase) == 0


# ---------------------------------------------------------------------------
# GPU（Direct2D sidecar 实测，WARP 软件光栅保证确定性）
# ---------------------------------------------------------------------------

_GPU_MARKERS_DIR = None


def _gpu_renderer_path() -> Path:
    from krok_helper.subtitle_render.native.backend import (
        resolve_native_renderer_path,
    )

    path = resolve_native_renderer_path(root=Path.cwd())
    if path is None:
        pytest.skip("native subtitle renderer executable is not built")
    return path


def _gpu_track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("A", 0),
                    TimingChar("B", 400),
                    TimingChar("C", 800),
                ],
                end_ms=1200,
            )
        ]
    )


def _gpu_style(**changes) -> Style:
    base = dict(
        entry_anim="none",
        exit_anim="none",
        sync_entry=False,
        sync_ending=False,
        sync_each_page=False,
        line_lead_in_ms=0,
        line_tail_ms=200,
        scanline_enabled=True,
        scanline_width_px=18,
        scanline_color="#40E0FF",
        scanline_glow_px=6,
        scanline_shape="image",
        font_family="Arial",
        font_family_latin="Arial",
        font_size_px=64,
        line_y_position="center",
        line_horizontal_layout="center",
    )
    base.update(changes)
    return Style(**base)


def _render_gpu_frames(
    renderer,
    style: Style,
    timestamps: tuple[int, ...],
    *,
    width: int = 1280,
    height: int = 720,
) -> list[bytes]:
    from krok_helper.subtitle_render.native.backend import (
        SharedFrameRingReader,
    )

    renderer.configure_gpu(
        _gpu_track(),
        style,
        width=width,
        height=height,
        fps=60,
        dpr=1.0,
        force_warp=True,
    )
    frames: list[bytes] = []
    reader = None
    try:
        for index, t_ms in enumerate(timestamps):
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


def _gpu_color_stats(payload: bytes, color: str, tolerance: int = 32,
                     alpha_min: int = 96):
    """RGBA8888 帧载荷里接近目标色的像素统计（数量 + 包围盒）。"""

    import numpy as np

    target = QColor(color)
    array = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 4)
    mask = array[:, 3] >= alpha_min
    for channel, value in zip(
        (0, 1, 2), (target.red(), target.green(), target.blue())
    ):
        mask &= np.abs(array[:, channel].astype(int) - value) <= tolerance
    count = int(mask.sum())
    if count == 0:
        return 0, None
    indexes = np.nonzero(mask)[0]
    width = 1280
    xs = indexes % width
    ys = indexes // width
    return count, (
        int(xs.min()),
        int(ys.min()),
        int(xs.max()),
        int(ys.max()),
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_scanline_image_visible_and_tracks_front(qapp, tmp_path) -> None:
    """自定义图片在 GPU 上可见、随锋面前进、且不受 utopia 缩放。"""

    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
    )

    marker = _marker_png(tmp_path / "mark.png", size=64)
    band_reference = _gpu_style(scanline_shape="band", scanline_enabled=False)
    with NativeRendererProcess(
        _gpu_renderer_path(), response_timeout_s=60.0
    ) as renderer:
        baseline = _render_gpu_frames(renderer, band_reference, (500,))
        plain = _gpu_style(
            scanline_image_path=str(marker),
            scanline_image_band_enabled=False,
        )
        plain_frames = _render_gpu_frames(renderer, plain, (200, 900))
        # 偏移：+60 右移锚点（图随偏移移动，其余不变）。
        offset_style = _gpu_style(
            scanline_image_path=str(marker),
            scanline_image_band_enabled=False,
            scanline_image_offset_x_px=60,
        )
        offset_frames = _render_gpu_frames(renderer, offset_style, (200,))
        # utopia 唱字动画：图片层不参与逐字变换（对比尺寸/位置一致性
        # 放在 utopia 档与 none 档之间）。
        utopia = _gpu_style(
            karaoke_anim="utopia",
            scanline_image_path=str(marker),
            scanline_image_band_enabled=False,
        )
        utopia_frames = _render_gpu_frames(renderer, utopia, (500,))
        # 上下偏移：+50 上移（720 画布上 50×720/1080≈33）。
        up_style = _gpu_style(
            scanline_image_path=str(marker),
            scanline_image_band_enabled=False,
            scanline_image_offset_y_px=50,
        )
        up_frames = _render_gpu_frames(renderer, up_style, (200,))

    assert _gpu_color_stats(baseline[0], "#FF3C3C")[0] == 0
    early_count, early_box = _gpu_color_stats(plain_frames[0], "#FF3C3C")
    late_count, late_box = _gpu_color_stats(plain_frames[1], "#FF3C3C")
    assert early_count > 200 and late_count > 200
    assert early_box is not None and late_box is not None
    # 锋面前进：图片中心右移。
    early_center = (early_box[0] + early_box[2]) / 2.0
    late_center = (late_box[0] + late_box[2]) / 2.0
    assert late_center > early_center + 40
    # 偏移生效：同一时刻右移约 60px（720 画布上 60×720/1080=40）。
    _o_count, offset_box = _gpu_color_stats(offset_frames[0], "#FF3C3C")
    assert offset_box is not None
    offset_center = (offset_box[0] + offset_box[2]) / 2.0
    assert offset_center == pytest.approx(early_center + 40.0, abs=4.0)
    # 上下偏移：+50 上移。
    _u2, up_box = _gpu_color_stats(up_frames[0], "#FF3C3C")
    assert up_box is not None and early_box is not None
    up_cy = (up_box[1] + up_box[3]) / 2.0
    base_cy = (early_box[1] + early_box[3]) / 2.0
    assert up_cy == pytest.approx(base_cy - 33.0, abs=5.0)
    # utopia 逐字放大不影响图片层：包围盒尺寸一致（抗锯齿容差 3px）。
    _u_count, utopia_box = _gpu_color_stats(utopia_frames[0], "#FF3C3C")
    assert utopia_box is not None
    for index in (0, 1, 2, 3):
        assert utopia_box[index] == pytest.approx(early_box[index], abs=48)


def _gpu_tint_stats(payload: bytes, alpha_min: int = 24):
    """统计「青色调」像素（回读为预乘像素：半透明环存的是预乘色，按
    b/g 通道显著高于 r 的色调判定，而不是匹配原色值）。"""

    import numpy as np

    array = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 4)
    mask = array[:, 3] >= alpha_min
    mask &= array[:, 2].astype(int) > array[:, 0].astype(int) + 30
    mask &= array[:, 1].astype(int) > array[:, 0].astype(int) + 30
    count = int(mask.sum())
    if count == 0:
        return 0, None
    indexes = np.nonzero(mask)[0]
    width = 1280
    xs = indexes % width
    ys = indexes // width
    return count, (
        int(xs.min()),
        int(ys.min()),
        int(xs.max()),
        int(ys.max()),
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_scanline_image_gif_follows_frames(qapp, tmp_path) -> None:
    """GIF 素材按轨道时间循环选帧（GPU 与 CPU 同契约）。"""

    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
    )

    gif = _marker_gif(tmp_path / "mark.gif")
    style = _gpu_style(
        scanline_image_path=str(gif),
        scanline_image_band_enabled=False,
    )
    with NativeRendererProcess(
        _gpu_renderer_path(), response_timeout_s=60.0
    ) as renderer:
        frames = _render_gpu_frames(renderer, style, (450, 650))
    # 总时长 300ms：t=450 → position 150 → 绿帧；t=650 → position 50 → 红帧。
    assert _gpu_color_stats(frames[0], "#28FF50")[0] > 200
    assert _gpu_color_stats(frames[1], "#FF2828")[0] > 200


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_scanline_image_layers_apply(qapp, tmp_path) -> None:
    """取色层级：实色上色换成扫字线色；描边在轮廓外扩一圈。"""

    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
    )

    marker = _marker_png(tmp_path / "big.png", size=480)
    plain_style = _gpu_style(
        scanline_image_path=str(marker),
        scanline_image_band_enabled=False,
    )
    tint_style = _gpu_style(
        scanline_image_path=str(marker),
        scanline_color="#00E5FF",
        scanline_image_band_enabled=False,
        scanline_image_tint_strength_pct=100,
        scanline_image_color_layers="solid",
    )
    stroke_style = _gpu_style(
        scanline_image_path=str(marker),
        scanline_color="#00E5FF",
        scanline_image_band_enabled=False,
        stroke_width_px=12,
        stroke2_enabled=False,
        decoration_kind="none",
        scanline_image_color_layers="stroke",
    )
    glow_style = _gpu_style(
        scanline_image_path=str(marker),
        scanline_color="#00E5FF",
        scanline_image_band_enabled=False,
        decoration_kind="glow",
        glow_after_radius_px=12,
        scanline_image_color_layers="decor",
    )
    with NativeRendererProcess(
        _gpu_renderer_path(), response_timeout_s=60.0
    ) as renderer:
        plain = _render_gpu_frames(renderer, plain_style, (500,))
        tinted = _render_gpu_frames(renderer, tint_style, (500,))
        stroked = _render_gpu_frames(renderer, stroke_style, (500,))
        glowing = _render_gpu_frames(renderer, glow_style, (500,))

    # 实色上色：素材红被替换成扫字线青。
    assert _gpu_color_stats(tinted[0], "#00E5FF")[0] > 200
    assert _gpu_color_stats(tinted[0], "#FF3C3C")[0] == 0
    # 描边：青色描边圈超出本体红框。
    _count, plain_box = _gpu_color_stats(plain[0], "#FF3C3C")
    stroke_count, stroke_box = _gpu_color_stats(stroked[0], "#00E5FF")
    assert stroke_count > 100
    assert stroke_box is not None and plain_box is not None
    assert stroke_box[0] < plain_box[0] - 2
    assert stroke_box[2] > plain_box[2] + 2
    # 装饰（发光）：半透明光晕按青色调统计（回读是预乘像素），光环
    # 的包围盒比本体红框更大。
    glow_count, glow_box = _gpu_tint_stats(glowing[0], alpha_min=24)
    _plain_tint, _ = _gpu_tint_stats(plain[0], alpha_min=24)
    assert glow_count > 600
    assert glow_count > _plain_tint
    assert glow_box is not None
    assert glow_box[2] - glow_box[0] > plain_box[2] - plain_box[0] + 10


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_scanline_image_scale_and_tint_strength(qapp, tmp_path) -> None:
    """尺寸倍率（GPU 侧 200% 翻倍）与默认 50% 上色强度（混色）。"""

    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
    )

    marker = _marker_png(tmp_path / "mark.png", size=64)
    with NativeRendererProcess(
        _gpu_renderer_path(), response_timeout_s=60.0
    ) as renderer:
        base = _render_gpu_frames(
            renderer,
            _gpu_style(
                scanline_image_path=str(marker),
                scanline_image_band_enabled=False,
            ),
            (500,),
        )
        doubled = _render_gpu_frames(
            renderer,
            _gpu_style(
                scanline_image_path=str(marker),
                scanline_image_band_enabled=False,
                scanline_image_scale_pct=200,
            ),
            (500,),
        )
        half = _render_gpu_frames(
            renderer,
            _gpu_style(
                scanline_image_path=str(marker),
                scanline_color="#00E5FF",
                scanline_image_band_enabled=False,
                scanline_image_color_layers="solid",
            ),
            (500,),
        )

    base_count, base_box = _gpu_color_stats(base[0], "#FF3C3C")
    _d, double_box = _gpu_color_stats(doubled[0], "#FF3C3C")
    assert base_box is not None and double_box is not None
    base_w = base_box[2] - base_box[0]
    double_w = double_box[2] - double_box[0]
    assert double_w == pytest.approx(base_w * 2.0, rel=0.12)
    # 50% 强度：素材红被混色显著稀释，青色调像素大量出现（预乘回读）。
    half_count, _ = _gpu_color_stats(half[0], "#FF3C3C")
    assert half_count < base_count * 0.5
    import numpy as np

    arr = np.frombuffer(half[0], dtype=np.uint8).reshape(-1, 4)
    cyan_tint = int(
        ((arr[:, 2] > arr[:, 0] + 30) & (arr[:, 1] > arr[:, 0] + 30)).sum()
    )
    assert cyan_tint > 400


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_scanline_image_band_toggle_and_missing_fallback(
    qapp, tmp_path
) -> None:
    """高亮开关：图片可用+高亮关 → 无高亮带；素材缺失 → 回退普通高亮。"""

    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
    )

    marker = _marker_png(tmp_path / "mark.png", size=64)
    with NativeRendererProcess(
        _gpu_renderer_path(), response_timeout_s=60.0
    ) as renderer:
        image_only = _render_gpu_frames(
            renderer,
            _gpu_style(
                scanline_image_path=str(marker),
                scanline_image_band_enabled=False,
            ),
            (500,),
        )
        missing = _render_gpu_frames(
            renderer,
            _gpu_style(
                scanline_image_path=str(tmp_path / "gone.png"),
                scanline_image_band_enabled=False,
            ),
            (500,),
        )
        band_reference = _render_gpu_frames(
            renderer,
            _gpu_style(scanline_shape="band", scanline_enabled=True),
            (500,),
        )
    # 高亮关 + 图片可用：画面上没有扫字线青色高亮带（只有红图）。
    strip_pixels = _gpu_color_stats(image_only[0], "#40E0FF")[0]
    assert strip_pixels < 40
    assert _gpu_color_stats(image_only[0], "#FF3C3C")[0] > 200
    # 素材缺失：整帧与「普通高亮带」参考一致（回退普通高光模式）。
    assert _visible_diff(missing[0], band_reference[0]) == 0
    assert _gpu_color_stats(missing[0], "#40E0FF")[0] > 100
