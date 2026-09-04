"""Animated GIF guide symbols: decode, anchor, frame pick, decor cache, GPU parity.

动图导唱符（GIF）按「行显示窗口起点」为锚点循环选帧；帧上限 60、帧延时钳
≥10ms、累积延时表线性查找是 Python QPainter 与 D2D sidecar 的共同契约
（见 :mod:`krok_helper.subtitle_render.engine.guide.metrics` 与
``d2d_paint_resources.cpp`` 的 ``loadWicAnimatedBitmaps``）。
"""

from __future__ import annotations

import os
import shutil
import time
from collections import Counter
from pathlib import Path

import pytest

from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.domain.timing import GuideSymbol  # noqa: E402
from krok_helper.subtitle_render.engine.guide.metrics import (  # noqa: E402
    GUIDE_ANIM_MAX_FRAMES,
    animated_bitmap_guide,
    bitmap_guide_frame_at,
    bitmap_guide_image,
)
import krok_helper.subtitle_render.engine.render.elements.horizontal.layers as horizontal_layers  # noqa: E402
from krok_helper.subtitle_render.domain.paint import (  # noqa: E402
    KaraokeColorState,
    KaraokeColors,
    PaintFill,
)
from krok_helper.subtitle_render.engine.guide.semantics import (  # noqa: E402
    render_line_with_guide_symbols,
)
from krok_helper.subtitle_render.engine.painter import paint_frame  # noqa: E402
from krok_helper.subtitle_render.native.protocol import (  # noqa: E402
    bitmap_guide_to_ir,
    timing_line_to_ir,
)

_GIF_3 = Path(__file__).parent / "data" / "guide_anim_3frames.gif"
_GIF_70 = Path(__file__).parent / "data" / "guide_anim_70frames.gif"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _animated_symbol(path: Path, **overrides) -> GuideSymbol:
    kwargs = dict(
        name="anim",
        kind="bitmap",
        bitmap_before_path=str(path),
        bitmap_after_path=str(path),
        duration_ms=400,
        count=1,
    )
    kwargs.update(overrides)
    return GuideSymbol(**kwargs)


def _animated_track(path: Path) -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(" ", 1000), TimingChar("GPU", 1100)],
                end_ms=1600,
                inline_guide_symbols={0: _animated_symbol(path)},
            )
        ]
    )


def _saturated_buckets(image: QImage) -> Counter:
    """Count saturated colour buckets (R/G/B dominance) in a rendered frame."""
    counts: Counter = Counter()
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            color = image.pixelColor(x, y)
            red, green, blue = color.red(), color.green(), color.blue()
            if color.alpha() < 30:
                continue
            if max(red, green, blue) - min(red, green, blue) < 80:
                continue
            bucket = (
                "R" if red > 150 else "-",
                "G" if green > 150 else "-",
                "B" if blue > 150 else "-",
            )
            counts[bucket] += 1
    return counts


def _dominant_avatar_color(image: QImage) -> str:
    """The avatar's dominant saturated colour ('R' / 'G' / 'B')."""
    buckets = _saturated_buckets(image)
    ranked = [item for item in buckets.most_common() if set(item[0]) != {"-"}]
    assert ranked, "no saturated avatar colour found in frame"
    return "".join(ranked[0][0]).replace("-", "")


# ---------------------------------------------------------------------------
# 解码层：合成帧 / 延时 / 限帧 / 选帧规则
# ---------------------------------------------------------------------------


def test_animated_guide_decodes_composited_frames_and_delays(qapp):
    animated = animated_bitmap_guide(str(_GIF_3))
    assert animated is not None
    assert len(animated.frames) == 3
    assert animated.delays_ms == (100, 100, 100)
    assert animated.total_ms == 300
    assert animated.frames[0].pixelColor(0, 0).name() == "#ff0000"
    assert animated.frames[1].pixelColor(0, 0).name() == "#00ff00"
    assert animated.frames[2].pixelColor(0, 0).name() == "#0000ff"


def test_animated_guide_caps_frames_at_shared_limit(qapp):
    animated = animated_bitmap_guide(str(_GIF_70))
    assert animated is not None
    assert len(animated.frames) == GUIDE_ANIM_MAX_FRAMES == 60
    assert all(delay == 20 for delay in animated.delays_ms)
    assert animated.total_ms == 1200


