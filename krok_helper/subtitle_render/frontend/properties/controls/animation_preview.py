"""Animated hover preview popover for subtitle entry/exit animation items.

Hovering an item inside :class:`GroupedComboBoxMenu` opens a floating,
input-transparent, theme-aware card next to the dropdown list that loops a
live QPainter preview of the hovered animation using the sample lyric
``渇いた 心で駆け抜ける``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Optional

from PyQt6 import sip
from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QTimer,
    Qt,
)
from PyQt6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
    QTransform,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from qfluentwidgets.components.widgets.menu import MenuAnimationType

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.paint import KaraokeColors, KaraokeColorState
from krok_helper.subtitle_render.engine.render.effects.fills import (
    brush_for_fill,
    fill_brush_rect,
)
from krok_helper.subtitle_render.engine.render.effects.metrics import (
    decoration_kind_for_state,
    shadow_offset_x,
    shadow_offset_y,
)
from krok_helper.subtitle_render.engine.render.effects.particles import FX_SPRITES
from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
    ZOOM_PULSE_SHRINK_MS,
    fx_unit_hash,
    spin_flip_skew,
    zoom_pulse_wipe_scale,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    WheelFocusedComboBox,
)
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed


ANIMATION_PREVIEW_LYRIC = "渇いた 心で駆け抜ける"


@dataclass(frozen=True)
class SampleSingChar:
    """One timed character in the shared singing preview lyric (from God knows... line 1).

    All timestamps are normalized relative to the first character's start (0 ms)
    so every singing-effect hover preview (zoom pulse, karaoke wipe, scanline,
    sing particles, stroke flash, etc.) shares the exact same musical rhythm.
    """

    char: str
    checkpoints_ms: tuple[int, ...] = ()
    end_ms: Optional[int] = None
    ruby: str = ""

    @property
    def start_ms(self) -> Optional[int]:
        return self.checkpoints_ms[0] if self.checkpoints_ms else None

    @property
    def is_sung(self) -> bool:
        return bool(self.checkpoints_ms) and self.end_ms is not None and self.end_ms > self.checkpoints_ms[0]


# Derived directly from the user's timed SUG lyric (offset by -24074 ms):
# 「渇(かわ)いた 心(こころ)で駆(か)け抜(ぬ)ける」— total singing span 0..5561 ms.
ANIMATION_PREVIEW_SING_CHARS: tuple[SampleSingChar, ...] = (
    SampleSingChar("渇", (0, 468), 710, "かわ"),
    SampleSingChar("い", (710,), 917),
    SampleSingChar("た", (917,), 1677),
    SampleSingChar(" ", (), None),
    SampleSingChar("心", (2607, 2839, 3067), 3273, "こころ"),
    SampleSingChar("で", (3273,), 3507),
    SampleSingChar("駆", (3507,), 3730, "か"),
    SampleSingChar("け", (3730,), 4239),
    SampleSingChar("抜", (4239,), 4484, "ぬ"),
    SampleSingChar("け", (4484,), 4727),
    SampleSingChar("る", (4727,), 5561),
)
ANIMATION_PREVIEW_SING_DURATION_MS = 5561


def sample_sing_wipe_progress(
    t_ms: float,
    spec: SampleSingChar,
    *,
    lead_ms: int = 0,
) -> float:
    """Piecewise karaoke wipe progress in ``[0, 1]`` honoring per-mora checkpoints."""
    if not spec.is_sung or spec.end_ms is None:
        return 0.0
    local_t = float(t_ms) - float(lead_ms)
    starts = spec.checkpoints_ms
    if local_t <= starts[0]:
        return 0.0
    if local_t >= spec.end_ms:
        return 1.0
    seg_count = len(starts)
    for i, seg_start in enumerate(starts):
        seg_end = starts[i + 1] if i + 1 < seg_count else spec.end_ms
        if local_t <= seg_end or i + 1 == seg_count:
            span = max(float(seg_end - seg_start), 1.0)
            seg_frac = _clamp01((local_t - seg_start) / span)
            return _clamp01((i + seg_frac) / float(seg_count))
    return 1.0


def sample_ruby_kana_progress(
    t_ms: float,
    spec: SampleSingChar,
    kana_index: int,
    kana_count: int,
    *,
    lead_ms: int = 0,
    reverse: bool = False,
    no_wipe: bool = False,
) -> float:
    """Per-kana karaoke wipe progress in ``[0, 1]`` for a kanji's ruby reading.

    When ``spec.checkpoints_ms`` matches ``kana_count`` (e.g. ``渇(かわ)`` has 2
    checkpoints and ``心(こころ)`` has 3), each ruby kana uses its exact timed
    interval from ``temp.sug``.  When ``reverse`` is True (or counts differ),
    progress flows cleanly across the kanji's active window.
    """
    if not spec.is_sung or spec.end_ms is None or kana_count <= 0:
        return 0.0
    local_t = float(t_ms) - float(lead_ms)
    starts = spec.checkpoints_ms
    order_idx = (kana_count - 1 - kana_index) if reverse else kana_index
    if len(starts) == kana_count and not reverse:
        k_start = float(starts[order_idx])
        k_end = float(starts[order_idx + 1] if order_idx + 1 < kana_count else spec.end_ms)
    else:
        total_start = float(starts[0])
        total_span = max(float(spec.end_ms) - total_start, 1.0)
        k_start = total_start + total_span * (order_idx / float(kana_count))
        k_end = total_start + total_span * ((order_idx + 1) / float(kana_count))
    if no_wipe:
        return 1.0 if local_t >= k_start else 0.0
    if local_t <= k_start:
        return 0.0
    if local_t >= k_end:
        return 1.0
    return _clamp01((local_t - k_start) / max(k_end - k_start, 1.0))


@dataclass(frozen=True)
class PreviewRubyLayout:
    """Precomputed vector geometry for one character's furigana ruby reading."""

    text: str
    font_px: float
    ascent: float
    descent: float
    baseline_y: float
    kana_xs: tuple[float, ...]
    kana_widths: tuple[float, ...]
    kana_paths: tuple[QPainterPath, ...]
    combined_path: QPainterPath
    total_width: float


def build_preview_ruby_layouts(
    main_font_px: float,
    main_ascent: float,
    char_widths: list[float],
    sing_chars: tuple[SampleSingChar, ...] = ANIMATION_PREVIEW_SING_CHARS,
) -> list[Optional[PreviewRubyLayout]]:
    """Build horizontally centered ruby layouts above each kanji in the sample lyric."""
    ruby_px = max(9.0, round(main_font_px * 0.44))
    ruby_font = QFont("Microsoft YaHei UI")
    ruby_font.setPixelSize(int(ruby_px))
    ruby_font.setWeight(QFont.Weight.Bold)
    rfm = QFontMetricsF(ruby_font)
    r_ascent = rfm.ascent()
    r_descent = rfm.descent()
    # Position ruby baseline snugly above main text cap height.
    gap = max(2.2, main_font_px * 0.10)
    ruby_baseline_y = -(main_ascent * 0.88 + r_descent + gap)
    tracking = 0.4

    layouts: list[Optional[PreviewRubyLayout]] = []
    for idx, spec in enumerate(sing_chars):
        ruby = spec.ruby
        if not ruby or idx >= len(char_widths):
            layouts.append(None)
            continue
        k_widths = [max(rfm.horizontalAdvance(ch), ruby_px * 0.6) for ch in ruby]
        total_w = sum(k_widths) + tracking * max(len(ruby) - 1, 0)
        start_x = (char_widths[idx] - total_w) * 0.5
        k_xs: list[float] = []
        k_paths: list[QPainterPath] = []
        combined = QPainterPath()
        cx = start_x
        for ch, kw in zip(ruby, k_widths):
            kp = QPainterPath()
            kp.addText(0.0, 0.0, ruby_font, ch)
            k_xs.append(cx)
            k_paths.append(kp)
            combined.addText(cx, ruby_baseline_y, ruby_font, ch)
            cx += kw + tracking
        layouts.append(
            PreviewRubyLayout(
                text=ruby,
                font_px=ruby_px,
                ascent=r_ascent,
                descent=r_descent,
                baseline_y=ruby_baseline_y,
                kana_xs=tuple(k_xs),
                kana_widths=tuple(k_widths),
                kana_paths=tuple(k_paths),
                combined_path=combined,
                total_width=total_w,
            )
        )
    return layouts


@dataclass(frozen=True)
class AnimationPreviewMeta:
    """Display metadata for one entry or exit animation preview."""

    title: str
    summary: str
    cycle_ms: int = 2300


ENTRY_ANIMATION_META: dict[str, AnimationPreviewMeta] = {
    "none": AnimationPreviewMeta("无", "直接上屏显示，不播放入场过渡动画。", 1800),
    "fade": AnimationPreviewMeta("淡入", "整行歌词由透明平滑淡入显示。", 2200),
    "slide_in": AnimationPreviewMeta("滑入", "整行歌词从侧方平滑滑入到位并渐显。", 2200),
    "rise": AnimationPreviewMeta("上移", "整行歌词从下方柔和上浮到位并渐显。", 2200),
    "char_fade": AnimationPreviewMeta("逐文字渐显", "歌词按从左到右的顺序逐字错峰淡入。", 2300),
    "char_drip": AnimationPreviewMeta("文字垂下", "歌词逐字以角点为轴纵向斜切垂落展开（N3 风格）。", 2400),
    "spin_flip": AnimationPreviewMeta("旋转翻转", "歌词逐字围绕字心缩放并伴随倾斜翻转出现。", 2400),
    "utopia": AnimationPreviewMeta("utopia", "逐字放大弹出至 1.3× 后轻弹回落至原大小。", 2400),
    "noise": AnimationPreviewMeta("噪点溶解", "以随机像素块噪点的方式逐步浮现成形。", 2400),
    "tracking_in": AnimationPreviewMeta("字距收拢", "逐字从宽字距由两侧向中心收拢归位。", 2300),
    "wave_in": AnimationPreviewMeta("波浪上浮", "逐字依次自下方呈波浪节奏浮起到位。", 2300),
    "stretch_in": AnimationPreviewMeta("逐字拉伸入场", "字符由横向拉伸光条逐字收束成清晰字形。", 2400),
    "glow_in": AnimationPreviewMeta("辉光浮现", "整行同步横向回声光痕向中心收拢并熄灭。", 2300),
    "assemble_in": AnimationPreviewMeta("粒子拼接", "像素方块粒子从四周飞入汇聚拼出字形。", 2500),
    "sparkle": AnimationPreviewMeta("星光闪烁", "逐字渐显同时伴随四角星光闪耀飘散。", 2500),
    "ripple": AnimationPreviewMeta("涟漪光环", "逐字渐显同时向外荡开多层细环水波纹。", 2500),
    "note": AnimationPreviewMeta("音符飘出", "逐字渐显同时有音符轻盈摇曳上浮。", 2500),
    "heart": AnimationPreviewMeta("爱心飘出", "逐字渐显同时有红粉爱心向上飘起。", 2500),
    "flame": AnimationPreviewMeta("火焰燃起", "火焰先行燃起，后段文字由火光中收束显现。", 2600),
    "feather": AnimationPreviewMeta("羽毛飘入", "逐字渐显同时白色羽毛随风斜飘滑落。", 2700),
    "lightning": AnimationPreviewMeta("闪电劈落", "逐字渐显同时电光闪电闪现并迸出火花。", 2400),
    "petal": AnimationPreviewMeta("花瓣飘入", "逐字渐显同时樱花花瓣迎风旋落飘过。", 2700),
    "snow": AnimationPreviewMeta("雪花飘入·镂空", "逐字渐显同时镂空雪花轻柔斜飘降落。", 2700),
    "snow_solid": AnimationPreviewMeta("雪花飘入·实心", "逐字渐显同时实心雪花轻柔斜飘降落。", 2700),
    "random_zoom": AnimationPreviewMeta("随机缩放", "按随机顺序逐字从底边中心弹出放大至原尺寸。", 2400),
}

EXIT_ANIMATION_META: dict[str, AnimationPreviewMeta] = {
    "none": AnimationPreviewMeta("无", "直接消失清屏，不播放退场过渡动画。", 1800),
    "fade": AnimationPreviewMeta("淡出", "整行歌词平滑降低透明度淡出消失。", 2200),
    "slide_out": AnimationPreviewMeta("滑出", "整行歌词向侧方平滑滑出并渐隐。", 2200),
    "rise": AnimationPreviewMeta("上移", "整行歌词向上柔和飘移并渐隐消失。", 2200),
    "char_fade": AnimationPreviewMeta("逐文字渐隐", "歌词按从左到右的顺序逐字错峰淡出。", 2300),
    "char_drip": AnimationPreviewMeta("文字垂出", "歌词逐字以角点为轴纵向斜切收起消失（N3 风格）。", 2400),
    "spin_flip": AnimationPreviewMeta("旋转翻转", "歌词逐字围绕字心倾斜翻转并缩小消失。", 2400),
    "utopia": AnimationPreviewMeta("utopia", "逐字缩小并水平翻转旋转，沿弧线飘离淡出。", 2500),
    "noise": AnimationPreviewMeta("噪点溶解", "以随机像素块噪点的方式逐步瓦解消散。", 2400),
    "scatter_out": AnimationPreviewMeta("碎散爆开", "逐字向随机方向旋转飞散并缩小淡出。", 2400),
    "converge_out": AnimationPreviewMeta("收拢消散", "逐字向整行中心聚拢微缩并淡出。", 2300),
    "stretch_out": AnimationPreviewMeta("逐字拉伸退场", "清晰字形逐字横向拉伸为光条弥散淡出。", 2400),
    "glow_out": AnimationPreviewMeta("辉光消散", "整行同步横向回声光痕外扩变疏并淡出。", 2300),
    "dissolve_out": AnimationPreviewMeta("粒子消散", "字形先行隐去，化作像素方块向四周飞散。", 2500),
    "sparkle": AnimationPreviewMeta("星光闪烁", "逐字淡出同时伴随四角星光闪耀消散。", 2500),
    "ripple": AnimationPreviewMeta("涟漪光环", "逐字淡出同时向外荡开多层细环水波纹。", 2500),
    "note": AnimationPreviewMeta("音符飘出", "逐字淡出同时有音符轻盈摇曳飘离。", 2500),
    "heart": AnimationPreviewMeta("爱心飘出", "逐字淡出同时有红粉爱心向上飘散。", 2500),
    "flame": AnimationPreviewMeta("火焰烧尽", "火焰燃起包裹字形，后段文字横向拉伸化光消散。", 2600),
    "feather": AnimationPreviewMeta("羽毛飘散", "逐字淡出同时白色羽毛随风斜飘散去。", 2700),
    "lightning": AnimationPreviewMeta("闪电消散", "逐字淡出同时电光闪电闪现劈散字形。", 2400),
    "petal": AnimationPreviewMeta("花瓣飘散", "逐字淡出同时樱花花瓣迎风飘散。", 2700),
    "snow": AnimationPreviewMeta("雪花飘出·镂空", "逐字淡出同时镂空雪花轻柔斜飘散去。", 2700),
    "snow_solid": AnimationPreviewMeta("雪花飘出·实心", "逐字淡出同时实心雪花轻柔斜飘散去。", 2700),
    "random_zoom": AnimationPreviewMeta("随机缩放", "按随机顺序逐字向底边中心缩回消失。", 2400),
}


_SPRITE_PATH_CACHE: dict[str, QPainterPath] = {}


