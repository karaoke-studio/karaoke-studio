"""Animated hover preview popover for subtitle entry/exit animation items.

Hovering an item inside :class:`GroupedComboBoxMenu` opens a floating,
input-transparent, theme-aware card next to the dropdown list that loops a
live QPainter preview of the hovered animation using the sample lyric
``渇いた 心で駆け抜ける``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from PyQt6.QtCore import (
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QTimer,
    Qt,
)
from PyQt6.QtGui import (
    QColor,
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

from krok_helper.subtitle_render.engine.render.effects.particles import FX_SPRITES
from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
    fx_unit_hash,
    spin_flip_skew,
)
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed


ANIMATION_PREVIEW_LYRIC = "渇いた 心で駆け抜ける"


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

        # Subtle stage baseline & frame guides.
        baseline_y = self.height() * 0.56 + self._ascent * 0.34
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
    ) -> None:
        if path.isEmpty():
            return
        pen = QPen(stroke_color, stroke_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill_color)
        painter.drawPath(path)

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
                self._draw_styled_glyph(painter, path, fill_color=fill_base, stroke_color=stroke_base)
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
                self._draw_styled_glyph(painter, path, fill_color=fill_base, stroke_color=stroke_base)
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
                    self._draw_styled_glyph(painter, path, fill_color=fill_base, stroke_color=stroke_base)
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
                bounds = path.boundingRect().adjusted(-2.5, -2.5, 2.5, 2.5)
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
                self._draw_styled_glyph(painter, path, fill_color=fill_base, stroke_color=stroke_base)
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
            if halo > 0.02:
                for width_mul, strength in ((8.5, 0.16), (4.8, 0.30), (2.2, 0.45)):
                    painter.save()
                    painter.setTransform(tr, combine=True)
                    painter.setOpacity(_clamp01(halo * strength * env_alpha))
                    halo_col = QColor(accent.red(), accent.green(), accent.blue(), 255)
                    painter.setPen(QPen(halo_col, width_mul, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPath(path)
                    painter.restore()

            if opacity > 0.005:
                painter.save()
                painter.setTransform(tr, combine=True)
                painter.setOpacity(_clamp01(opacity * env_alpha))
                self._draw_styled_glyph(painter, path, fill_color=fill_base, stroke_color=stroke_base)
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


__all__ = [
    "ANIMATION_PREVIEW_LYRIC",
    "AnimationPreviewCanvas",
    "AnimationPreviewMeta",
    "AnimationPreviewPopover",
    "ENTRY_ANIMATION_META",
    "EXIT_ANIMATION_META",
    "animation_preview_meta",
]