def test_guide_decor_cache_invalidates_when_gif_content_changes(qapp, monkeypatch, tmp_path):
    """装饰剪影缓存必须带文件签名：GIF 换内容后旧剪影不能复用。"""
    gif = tmp_path / "replaceable.gif"
    shutil.copy(_GIF_3, gif)
    track = _animated_track(gif)
    style = _avatar_only_style(
        decoration_kind="shadow",
        shadow_offset_x=3,
        shadow_offset_y=3,
    )

    calls = {"count": 0}
    original = horizontal_layers._tinted_guide_silhouette

    def counting(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(horizontal_layers, "_tinted_guide_silhouette", counting)
    horizontal_layers._GUIDE_SILHOUETTE_CACHE.clear()

    image = QImage(320, 180, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    paint_frame(image, track, 1150, style)
    baseline = calls["count"]
    assert baseline > 0

    # 同内容再画一次：命中缓存，不重算。
    image.fill(0)
    paint_frame(image, track, 1150, style)
    assert calls["count"] == baseline

    # 磁盘上替换 GIF 内容（mtime/size 变化）后签名失效：剪影必须重算。
    time.sleep(0.01)
    shutil.copy(_GIF_70, gif)
    os.utime(gif, None)
    image.fill(0)
    paint_frame(image, track, 1150, style)
    assert calls["count"] > baseline


def test_animated_guide_frame_selection_loops_from_zero(qapp):
    animated = animated_bitmap_guide(str(_GIF_3))
    assert animated is not None
    expectations = {
        0: 0,
        99: 0,
        100: 1,
        250: 2,
        299: 2,
        300: 0,  # 循环回到第 0 帧
        450: 1,
        -5: 0,  # 锚点前钳到 0
    }
    for anim_ms, expected in expectations.items():
        assert animated.frame_at(anim_ms) == expected, anim_ms


def test_bitmap_guide_frame_at_static_image_keeps_first_frame(qapp, tmp_path):
    static = tmp_path / "static.png"
    image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0xFF20DD20)
    image.save(str(static), "PNG")
    for anim_ms in (None, 0, 12345):
        frame = bitmap_guide_frame_at(str(static), anim_ms)
        assert frame is not None
        assert frame.index == 0
        assert not frame.image.isNull()


def test_bitmap_guide_frame_at_missing_file_returns_none(qapp, tmp_path):
    assert bitmap_guide_frame_at(str(tmp_path / "missing.gif"), 100) is None


def test_static_png_still_uses_plain_bitmap_cache(qapp, tmp_path):
    static = tmp_path / "plain.png"
    image = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0xFF102030)
    image.save(str(static), "PNG")
    assert animated_bitmap_guide(str(static)) is None
    assert bitmap_guide_image(str(static)) is not None


# ---------------------------------------------------------------------------
# 锚点：IR 字段 + 行级回退口径
# ---------------------------------------------------------------------------


def test_bitmap_guide_ir_carries_anim_anchor():
    ir = bitmap_guide_to_ir(_animated_symbol(_GIF_3), anim_anchor_ms=1234)
    assert ir is not None
    assert ir["anim_anchor_ms"] == 1234
    legacy = bitmap_guide_to_ir(_animated_symbol(_GIF_3))
    assert legacy is not None
    assert legacy["anim_anchor_ms"] == 0


def test_timing_line_ir_anchor_uses_display_window_start():
    track = _animated_track(_GIF_3)
    render_line = render_line_with_guide_symbols(track.lines[0])
    ir = timing_line_to_ir(
        render_line,
        display_start_ms=770,
        display_end_ms=1900,
    )
    guide = ir["chars"][0]["bitmap_guide"]
    assert guide is not None
    assert guide["anim_anchor_ms"] == 770


def test_timing_line_ir_anchor_falls_back_to_line_start():
    track = _animated_track(_GIF_3)
    render_line = render_line_with_guide_symbols(track.lines[0])
    ir = timing_line_to_ir(render_line)
    guide = ir["chars"][0]["bitmap_guide"]
    assert guide is not None
    # 回退口径 = 行起点（导唱符占位字符本身最早）。
    assert guide["anim_anchor_ms"] == 1000


# ---------------------------------------------------------------------------
# CPU painter：锚点选帧 + 装饰剪影按帧缓存
# ---------------------------------------------------------------------------


def _avatar_only_style(**overrides) -> Style:
    """Style whose text stays non-saturated so colour buckets isolate the avatar."""
    state = KaraokeColorState(
        text=PaintFill(mode="solid", color="#FFF2F2F2"),
        stroke=PaintFill(mode="solid", color="#00000000"),
        shadow=PaintFill(mode="solid", color="#00000000"),
    )
    kwargs = dict(
        font_size_px=48,
        line_lead_in_ms=0,
        line_tail_ms=0,
        karaoke_colors=KaraokeColors(before=state, after=state),
    )
    kwargs.update(overrides)
    return Style(**kwargs)


def _render_painter_frame(track: TimingTrack, t_ms: int) -> QImage:
    image = QImage(320, 180, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    paint_frame(image, track, t_ms, _avatar_only_style())
    return image


def test_painter_advances_animated_guide_from_display_anchor(qapp):
    track = _animated_track(_GIF_3)
    # 显示窗口起点由布局计划决定（该 fixture 下为 700ms）：
    #   t=1050 → 过 350ms → 第 0 帧红；1150 → 450 → 第 1 帧绿；1290 → 590 → 第 2 帧蓝。
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir

    ir = build_render_ir(track, _avatar_only_style(), width=320, height=180, fps=60)
    anchor = ir["track"]["lines"][0]["display_start_ms"]
    assert anchor == 700
    expectations = {1050: "R", 1150: "G", 1290: "B"}
    for t_ms, expected in expectations.items():
        image = _render_painter_frame(track, t_ms)
        assert _dominant_avatar_color(image) == expected, (t_ms, expected)


def test_painter_animates_before_only_guide(qapp):
    # 仅「走字前」一侧挂动图（分色占位符之外的常见形态）：帧选择同锚点。
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(" ", 1000), TimingChar("GPU", 1100)],
                end_ms=1600,
                inline_guide_symbols={
                    0: GuideSymbol(
                        name="before-only",
                        kind="bitmap",
                        bitmap_before_path=str(_GIF_3),
                        duration_ms=400,
                        count=1,
                    )
                },
            )
        ]
    )
    assert _dominant_avatar_color(_render_painter_frame(track, 1150)) == "G"
    assert _dominant_avatar_color(_render_painter_frame(track, 1290)) == "B"