def _sprite_painter_path(sprite_name: str) -> QPainterPath:
    """Convert one ``FX_SPRITES`` contour (1000-unit em box) to a unit QPainterPath."""
    cached = _SPRITE_PATH_CACHE.get(sprite_name)
    if cached is not None:
        return cached
    spec = FX_SPRITES.get(sprite_name)
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    if spec is not None:
        scale = 1.0 / float(spec.get("units_per_em") or 1000.0)
        for cmd in spec.get("path_commands", ()):
            op = cmd[0]
            if op == "M":
                path.moveTo(float(cmd[1]) * scale, float(cmd[2]) * scale)
            elif op == "L":
                path.lineTo(float(cmd[1]) * scale, float(cmd[2]) * scale)
            elif op == "Q":
                path.quadTo(
                    float(cmd[1]) * scale,
                    float(cmd[2]) * scale,
                    float(cmd[3]) * scale,
                    float(cmd[4]) * scale,
                )
            elif op == "C":
                path.cubicTo(
                    float(cmd[1]) * scale,
                    float(cmd[2]) * scale,
                    float(cmd[3]) * scale,
                    float(cmd[4]) * scale,
                    float(cmd[5]) * scale,
                    float(cmd[6]) * scale,
                )
            elif op == "Z":
                path.closeSubpath()
    _SPRITE_PATH_CACHE[sprite_name] = path
    return path