def test_guide_decor_silhouette_is_cached_per_frame(qapp, monkeypatch):
    calls = {"count": 0}
    original = horizontal_layers._tinted_guide_silhouette

    def counting(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(horizontal_layers, "_tinted_guide_silhouette", counting)
    horizontal_layers._GUIDE_SILHOUETTE_CACHE.clear()
    track = _animated_track(_GIF_3)
    style = Style(
        font_size_px=48,
        line_lead_in_ms=0,
        line_tail_ms=0,
        decoration_kind="shadow",
        shadow_offset_x=3,
        shadow_offset_y=3,
    )
    # 同一时刻渲染两次：第二次的剪影必须来自缓存（计数值不增）。
    for _ in range(2):
        image = QImage(320, 180, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        paint_frame(image, track, 1150, style)
    first = calls["count"]
    assert first > 0
    image = QImage(320, 180, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    paint_frame(image, track, 1150, style)
    assert calls["count"] == first
    # 换一帧（t=1290 → 蓝帧）后需要重算一次新剪影。
    paint_frame(image, track, 1290, style)
    assert calls["count"] > first


# ---------------------------------------------------------------------------
# GPU parity：D2D sidecar 与 painter 同锚点同帧
# ---------------------------------------------------------------------------

gpu = pytest.importorskip("krok_helper.subtitle_render.native.backend")


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_animated_guide_matches_painter_frame_sequence(qapp):
    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
        SharedFrameRingReader,
        resolve_native_renderer_path,
    )

    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    track = _animated_track(_GIF_3)
    style = _avatar_only_style()
    expectations = {1050: "R", 1150: "G", 1290: "B"}
    painter_frames = {
        t_ms: _dominant_avatar_color(_render_painter_frame(track, t_ms))
        for t_ms in expectations
    }

    with NativeRendererProcess(str(renderer_path), response_timeout_s=30.0) as renderer:
        configured = renderer.configure_gpu(
            track, style, width=320, height=180, fps=60, force_warp=True
        )
        assert configured.get("ok")
        reader = None
        for frame_index, t_ms in enumerate(expectations):
            event = renderer.render_gpu_frame(t_ms, force_warp=True, frame_index=frame_index)
            if reader is None:
                reader = SharedFrameRingReader.from_event(event)
                reader.attach()
            gpu_image = reader.read_qimage(event)
            assert (
                _dominant_avatar_color(gpu_image) == expectations[t_ms] == painter_frames[t_ms]
            ), (t_ms, expectations[t_ms], painter_frames[t_ms])


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_caps_animated_guide_frames_at_shared_limit(qapp):
    """GPU 侧限帧契约：70 帧截前 60 帧（循环 1200ms），与 painter 同帧。

    t = 锚点 + 1250ms 时：限帧口径下 1250 % 1200 = 50 → 第 2 帧蓝；
    若未限帧（1400ms 循环）则是第 62 帧橙。用颜色区分两种口径。
    """
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir
    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess,
        SharedFrameRingReader,
        resolve_native_renderer_path,
    )

    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native subtitle renderer executable is not built")

    # 时间轴后移：显示窗口要容得下锚点 + 1250ms（首个循环回绕点之后）。
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(" ", 3000), TimingChar("GPU", 3100)],
                end_ms=4000,
                inline_guide_symbols={0: _animated_symbol(_GIF_70)},
            )
        ]
    )
    style = _avatar_only_style()
    ir = build_render_ir(track, style, width=320, height=180, fps=60)
    anchor = ir["track"]["lines"][0]["display_start_ms"]
    t_ms = anchor + 1250
    assert anchor + 1200 < ir["track"]["lines"][0]["display_end_ms"]

    painter_color = _dominant_avatar_color(_render_painter_frame(track, t_ms))
    assert painter_color == "B", painter_color

    with NativeRendererProcess(str(renderer_path), response_timeout_s=30.0) as renderer:
        configured = renderer.configure_gpu(
            track, style, width=320, height=180, fps=60, force_warp=True
        )
        assert configured.get("ok")
        event = renderer.render_gpu_frame(t_ms, force_warp=True, frame_index=0)
        with SharedFrameRingReader.from_event(event) as reader:
            reader.attach()
            gpu_image = reader.read_qimage(event)
        assert _dominant_avatar_color(gpu_image) == "B" == painter_color