def draw_preview_sprite(
    painter: QPainter,
    sprite_name: str,
    x: float,
    y: float,
    size: float,
    *,
    color: QColor,
    alpha: float,
    angle_deg: float = 0.0,
    scale_x: float = 1.0,
) -> None:
    """Paint one vector ``FX_SPRITES`` silhouette at ``(x, y)``."""
    if alpha <= 0.01 or size <= 0.4:
        return
    path = _sprite_painter_path(sprite_name)
    if path.isEmpty():
        return
    painter.save()
    painter.setOpacity(_clamp01(alpha))
    painter.translate(x, y)
    if angle_deg:
        painter.rotate(angle_deg)
    painter.scale(size * scale_x, size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawPath(path)
    painter.restore()


def animation_preview_meta(
    direction: str,
    anim_key: str,
    fallback_title: str = "",
) -> AnimationPreviewMeta:
    table = EXIT_ANIMATION_META if direction == "exit" else ENTRY_ANIMATION_META
    meta = table.get(anim_key)
    if meta is not None:
        return meta
    title = fallback_title or anim_key
    summary = (
        "退场动画效果演示。"
        if direction == "exit"
        else "入场动画效果演示。"
    )
    return AnimationPreviewMeta(title=title, summary=summary)


def _clamp01(value: float) -> float:
    return 0.0 if value <= 0.0 else (1.0 if value >= 1.0 else value)


def _smoothstep(value: float) -> float:
    t = _clamp01(value)
    return t * t * (3.0 - 2.0 * t)


def _ui_font(point_size: float, *, bold: bool = False) -> QFont:
    font = QFont("Microsoft YaHei UI")
    font.setPointSizeF(point_size)
    if bold:
        font.setWeight(QFont.Weight.DemiBold)
    return font


def _lyric_font() -> QFont:
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(24)
    font.setWeight(QFont.Weight.Bold)
    return font


def _stagger_progress(progress: float, index: int, count: int) -> float:
    """Mirror the 7:5 stagger-to-travel schedule used by transitions.py."""
    if count <= 1:
        return _clamp01(progress)
    stagger_share = 7.0 / 12.0
    travel_share = 5.0 / 12.0
    start = stagger_share * (index / float(count - 1))
    return _clamp01((progress - start) / travel_share)


def _random_rank(index: int, count: int, exit_phase: bool) -> int:
    salt = (31 if exit_phase else 17) + max(count, 1) * 13
    my_key = fx_unit_hash(index, salt)
    rank = 0
    for j in range(count):
        if j == index:
            continue
        other_key = fx_unit_hash(j, salt)
        if other_key < my_key or (other_key == my_key and j < index):
            rank += 1
    return rank


class AnimationPreviewCanvas(QWidget):
    """Live QPainter mini-stage demonstrating one entry or exit animation."""

    _CANVAS_W = 368
    _CANVAS_H = 122
    _FRAME_INTERVAL_MS = 28

    def __init__(
        self,
        direction: str = "entry",
        anim_key: str = "fade",
        parent: Optional[QWidget] = None,
        *,
        on_status_change: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self._direction = "exit" if direction == "exit" else "entry"
        self._anim_key = anim_key
        self._meta = animation_preview_meta(self._direction, self._anim_key)
        self._phase = 0.0
        self._on_status_change = on_status_change
        self._last_status: tuple[str, bool] = ("", False)
        self._timer = QTimer(self)
        self._timer.setInterval(self._FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)
        self.setFixedSize(self._CANVAS_W, self._CANVAS_H)

        # Precompute character layout metrics for the fixed sample lyric.
        self._font = _lyric_font()
        fm = QFontMetricsF(self._font)
        self._char_height = fm.height()
        self._ascent = fm.ascent()
        self._chars = list(ANIMATION_PREVIEW_LYRIC)
        self._char_widths = [max(fm.horizontalAdvance(ch), 7.0) for ch in self._chars]
        self._line_width = sum(self._char_widths)
        # Prebuild vector glyph paths centered around each glyph origin for crisp transforms.
        self._glyph_paths: list[QPainterPath] = []
        for ch in self._chars:
            gp = QPainterPath()
            if ch.strip():
                gp.addText(0.0, 0.0, self._font, ch)
            self._glyph_paths.append(gp)
        self._ruby_layouts = build_preview_ruby_layouts(24.0, self._ascent, self._char_widths)

    @property
    def direction(self) -> str:
        return self._direction

    @property
    def anim_key(self) -> str:
        return self._anim_key

    @property
    def phase(self) -> float:
        return self._phase

    def set_animation(self, direction: str, anim_key: str) -> None:
        self._direction = "exit" if direction == "exit" else "entry"
        self._anim_key = anim_key
        self._meta = animation_preview_meta(self._direction, self._anim_key)
        self._phase = 0.0
        self._last_status = ("", False)
        self.update()

    def set_phase(self, phase: float) -> None:
        """Set animation phase in ``[0, 1)`` deterministically (useful for tests)."""
        self._phase = float(phase) % 1.0
        self.update()

    def start_animation(self) -> None:
        self._phase = 0.0
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def stop_animation(self) -> None:
        if self._timer.isActive():
            self._timer.stop()

    def is_animating(self) -> bool:
        return self._timer.isActive()

    def _on_tick(self) -> None:
        step = self._FRAME_INTERVAL_MS / float(max(self._meta.cycle_ms, 800))
        self._phase = (self._phase + step) % 1.0
        self.update()

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self.stop_animation()
        super().hideEvent(event)

    # ------------------------------------------------------------------ phase schedule

    def _resolve_stage(self) -> tuple[str, float, float]:
        """Return ``(stage_mode, anim_progress, envelope_alpha)``.

        - ``stage_mode``: ``"active"`` during the actual entry/exit animation,
          ``"hold"`` while holding the steady lyric, ``"empty"`` while blank,
          or ``"reset"`` during the brief loop-reset transition.
        - ``anim_progress``: normalized ``[0, 1]`` progress of the entry/exit effect.
        - ``envelope_alpha``: loop reset fade multiplier.
        """
        p = self._phase
        if self._direction == "entry":
            if p < 0.07:
                return "empty", 0.0, 0.0
            if p < 0.62:
                return "active", (p - 0.07) / 0.55, 1.0
            if p < 0.91:
                return "hold", 1.0, 1.0
            return "reset", 1.0, 1.0 - _smoothstep((p - 0.91) / 0.09)
        # Exit direction
        if p < 0.24:
            return "hold", 0.0, 1.0
        if p < 0.79:
            return "active", (p - 0.24) / 0.55, 1.0
        if p < 0.91:
            return "empty", 1.0, 0.0
        return "reset", 0.0, _smoothstep((p - 0.91) / 0.09)

    # ------------------------------------------------------------------ painting

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = palette()
        accent = QColor(p.accent_primary)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        canvas = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setPen(QPen(QColor(p.preview_border), 1.0))
        painter.setBrush(QColor("#11141B"))
        painter.drawRoundedRect(canvas, 7.0, 7.0)

        painter.save()
        clip_path = QPainterPath()
        clip_path.addRoundedRect(canvas.adjusted(0.5, 0.5, -0.5, -0.5), 6.5, 6.5)
        painter.setClipPath(clip_path)

        # Subtle stage baseline & frame guides (leave headroom for furigana ruby above kanji).
        baseline_y = self.height() * 0.62 + self._ascent * 0.28
        center_y = baseline_y - self._ascent * 0.42
        painter.setPen(QPen(QColor(255, 255, 255, 18), 1.0, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(16.0, baseline_y + 4.0), QPointF(self.width() - 16.0, baseline_y + 4.0))

        stage_mode, anim_progress, env_alpha = self._resolve_stage()
        is_exit = self._direction == "exit"

        # Compute per-character x positions centered in stage.
        start_x = (self.width() - self._line_width) / 2.0
        line_center_x = self.width() / 2.0
        char_xs: list[float] = []
        cursor = start_x
        for w in self._char_widths:
            char_xs.append(cursor)
            cursor += w

        # Paint back-layer particles (flame glow / smoke / ripple / sparkle back).
        if stage_mode == "active" or (stage_mode == "hold" and not is_exit and self._anim_key in {"petal", "snow", "snow_solid", "feather", "flame"}):
            particle_p = anim_progress if stage_mode == "active" else (1.0 + (self._phase - 0.62) * 0.7)
            self._paint_particles(
                painter,
                char_xs,
                center_y,
                baseline_y,
                particle_p,
                is_exit=is_exit,
                layer="back",
                accent=accent,
            )

        # Paint lyric characters.
        if stage_mode != "empty":
            self._paint_lyric(
                painter,
                char_xs,
                line_center_x,
                baseline_y,
                center_y,
                stage_mode=stage_mode,
                anim_progress=anim_progress,
                env_alpha=env_alpha,
                is_exit=is_exit,
                accent=accent,
            )

        # Paint front-layer particles.
        if stage_mode == "active" or (stage_mode == "hold" and not is_exit and self._anim_key in {"petal", "snow", "snow_solid", "feather", "flame"}):
            particle_p = anim_progress if stage_mode == "active" else (1.0 + (self._phase - 0.62) * 0.7)
            self._paint_particles(
                painter,
                char_xs,
                center_y,
                baseline_y,
                particle_p,
                is_exit=is_exit,
                layer="front",
                accent=accent,
            )

        # Subtle bottom loop progress bar.
        bar_x = 12.0
        bar_w = self.width() - 24.0
        bar_y = self.height() - 8.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 26))
        painter.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, 3.0), 1.5, 1.5)

        # Highlight active animation window on the rail.
        win_start, win_end = (0.07, 0.62) if not is_exit else (0.24, 0.79)
        win_col = QColor(accent.red(), accent.green(), accent.blue(), 80)
        painter.setBrush(win_col)
        painter.drawRoundedRect(
            QRectF(bar_x + bar_w * win_start, bar_y, bar_w * (win_end - win_start), 3.0),
            1.5,
            1.5,
        )
        # Current playhead progress.
        painter.setBrush(accent if stage_mode == "active" else QColor(255, 255, 255, 130))
        painter.drawRoundedRect(
            QRectF(bar_x, bar_y, max(3.0, bar_w * self._phase), 3.0),
            1.5,
            1.5,
        )

        # Status badge update.
        if self._anim_key == "none":
            status = ("无过渡动画", False)
        elif stage_mode == "active":
            status = ("退场动画中" if is_exit else "入场动画中", True)
        elif stage_mode == "hold":
            status = ("静态停留", False)
        else:
            status = ("循环准备", False)
        if status != self._last_status:
            self._last_status = status
            if self._on_status_change is not None:
                self._on_status_change(status[0], status[1])

        painter.restore()
        painter.end()

    # ------------------------------------------------------------------ glyph painting

    def _draw_styled_glyph(
        self,
        painter: QPainter,
        path: QPainterPath,
        *,
        fill_color: QColor,
        stroke_color: QColor,
        stroke_width: float = 2.6,
        ruby_path: Optional[QPainterPath] = None,
    ) -> None:
        if not path.isEmpty():
            pen = QPen(stroke_color, stroke_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill_color)
            painter.drawPath(path)
        if ruby_path is not None and not ruby_path.isEmpty():
            rpen = QPen(stroke_color, max(1.2, stroke_width * 0.58), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(rpen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(ruby_path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill_color)
            painter.drawPath(ruby_path)

    def _ruby_path_for(self, idx: int) -> Optional[QPainterPath]:
        if 0 <= idx < len(self._ruby_layouts):
            layout = self._ruby_layouts[idx]
            if layout is not None:
                return layout.combined_path
        return None

    def _paint_lyric(
        self,
        painter: QPainter,
        char_xs: list[float],
        line_center_x: float,
        baseline_y: float,
        center_y: float,
        *,
        stage_mode: str,
        anim_progress: float,
        env_alpha: float,
        is_exit: bool,
        accent: QColor,
    ) -> None:
        key = self._anim_key
        count = len(self._chars)
        font_px = 24.0
        fill_base = QColor("#F8FAFC")
        stroke_base = QColor("#0F172A")

        # Whole-line hold / reset / none shortcut.
        if stage_mode in {"hold", "reset"} or key == "none":
            if key == "none" and stage_mode == "active":
                visible = (anim_progress >= 0.15) if not is_exit else (anim_progress < 0.85)
                if not visible:
                    return
            painter.save()
            painter.setOpacity(_clamp01(env_alpha))
            for idx, path in enumerate(self._glyph_paths):
                if path.isEmpty():
                    continue
                painter.save()
                painter.translate(char_xs[idx], baseline_y)
                self._draw_styled_glyph(
                    painter,
                    path,
                    fill_color=fill_base,
                    stroke_color=stroke_base,
                    ruby_path=self._ruby_path_for(idx),
                )
                painter.restore()
            painter.restore()
            return

        # Whole-line transforms: fade / slide_in / slide_out / rise.
        if key in {"fade", "slide_in", "slide_out", "rise"}:
            if not is_exit:
                eased = 1.0 - (1.0 - anim_progress) ** 2
                alpha = eased
                dx = -34.0 * (1.0 - eased) if key == "slide_in" else 0.0
                dy = 22.0 * (1.0 - eased) if key == "rise" else 0.0
            else:
                remaining = (1.0 - anim_progress) ** 2
                alpha = remaining
                dx = 34.0 * (1.0 - remaining) if key == "slide_out" else 0.0
                dy = -22.0 * (1.0 - remaining) if key == "rise" else 0.0
            painter.save()
            painter.setOpacity(_clamp01(alpha * env_alpha))
            for idx, path in enumerate(self._glyph_paths):
                if path.isEmpty():
                    continue
                painter.save()
                painter.translate(char_xs[idx] + dx, baseline_y + dy)
                self._draw_styled_glyph(
                    painter,
                    path,
                    fill_color=fill_base,
                    stroke_color=stroke_base,
                    ruby_path=self._ruby_path_for(idx),
                )
                painter.restore()
            painter.restore()
            return

        # Glow horizontal echo (whole-line synchronized).
        if key in {"glow_in", "glow_out"}:
            q = _smoothstep(anim_progress)
            if key == "glow_in":
                alpha = min(anim_progress * 3.0, 1.0)
                scale_x = 1.0 + 0.6 * (1.0 - q)
                glow_strength = (1.0 - q) * math.sqrt(max(0.0, 1.0 - q))
                pitch_mul = 1.0 - 0.25 * q
            else:
                alpha = 1.0 - q
                scale_x = 1.0 + 0.6 * q
                glow_strength = math.sin(math.pi * q)
                pitch_mul = 1.0 + 0.9 * q
            pitch = font_px * 0.14 * pitch_mul
            # Draw horizontal decaying echoes on both sides.
            if glow_strength > 0.02:
                glow_col = QColor(accent.red(), accent.green(), accent.blue(), 255)
                for step in range(9, 0, -1):
                    echo_alpha = glow_strength * (0.76 ** step) * 0.52 * env_alpha
                    if echo_alpha < 0.015:
                        continue
                    offset = step * pitch
                    for sign in (-1.0, 1.0):
                        painter.save()
                        painter.setOpacity(_clamp01(echo_alpha))
                        for idx, path in enumerate(self._glyph_paths):
                            if path.isEmpty():
                                continue
                            painter.save()
                            cx = char_xs[idx] + self._char_widths[idx] * 0.5
                            tr = QTransform()
                            tr.translate(cx + sign * offset, center_y)
                            tr.scale(scale_x, 1.0)
                            tr.translate(-self._char_widths[idx] * 0.5, baseline_y - center_y)
                            painter.setTransform(tr, combine=True)
                            painter.setPen(Qt.PenStyle.NoPen)
                            painter.setBrush(glow_col)
                            painter.drawPath(path)
                            ruby_p = self._ruby_path_for(idx)
                            if ruby_p is not None:
                                painter.drawPath(ruby_p)
                            painter.restore()
                        painter.restore()
            # Main stretched text.
            if alpha > 0.01:
                painter.save()
                painter.setOpacity(_clamp01(alpha * env_alpha))
                for idx, path in enumerate(self._glyph_paths):
                    if path.isEmpty():
                        continue
                    painter.save()
                    cx = char_xs[idx] + self._char_widths[idx] * 0.5
                    tr = QTransform()
                    tr.translate(cx, center_y)
                    tr.scale(scale_x, 1.0)
                    tr.translate(-self._char_widths[idx] * 0.5, baseline_y - center_y)
                    painter.setTransform(tr, combine=True)
                    self._draw_styled_glyph(
                        painter,
                        path,
                        fill_color=fill_base,
                        stroke_color=stroke_base,
                        ruby_path=self._ruby_path_for(idx),
                    )
                    painter.restore()
                painter.restore()
            return

        # Noise block dissolve.
        if key == "noise":
            threshold = _smoothstep(anim_progress)
            cell = 3.5
            for idx, path in enumerate(self._glyph_paths):
                if path.isEmpty():
                    continue
                ruby_p = self._ruby_path_for(idx)
                bounds = path.boundingRect()
                if ruby_p is not None and not ruby_p.isEmpty():
                    bounds = bounds.united(ruby_p.boundingRect())
                bounds = bounds.adjusted(-2.5, -2.5, 2.5, 2.5)
                cols = max(1, int(math.ceil(bounds.width() / cell)))
                rows = max(1, int(math.ceil(bounds.height() / cell)))
                mask_path = QPainterPath()
                for r in range(rows):
                    for c in range(cols):
                        h = fx_unit_hash(idx * 257 + r * 31 + c, 911)
                        show_cell = (h <= threshold) if not is_exit else (h >= threshold)
                        if show_cell:
                            mask_path.addRect(
                                bounds.left() + c * cell,
                                bounds.top() + r * cell,
                                cell + 0.2,
                                cell + 0.2,
                            )
                if mask_path.isEmpty():
                    continue
                painter.save()
                painter.translate(char_xs[idx], baseline_y)
                painter.setClipPath(mask_path)
                painter.setOpacity(_clamp01(env_alpha))
                self._draw_styled_glyph(
                    painter,
                    path,
                    fill_color=fill_base,
                    stroke_color=stroke_base,
                    ruby_path=ruby_p,
                )
                painter.restore()
            return

        # Per-character transitions.
        for idx, path in enumerate(self._glyph_paths):
            if path.isEmpty():
                continue
            char_w = self._char_widths[idx]
            char_cx = char_xs[idx] + char_w * 0.5
            opacity, dx, dy, rot, sx, sy, skew_y, pivot_mode, halo = self._char_state(
                key,
                idx,
                count,
                anim_progress,
                is_exit=is_exit,
                font_px=font_px,
                char_cx=char_cx,
                line_cx=line_center_x,
            )
            if opacity <= 0.005 and halo <= 0.01:
                continue

            if pivot_mode == "corner_entry":
                px, py = char_xs[idx] + char_w, baseline_y
            elif pivot_mode == "corner_exit":
                px, py = char_xs[idx] + char_w, baseline_y - self._ascent
            else:
                px, py = char_cx, center_y

            tr = QTransform()
            tr.translate(px + dx, py + dy)
            if rot:
                tr.rotate(rot)
            if skew_y:
                tr.shear(0.0, skew_y)
            if sx != 1.0 or sy != 1.0:
                tr.scale(sx, sy)
            tr.translate(char_xs[idx] - px, baseline_y - py)

            # Optional luminous halo behind stretch_in / stretch_out.
            ruby_p = self._ruby_path_for(idx)
            if halo > 0.02:
                for width_mul, strength in ((8.5, 0.16), (4.8, 0.30), (2.2, 0.45)):
                    painter.save()
                    painter.setTransform(tr, combine=True)
                    painter.setOpacity(_clamp01(halo * strength * env_alpha))
                    halo_col = QColor(accent.red(), accent.green(), accent.blue(), 255)
                    painter.setPen(QPen(halo_col, width_mul, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPath(path)
                    if ruby_p is not None:
                        painter.setPen(QPen(halo_col, max(1.2, width_mul * 0.55), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                        painter.drawPath(ruby_p)
                    painter.restore()

            if opacity > 0.005:
                painter.save()
                painter.setTransform(tr, combine=True)
                painter.setOpacity(_clamp01(opacity * env_alpha))
                self._draw_styled_glyph(
                    painter,
                    path,
                    fill_color=fill_base,
                    stroke_color=stroke_base,
                    ruby_path=ruby_p,
                )
                painter.restore()

    def _char_state(
        self,
        key: str,
        index: int,
        count: int,
        progress: float,
        *,
        is_exit: bool,
        font_px: float,
        char_cx: float,
        line_cx: float,
    ) -> tuple[float, float, float, float, float, float, float, str, float]:
        """Return ``(opacity, dx, dy, rot, sx, sy, skew_y, pivot_mode, halo)``."""
        p = _stagger_progress(progress, index, count)
        q = _smoothstep(p)

        if key == "char_fade":
            return ((1.0 - p) if is_exit else p), 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0

        if key == "char_drip":
            prog = (1.0 - p) if is_exit else p
            if prog <= 0.001:
                return 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0
            direction = -1.0 if is_exit else 1.0
            skew_y = direction * min(spin_flip_skew(prog), 4.5)
            pivot = "corner_exit" if is_exit else "corner_entry"
            return 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, skew_y, pivot, 0.0

        if key == "spin_flip":
            prog = (1.0 - p) if is_exit else p
            if prog <= 0.001:
                return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "center", 0.0
            direction = 1.0 if is_exit else -1.0
            skew_y = direction * min(spin_flip_skew(prog), 4.5)
            return prog, 0.0, 0.0, 0.0, prog, prog, skew_y, "center", 0.0

        if key == "utopia":
            if not is_exit:
                if p <= 0.0:
                    return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "center", 0.0
                if p < 0.8:
                    ratio = p / 0.8
                    scale = 1.32 * ratio
                    opacity = min(ratio, 1.0)
                else:
                    rem = (1.0 - p) / 0.2
                    scale = 1.0 + 0.32 * rem
                    opacity = 1.0
                return opacity, 0.0, 0.0, 0.0, scale, scale, 0.0, "center", 0.0
            # Utopia exit: shrink + horizontal cosine flip + -180° spin + downward-left arc.
            opacity = max(0.0, 1.0 - p)
            shrink = 1.0 - p
            amp = 22.0
            x_travel = (
                math.sin(math.pi * p) * amp
                if p <= 0.5
                else amp + math.sin((p - 0.5) * math.pi) * amp
            )
            y_travel = math.sin(math.pi * p / 2.0) * amp
            x_flip = math.cos(math.pi * p)
            return opacity, -x_travel, y_travel, -180.0 * p, shrink * x_flip, shrink, 0.0, "center", 0.0

        if key == "tracking_in":
            norm = 0.0 if count <= 1 else (index - (count - 1) / 2.0) / ((count - 1) / 2.0)
            dx = norm * font_px * 1.75 * (1.0 - q)
            return min(p * 4.0, 1.0), dx, 0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0

        if key == "wave_in":
            dy = 24.0 * (1.0 - q)
            return min(p * 4.0, 1.0), 0.0, dy, 0.0, 1.0, 1.0, 0.0, "center", 0.0

        if key == "scatter_out":
            theta = fx_unit_hash(index, 1) * 2.0 * math.pi
            speed = 0.55 + 0.45 * fx_unit_hash(index, 2)
            spin = (fx_unit_hash(index, 3) - 0.5) * 420.0
            travel = font_px * 2.1 * speed * (1.0 - (1.0 - p) ** 2)
            scale = 1.0 - 0.4 * p
            fade = (1.0 - p) * (1.0 - p)
            return fade, math.cos(theta) * travel, math.sin(theta) * travel, spin * p, scale, scale, 0.0, "center", 0.0

        if key == "converge_out":
            dx = (line_cx - char_cx) * q
            scale = 1.0 - 0.22 * q
            return 1.0 - q, dx, 0.0, 0.0, scale, scale, 0.0, "center", 0.0

        if key == "stretch_in":
            halo = (1.0 - q) ** 1.4
            return min(p * 4.0, 1.0), 0.0, 0.0, 0.0, 1.0 + 2.2 * (1.0 - q), 1.0, 0.0, "center", halo

        if key == "stretch_out":
            halo = math.sin(math.pi * q) * 0.95
            return 1.0 - q, 0.0, 0.0, 0.0, 1.0 + 2.2 * q, 1.0, 0.0, "center", halo

        if key == "random_zoom":
            rank = _random_rank(index, count, is_exit)
            rp = _stagger_progress(progress, rank, count)
            if is_exit:
                if rp >= 1.0:
                    return 0.0, 0.0, 0.0, 0.0, 0.001, 0.001, 0.0, "center", 0.0
                scale = max(1.0 - rp * rp, 0.001)
                return min((1.0 - rp) * 6.0, 1.0), 0.0, (1.0 - scale) * font_px * 0.42, 0.0, scale, scale, 0.0, "center", 0.0
            if rp <= 0.0:
                return 0.0, 0.0, 0.0, 0.0, 0.001, 0.001, 0.0, "center", 0.0
            scale = max(1.0 - (1.0 - rp) ** 2, 0.001)
            return min(rp * 6.0, 1.0), 0.0, (1.0 - scale) * font_px * 0.42, 0.0, scale, scale, 0.0, "center", 0.0

        if key == "flame":
            raw = _clamp01((p - 0.56) / 0.44)
            fq = _smoothstep(raw)
            if is_exit:
                return 1.0 - fq, 0.0, 0.0, 0.0, 1.0 + 2.2 * fq, 1.0, 0.0, "center", 0.0
            return min(raw * 4.0, 1.0), 0.0, 0.0, 0.0, 1.0 + 2.2 * (1.0 - fq), 1.0, 0.0, "center", 0.0

        if key == "assemble_in":
            appear = _smoothstep(p)
            return max(0.0, (appear - 0.45) / 0.55), 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0

        if key == "dissolve_out":
            vanish = _smoothstep(_clamp01(p / 0.55))
            return 1.0 - vanish, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0

        # Decorative particle animations: staggered crisp fade in / out.
        if is_exit:
            return 1.0 - q, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0
        return min(p * 4.0, 1.0), 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0

    # ------------------------------------------------------------------ particle painting

    def _draw_sprite(
        self,
        painter: QPainter,
        sprite_name: str,
        x: float,
        y: float,
        size: float,
        *,
        color: QColor,
        alpha: float,
        angle_deg: float = 0.0,
        scale_x: float = 1.0,
    ) -> None:
        if alpha <= 0.01 or size <= 0.4:
            return
        path = _sprite_painter_path(sprite_name)
        if path.isEmpty():
            return
        painter.save()
        painter.setOpacity(_clamp01(alpha))
        painter.translate(x, y)
        if angle_deg:
            painter.rotate(angle_deg)
        painter.scale(size * scale_x, size)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPath(path)
        painter.restore()

    def _paint_particles(
        self,
        painter: QPainter,
        char_xs: list[float],
        center_y: float,
        baseline_y: float,
        progress: float,
        *,
        is_exit: bool,
        layer: str,
        accent: QColor,
    ) -> None:
        key = self._anim_key
        if key not in {
            "assemble_in",
            "dissolve_out",
            "sparkle",
            "ripple",
            "note",
            "heart",
            "flame",
            "feather",
            "lightning",
            "petal",
            "snow",
            "snow_solid",
        }:
            return

        count = len(self._chars)

        if key in {"assemble_in", "dissolve_out"}:
            if layer != "front":
                return
            pixel_col_a = QColor("#F8FAFC")
            pixel_col_b = QColor(accent.red(), accent.green(), accent.blue(), 255)
            for idx, ch in enumerate(self._chars):
                if not ch.strip():
                    continue
                p = _stagger_progress(progress, idx, count)
                if p <= 0.0 or p >= 1.0:
                    continue
                cx = char_xs[idx] + self._char_widths[idx] * 0.5
                for k in range(7):
                    seed = idx * 31 + k
                    ang = fx_unit_hash(seed, 101) * 2.0 * math.pi
                    dist = (24.0 + 30.0 * fx_unit_hash(seed, 102))
                    ox = (fx_unit_hash(seed, 103) - 0.5) * 14.0
                    oy = (fx_unit_hash(seed, 104) - 0.5) * 16.0
                    if key == "assemble_in":
                        q = _smoothstep(p)
                        r = dist * (1.0 - q)
                        alpha = math.sin(math.pi * _clamp01(p))
                    else:
                        q = 1.0 - (1.0 - p) ** 2
                        r = dist * q
                        alpha = (1.0 - p) ** 1.2
                    px = cx + ox + math.cos(ang) * r
                    py = center_y + oy + math.sin(ang) * r
                    size = 6.0 + 4.0 * fx_unit_hash(seed, 105)
                    col = pixel_col_a if k % 3 != 0 else pixel_col_b
                    self._draw_sprite(painter, "pixel", px, py, size, color=col, alpha=alpha * 0.92, angle_deg=p * 90.0)
            return

        if key == "ripple":
            if layer != "back":
                return
            ring_a = QColor("#FFB7C5")
            ring_b = QColor("#FFD7E0")
            for idx, ch in enumerate(self._chars):
                if not ch.strip():
                    continue
                p = _stagger_progress(progress, idx, count)
                if p <= 0.0:
                    continue
                cx = char_xs[idx] + self._char_widths[idx] * 0.5
                for ring_i in range(3):
                    rp = _clamp01((p - ring_i * 0.16) / 0.68)
                    if rp <= 0.0 or rp >= 1.0:
                        continue
                    size = 14.0 + 34.0 * (1.0 - (1.0 - rp) ** 2)
                    alpha = (1.0 - rp) * 0.78
                    col = ring_a if ring_i % 2 == 0 else ring_b
                    self._draw_sprite(painter, "ring", cx, center_y, size, color=col, alpha=alpha)
            return

        if key == "sparkle":
            if layer != "front":
                return
            col_a = QColor("#FFB7C5")
            col_b = QColor("#FFF1F5")
            for idx, ch in enumerate(self._chars):
                if not ch.strip():
                    continue
                p = _stagger_progress(progress, idx, count)
                if p <= 0.0 or p >= 1.0:
                    continue
                cx = char_xs[idx] + self._char_widths[idx] * 0.5
                for k in range(3):
                    seed = idx * 17 + k
                    ox = (fx_unit_hash(seed, 201) - 0.5) * 26.0
                    oy = (fx_unit_hash(seed, 202) - 0.5) * 28.0 - p * 8.0
                    pulse = math.sin(math.pi * _clamp01(p))
                    size = (8.0 + 8.0 * fx_unit_hash(seed, 203)) * (0.45 + 0.65 * pulse)
                    angle = (fx_unit_hash(seed, 204) - 0.5) * 40.0 + p * 35.0
                    col = col_a if (idx + k) % 2 == 0 else col_b
                    self._draw_sprite(painter, "star4", cx + ox, center_y + oy, size, color=col, alpha=pulse * 0.95, angle_deg=angle)
            return

        if key in {"note", "heart"}:
            if layer != "front":
                return
            sprite = "note" if key == "note" else "heart"
            col_a = QColor("#FFB7C5" if key == "note" else "#FF4D6D")
            col_b = QColor("#FFD7E0" if key == "note" else "#FF9EBB")
            for idx, ch in enumerate(self._chars):
                if not ch.strip():
                    continue
                p = _stagger_progress(progress, idx, count)
                if p <= 0.0 or p >= 1.0:
                    continue
                cx = char_xs[idx] + self._char_widths[idx] * 0.5
                for k in range(2):
                    seed = idx * 19 + k
                    drift_x = math.sin(p * 4.5 + seed) * 6.0 + (fx_unit_hash(seed, 301) - 0.5) * 14.0
                    drift_y = -10.0 - p * (26.0 + 14.0 * fx_unit_hash(seed, 302))
                    alpha = math.sin(math.pi * _clamp01(p)) ** 0.75
                    size = 11.0 + 5.0 * fx_unit_hash(seed, 303)
                    angle = math.sin(p * 5.0 + seed) * 16.0
                    col = col_a if (idx + k) % 2 == 0 else col_b
                    self._draw_sprite(painter, sprite, cx + drift_x, center_y + drift_y, size, color=col, alpha=alpha * 0.92, angle_deg=angle)
            return

        if key == "flame":
            flame_sprites = ("flame", "flame_tall", "flame_wide", "flame_curl")
            for idx, ch in enumerate(self._chars):
                if not ch.strip():
                    continue
                p = _stagger_progress(progress, idx, count)
                if p <= 0.0 or p >= 1.15:
                    continue
                lp = _clamp01(p)
                cx = char_xs[idx] + self._char_widths[idx] * 0.5
                if layer == "back":
                    # Warm glow & smoke puff behind text.
                    smoke_alpha = math.sin(math.pi * lp) * 0.25
                    self._draw_sprite(painter, "smoke", cx, center_y - lp * 16.0, 26.0 + lp * 14.0, color=QColor("#FB923C"), alpha=smoke_alpha)
                else:
                    # Layered rising flame tongues + sparks.
                    for k in range(3):
                        seed = idx * 23 + k
                        sp = _clamp01((p - k * 0.08) / 0.85)
                        if sp <= 0.0 or sp >= 1.0:
                            continue
                        ox = (fx_unit_hash(seed, 401) - 0.5) * 14.0
                        oy = baseline_y - 4.0 - sp * (24.0 + 12.0 * fx_unit_hash(seed, 402))
                        env = math.sin(math.pi * sp)
                        size = (16.0 + 10.0 * fx_unit_hash(seed, 403)) * (0.65 + 0.5 * env)
                        col = QColor("#FF4500") if k == 0 else (QColor("#FF9E1B") if k == 1 else QColor("#FFF3B0"))
                        sprite = flame_sprites[(idx + k) % len(flame_sprites)]
                        self._draw_sprite(painter, sprite, cx + ox, oy, size, color=col, alpha=env * 0.88, angle_deg=(fx_unit_hash(seed, 404) - 0.5) * 18.0)
                    # Bright ember sparks rising above flame.
                    ep = _clamp01((p - 0.1) / 0.9)
                    if 0.0 < ep < 1.0:
                        ex = cx + (fx_unit_hash(idx, 409) - 0.5) * 18.0 + ep * 8.0
                        ey = center_y - ep * 36.0
                        self._draw_sprite(painter, "dot", ex, ey, 3.2, color=QColor("#FEF08A"), alpha=(1.0 - ep) * 0.95)
            return

        if key == "lightning":
            if layer != "front":
                return
            bolts = ("bolt", "bolt_b", "bolt_c")
            for idx, ch in enumerate(self._chars):
                if not ch.strip():
                    continue
                p = _stagger_progress(progress, idx, count)
                if p <= 0.0 or p >= 1.0:
                    continue
                cx = char_xs[idx] + self._char_widths[idx] * 0.5
                # Crisp rapid flicker envelope like real electric discharge.
                flicker = 0.55 + 0.45 * abs(math.sin(p * 18.0 + idx * 1.7))
                env = math.sin(math.pi * p) * flicker
                bolt_sprite = bolts[(idx + int(p * 6.0)) % len(bolts)]
                # Outer electric blue glow + bright cyan-white core.
                self._draw_sprite(painter, bolt_sprite, cx, center_y - 2.0, 34.0, color=QColor("#38BDF8"), alpha=env * 0.65, angle_deg=(fx_unit_hash(idx, 501) - 0.5) * 16.0)
                self._draw_sprite(painter, bolt_sprite, cx, center_y - 2.0, 27.0, color=QColor("#E0FBFF"), alpha=env * 0.95, angle_deg=(fx_unit_hash(idx, 501) - 0.5) * 16.0)
                # Electric spark dots.
                for k in range(2):
                    sx = cx + (fx_unit_hash(idx * 7 + k, 505) - 0.5) * 26.0
                    sy = center_y + (fx_unit_hash(idx * 7 + k, 506) - 0.5) * 26.0
                    self._draw_sprite(painter, "dot", sx, sy, 3.0, color=QColor("#E0FBFF"), alpha=env * 0.9)
            return

        # Falling wind-drifted decorative particles: petal / feather / snow / snow_solid.
        if layer != "front":
            return
        sprite_map = {
            "petal": ("petal", QColor("#FFB7C5"), QColor("#FFD7E0"), -22.0, 12.0),
            "feather": ("feather", QColor("#FFFFFF"), QColor("#FFF5E4"), -28.0, 16.0),
            "snow": ("snow", QColor("#FFFFFF"), QColor("#BAE6FD"), -26.0, 12.5),
            "snow_solid": ("snow_solid", QColor("#FFFFFF"), QColor("#BAE6FD"), -26.0, 12.0),
        }
        sprite, col_a, col_b, lean_deg, base_size = sprite_map[key]
        for idx, ch in enumerate(self._chars):
            if not ch.strip():
                continue
            p = _stagger_progress(min(progress, 1.35), idx, count)
            if p <= 0.0:
                continue
            cx = char_xs[idx] + self._char_widths[idx] * 0.5
            for k in range(2):
                seed = idx * 29 + k
                fp = _clamp01((p - k * 0.12) / 0.95)
                if fp <= 0.0 or fp >= 1.0:
                    continue
                fade_in = min(fp / 0.14, 1.0)
                fade_out = 1.0 if fp < 0.80 else (1.0 - (fp - 0.80) / 0.20)
                alpha = fade_in * fade_out * 0.92
                wind_x = fp * (22.0 + 12.0 * fx_unit_hash(seed, 601))
                sway_x = math.sin(fp * 5.0 + seed) * 5.5
                start_y = center_y - 34.0 - 12.0 * fx_unit_hash(seed, 602)
                fall_y = start_y + fp * (68.0 + 14.0 * fx_unit_hash(seed, 603))
                px = cx + (fx_unit_hash(seed, 604) - 0.5) * 14.0 + wind_x + sway_x - 10.0
                size = base_size * (0.78 + 0.42 * fx_unit_hash(seed, 605))
                if key in {"snow", "snow_solid"}:
                    angle = lean_deg + fp * (80.0 if k % 2 == 0 else -80.0)
                    sx = 1.0
                else:
                    angle = lean_deg + math.sin(fp * 6.0 + seed) * 20.0
                    sx = 0.55 + 0.45 * abs(math.cos(fp * 4.2 + seed))
                col = col_a if (idx + k) % 2 == 0 else col_b
                self._draw_sprite(painter, sprite, px, fall_y, size, color=col, alpha=alpha, angle_deg=angle, scale_x=sx)


class AnimationPreviewPopover(QFrame):
    """Floating theme-aware card displaying live animation previews beside dropdowns."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setObjectName("AnimationPreviewPopover")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self._title_label = QLabel("", self)
        self._title_label.setObjectName("AnimationPreviewTitle")
        header.addWidget(self._title_label)
        header.addStretch(1)
        self._status_label = QLabel(" ", self)
        self._status_label.setObjectName("AnimationPreviewStatus")
        self._status_label.setFixedHeight(20)
        self._status_highlighted = False
        header.addWidget(self._status_label)
        layout.addLayout(header)

        self._canvas = AnimationPreviewCanvas(
            "entry",
            "fade",
            self,
            on_status_change=self._update_status_badge,
        )
        layout.addWidget(self._canvas)

        self._summary_label = QLabel("", self)
        self._summary_label.setObjectName("AnimationPreviewSummary")
        self._summary_label.setWordWrap(True)
        self._summary_label.setFixedWidth(self._canvas.width())
        layout.addWidget(self._summary_label)

        themed(self, self._build_qss)
        self.adjustSize()

    @property
    def canvas(self) -> AnimationPreviewCanvas:
        return self._canvas

    def _build_qss(self) -> str:
        p = palette()
        dark = bool(getattr(p, "is_dark", False))
        if self._status_highlighted:
            accent = QColor(p.accent_primary)
            alpha = 32 if not dark else 52
            pill_bg = f"rgba({accent.red()}, {accent.green()}, {accent.blue()}, {alpha})"
            pill_border = p.accent_primary
            pill_fg = p.accent_primary if not dark else "#FF8A9A"
        else:
            pill_bg = p.preview_bg
            pill_border = p.card_border
            pill_fg = p.text_secondary
        return (
            f"""
            QFrame#AnimationPreviewPopover {{
                background: transparent;
                border: none;
            }}
            QLabel#AnimationPreviewTitle {{
                color: {p.title_text};
                font-size: 9.5pt;
                font-weight: 700;
                background: transparent;
            }}
            QLabel#AnimationPreviewStatus {{
                color: {pill_fg};
                background: {pill_bg};
                border: 1px solid {pill_border};
                border-radius: 9px;
                padding: 1px 8px;
                font-size: 8pt;
                font-weight: 600;
            }}
            QLabel#AnimationPreviewSummary {{
                color: {p.text_secondary};
                font-size: 8.5pt;
                background: transparent;
            }}
            """
        )

    def _update_status_badge(self, text: str, highlighted: bool) -> None:
        self._status_label.setText(text)
        if highlighted != self._status_highlighted:
            self._status_highlighted = highlighted
            self.setStyleSheet(self._build_qss())
        self._status_label.adjustSize()
        if self.layout() is not None:
            self.layout().activate()

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setPen(QPen(QColor(p.input_border_hover), 1.0))
        painter.setBrush(QColor(p.card_bg))
        painter.drawRoundedRect(card, 10.0, 10.0)
        painter.end()

    def set_animation(
        self,
        direction: str,
        anim_key: str,
        *,
        display_label: str = "",
    ) -> None:
        meta = animation_preview_meta(direction, anim_key, fallback_title=display_label)
        prefix = "退场 · " if direction == "exit" else "入场 · "
        self._title_label.setText(f"{prefix}{display_label or meta.title}")
        self._summary_label.setText(meta.summary)
        self._canvas.set_animation(direction, anim_key)
        self.adjustSize()

    def show_beside(self, menu_rect: QRect, row_rect: QRect) -> None:
        self.adjustSize()
        pos = self._compute_position(menu_rect, row_rect)
        self.move(pos)
        if not self.isVisible():
            self.show()
        self.raise_()
        self._canvas.start_animation()

    def _compute_position(self, menu_rect: QRect, row_rect: QRect) -> QPoint:
        pop_w = self.width() or self.sizeHint().width()
        pop_h = self.height() or self.sizeHint().height()
        gap = 8

        anchor_pt = row_rect.center() if not row_rect.isNull() else menu_rect.center()
        screen = QApplication.screenAt(anchor_pt) or QApplication.primaryScreen()
        if screen is None:
            return QPoint(menu_rect.right() + gap, row_rect.center().y() - pop_h // 2)

        avail = screen.availableGeometry()
        right_x = menu_rect.right() + gap
        left_x = menu_rect.left() - pop_w - gap
        if right_x + pop_w <= avail.right() - 8:
            x = right_x
        elif left_x >= avail.left() + 8:
            x = left_x
        else:
            x = max(avail.left() + 8, min(avail.right() - pop_w - 8, right_x))

        ideal_y = (row_rect.center().y() if not row_rect.isNull() else menu_rect.center().y()) - pop_h // 2
        y = max(avail.top() + 8, min(avail.bottom() - pop_h - 8, ideal_y))
        return QPoint(x, y)

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self._canvas.stop_animation()
        super().hideEvent(event)


ZOOM_PULSE_CURVE_META: dict[int, AnimationPreviewMeta] = {
    0: AnimationPreviewMeta(
        "0级（线性）",
        "唱字期间匀速线性放大至峰值，唱完后 300ms 内匀速线性缩回。",
        2600,
    ),
    1: AnimationPreviewMeta(
        "1级（匀速·默认）",
        "一阶匀速曲线（与 0 级等效），放大与缩回节奏平稳均匀。",
        2600,
    ),
    2: AnimationPreviewMeta(
        "2级（稍快）",
        "二阶缓出放大、缓入缩回，起唱更快接近峰值并略作停留。",
        2600,
    ),
    3: AnimationPreviewMeta(
        "3级（较快）",
        "三阶曲线加快起跳速度，唱字中后段饱满停留在峰值附近。",
        2600,
    ),
    4: AnimationPreviewMeta(
        "4级（快）",
        "四阶曲线迅速跃向峰值，唱完前后均有清晰的峰顶停留感。",
        2600,
    ),
    5: AnimationPreviewMeta(
        "5级（极快）",
        "五阶极速弹出贴近峰值并长时间饱满停留，尾段干脆收落。",
        2600,
    ),
}


def zoom_pulse_curve_meta(level: int) -> AnimationPreviewMeta:
    clamped = max(0, min(5, int(level)))
    return ZOOM_PULSE_CURVE_META[clamped]


@dataclass(frozen=True)
class _PreviewTextAppearance:
    """Resolved karaoke colors and scaled outline/decor metrics for mini preview."""

    colors: KaraokeColors
    stroke1_px: float
    stroke2_px: float
    before_decor: str
    after_decor: str
    before_glow_px: float
    after_glow_px: float
    before_shadow: tuple[float, float]
    after_shadow: tuple[float, float]


def _resolve_preview_text_appearance(
    style: Any,
    *,
    preview_font_px: float = 22.0,
) -> _PreviewTextAppearance:
    """Extract global default karaoke colors & scaled strokes from ``Style``."""
    if not isinstance(style, Style):
        return _PreviewTextAppearance(
            colors=KaraokeColors(),
            stroke1_px=2.4,
            stroke2_px=0.0,
            before_decor="none",
            after_decor="none",
            before_glow_px=0.0,
            after_glow_px=0.0,
            before_shadow=(0.0, 0.0),
            after_shadow=(0.0, 0.0),
        )
    colors = effective_karaoke_colors(style)
    base_font = max(float(getattr(style, "font_size_px", 56) or 56), 16.0)
    scale = preview_font_px / base_font

    raw_s1 = max(float(getattr(style, "stroke_width_px", 0) or 0), 0.0)
    stroke1_px = min(max(raw_s1 * scale * 2.0, 1.0), 4.5) if raw_s1 > 0 else 0.0

    stroke2_enabled = bool(getattr(style, "stroke2_enabled", False))
    raw_s2 = max(float(getattr(style, "stroke2_width_px", 0) or 0), 0.0)
    stroke2_px = (
        min(max(raw_s2 * scale * 2.0, 0.8), 3.6)
        if stroke2_enabled and raw_s2 > 0
        else 0.0
    )

    before_decor = decoration_kind_for_state(style, after=False)
    after_decor = decoration_kind_for_state(style, after=True)
    raw_gb = max(float(getattr(style, "glow_before_radius_px", 0) or 0), 0.0)
    raw_ga = max(float(getattr(style, "glow_after_radius_px", 0) or 0), 0.0)
    before_glow_px = min(max(raw_gb * scale, 1.2), 7.0) if before_decor == "glow" and raw_gb > 0 else 0.0
    after_glow_px = min(max(raw_ga * scale, 1.2), 7.0) if after_decor == "glow" and raw_ga > 0 else 0.0

    before_shadow = (
        (
            max(-4.0, min(4.0, shadow_offset_x(style, after=False) * scale)),
            max(-4.0, min(4.0, shadow_offset_y(style, after=False) * scale)),
        )
        if before_decor == "shadow"
        else (0.0, 0.0)
    )
    after_shadow = (
        (
            max(-4.0, min(4.0, shadow_offset_x(style, after=True) * scale)),
            max(-4.0, min(4.0, shadow_offset_y(style, after=True) * scale)),
        )
        if after_decor == "shadow"
        else (0.0, 0.0)
    )
    return _PreviewTextAppearance(
        colors=colors,
        stroke1_px=stroke1_px,
        stroke2_px=stroke2_px,
        before_decor=before_decor,
        after_decor=after_decor,
        before_glow_px=before_glow_px,
        after_glow_px=after_glow_px,
        before_shadow=before_shadow,
        after_shadow=after_shadow,
    )


def paint_styled_karaoke_glyph(
    painter: QPainter,
    path: QPainterPath,
    char_rect: QRectF,
    line_rect: QRectF,
    state: KaraokeColorState,
    appearance: _PreviewTextAppearance,
    *,
    after: bool,
    flash_strength: float = 0.0,
    override_fill_color: Optional[QColor] = None,
) -> None:
    """Draw shadow/glow -> stroke2 -> stroke1 -> text fill for one karaoke state.

    Supports optional ``flash_strength`` (white contour flash pulse for
    ``唱字闪光``) and ``override_fill_color`` (uniform highlight band for
    scanline preview).
    """
    if path.isEmpty():
        return
    s1 = appearance.stroke1_px
    s2 = appearance.stroke2_px
    outer_base = s1 + s2 * 2.0

    if override_fill_color is not None:
        if outer_base > 0.0:
            painter.setPen(
                QPen(
                    override_fill_color,
                    outer_base,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(override_fill_color)
        painter.drawPath(path)
        return

    decor = appearance.after_decor if after else appearance.before_decor
    glow_px = appearance.after_glow_px if after else appearance.before_glow_px
    sdx, sdy = appearance.after_shadow if after else appearance.before_shadow

    # 1. Shadow or soft multi-ring glow.
    if decor == "glow" and glow_px > 0.0:
        glow_brush = brush_for_fill(
            state.shadow,
            fill_brush_rect(state.shadow, char_rect, line_rect),
        )
        for ring_mul, alpha in ((1.9, 0.14), (1.25, 0.24), (0.7, 0.36)):
            painter.save()
            painter.setOpacity(painter.opacity() * alpha)
            pen = QPen(
                glow_brush,
                outer_base + glow_px * ring_mul,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.restore()
    elif decor == "shadow" and (abs(sdx) > 0.05 or abs(sdy) > 0.05):
        shadow_brush = brush_for_fill(
            state.shadow,
            fill_brush_rect(state.shadow, char_rect, line_rect),
        )
        painter.save()
        painter.translate(sdx, sdy)
        if outer_base > 0.0:
            painter.setPen(
                QPen(
                    shadow_brush,
                    outer_base,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
        else:
            painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow_brush)
        painter.drawPath(path)
        painter.restore()

    # Optional white contour flash halo behind strokes during 「唱字闪光」.
    flash = _clamp01(flash_strength)
    if flash > 0.01:
        flash_base = max(outer_base, 2.0)
        for extra_w, alpha in ((7.5, 0.24), (4.2, 0.45), (1.6, 0.78)):
            painter.save()
            painter.setOpacity(painter.opacity() * flash * alpha)
            painter.setPen(
                QPen(
                    QColor("#FFFFFF"),
                    flash_base + extra_w,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.restore()

    # 2. Outer secondary stroke.
    if s2 > 0.0:
        s2_brush = brush_for_fill(
            state.stroke2,
            fill_brush_rect(state.stroke2, char_rect, line_rect),
        )
        painter.setPen(
            QPen(
                s2_brush,
                s1 + s2 * 2.0,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    # 3. Primary stroke.
    if s1 > 0.0:
        s1_brush = brush_for_fill(
            state.stroke,
            fill_brush_rect(state.stroke, char_rect, line_rect),
        )
        painter.setPen(
            QPen(
                s1_brush,
                s1,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    # White stroke flash overlay on the primary stroke contour.
    if flash > 0.01:
        painter.save()
        painter.setOpacity(painter.opacity() * flash * 0.96)
        painter.setPen(
            QPen(
                QColor("#FFFFFF"),
                max(s1, 2.4) + 1.1 * flash,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.restore()

    # 4. Main text fill.
    text_brush = brush_for_fill(
        state.text,
        fill_brush_rect(state.text, char_rect, line_rect),
    )
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(text_brush)
    painter.drawPath(path)


def resolve_preview_ruby_appearance(
    style: Any,
    *,
    ruby_font_px: float = 10.0,
) -> _PreviewTextAppearance:
    """Resolve scaled stroke/glow/shadow metrics suitable for delicate ruby furigana."""
    base = _resolve_preview_text_appearance(style, preview_font_px=ruby_font_px)
    return _PreviewTextAppearance(
        colors=base.colors,
        stroke1_px=min(base.stroke1_px, 1.7),
        stroke2_px=min(base.stroke2_px, 1.1),
        before_decor=base.before_decor,
        after_decor=base.after_decor,
        before_glow_px=min(base.before_glow_px, 3.2),
        after_glow_px=min(base.after_glow_px, 3.2),
        before_shadow=(base.before_shadow[0] * 0.55, base.before_shadow[1] * 0.55),
        after_shadow=(base.after_shadow[0] * 0.55, base.after_shadow[1] * 0.55),
    )


def paint_styled_ruby_for_char(
    painter: QPainter,
    ruby_layout: Optional[PreviewRubyLayout],
    *,
    ruby_appearance: _PreviewTextAppearance,
    line_start_rel_x: float,
    line_width: float,
    kana_wipes: tuple[float, ...],
    reverse: bool = False,
    override_fill_color: Optional[QColor] = None,
) -> None:
    """Paint all ruby kana above the current parent kanji in kanji-local coordinates.

    Expects the painter's origin to be at the parent kanji's ``(left, baseline_y)``.
    Honors the engine rule that ``唱字闪光`` never flashes ruby (``flash_strength=0``).
    """
    if ruby_layout is None:
        return
    colors = ruby_appearance.colors
    clip_pad = 6.0
    clip_top = -ruby_layout.ascent - clip_pad
    clip_h = ruby_layout.ascent + ruby_layout.descent + clip_pad * 2.0
    for k_idx, (kx, kw, kpath) in enumerate(
        zip(ruby_layout.kana_xs, ruby_layout.kana_widths, ruby_layout.kana_paths)
    ):
        if kpath.isEmpty():
            continue
        wipe = kana_wipes[k_idx] if k_idx < len(kana_wipes) else 0.0
        kana_rect = QRectF(0.0, -ruby_layout.ascent, kw, ruby_layout.ascent + ruby_layout.descent)
        line_rect = QRectF(
            line_start_rel_x - kx,
            -ruby_layout.ascent,
            line_width,
            ruby_layout.ascent + ruby_layout.descent,
        )
        painter.save()
        painter.translate(kx, ruby_layout.baseline_y)
        if override_fill_color is not None:
            paint_styled_karaoke_glyph(
                painter,
                kpath,
                kana_rect,
                line_rect,
                colors.after,
                ruby_appearance,
                after=True,
                override_fill_color=override_fill_color,
            )
        elif wipe <= 0.0:
            paint_styled_karaoke_glyph(
                painter, kpath, kana_rect, line_rect, colors.before, ruby_appearance, after=False
            )
        elif wipe >= 1.0:
            paint_styled_karaoke_glyph(
                painter, kpath, kana_rect, line_rect, colors.after, ruby_appearance, after=True
            )
        else:
            if not reverse:
                split_x = kw * wipe
                painter.save()
                painter.setClipRect(QRectF(-clip_pad, clip_top, split_x + clip_pad, clip_h))
                paint_styled_karaoke_glyph(
                    painter, kpath, kana_rect, line_rect, colors.after, ruby_appearance, after=True
                )
                painter.restore()
                painter.save()
                painter.setClipRect(QRectF(split_x, clip_top, kw - split_x + clip_pad, clip_h))
                paint_styled_karaoke_glyph(
                    painter, kpath, kana_rect, line_rect, colors.before, ruby_appearance, after=False
                )
                painter.restore()
            else:
                split_x = kw * (1.0 - wipe)
                painter.save()
                painter.setClipRect(QRectF(-clip_pad, clip_top, split_x + clip_pad, clip_h))
                paint_styled_karaoke_glyph(
                    painter, kpath, kana_rect, line_rect, colors.before, ruby_appearance, after=False
                )
                painter.restore()
                painter.save()
                painter.setClipRect(QRectF(split_x, clip_top, kw - split_x + clip_pad, clip_h))
                paint_styled_karaoke_glyph(
                    painter, kpath, kana_rect, line_rect, colors.after, ruby_appearance, after=True
                )
                painter.restore()
        painter.restore()


class ZoomPulseCurvePreviewCanvas(QWidget):
    """Live QPainter preview showing zoom-pulse karaoke lyric + scale curve."""

    _CANVAS_W = 368
    _CANVAS_H = 160
    _FRAME_INTERVAL_MS = 28
    _LEAD_MS = 180
    _HOLD_TAIL_MS = 260
    _CURVE_SPLIT = 0.50

    def __init__(
        self,
        curve_level: int = 1,
        peak_pct: int = 125,
        parent: Optional[QWidget] = None,
        *,
        style: Any = None,
        on_status_change: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self._curve_level = max(0, min(5, int(curve_level)))
        self._raw_peak_pct = max(100, min(200, int(peak_pct)))
        self._style = style
        self._appearance = _resolve_preview_text_appearance(style, preview_font_px=22.0)
        self._ruby_appearance = resolve_preview_ruby_appearance(style, ruby_font_px=10.0)
        self._meta = zoom_pulse_curve_meta(self._curve_level)
        self._phase = 0.0
        self._on_status_change = on_status_change
        self._last_status: tuple[str, bool] = ("", False)
        self._timer = QTimer(self)
        self._timer.setInterval(self._FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)
        self.setFixedSize(self._CANVAS_W, self._CANVAS_H)

        self._font = QFont("Microsoft YaHei UI")
        self._font.setPixelSize(22)
        self._font.setWeight(QFont.Weight.Bold)
        fm = QFontMetricsF(self._font)
        self._ascent = fm.ascent()
        self._descent = fm.descent()
        self._sing_chars = ANIMATION_PREVIEW_SING_CHARS
        self._chars = [spec.char for spec in self._sing_chars]
        self._char_widths = [max(fm.horizontalAdvance(ch), 6.5) for ch in self._chars]
        self._line_width = sum(self._char_widths)
        self._glyph_paths: list[QPainterPath] = []
        self._char_intervals: list[tuple[int, int] | None] = []
        for spec in self._sing_chars:
            gp = QPainterPath()
            if spec.char.strip():
                gp.addText(0.0, 0.0, self._font, spec.char)
            self._glyph_paths.append(gp)
            if spec.is_sung and spec.start_ms is not None and spec.end_ms is not None:
                self._char_intervals.append(
                    (self._LEAD_MS + spec.start_ms, self._LEAD_MS + spec.end_ms)
                )
            else:
                self._char_intervals.append(None)
        self._total_ms = (
            self._LEAD_MS
            + ANIMATION_PREVIEW_SING_DURATION_MS
            + ZOOM_PULSE_SHRINK_MS
            + self._HOLD_TAIL_MS
        )
        self._ruby_layouts = build_preview_ruby_layouts(22.0, self._ascent, self._char_widths)

    @property
    def curve_level(self) -> int:
        return self._curve_level

    @property
    def peak_ratio(self) -> float:
        # When user sets 100% (no zoom), keep a 1.25x demo floor so curve levels remain readable.
        effective_pct = self._raw_peak_pct if self._raw_peak_pct > 100 else 125
        return effective_pct / 100.0

    @property
    def phase(self) -> float:
        return self._phase

    def set_curve(
        self,
        curve_level: int,
        *,
        peak_pct: Optional[int] = None,
        style: Any = None,
    ) -> None:
        self._curve_level = max(0, min(5, int(curve_level)))
        if peak_pct is not None:
            self._raw_peak_pct = max(100, min(200, int(peak_pct)))
        if style is not None:
            self._style = style
            self._appearance = _resolve_preview_text_appearance(style, preview_font_px=22.0)
            self._ruby_appearance = resolve_preview_ruby_appearance(style, ruby_font_px=10.0)
        self._meta = zoom_pulse_curve_meta(self._curve_level)
        self._phase = 0.0
        self._last_status = ("", False)
        self.update()

    def _paint_state_glyph(
        self,
        painter: QPainter,
        path: QPainterPath,
        char_rect: QRectF,
        line_rect: QRectF,
        state: KaraokeColorState,
        *,
        after: bool,
    ) -> None:
        """Draw shadow/glow -> stroke2 -> stroke1 -> text fill for one karaoke state."""
        paint_styled_karaoke_glyph(
            painter,
            path,
            char_rect,
            line_rect,
            state,
            self._appearance,
            after=after,
        )

    def set_phase(self, phase: float) -> None:
        self._phase = float(phase) % 1.0
        self.update()

    def start_animation(self) -> None:
        self._phase = 0.0
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def stop_animation(self) -> None:
        if self._timer.isActive():
            self._timer.stop()

    def is_animating(self) -> bool:
        return self._timer.isActive()

    def _on_tick(self) -> None:
        step = self._FRAME_INTERVAL_MS / float(max(self._total_ms, 800))
        self._phase = (self._phase + step) % 1.0
        self.update()

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self.stop_animation()
        super().hideEvent(event)

    def _curve_eased_at(self, u: float, level: int, split: float) -> float:
        """Evaluate normalized [0, 1] magnification along the single-char curve."""
        u = _clamp01(u)
        if u <= split:
            progress = u / max(split, 1e-6)
            return progress if level <= 0 else 1.0 - (1.0 - progress) ** level
        shrink = (u - split) / max(1.0 - split, 1e-6)
        return (1.0 - shrink) if level <= 0 else 1.0 - shrink**level

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = palette()
        accent = QColor(p.accent_primary)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        canvas = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setPen(QPen(QColor(p.preview_border), 1.0))
        painter.setBrush(QColor("#11141B"))
        painter.drawRoundedRect(canvas, 7.0, 7.0)

        painter.save()
        clip_path = QPainterPath()
        clip_path.addRoundedRect(canvas.adjusted(0.5, 0.5, -0.5, -0.5), 6.5, 6.5)
        painter.setClipPath(clip_path)

        t_ms = int(round(self._phase * self._total_ms))
        peak_ratio = self.peak_ratio
        level = self._curve_level

        # ---- Upper stage: live karaoke lyric with zoom-pulse scaling ----
        baseline_y = 60.0
        center_y = baseline_y - self._ascent * 0.42
        painter.setPen(QPen(QColor(255, 255, 255, 18), 1.0, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(16.0, baseline_y + 5.0), QPointF(self.width() - 16.0, baseline_y + 5.0))

        start_x = (self.width() - self._line_width) / 2.0
        cursor_x = start_x
        active_dot_u: Optional[float] = None
        active_scale = 1.0
        colors = self._appearance.colors

        # Draw non-scaled chars first, then scaled chars on top so zoomed glyphs never clip behind neighbors.
        split = self._CURVE_SPLIT
        char_entries: list[tuple[int, float, float, float, float]] = []
        for idx, path in enumerate(self._glyph_paths):
            w = self._char_widths[idx]
            interval = self._char_intervals[idx]
            if not path.isEmpty() and interval is not None:
                c_start, c_end = interval
                scale = zoom_pulse_wipe_scale(
                    t_ms,
                    c_start,
                    c_end,
                    curve_level=level,
                    peak_ratio=peak_ratio,
                )
                wipe = sample_sing_wipe_progress(
                    t_ms,
                    self._sing_chars[idx],
                    lead_ms=self._LEAD_MS,
                )
                # Track active character progress for the synchronized curve dot below.
                if c_start <= t_ms <= c_end:
                    char_progress = (t_ms - c_start) / float(max(c_end - c_start, 1))
                    active_dot_u = char_progress * split
                    active_scale = scale
                elif active_dot_u is None and c_end < t_ms < c_end + ZOOM_PULSE_SHRINK_MS:
                    shrink = (t_ms - c_end) / float(ZOOM_PULSE_SHRINK_MS)
                    active_dot_u = split + shrink * (1.0 - split)
                    active_scale = scale
                char_entries.append((idx, cursor_x, w, scale, wipe))
            cursor_x += w

        char_entries.sort(key=lambda item: item[3])
        for idx, cx_left, w, scale, wipe in char_entries:
            path = self._glyph_paths[idx]
            char_cx = cx_left + w * 0.5
            # Glyph-local boxes for vertical/horizontal/split/image brush alignment.
            char_rect = QRectF(0.0, -self._ascent, w, self._ascent + self._descent)
            line_rect = QRectF(
                start_x - cx_left,
                -self._ascent,
                self._line_width,
                self._ascent + self._descent,
            )
            painter.save()
            tr = QTransform()
            tr.translate(char_cx, center_y)
            if scale != 1.0:
                tr.scale(scale, scale)
            tr.translate(-w * 0.5, baseline_y - center_y)
            painter.setTransform(tr, combine=True)

            if wipe <= 0.0:
                self._paint_state_glyph(
                    painter, path, char_rect, line_rect, colors.before, after=False
                )
            elif wipe >= 1.0:
                self._paint_state_glyph(
                    painter, path, char_rect, line_rect, colors.after, after=True
                )
            else:
                clip_pad = 12.0
                clip_top = -self._ascent - clip_pad
                clip_h = self._ascent + self._descent + clip_pad * 2.0
                split_x = w * wipe
                painter.save()
                painter.setClipRect(QRectF(-clip_pad, clip_top, split_x + clip_pad, clip_h))
                self._paint_state_glyph(
                    painter, path, char_rect, line_rect, colors.after, after=True
                )
                painter.restore()
                painter.save()
                painter.setClipRect(QRectF(split_x, clip_top, w - split_x + clip_pad, clip_h))
                self._paint_state_glyph(
                    painter, path, char_rect, line_rect, colors.before, after=False
                )
                painter.restore()
            ruby_layout = self._ruby_layouts[idx] if idx < len(self._ruby_layouts) else None
            if ruby_layout is not None:
                spec = self._sing_chars[idx]
                k_count = len(ruby_layout.kana_paths)
                kana_wipes = tuple(
                    sample_ruby_kana_progress(
                        t_ms, spec, k_i, k_count, lead_ms=self._LEAD_MS
                    )
                    for k_i in range(k_count)
                )
                paint_styled_ruby_for_char(
                    painter,
                    ruby_layout,
                    ruby_appearance=self._ruby_appearance,
                    line_start_rel_x=start_x - cx_left,
                    line_width=self._line_width,
                    kana_wipes=kana_wipes,
                )
            painter.restore()

        # ---- Lower stage: synchronized single-char magnification curve ----
        divider_y = 84.0
        painter.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
        painter.drawLine(QPointF(12.0, divider_y), QPointF(self.width() - 12.0, divider_y))

        plot_left = 44.0
        plot_right = self.width() - 18.0
        plot_top = 96.0
        plot_bottom = 138.0
        plot_w = plot_right - plot_left
        plot_h = plot_bottom - plot_top
        peak_x = plot_left + plot_w * split

        # Baseline (1.0x) and peak ceiling guides.
        painter.setPen(QPen(QColor(255, 255, 255, 36), 1.0))
        painter.drawLine(QPointF(plot_left, plot_bottom), QPointF(plot_right, plot_bottom))
        painter.setPen(QPen(QColor(255, 255, 255, 24), 1.0, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(plot_left, plot_top), QPointF(plot_right, plot_top))
        painter.drawLine(QPointF(peak_x, plot_top - 2.0), QPointF(peak_x, plot_bottom))

        # Y-axis & zone captions.
        axis_font = _ui_font(6.8, bold=True)
        painter.setFont(axis_font)
        painter.setPen(QColor("#9CA3AF"))
        painter.drawText(QRectF(4.0, plot_top - 6.0, 36.0, 14.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{peak_ratio:.2f}×")
        painter.drawText(QRectF(4.0, plot_bottom - 8.0, 36.0, 14.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "1.00×")
        painter.drawText(QRectF(plot_left, plot_bottom + 2.0, peak_x - plot_left, 14.0), Qt.AlignmentFlag.AlignCenter, "唱字放大")
        painter.drawText(QRectF(peak_x, plot_bottom + 2.0, plot_right - peak_x, 14.0), Qt.AlignmentFlag.AlignCenter, "300ms 缩回")

        # Dashed linear baseline reference when level >= 2.
        if level >= 2:
            ref_path = QPainterPath()
            ref_path.moveTo(plot_left, plot_bottom)
            ref_path.lineTo(peak_x, plot_top)
            ref_path.lineTo(plot_right, plot_bottom)
            painter.setPen(QPen(QColor(148, 163, 184, 115), 1.1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(ref_path)

        # Active polynomial curve + soft area fill.
        samples = 72
        curve_path = QPainterPath()
        fill_path = QPainterPath()
        fill_path.moveTo(plot_left, plot_bottom)
        for i in range(samples + 1):
            u = i / float(samples)
            val = self._curve_eased_at(u, level, split)
            x = plot_left + plot_w * u
            y = plot_bottom - plot_h * val
            if i == 0:
                curve_path.moveTo(x, y)
            else:
                curve_path.lineTo(x, y)
            fill_path.lineTo(x, y)
        fill_path.lineTo(plot_right, plot_bottom)
        fill_path.closeSubpath()

        fill_col = QColor(accent.red(), accent.green(), accent.blue(), 46)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill_col)
        painter.drawPath(fill_path)
        painter.setPen(QPen(accent, 2.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(curve_path)

        # Live dot riding the curve synchronized with the singing character.
        if active_dot_u is not None:
            dot_x = plot_left + plot_w * active_dot_u
            dot_val = self._curve_eased_at(active_dot_u, level, split)
            dot_y = plot_bottom - plot_h * dot_val
            painter.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 110), 1.0, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(dot_x, dot_y), QPointF(dot_x, plot_bottom))
            painter.setPen(QPen(QColor("#FFFFFF"), 1.6))
            painter.setBrush(accent)
            painter.drawEllipse(QPointF(dot_x, dot_y), 3.6, 3.6)

        # Status badge update.
        if active_dot_u is not None and active_scale > 1.01:
            status = (f"当前 {active_scale:.2f}×", True)
        elif self._raw_peak_pct <= 100:
            status = (f"演示 {peak_ratio:.2f}×", False)
        else:
            status = (f"峰值 {peak_ratio:.2f}×", False)
        if status != self._last_status:
            self._last_status = status
            if self._on_status_change is not None:
                self._on_status_change(status[0], status[1])

        painter.restore()
        painter.end()


class ZoomPulseCurvePopover(QFrame):
    """Floating theme-aware card displaying zoom-pulse curve previews beside dropdowns."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setObjectName("ZoomPulseCurvePopover")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self._title_label = QLabel("", self)
        self._title_label.setObjectName("ZoomPulseCurveTitle")
        header.addWidget(self._title_label)
        header.addStretch(1)
        self._status_label = QLabel(" ", self)
        self._status_label.setObjectName("ZoomPulseCurveStatus")
        self._status_label.setFixedHeight(20)
        self._status_highlighted = False
        header.addWidget(self._status_label)
        layout.addLayout(header)

        self._canvas = ZoomPulseCurvePreviewCanvas(
            1,
            125,
            self,
            on_status_change=self._update_status_badge,
        )
        layout.addWidget(self._canvas)

        self._summary_label = QLabel("", self)
        self._summary_label.setObjectName("ZoomPulseCurveSummary")
        self._summary_label.setWordWrap(True)
        self._summary_label.setFixedWidth(self._canvas.width())
        layout.addWidget(self._summary_label)

        themed(self, self._build_qss)
        self.adjustSize()

    @property
    def canvas(self) -> ZoomPulseCurvePreviewCanvas:
        return self._canvas

    def _build_qss(self) -> str:
        p = palette()
        dark = bool(getattr(p, "is_dark", False))
        if self._status_highlighted:
            accent = QColor(p.accent_primary)
            alpha = 32 if not dark else 52
            pill_bg = f"rgba({accent.red()}, {accent.green()}, {accent.blue()}, {alpha})"
            pill_border = p.accent_primary
            pill_fg = p.accent_primary if not dark else "#FF8A9A"
        else:
            pill_bg = p.preview_bg
            pill_border = p.card_border
            pill_fg = p.text_secondary
        return (
            f"""
            QFrame#ZoomPulseCurvePopover {{
                background: transparent;
                border: none;
            }}
            QLabel#ZoomPulseCurveTitle {{
                color: {p.title_text};
                font-size: 9.5pt;
                font-weight: 700;
                background: transparent;
            }}
            QLabel#ZoomPulseCurveStatus {{
                color: {pill_fg};
                background: {pill_bg};
                border: 1px solid {pill_border};
                border-radius: 9px;
                padding: 1px 8px;
                font-size: 8pt;
                font-weight: 600;
            }}
            QLabel#ZoomPulseCurveSummary {{
                color: {p.text_secondary};
                font-size: 8.5pt;
                background: transparent;
            }}
            """
        )

    def _update_status_badge(self, text: str, highlighted: bool) -> None:
        self._status_label.setText(text)
        if highlighted != self._status_highlighted:
            self._status_highlighted = highlighted
            self.setStyleSheet(self._build_qss())
        self._status_label.adjustSize()
        if self.layout() is not None:
            self.layout().activate()

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setPen(QPen(QColor(p.input_border_hover), 1.0))
        painter.setBrush(QColor(p.card_bg))
        painter.drawRoundedRect(card, 10.0, 10.0)
        painter.end()

    def set_curve(
        self,
        curve_level: int,
        *,
        peak_pct: int = 125,
        display_label: str = "",
        style: Any = None,
    ) -> None:
        meta = zoom_pulse_curve_meta(curve_level)
        self._title_label.setText(f"整字放大 · {display_label or meta.title}")
        self._summary_label.setText(meta.summary)
        self._canvas.set_curve(curve_level, peak_pct=peak_pct, style=style)
        self.adjustSize()

    def show_beside(self, menu_rect: QRect, row_rect: QRect) -> None:
        self.adjustSize()
        pos = self._compute_position(menu_rect, row_rect)
        self.move(pos)
        if not self.isVisible():
            self.show()
        self.raise_()
        self._canvas.start_animation()

    def _compute_position(self, menu_rect: QRect, row_rect: QRect) -> QPoint:
        pop_w = self.width() or self.sizeHint().width()
        pop_h = self.height() or self.sizeHint().height()
        gap = 8

        anchor_pt = row_rect.center() if not row_rect.isNull() else menu_rect.center()
        screen = QApplication.screenAt(anchor_pt) or QApplication.primaryScreen()
        if screen is None:
            return QPoint(menu_rect.right() + gap, row_rect.center().y() - pop_h // 2)

        avail = screen.availableGeometry()
        right_x = menu_rect.right() + gap
        left_x = menu_rect.left() - pop_w - gap
        if right_x + pop_w <= avail.right() - 8:
            x = right_x
        elif left_x >= avail.left() + 8:
            x = left_x
        else:
            x = max(avail.left() + 8, min(avail.right() - pop_w - 8, right_x))

        ideal_y = (row_rect.center().y() if not row_rect.isNull() else menu_rect.center().y()) - pop_h // 2
        y = max(avail.top() + 8, min(avail.bottom() - pop_h - 8, ideal_y))
        return QPoint(x, y)

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self._canvas.stop_animation()
        super().hideEvent(event)


class ZoomPulseCurveComboMenu(ComboBoxMenu):
    """Fluent dropdown menu showing live zoom-pulse previews on item hover."""

    _PREVIEW_SHOW_DELAY_MS = 110
    _PREVIEW_HIDE_DELAY_MS = 60
    _PREVIEW_CURSOR_POLL_MS = 50

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        peak_pct_provider: Optional[Callable[[], int]] = None,
        style_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__(parent=parent)
        self._peak_pct_provider = peak_pct_provider
        self._style_provider = style_provider
        self._row_levels: list[tuple[int, str]] = []
        self._combo_actions: dict[int, QAction] = {}
        self._preview_popover: Optional[ZoomPulseCurvePopover] = None
        self._hovered_row = -1
        self._pending_preview_row = -1

        self._preview_show_timer = QTimer(self)
        self._preview_show_timer.setSingleShot(True)
        self._preview_show_timer.setInterval(self._PREVIEW_SHOW_DELAY_MS)
        self._preview_show_timer.timeout.connect(self._show_pending_preview)

        self._preview_hide_timer = QTimer(self)
        self._preview_hide_timer.setSingleShot(True)
        self._preview_hide_timer.setInterval(self._PREVIEW_HIDE_DELAY_MS)
        self._preview_hide_timer.timeout.connect(self.hide_preview)

        self._preview_cursor_timer = QTimer(self)
        self._preview_cursor_timer.setInterval(self._PREVIEW_CURSOR_POLL_MS)
        self._preview_cursor_timer.timeout.connect(self._sync_preview_with_cursor)

        self.setMouseTracking(True)
        self.view.setMouseTracking(True)
        self.view.viewport().setMouseTracking(True)
        self.view.installEventFilter(self)
        self.view.viewport().installEventFilter(self)
        self.view.itemEntered.connect(self._on_list_item_entered)
        self.view.verticalScrollBar().valueChanged.connect(
            lambda _v: self._reposition_active_preview()
        )
        self.aboutToHide.connect(self.hide_preview)

    def add_curve_action(self, combo_index: int, level: int, action: QAction) -> None:
        self.addAction(action)
        self._row_levels.append((int(level), action.text()))
        self._combo_actions[combo_index] = action

    def action_for_combo_index(self, combo_index: int) -> Optional[QAction]:
        return self._combo_actions.get(combo_index)

    def row_curve_level(self, row: int) -> Optional[int]:
        if 0 <= row < len(self._row_levels):
            return self._row_levels[row][0]
        return None

    def preview_popover(self) -> Optional[ZoomPulseCurvePopover]:
        if self._preview_popover is not None and sip.isdeleted(self._preview_popover):
            self._preview_popover = None
        return self._preview_popover

    def _current_peak_pct(self) -> int:
        if self._peak_pct_provider is not None:
            try:
                return int(self._peak_pct_provider())
            except (TypeError, ValueError):
                pass
        return 125

    def _current_style(self) -> Any:
        if self._style_provider is not None:
            try:
                return self._style_provider()
            except Exception:
                return None
        return None

    def _ensure_preview_popover(self) -> ZoomPulseCurvePopover:
        pop = self.preview_popover()
        if pop is None:
            pop = ZoomPulseCurvePopover(None)
            self._preview_popover = pop

            def _cleanup(_obj: Any = None, target: ZoomPulseCurvePopover = pop) -> None:
                if not sip.isdeleted(target):
                    target.hide()
                    target.close()
                    target.deleteLater()

            self.destroyed.connect(_cleanup)
        return pop

    def _row_global_rect(self, row: int) -> QRect:
        item = self.view.item(row)
        if item is None or item.isHidden():
            return QRect()
        rect = self.view.visualItemRect(item)
        return QRect(self.view.viewport().mapToGlobal(rect.topLeft()), rect.size())

    def _menu_global_rect(self) -> QRect:
        return QRect(self.mapToGlobal(QPoint(0, 0)), self.size())

    def _row_under_cursor(self) -> int:
        if not self.isVisible():
            return -1
        viewport = self.view.viewport()
        local_pos = viewport.mapFromGlobal(QCursor.pos())
        if not viewport.rect().contains(local_pos):
            return -1
        item = self.view.itemAt(local_pos)
        if item is None or item.isHidden():
            return -1
        row = self.view.row(item)
        return row if 0 <= row < len(self._row_levels) else -1

    def preview_for_row(
        self,
        row: int,
        *,
        track_cursor: bool = False,
    ) -> Optional[ZoomPulseCurvePopover]:
        self._preview_show_timer.stop()
        self._preview_hide_timer.stop()
        if not (0 <= row < len(self._row_levels)):
            self.hide_preview()
            return None
        item = self.view.item(row)
        if item is None or item.isHidden():
            self.hide_preview()
            return None
        level, label = self._row_levels[row]
        self._hovered_row = row
        pop = self._ensure_preview_popover()
        pop.set_curve(
            level,
            peak_pct=self._current_peak_pct(),
            display_label=label,
            style=self._current_style(),
        )
        pop.show_beside(self._menu_global_rect(), self._row_global_rect(row))
        if track_cursor and not self._preview_cursor_timer.isActive():
            self._preview_cursor_timer.start()
        return pop

    def hide_preview(self) -> None:
        self._preview_show_timer.stop()
        self._preview_hide_timer.stop()
        self._preview_cursor_timer.stop()
        self._hovered_row = -1
        self._pending_preview_row = -1
        pop = self.preview_popover()
        if pop is not None and pop.isVisible():
            pop.hide()

    def _clear_viewport_hover(self) -> None:
        viewport = self.view.viewport()
        if viewport is not None and not sip.isdeleted(viewport):
            QApplication.sendEvent(viewport, QEvent(QEvent.Type.Leave))
            viewport.update()

    def _schedule_preview_for_row(self, row: int) -> None:
        if not (0 <= row < len(self._row_levels)):
            self._schedule_hide_preview()
            return
        self._preview_hide_timer.stop()
        if not self._preview_cursor_timer.isActive():
            self._preview_cursor_timer.start()
        pop = self.preview_popover()
        if row == self._hovered_row and pop is not None and pop.isVisible():
            return
        if pop is not None and pop.isVisible():
            self.preview_for_row(row, track_cursor=True)
            return
        self._pending_preview_row = row
        self._preview_show_timer.start()

    def _schedule_hide_preview(self) -> None:
        self._preview_show_timer.stop()
        self._pending_preview_row = -1
        pop = self.preview_popover()
        if pop is not None and pop.isVisible():
            self._preview_hide_timer.start()
        else:
            self._preview_cursor_timer.stop()

    def _show_pending_preview(self) -> None:
        row = self._pending_preview_row
        self._pending_preview_row = -1
        if row >= 0 and self.isVisible() and self._row_under_cursor() == row:
            self.preview_for_row(row, track_cursor=True)
        else:
            self.hide_preview()

    def _sync_preview_with_cursor(self) -> None:
        row = self._row_under_cursor()
        if row < 0:
            self.hide_preview()
            self._clear_viewport_hover()
            return
        pop = self.preview_popover()
        if pop is not None and pop.isVisible():
            if row != self._hovered_row:
                self.preview_for_row(row, track_cursor=True)
        elif self._pending_preview_row >= 0 and row != self._pending_preview_row:
            self._pending_preview_row = row

    def _reposition_active_preview(self) -> None:
        pop = self.preview_popover()
        if pop is None or not pop.isVisible() or self._hovered_row < 0:
            return
        row_rect = self._row_global_rect(self._hovered_row)
        if row_rect.isNull():
            self.hide_preview()
            return
        pop.move(pop._compute_position(self._menu_global_rect(), row_rect))

    def _on_list_item_entered(self, item: Any) -> None:
        if item is None:
            self._schedule_hide_preview()
            return
        self._schedule_preview_for_row(self.view.row(item))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self.view.viewport():
            etype = event.type()
            if etype == QEvent.Type.MouseMove:
                item = self.view.itemAt(event.position().toPoint())
                if item is None:
                    self.hide_preview()
                else:
                    self._schedule_preview_for_row(self.view.row(item))
            elif etype in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
                self.hide_preview()
        elif watched is self.view:
            if event.type() in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
                self.hide_preview()
        return super().eventFilter(watched, event)

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802
        if self._row_under_cursor() < 0:
            self.hide_preview()
            self._clear_viewport_hover()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802
        self.hide_preview()
        self._clear_viewport_hover()
        super().leaveEvent(event)

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self.hide_preview()
        super().hideEvent(event)

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        self.hide_preview()
        super().closeEvent(event)

    def exec(self, pos, ani=True, aniType=MenuAnimationType.DROP_DOWN):  # noqa: A003
        self.view.adjustSize(pos, MenuAnimationType.NONE)
        self.adjustSize()
        return super().exec(pos, False, MenuAnimationType.NONE)


class ZoomPulseCurveComboBox(WheelFocusedComboBox):
    """Combo box for zoom-pulse speed levels with live hover curve previews."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        peak_pct_provider: Optional[Callable[[], int]] = None,
        style_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__(parent)
        self._peak_pct_provider = peak_pct_provider
        self._style_provider = style_provider

    def set_peak_pct_provider(self, provider: Optional[Callable[[], int]]) -> None:
        self._peak_pct_provider = provider

    def set_style_provider(self, provider: Optional[Callable[[], Any]]) -> None:
        self._style_provider = provider

    def build_preview_menu(self) -> ZoomPulseCurveComboMenu:
        menu = ZoomPulseCurveComboMenu(
            self,
            peak_pct_provider=self._peak_pct_provider,
            style_provider=self._style_provider,
        )
        for idx, item in enumerate(self.items):
            level = int(item.userData) if item.userData is not None else idx
            action = QAction(
                item.icon,
                item.text,
                menu,
                triggered=lambda _c=False, x=idx: self._onItemClicked(x),
            )
            action.setEnabled(item.isEnabled)
            menu.add_curve_action(idx, level, action)

        menu.view.setMinimumWidth(max(self.width(), 180))
        menu.adjustSize()
        current = self.currentIndex()
        if current >= 0:
            default_action = menu.action_for_combo_index(current)
            if default_action is not None:
                menu.setDefaultAction(default_action)
        return menu

    def _showComboMenu(self) -> None:  # noqa: N802 - qfluentwidgets override
        if not self.items:
            return
        menu = self.build_preview_menu()
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.closedSignal.connect(self._onDropMenuClosed)
        self.dropMenu = menu

        x = -menu.width() // 2 + menu.layout().contentsMargins().left() + self.width() // 2
        pd = self.mapToGlobal(QPoint(x, self.height()))
        hd = menu.view.heightForAnimation(pd, MenuAnimationType.DROP_DOWN)
        pu = self.mapToGlobal(QPoint(x, 0))
        hu = menu.view.heightForAnimation(pu, MenuAnimationType.PULL_UP)

        if hd >= hu:
            menu.exec(pd, aniType=MenuAnimationType.DROP_DOWN)
        else:
            menu.view.adjustSize(pu, MenuAnimationType.NONE)
            menu.adjustSize()
            top_pos = QPoint(pu.x(), max(0, pu.y() - menu.height()))
            menu.exec(top_pos, aniType=MenuAnimationType.PULL_UP)


class PreviewPopoverFrame(QFrame):
    """Reusable theme-aware floating card shell hosting any animated preview canvas."""

    def __init__(
        self,
        canvas: QWidget,
        parent: Optional[QWidget] = None,
        *,
        object_name: str = "EffectPreviewPopover",
    ) -> None:
        super().__init__(
            parent,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self._object_name = object_name
        self.setObjectName(object_name)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self._title_label = QLabel("", self)
        self._title_label.setObjectName(f"{object_name}Title")
        header.addWidget(self._title_label)
        header.addStretch(1)
        self._status_label = QLabel(" ", self)
        self._status_label.setObjectName(f"{object_name}Status")
        self._status_label.setFixedHeight(20)
        self._status_highlighted = False
        header.addWidget(self._status_label)
        layout.addLayout(header)

        self._canvas = canvas
        self._canvas.setParent(self)
        if hasattr(self._canvas, "set_status_callback"):
            self._canvas.set_status_callback(self._update_status_badge)
        layout.addWidget(self._canvas)

        self._summary_label = QLabel("", self)
        self._summary_label.setObjectName(f"{object_name}Summary")
        self._summary_label.setWordWrap(True)
        self._summary_label.setFixedWidth(self._canvas.width())
        layout.addWidget(self._summary_label)

        themed(self, self._build_qss)
        self.adjustSize()

    @property
    def canvas(self) -> QWidget:
        return self._canvas

    def set_header_and_summary(self, title: str, summary: str) -> None:
        self._title_label.setText(title)
        self._summary_label.setText(summary)
        self.adjustSize()

    def _build_qss(self) -> str:
        p = palette()
        dark = bool(getattr(p, "is_dark", False))
        if self._status_highlighted:
            accent = QColor(p.accent_primary)
            alpha = 32 if not dark else 52
            pill_bg = f"rgba({accent.red()}, {accent.green()}, {accent.blue()}, {alpha})"
            pill_border = p.accent_primary
            pill_fg = p.accent_primary if not dark else "#FF8A9A"
        else:
            pill_bg = p.preview_bg
            pill_border = p.card_border
            pill_fg = p.text_secondary
        name = self._object_name
        return (
            f"""
            QFrame#{name} {{
                background: transparent;
                border: none;
            }}
            QLabel#{name}Title {{
                color: {p.title_text};
                font-size: 9.5pt;
                font-weight: 700;
                background: transparent;
            }}
            QLabel#{name}Status {{
                color: {pill_fg};
                background: {pill_bg};
                border: 1px solid {pill_border};
                border-radius: 9px;
                padding: 1px 8px;
                font-size: 8pt;
                font-weight: 600;
            }}
            QLabel#{name}Summary {{
                color: {p.text_secondary};
                font-size: 8.5pt;
                background: transparent;
            }}
            """
        )

    def _update_status_badge(self, text: str, highlighted: bool) -> None:
        self._status_label.setText(text)
        if highlighted != self._status_highlighted:
            self._status_highlighted = highlighted
            self.setStyleSheet(self._build_qss())
        self._status_label.adjustSize()
        if self.layout() is not None:
            self.layout().activate()

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setPen(QPen(QColor(p.input_border_hover), 1.0))
        painter.setBrush(QColor(p.card_bg))
        painter.drawRoundedRect(card, 10.0, 10.0)
        painter.end()

    def show_beside(self, menu_rect: QRect, row_rect: QRect) -> None:
        self.adjustSize()
        pos = self._compute_position(menu_rect, row_rect)
        self.move(pos)
        if not self.isVisible():
            self.show()
        self.raise_()
        if hasattr(self._canvas, "start_animation"):
            self._canvas.start_animation()

    def show_for_anchor(self, anchor: QWidget) -> None:
        self.adjustSize()
        pop_w = self.width() or self.sizeHint().width()
        pop_h = self.height() or self.sizeHint().height()
        gap = 8
        top_left = anchor.mapToGlobal(QPoint(0, 0))
        anchor_rect = QRect(top_left, anchor.size())
        screen = QApplication.screenAt(anchor_rect.center()) or QApplication.primaryScreen()
        if screen is None:
            pos = QPoint(anchor_rect.left(), anchor_rect.bottom() + gap)
        else:
            avail = screen.availableGeometry()
            x = max(avail.left() + 8, min(avail.right() - pop_w - 8, anchor_rect.center().x() - pop_w // 2))
            below_y = anchor_rect.bottom() + gap
            above_y = anchor_rect.top() - pop_h - gap
            if below_y + pop_h <= avail.bottom() - 8:
                y = below_y
            elif above_y >= avail.top() + 8:
                y = above_y
            else:
                y = max(avail.top() + 8, min(avail.bottom() - pop_h - 8, below_y))
            pos = QPoint(x, y)
        self.move(pos)
        if not self.isVisible():
            self.show()
        self.raise_()
        if hasattr(self._canvas, "start_animation"):
            self._canvas.start_animation()

    def _compute_position(self, menu_rect: QRect, row_rect: QRect) -> QPoint:
        pop_w = self.width() or self.sizeHint().width()
        pop_h = self.height() or self.sizeHint().height()
        gap = 8

        anchor_pt = row_rect.center() if not row_rect.isNull() else menu_rect.center()
        screen = QApplication.screenAt(anchor_pt) or QApplication.primaryScreen()
        if screen is None:
            return QPoint(menu_rect.right() + gap, row_rect.center().y() - pop_h // 2)

        avail = screen.availableGeometry()
        right_x = menu_rect.right() + gap
        left_x = menu_rect.left() - pop_w - gap
        if right_x + pop_w <= avail.right() - 8:
            x = right_x
        elif left_x >= avail.left() + 8:
            x = left_x
        else:
            x = max(avail.left() + 8, min(avail.right() - pop_w - 8, right_x))

        ideal_y = (row_rect.center().y() if not row_rect.isNull() else menu_rect.center().y()) - pop_h // 2
        y = max(avail.top() + 8, min(avail.bottom() - pop_h - 8, ideal_y))
        return QPoint(x, y)

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        if hasattr(self._canvas, "stop_animation"):
            self._canvas.stop_animation()
        super().hideEvent(event)


class HoverPreviewComboMenu(ComboBoxMenu):
    """Generic Fluent dropdown menu with reliable Windows hover preview + cursor watchdog."""

    _PREVIEW_SHOW_DELAY_MS = 110
    _PREVIEW_HIDE_DELAY_MS = 60
    _PREVIEW_CURSOR_POLL_MS = 50

    def __init__(
        self,
        popover_factory: Callable[[], PreviewPopoverFrame],
        configure_popover: Callable[[PreviewPopoverFrame, Any, str], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent=parent)
        self._popover_factory = popover_factory
        self._configure_popover = configure_popover
        self._row_payloads: list[tuple[Any, str]] = []
        self._combo_actions: dict[int, QAction] = {}
        self._preview_popover: Optional[PreviewPopoverFrame] = None
        self._hovered_row = -1
        self._pending_preview_row = -1

        self._preview_show_timer = QTimer(self)
        self._preview_show_timer.setSingleShot(True)
        self._preview_show_timer.setInterval(self._PREVIEW_SHOW_DELAY_MS)
        self._preview_show_timer.timeout.connect(self._show_pending_preview)

        self._preview_hide_timer = QTimer(self)
        self._preview_hide_timer.setSingleShot(True)
        self._preview_hide_timer.setInterval(self._PREVIEW_HIDE_DELAY_MS)
        self._preview_hide_timer.timeout.connect(self.hide_preview)

        self._preview_cursor_timer = QTimer(self)
        self._preview_cursor_timer.setInterval(self._PREVIEW_CURSOR_POLL_MS)
        self._preview_cursor_timer.timeout.connect(self._sync_preview_with_cursor)

        self.setMouseTracking(True)
        self.view.setMouseTracking(True)
        self.view.viewport().setMouseTracking(True)
        self.view.installEventFilter(self)
        self.view.viewport().installEventFilter(self)
        self.view.itemEntered.connect(self._on_list_item_entered)
        self.view.verticalScrollBar().valueChanged.connect(
            lambda _v: self._reposition_active_preview()
        )
        self.aboutToHide.connect(self.hide_preview)

    def add_preview_action(self, combo_index: int, item_data: Any, action: QAction) -> None:
        self.addAction(action)
        self._row_payloads.append((item_data, action.text()))
        self._combo_actions[combo_index] = action

    def action_for_combo_index(self, combo_index: int) -> Optional[QAction]:
        return self._combo_actions.get(combo_index)

    def row_item_data(self, row: int) -> Any:
        if 0 <= row < len(self._row_payloads):
            return self._row_payloads[row][0]
        return None

    def preview_popover(self) -> Optional[PreviewPopoverFrame]:
        if self._preview_popover is not None and sip.isdeleted(self._preview_popover):
            self._preview_popover = None
        return self._preview_popover

    def _ensure_preview_popover(self) -> PreviewPopoverFrame:
        pop = self.preview_popover()
        if pop is None:
            pop = self._popover_factory()
            self._preview_popover = pop

            def _cleanup(_obj: Any = None, target: PreviewPopoverFrame = pop) -> None:
                if not sip.isdeleted(target):
                    target.hide()
                    target.close()
                    target.deleteLater()

            self.destroyed.connect(_cleanup)
        return pop

    def _row_global_rect(self, row: int) -> QRect:
        item = self.view.item(row)
        if item is None or item.isHidden():
            return QRect()
        rect = self.view.visualItemRect(item)
        return QRect(self.view.viewport().mapToGlobal(rect.topLeft()), rect.size())

    def _menu_global_rect(self) -> QRect:
        return QRect(self.mapToGlobal(QPoint(0, 0)), self.size())

    def _row_under_cursor(self) -> int:
        if not self.isVisible():
            return -1
        viewport = self.view.viewport()
        local_pos = viewport.mapFromGlobal(QCursor.pos())
        if not viewport.rect().contains(local_pos):
            return -1
        item = self.view.itemAt(local_pos)
        if item is None or item.isHidden():
            return -1
        row = self.view.row(item)
        return row if 0 <= row < len(self._row_payloads) else -1

    def preview_for_row(
        self,
        row: int,
        *,
        track_cursor: bool = False,
    ) -> Optional[PreviewPopoverFrame]:
        self._preview_show_timer.stop()
        self._preview_hide_timer.stop()
        if not (0 <= row < len(self._row_payloads)):
            self.hide_preview()
            return None
        item = self.view.item(row)
        if item is None or item.isHidden():
            self.hide_preview()
            return None
        item_data, label = self._row_payloads[row]
        self._hovered_row = row
        pop = self._ensure_preview_popover()
        self._configure_popover(pop, item_data, label)
        pop.show_beside(self._menu_global_rect(), self._row_global_rect(row))
        if track_cursor and not self._preview_cursor_timer.isActive():
            self._preview_cursor_timer.start()
        return pop

    def hide_preview(self) -> None:
        self._preview_show_timer.stop()
        self._preview_hide_timer.stop()
        self._preview_cursor_timer.stop()
        self._hovered_row = -1
        self._pending_preview_row = -1
        pop = self.preview_popover()
        if pop is not None and pop.isVisible():
            pop.hide()

    def _clear_viewport_hover(self) -> None:
        viewport = self.view.viewport()
        if viewport is not None and not sip.isdeleted(viewport):
            QApplication.sendEvent(viewport, QEvent(QEvent.Type.Leave))
            viewport.update()

    def _schedule_preview_for_row(self, row: int) -> None:
        if not (0 <= row < len(self._row_payloads)):
            self._schedule_hide_preview()
            return
        self._preview_hide_timer.stop()
        if not self._preview_cursor_timer.isActive():
            self._preview_cursor_timer.start()
        pop = self.preview_popover()
        if row == self._hovered_row and pop is not None and pop.isVisible():
            return
        if pop is not None and pop.isVisible():
            self.preview_for_row(row, track_cursor=True)
            return
        self._pending_preview_row = row
        self._preview_show_timer.start()

    def _schedule_hide_preview(self) -> None:
        self._preview_show_timer.stop()
        self._pending_preview_row = -1
        pop = self.preview_popover()
        if pop is not None and pop.isVisible():
            self._preview_hide_timer.start()
        else:
            self._preview_cursor_timer.stop()

    def _show_pending_preview(self) -> None:
        row = self._pending_preview_row
        self._pending_preview_row = -1
        if row >= 0 and self.isVisible() and self._row_under_cursor() == row:
            self.preview_for_row(row, track_cursor=True)
        else:
            self.hide_preview()

    def _sync_preview_with_cursor(self) -> None:
        row = self._row_under_cursor()
        if row < 0:
            self.hide_preview()
            self._clear_viewport_hover()
            return
        pop = self.preview_popover()
        if pop is not None and pop.isVisible():
            if row != self._hovered_row:
                self.preview_for_row(row, track_cursor=True)
        elif self._pending_preview_row >= 0 and row != self._pending_preview_row:
            self._pending_preview_row = row

    def _reposition_active_preview(self) -> None:
        pop = self.preview_popover()
        if pop is None or not pop.isVisible() or self._hovered_row < 0:
            return
        row_rect = self._row_global_rect(self._hovered_row)
        if row_rect.isNull():
            self.hide_preview()
            return
        pop.move(pop._compute_position(self._menu_global_rect(), row_rect))

    def _on_list_item_entered(self, item: Any) -> None:
        if item is None:
            self._schedule_hide_preview()
            return
        self._schedule_preview_for_row(self.view.row(item))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self.view.viewport():
            etype = event.type()
            if etype == QEvent.Type.MouseMove:
                item = self.view.itemAt(event.position().toPoint())
                if item is None:
                    self.hide_preview()
                else:
                    self._schedule_preview_for_row(self.view.row(item))
            elif etype in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
                self.hide_preview()
        elif watched is self.view:
            if event.type() in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
                self.hide_preview()
        return super().eventFilter(watched, event)

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802
        if self._row_under_cursor() < 0:
            self.hide_preview()
            self._clear_viewport_hover()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802
        self.hide_preview()
        self._clear_viewport_hover()
        super().leaveEvent(event)

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self.hide_preview()
        super().hideEvent(event)

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        self.hide_preview()
        super().closeEvent(event)

    def exec(self, pos, ani=True, aniType=MenuAnimationType.DROP_DOWN):  # noqa: A003
        self.view.adjustSize(pos, MenuAnimationType.NONE)
        self.adjustSize()
        return super().exec(pos, False, MenuAnimationType.NONE)


class HoverPreviewComboBox(WheelFocusedComboBox):
    """Combo box whose dropdown rows display live animated hover previews."""

    def __init__(
        self,
        popover_factory: Callable[[], PreviewPopoverFrame],
        configure_popover: Callable[[PreviewPopoverFrame, Any, str], None],
        parent: Optional[QWidget] = None,
        *,
        min_menu_width: int = 180,
    ) -> None:
        super().__init__(parent)
        self._popover_factory = popover_factory
        self._configure_popover = configure_popover
        self._min_menu_width = int(min_menu_width)

    def build_preview_menu(self) -> HoverPreviewComboMenu:
        menu = HoverPreviewComboMenu(
            self._popover_factory,
            self._configure_popover,
            self,
        )
        for idx, item in enumerate(self.items):
            action = QAction(
                item.icon,
                item.text,
                menu,
                triggered=lambda _c=False, x=idx: self._onItemClicked(x),
            )
            action.setEnabled(item.isEnabled)
            menu.add_preview_action(idx, item.userData, action)

        menu.view.setMinimumWidth(max(self.width(), self._min_menu_width))
        menu.adjustSize()
        current = self.currentIndex()
        if current >= 0:
            default_action = menu.action_for_combo_index(current)
            if default_action is not None:
                menu.setDefaultAction(default_action)
        return menu

    def _showComboMenu(self) -> None:  # noqa: N802 - qfluentwidgets override
        if not self.items:
            return
        menu = self.build_preview_menu()
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.closedSignal.connect(self._onDropMenuClosed)
        self.dropMenu = menu

        x = -menu.width() // 2 + menu.layout().contentsMargins().left() + self.width() // 2
        pd = self.mapToGlobal(QPoint(x, self.height()))
        hd = menu.view.heightForAnimation(pd, MenuAnimationType.DROP_DOWN)
        pu = self.mapToGlobal(QPoint(x, 0))
        hu = menu.view.heightForAnimation(pu, MenuAnimationType.PULL_UP)

        if hd >= hu:
            menu.exec(pd, aniType=MenuAnimationType.DROP_DOWN)
        else:
            menu.view.adjustSize(pu, MenuAnimationType.NONE)
            menu.adjustSize()
            top_pos = QPoint(pu.x(), max(0, pu.y() - menu.height()))
            menu.exec(top_pos, aniType=MenuAnimationType.PULL_UP)


_WIDGET_PREVIEW_HOVER_ATTR = "_krok_widget_preview_hover_filter"
_FLUENT_TOOLTIP_FILTER_ATTR = "_strange_uta_game_fluent_tooltip_filter"


class WidgetPreviewHoverFilter(QObject):
    """Show a :class:`PreviewPopoverFrame` directly when hovering a widget (e.g. CheckBox)."""

    _DEFAULT_SHOW_DELAY_MS = 220
    _HIDE_DELAY_MS = 100

    def __init__(
        self,
        target: QWidget,
        popover_factory: Callable[[], PreviewPopoverFrame],
        configure_popover: Callable[[PreviewPopoverFrame], None],
        *,
        show_delay_ms: int = _DEFAULT_SHOW_DELAY_MS,
    ) -> None:
        super().__init__(target)
        self._target = target
        self._popover_factory = popover_factory
        self._configure_popover = configure_popover
        self._popover: Optional[PreviewPopoverFrame] = None

        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.setInterval(max(0, int(show_delay_ms)))
        self._show_timer.timeout.connect(self.show_popover)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(self._HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self._hide_if_unhovered)

        target.setMouseTracking(True)
        target.installEventFilter(self)

    def popover(self) -> Optional[PreviewPopoverFrame]:
        if self._popover is not None and sip.isdeleted(self._popover):
            self._popover = None
        return self._popover

    def _is_cursor_inside(self, widget: Optional[QWidget]) -> bool:
        if widget is None or sip.isdeleted(widget) or not widget.isVisible():
            return False
        local_pos = widget.mapFromGlobal(QCursor.pos())
        return widget.rect().contains(local_pos)

    def _ensure_popover(self) -> PreviewPopoverFrame:
        pop = self.popover()
        if pop is None:
            pop = self._popover_factory()
            self._popover = pop

            def _cleanup(_obj: Any = None, target: PreviewPopoverFrame = pop) -> None:
                if not sip.isdeleted(target):
                    target.hide()
                    target.close()
                    target.deleteLater()

            self._target.destroyed.connect(_cleanup)
        return pop

    def show_popover(self) -> None:
        self._show_timer.stop()
        self._hide_timer.stop()
        if sip.isdeleted(self._target) or not self._target.isVisible():
            return
        pop = self._ensure_popover()
        self._configure_popover(pop)
        pop.show_for_anchor(self._target)

    def hide_popover(self) -> None:
        self._show_timer.stop()
        self._hide_timer.stop()
        pop = self.popover()
        if pop is not None and pop.isVisible():
            pop.hide()

    def _hide_if_unhovered(self) -> None:
        if self._is_cursor_inside(self._target):
            return
        self.hide_popover()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is not self._target:
            return super().eventFilter(watched, event)
        etype = event.type()
        if etype == QEvent.Type.ToolTip:
            event.accept()
            return True
        if etype in (QEvent.Type.Enter, QEvent.Type.HoverEnter):
            fluent_filter = getattr(self._target, _FLUENT_TOOLTIP_FILTER_ATTR, None)
            if fluent_filter is not None:
                self._target.removeEventFilter(fluent_filter)
            self._hide_timer.stop()
            self._show_timer.start()
        elif etype in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
            self._show_timer.stop()
            self._hide_timer.start()
        elif etype in (QEvent.Type.MouseButtonPress, QEvent.Type.Hide, QEvent.Type.Wheel):
            self.hide_popover()
        return super().eventFilter(watched, event)


def install_widget_preview_hover(
    widget: QWidget,
    popover_factory: Callable[[], PreviewPopoverFrame],
    configure_popover: Callable[[PreviewPopoverFrame], None],
    *,
    show_delay_ms: int = WidgetPreviewHoverFilter._DEFAULT_SHOW_DELAY_MS,
) -> WidgetPreviewHoverFilter:
    """Attach a hover-triggered animated preview popover to a standalone widget."""
    existing_fluent = getattr(widget, _FLUENT_TOOLTIP_FILTER_ATTR, None)
    if existing_fluent is not None:
        widget.removeEventFilter(existing_fluent)

    existing = getattr(widget, _WIDGET_PREVIEW_HOVER_ATTR, None)
    if isinstance(existing, WidgetPreviewHoverFilter):
        existing.hide_popover()
        widget.removeEventFilter(existing)

    hover_filter = WidgetPreviewHoverFilter(
        widget,
        popover_factory,
        configure_popover,
        show_delay_ms=show_delay_ms,
    )
    setattr(widget, _WIDGET_PREVIEW_HOVER_ATTR, hover_filter)
    return hover_filter


__all__ = [
    "ANIMATION_PREVIEW_LYRIC",
    "ANIMATION_PREVIEW_SING_CHARS",
    "ANIMATION_PREVIEW_SING_DURATION_MS",
    "AnimationPreviewCanvas",
    "AnimationPreviewMeta",
    "AnimationPreviewPopover",
    "ENTRY_ANIMATION_META",
    "EXIT_ANIMATION_META",
    "HoverPreviewComboBox",
    "HoverPreviewComboMenu",
    "PreviewPopoverFrame",
    "PreviewRubyLayout",
    "SampleSingChar",
    "WidgetPreviewHoverFilter",
    "ZOOM_PULSE_CURVE_META",
    "ZoomPulseCurveComboBox",
    "ZoomPulseCurveComboMenu",
    "ZoomPulseCurvePopover",
    "ZoomPulseCurvePreviewCanvas",
    "animation_preview_meta",
    "build_preview_ruby_layouts",
    "draw_preview_sprite",
    "install_widget_preview_hover",
    "paint_styled_karaoke_glyph",
    "paint_styled_ruby_for_char",
    "resolve_preview_ruby_appearance",
    "sample_ruby_kana_progress",
    "sample_sing_wipe_progress",
    "zoom_pulse_curve_meta",
]
