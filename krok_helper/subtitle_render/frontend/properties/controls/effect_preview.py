"""Animated hover previews for core visual effects on the Effects property page.

Covers:
- Karaoke singing animations (``karaoke_anim``) & reverse singing animations
  (``reverse_karaoke_anim``)
- Singing stroke flash (``stroke_flash`` checkbox hover)
- Scanline shapes (``scanline_shape``) & scanline trailing particle effects
  (``scanline_effect``)
- Singing decorative particles (``sing_fx``)
- Countdown lit indicator shapes (``lit_style``) & extinguish transitions
  (``lit_transition_mode``)

All singing previews share the timed sample lyric ``ANIMATION_PREVIEW_SING_CHARS``
(``渇いた 心で駆け抜ける``) and apply the user's current global default color
scheme via ``style_provider``.
"""

from __future__ import annotations

from dataclasses import replace
import math
from typing import Any, Callable, Optional

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
    QTransform,
)
from PyQt6.QtWidgets import QWidget

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.engine.render.effects.particles import (
    FALL_CHAR_SEED_SALT,
    SCANLINE_FX_DEFAULT_PALETTES,
    SCANLINE_FX_SPRITES,
    SCANLINE_FX_TAIL_MS,
    SCANLINE_FX_TRAVEL_SCALE,
    burst_particles_at,
    scanline_particles_per_char,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.scanline import (
    ScanlineParams,
    _scanline_column_palette,
    paint_scanline_column,
    scanline_feather_slices,
    scanline_params_for_style,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
    ZOOM_PULSE_SHRINK_MS,
    fx_unit_hash,
    utopia_wipe_scale,
    zoom_pulse_curve_level,
    zoom_pulse_peak_ratio,
    zoom_pulse_wipe_scale,
)
from krok_helper.subtitle_render.engine.render.elements.signal import (
    _lit_highlight_geometry,
    _lit_shape_path,
    cached_lit_image,
    shape_active_index_and_phase,
)
from krok_helper.subtitle_render.frontend.properties.controls.animation_preview import (
    ANIMATION_PREVIEW_SING_CHARS,
    ANIMATION_PREVIEW_SING_DURATION_MS,
    AnimationPreviewMeta,
    HoverPreviewComboBox,
    PreviewPopoverFrame,
    WidgetPreviewHoverFilter,
    _clamp01,
    _resolve_preview_text_appearance,
    _smoothstep,
    _ui_font,
    build_preview_ruby_layouts,
    draw_preview_sprite,
    install_widget_preview_hover,
    paint_styled_karaoke_glyph,
    paint_styled_ruby_for_char,
    resolve_preview_ruby_appearance,
    sample_ruby_kana_progress,
    sample_sing_wipe_progress,
)
from krok_helper.subtitle_render.frontend.widgets.theme import palette


# ------------------------------------------------------------------ metadata

KARAOKE_ANIM_META: dict[str, AnimationPreviewMeta] = {
    "none": AnimationPreviewMeta(
        "无（标准 Wipe）",
        "按打轴时间从左到右平滑扫色着色，是最经典的卡拉 OK 走字方式。",
    ),
    "no_wipe": AnimationPreviewMeta(
        "无 Wipe",
        "唱到每个字起唱瞬间整字直接切换为已唱配色，不做平滑扫色过渡。",
    ),
    "utopia": AnimationPreviewMeta(
        "utopia",
        "走字同时当前字快速放大弹出至 1.3×，并在唱字窗口内弹回原大小。",
    ),
    "zoom_pulse": AnimationPreviewMeta(
        "整字放大",
        "唱字期间沿设定曲线持续放大至峰值，唱完后 300ms 内平滑缩回原大。",
    ),
}

REVERSE_KARAOKE_ANIM_META: dict[str, AnimationPreviewMeta] = {
    "inherit": AnimationPreviewMeta(
        "跟随唱字特效",
        "反向唱字行直接沿用上方「唱字特效」的当前选择，从右向左反向走字。",
    ),
    "none": AnimationPreviewMeta(
        "Wipe",
        "反向唱字行按从右到左的顺序逐字平滑扫色着色。",
    ),
    "no_wipe": AnimationPreviewMeta(
        "无 Wipe",
        "反向唱字行从右到左逐字在起唱瞬间整字切色，不做扫色过渡。",
    ),
    "utopia": AnimationPreviewMeta(
        "Utopia",
        "反向唱字行从右到左走字，同时当前字弹出放大至 1.3× 并回落。",
    ),
    "zoom_pulse": AnimationPreviewMeta(
        "整字放大",
        "反向唱字行从右到左走字，唱字期间放大至峰值、唱完后 300ms 缩回。",
    ),
}

STROKE_FLASH_META = AnimationPreviewMeta(
    "唱字闪光",
    "每个字唱到的瞬间，描边沿字形轮廓闪白脉冲约 240ms 后回落；可与任意唱字特效叠加。",
)

SCANLINE_SHAPE_META: dict[str, AnimationPreviewMeta] = {
    "band": AnimationPreviewMeta(
        "普通高亮",
        "走字锋面叠加一条柔和亮光带，随歌词着色边界平滑扫过字形。",
    ),
    "flame": AnimationPreviewMeta(
        "烈焰棒",
        "走字锋面化作发光火棒，火舌围绕柱体旋转摇曳并迸出火星。",
    ),
    "bubble": AnimationPreviewMeta(
        "水泡",
        "走字锋面亮起浅蓝光柱，从底部持续冒出前后双层镂空水泡。",
    ),
    "image": AnimationPreviewMeta(
        "自定义图片",
        "在走字锋面锚点叠加自定义图片或动图素材，随走字实时移动。",
    ),
}

SCANLINE_EFFECT_META: dict[str, AnimationPreviewMeta] = {
    "none": AnimationPreviewMeta(
        "无",
        "仅保留扫字线本体，不沿锋面发射额外的拖尾装饰粒子。",
    ),
    "sakura": AnimationPreviewMeta(
        "樱花飘散",
        "沿走字锋面向后上方飞散旋转樱花花瓣，换字后继续拖尾消散。",
    ),
    "feather": AnimationPreviewMeta(
        "羽毛飘散",
        "沿走字锋面向后上方轻柔摇曳飘散洁白羽毛，富有流动速度感。",
    ),
    "snow": AnimationPreviewMeta(
        "雪花飘散·镂空",
        "沿走字锋面向后上方旋飞镂空雪花，换字后保留轻盈雪尾。",
    ),
    "snow_solid": AnimationPreviewMeta(
        "雪花飘散·实心",
        "沿走字锋面向后上方旋飞实心雪花，在浅色背景下更清晰醒目。",
    ),
    "star": AnimationPreviewMeta(
        "星光飞散",
        "沿走字锋面向后上方飞散闪烁的四角星光，勾勒明亮走字轨迹。",
    ),
    "note": AnimationPreviewMeta(
        "音符飘散",
        "沿走字锋面向后上方轻盈摇曳飘散小音符，增强律动氛围。",
    ),
}

SING_FX_META: dict[str, AnimationPreviewMeta] = {
    "none": AnimationPreviewMeta(
        "无",
        "走字时不触发额外的逐字装饰粒子。",
    ),
    "twinkle": AnimationPreviewMeta(
        "星光闪烁",
        "唱到字时在字框偏上位置错峰冒出四角星光，向上漂移并逐步缩小淡出。",
    ),
    "twinkle_classic": AnimationPreviewMeta(
        "星光闪烁（旧版）",
        "旧版原地闪烁样式：字框内随机位置出生，原位放大再熄灭、不漂移。",
    ),
    "note": AnimationPreviewMeta(
        "音符飘出",
        "唱到每个字时自字框升起轻盈摇曳的音符并渐隐。",
    ),
    "heart": AnimationPreviewMeta(
        "爱心飘出",
        "唱到每个字时自字框飘起粉红爱心，轻柔摆动上升。",
    ),
    "flame": AnimationPreviewMeta(
        "火焰燃烧",
        "唱到每个字时自字底燃起橙黄火舌与火星，向上升腾跃动。",
    ),
    "ripple": AnimationPreviewMeta(
        "涟漪光环",
        "唱到每个字时向外荡开双圈细腻水波圆环并扩散淡出。",
    ),
    "petal": AnimationPreviewMeta(
        "花瓣飘动",
        "唱到每个字时有樱花花瓣迎风旋落飘过字形。",
    ),
    "feather": AnimationPreviewMeta(
        "羽毛飘动",
        "唱到每个字时有洁白羽毛随风斜飘滑落。",
    ),
    "lightning": AnimationPreviewMeta(
        "闪电闪烁",
        "唱到每个字时闪现蓝白电光闪电并迸发细碎火花。",
    ),
    "snow": AnimationPreviewMeta(
        "雪花飘动·镂空",
        "唱到每个字时有镂空雪花旋转斜飘降落。",
    ),
    "snow_solid": AnimationPreviewMeta(
        "雪花飘动·实心",
        "唱到每个字时有实心雪花旋转斜飘降落。",
    ),
}

LIT_STYLE_META: dict[str, AnimationPreviewMeta] = {
    "circle": AnimationPreviewMeta(
        "圆形",
        "经典圆形指示灯，开唱前按节拍逐盏熄灭倒计时。",
    ),
    "square": AnimationPreviewMeta(
        "方形",
        "利落方形指示灯，带立体高光与描边，按节拍逐盏熄灭。",
    ),
    "rounded": AnimationPreviewMeta(
        "圆角",
        "柔和圆角方形指示灯，兼顾规整轮廓与温润观感。",
    ),
    "star": AnimationPreviewMeta(
        "星型",
        "五角星造型指示灯，按节拍逐颗熄灭提示进唱。",
    ),
    "note8": AnimationPreviewMeta(
        "八分音符",
        "单枚八分音符造型指示灯，呼应歌曲节拍倒计时。",
    ),
    "note16": AnimationPreviewMeta(
        "十六分音符",
        "双旗十六分音符造型指示灯，层次更灵动。",
    ),
    "notepair": AnimationPreviewMeta(
        "组合八分音符",
        "横梁相连的双音符造型指示灯，辨识度更强。",
    ),
    "image": AnimationPreviewMeta(
        "图片",
        "使用自定义图片素材作为每盏指示灯，按节拍逐盏熄灭。",
    ),
}

LIT_TRANSITION_META: dict[str, AnimationPreviewMeta] = {
    "none": AnimationPreviewMeta(
        "无",
        "到达节拍点时当前指示灯直接瞬间熄灭，干脆利落。",
    ),
    "fade": AnimationPreviewMeta(
        "淡入淡出",
        "当前指示灯在任期尾段平滑降低透明度淡出熄灭。",
    ),
    "slide": AnimationPreviewMeta(
        "滑动",
        "当前指示灯在任期尾段沿设定角度平滑滑出并渐隐熄灭。",
    ),
}


_CATEGORY_PREFIX: dict[str, str] = {
    "karaoke_anim": "唱字特效 · ",
    "reverse_karaoke_anim": "反向唱字 · ",
    "stroke_flash": "",
    "scanline_shape": "扫字线形态 · ",
    "scanline_effect": "扫字线特效 · ",
    "sing_fx": "唱字粒子 · ",
    "lit_style": "指示灯形状 · ",
    "lit_transition_mode": "指示灯转场 · ",
}

_CATEGORY_META_MAP: dict[str, dict[str, AnimationPreviewMeta]] = {
    "karaoke_anim": KARAOKE_ANIM_META,
    "reverse_karaoke_anim": REVERSE_KARAOKE_ANIM_META,
    "scanline_shape": SCANLINE_SHAPE_META,
    "scanline_effect": SCANLINE_EFFECT_META,
    "sing_fx": SING_FX_META,
    "lit_style": LIT_STYLE_META,
    "lit_transition_mode": LIT_TRANSITION_META,
}


def effect_preview_meta(
    category: str,
    effect_key: str,
    fallback_title: str = "",
) -> AnimationPreviewMeta:
    if category == "stroke_flash":
        return STROKE_FLASH_META
    table = _CATEGORY_META_MAP.get(category, {})
    meta = table.get(str(effect_key))
    if meta is not None:
        return meta
    return AnimationPreviewMeta(
        title=fallback_title or str(effect_key),
        summary="特效演示预览。",
    )


# ------------------------------------------------------------------ singing canvas


class SingingEffectPreviewCanvas(QWidget):
    """Live QPainter mini-stage for singing, scanline, particle & stroke-flash effects."""

    _CANVAS_W = 368
    _CANVAS_H = 126
    _FRAME_INTERVAL_MS = 28
    _LEAD_MS = 220
    _TAIL_MS = 640
    _STROKE_FLASH_MS = 240.0

    def __init__(
        self,
        category: str = "karaoke_anim",
        effect_key: str = "none",
        parent: Optional[QWidget] = None,
        *,
        style: Any = None,
        on_status_change: Optional[Callable[[str, bool], None]] = None,
    ) -> None:
        super().__init__(parent)
        self._category = category
        self._effect_key = str(effect_key)
        self._resolved_inherit_key = "none"
        self._style = style
        self._appearance = _resolve_preview_text_appearance(style, preview_font_px=23.0)
        self._ruby_appearance = resolve_preview_ruby_appearance(style, ruby_font_px=10.0)
        self._phase = 0.0
        self._on_status_change = on_status_change
        self._last_status: tuple[str, bool] = ("", False)
        self._scanline_image_cache_path = ""
        self._scanline_image_cache: Optional[QImage] = None

        self._timer = QTimer(self)
        self._timer.setInterval(self._FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)
        self.setFixedSize(self._CANVAS_W, self._CANVAS_H)

        self._font = QFont("Microsoft YaHei UI")
        self._font.setPixelSize(23)
        self._font.setWeight(QFont.Weight.Bold)
        fm = QFontMetricsF(self._font)
        self._ascent = fm.ascent()
        self._descent = fm.descent()

        self._sing_chars = ANIMATION_PREVIEW_SING_CHARS
        self._chars = [spec.char for spec in self._sing_chars]
        self._char_widths = [max(fm.horizontalAdvance(ch), 7.0) for ch in self._chars]
        self._line_width = sum(self._char_widths)
        self._glyph_paths: list[QPainterPath] = []
        for spec in self._sing_chars:
            gp = QPainterPath()
            if spec.char.strip():
                gp.addText(0.0, 0.0, self._font, spec.char)
            self._glyph_paths.append(gp)

        # Forward schedule directly follows ANIMATION_PREVIEW_SING_CHARS;
        # reverse schedule maps the same musical intervals onto visual chars right-to-left.
        self._sung_indices = [i for i, spec in enumerate(self._sing_chars) if spec.is_sung]
        self._sung_specs = [self._sing_chars[i] for i in self._sung_indices]
        self._total_ms = self._LEAD_MS + ANIMATION_PREVIEW_SING_DURATION_MS + self._TAIL_MS
        self._ruby_layouts = build_preview_ruby_layouts(23.0, self._ascent, self._char_widths)

    @property
    def category(self) -> str:
        return self._category

    @property
    def effect_key(self) -> str:
        return self._effect_key

    @property
    def phase(self) -> float:
        return self._phase

    def set_status_callback(self, callback: Optional[Callable[[str, bool], None]]) -> None:
        self._on_status_change = callback

    def set_effect(
        self,
        category: str,
        effect_key: str,
        *,
        style: Any = None,
        resolved_inherit_key: str = "none",
    ) -> None:
        self._category = category
        self._effect_key = str(effect_key)
        self._resolved_inherit_key = str(resolved_inherit_key or "none")
        if style is not None:
            self._style = style
            self._appearance = _resolve_preview_text_appearance(style, preview_font_px=23.0)
            self._ruby_appearance = resolve_preview_ruby_appearance(style, ruby_font_px=10.0)
        self._phase = 0.0
        self._last_status = ("", False)
        self.update()

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

    # ------------------------------------------------------------------ helpers

    def _effective_karaoke_mode(self) -> str:
        if self._category == "karaoke_anim":
            return self._effect_key
        if self._category == "reverse_karaoke_anim":
            if self._effect_key == "inherit":
                return self._resolved_inherit_key
            return self._effect_key
        return "none"

    def _zoom_params(self) -> tuple[int, float]:
        if isinstance(self._style, Style):
            level = zoom_pulse_curve_level(self._style)
            peak = zoom_pulse_peak_ratio(self._style)
            if peak <= 1.0:
                peak = 1.25
            return level, peak
        return 1, 1.25

    def _spec_for_visual_index(self, visual_idx: int, *, reverse: bool):
        if not reverse:
            spec = self._sing_chars[visual_idx]
            return spec if spec.is_sung else None
        if visual_idx not in self._sung_indices:
            return None
        pos = self._sung_indices.index(visual_idx)
        mirrored_pos = len(self._sung_specs) - 1 - pos
        return self._sung_specs[mirrored_pos]

    def _scanline_custom_image(self) -> Optional[QImage]:
        path = str(getattr(self._style, "scanline_image_path", "") or "").strip()
        if not path:
            return None
        if path != self._scanline_image_cache_path:
            self._scanline_image_cache_path = path
            img = QImage(path)
            self._scanline_image_cache = img if not img.isNull() else None
        return self._scanline_image_cache

    def _scanline_base_color(self) -> QColor:
        if isinstance(self._style, Style):
            mode = str(getattr(self._style, "scanline_mode", "default") or "default")
            if mode == "color":
                c = QColor(str(getattr(self._style, "scanline_color", "#FFFFFF") or "#FFFFFF"))
                if c.isValid():
                    return c
        return QColor("#FFFFFF")

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

        t_ms = float(self._phase * self._total_ms)
        t_int = int(round(t_ms))
        reverse = self._category == "reverse_karaoke_anim"
        karaoke_mode = self._effective_karaoke_mode()
        zoom_level, zoom_peak = self._zoom_params()

        baseline_y = self.height() * 0.63 + self._ascent * 0.28
        center_y = baseline_y - self._ascent * 0.42

        # Subtle stage baseline guide.
        painter.setPen(QPen(QColor(255, 255, 255, 18), 1.0, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(16.0, baseline_y + 5.0), QPointF(self.width() - 16.0, baseline_y + 5.0))

        # Subtle direction cue for reverse karaoke.
        if reverse:
            painter.setFont(_ui_font(7.0, bold=True))
            painter.setPen(QColor(accent.red(), accent.green(), accent.blue(), 170))
            painter.drawText(
                QRectF(12.0, 6.0, self.width() - 24.0, 14.0),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                "← 反向走字方向",
            )

        start_x = (self.width() - self._line_width) / 2.0
        char_xs: list[float] = []
        cursor_x = start_x
        for w in self._char_widths:
            char_xs.append(cursor_x)
            cursor_x += w

        # Compute per-char wipe, scale, flash & active scanline front.
        entries: list[tuple[int, float, float, float, float, float, Optional[tuple[int, int]]]] = []
        active_front_x: Optional[float] = None
        any_singing = False
        max_flash = 0.0
        max_scale = 1.0

        for idx, path in enumerate(self._glyph_paths):
            if path.isEmpty():
                continue
            w = self._char_widths[idx]
            cx_left = char_xs[idx]
            spec = self._spec_for_visual_index(idx, reverse=reverse)
            if spec is None or spec.start_ms is None or spec.end_ms is None:
                continue
            c_start = self._LEAD_MS + spec.start_ms
            c_end = self._LEAD_MS + spec.end_ms
            raw_wipe = sample_sing_wipe_progress(t_ms, spec, lead_ms=self._LEAD_MS)
            if karaoke_mode == "no_wipe":
                wipe = 1.0 if t_ms >= c_start else 0.0
            else:
                wipe = raw_wipe

            if c_start <= t_ms <= c_end:
                any_singing = True
                active_front_x = (
                    cx_left + w * (1.0 - raw_wipe) if reverse else cx_left + w * raw_wipe
                )

            scale = 1.0
            if karaoke_mode == "utopia":
                scale = utopia_wipe_scale(t_int, c_start, c_end)
            elif karaoke_mode == "zoom_pulse":
                scale = zoom_pulse_wipe_scale(
                    t_int,
                    c_start,
                    c_end,
                    curve_level=zoom_level,
                    peak_ratio=zoom_peak,
                )
            max_scale = max(max_scale, scale)

            flash = 0.0
            if self._category == "stroke_flash":
                dt = t_ms - c_start
                if 0.0 <= dt <= self._STROKE_FLASH_MS:
                    flash = (1.0 - dt / self._STROKE_FLASH_MS) ** 1.35
                    max_flash = max(max_flash, flash)

            entries.append((idx, cx_left, w, scale, wipe, flash, (c_start, c_end)))

        # Back-layer effects (sing_fx ripple / flame smoke).
        if self._category == "sing_fx" and self._effect_key in {"ripple", "flame"}:
            self._paint_sing_particles(
                painter, char_xs, center_y, baseline_y, t_ms, layer="back", accent=accent
            )

        # Paint lyric characters (draw larger scaled chars on top so they never clip behind neighbors).
        colors = self._appearance.colors
        draw_order = sorted(entries, key=lambda item: (item[3], item[5]))
        for idx, cx_left, w, scale, wipe, flash, _interval in draw_order:
            path = self._glyph_paths[idx]
            char_cx = cx_left + w * 0.5
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
                paint_styled_karaoke_glyph(
                    painter, path, char_rect, line_rect, colors.before, self._appearance,
                    after=False, flash_strength=flash,
                )
            elif wipe >= 1.0:
                paint_styled_karaoke_glyph(
                    painter, path, char_rect, line_rect, colors.after, self._appearance,
                    after=True, flash_strength=flash,
                )
            else:
                clip_pad = 14.0
                clip_top = -self._ascent - clip_pad
                clip_h = self._ascent + self._descent + clip_pad * 2.0
                if not reverse:
                    split_x = w * wipe
                    painter.save()
                    painter.setClipRect(QRectF(-clip_pad, clip_top, split_x + clip_pad, clip_h))
                    paint_styled_karaoke_glyph(
                        painter, path, char_rect, line_rect, colors.after, self._appearance,
                        after=True, flash_strength=flash,
                    )
                    painter.restore()
                    painter.save()
                    painter.setClipRect(QRectF(split_x, clip_top, w - split_x + clip_pad, clip_h))
                    paint_styled_karaoke_glyph(
                        painter, path, char_rect, line_rect, colors.before, self._appearance,
                        after=False, flash_strength=flash,
                    )
                    painter.restore()
                else:
                    split_x = w * (1.0 - wipe)
                    painter.save()
                    painter.setClipRect(QRectF(-clip_pad, clip_top, split_x + clip_pad, clip_h))
                    paint_styled_karaoke_glyph(
                        painter, path, char_rect, line_rect, colors.before, self._appearance,
                        after=False, flash_strength=flash,
                    )
                    painter.restore()
                    painter.save()
                    painter.setClipRect(QRectF(split_x, clip_top, w - split_x + clip_pad, clip_h))
                    paint_styled_karaoke_glyph(
                        painter, path, char_rect, line_rect, colors.after, self._appearance,
                        after=True, flash_strength=flash,
                    )
                    painter.restore()
            ruby_layout = self._ruby_layouts[idx] if idx < len(self._ruby_layouts) else None
            if ruby_layout is not None:
                ruby_spec = self._spec_for_visual_index(idx, reverse=reverse)
                k_count = len(ruby_layout.kana_paths)
                if ruby_spec is not None:
                    kana_wipes = tuple(
                        sample_ruby_kana_progress(
                            t_ms,
                            ruby_spec,
                            k_i,
                            k_count,
                            lead_ms=self._LEAD_MS,
                            reverse=reverse,
                            no_wipe=(karaoke_mode == "no_wipe"),
                        )
                        for k_i in range(k_count)
                    )
                else:
                    kana_wipes = (0.0,) * k_count
                paint_styled_ruby_for_char(
                    painter,
                    ruby_layout,
                    ruby_appearance=self._ruby_appearance,
                    line_start_rel_x=start_x - cx_left,
                    line_width=self._line_width,
                    kana_wipes=kana_wipes,
                    reverse=reverse,
                )
            painter.restore()

        # Scanline highlight band / column shape / trailing particles.
        if self._category == "scanline_shape" and active_front_x is not None:
            self._paint_scanline_shape(
                painter, char_xs, start_x, baseline_y, center_y, active_front_x, t_ms, accent
            )
        elif self._category == "scanline_effect":
            if active_front_x is not None:
                self._paint_scanline_band(
                    painter,
                    char_xs,
                    start_x,
                    baseline_y,
                    active_front_x,
                    self._scaled_scanline_params("band", t_ms),
                )
            if self._effect_key != "none":
                self._paint_scanline_effect_particles(
                    painter, char_xs, center_y, t_ms
                )
        elif self._category == "sing_fx" and self._effect_key != "none":
            self._paint_sing_particles(
                painter, char_xs, center_y, baseline_y, t_ms, layer="front", accent=accent
            )

        # Bottom timeline progress rail.
        bar_x = 12.0
        bar_w = self.width() - 24.0
        bar_y = self.height() - 8.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 26))
        painter.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, 3.0), 1.5, 1.5)

        win_start = self._LEAD_MS / float(self._total_ms)
        win_end = (self._LEAD_MS + ANIMATION_PREVIEW_SING_DURATION_MS) / float(self._total_ms)
        painter.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 80))
        painter.drawRoundedRect(
            QRectF(bar_x + bar_w * win_start, bar_y, bar_w * (win_end - win_start), 3.0),
            1.5,
            1.5,
        )
        painter.setBrush(accent if any_singing else QColor(255, 255, 255, 130))
        painter.drawRoundedRect(
            QRectF(bar_x, bar_y, max(3.0, bar_w * self._phase), 3.0),
            1.5,
            1.5,
        )

        # Status badge text.
        status = self._compute_status(any_singing, max_flash, max_scale, karaoke_mode)
        if status != self._last_status:
            self._last_status = status
            if self._on_status_change is not None:
                self._on_status_change(status[0], status[1])

        painter.restore()
        painter.end()

    def _compute_status(
        self,
        any_singing: bool,
        max_flash: float,
        max_scale: float,
        karaoke_mode: str,
    ) -> tuple[str, bool]:
        if self._category == "stroke_flash":
            if max_flash > 0.12:
                return ("描边闪白中", True)
            return ("走字演示", any_singing)
        if self._category == "karaoke_anim":
            if karaoke_mode in {"utopia", "zoom_pulse"} and max_scale > 1.02:
                return (f"当前 {max_scale:.2f}×", True)
            if karaoke_mode == "no_wipe":
                return ("整字瞬切" if any_singing else "准备唱字", any_singing)
            return ("平滑走字中" if any_singing else "准备唱字", any_singing)
        if self._category == "reverse_karaoke_anim":
            if self._effect_key == "inherit":
                follow_label = KARAOKE_ANIM_META.get(
                    karaoke_mode, AnimationPreviewMeta("Wipe", "")
                ).title
                short = follow_label.split("（")[0]
                return (f"跟随：{short}", any_singing)
            if karaoke_mode in {"utopia", "zoom_pulse"} and max_scale > 1.02:
                return (f"反向 {max_scale:.2f}×", True)
            return ("反向走字中" if any_singing else "准备唱字", any_singing)
        if self._category == "scanline_shape":
            labels = {
                "band": "锋面高亮扫过",
                "flame": "烈焰火棒扫过",
                "bubble": "水泡光柱扫过",
                "image": "素材随锋面移动",
            }
            return (labels.get(self._effect_key, "扫字线演示") if any_singing else "准备唱字", any_singing)
        if self._category == "scanline_effect":
            if self._effect_key == "none":
                return ("仅扫字线", False)
            return ("锋面拖尾发射" if any_singing else "拖尾余韵", any_singing)
        if self._category == "sing_fx":
            if self._effect_key == "none":
                return ("无唱字粒子", False)
            return ("逐字粒子触发" if any_singing else "粒子余韵", any_singing)
        return ("效果演示", any_singing)

    # ------------------------------------------------------------------ scanline painting

    def _scaled_scanline_params(self, shape_override: str, t_ms: float) -> ScanlineParams:
        """Build preview-scaled ``ScanlineParams`` matching real engine/Skia proportions."""
        if isinstance(self._style, Style):
            raw = scanline_params_for_style(self._style)
            base_font = max(float(getattr(self._style, "font_size_px", 56) or 56), 16.0)
            scale = 23.0 / base_font
            width_px = max(3.5, min(raw.width_px * scale, 18.0))
            glow_px = max(1, int(round(raw.glow_px * scale))) if raw.glow_px > 0 else 0
            return replace(
                raw,
                shape=shape_override,
                width_px=width_px,
                glow_px=glow_px,
                time_ms=int(round(t_ms)),
            )
        return ScanlineParams(
            width_px=6.5,
            color="#FFFFFF",
            glow_px=2,
            mode="default",
            shape=shape_override,
            time_ms=int(round(t_ms)),
        )

    def _ruby_top_for_line(self, baseline_y: float) -> float:
        tops = [
            baseline_y + rl.baseline_y - rl.ascent
            for rl in self._ruby_layouts
            if rl is not None
        ]
        if tops:
            return min(tops) - 3.5
        return baseline_y - self._ascent - 4.0

    def _paint_scanline_band(
        self,
        painter: QPainter,
        char_xs: list[float],
        start_x: float,
        baseline_y: float,
        front_x: float,
        params: ScanlineParams,
    ) -> None:
        """Draw Skia-faithful glyph-only feathered scanline highlight at ``front_x``.

        Uses ``scanline_feather_slices`` strictly clipped to glyph & ruby ink with
        zero rectangular wash on the dark stage behind the text.
        """
        band_color = QColor(params.color) if QColor(params.color).isValid() else QColor("#FFFFFF")
        band_top = self._ruby_top_for_line(baseline_y) - 4.0
        band_bottom = baseline_y + self._descent + 4.0
        band_rect = QRectF(start_x - 8.0, band_top, self._line_width + 16.0, band_bottom - band_top)
        slices = scanline_feather_slices(band_rect, front_x, params, vertical=False)

        for slice_rect, alpha in slices:
            if alpha <= 0.01:
                continue
            painter.save()
            painter.setClipRect(slice_rect)
            painter.setOpacity(alpha)
            for idx, path in enumerate(self._glyph_paths):
                if path.isEmpty():
                    continue
                cx_left = char_xs[idx]
                w = self._char_widths[idx]
                if cx_left + w + 6.0 < slice_rect.left() or cx_left - 6.0 > slice_rect.right():
                    continue
                painter.save()
                painter.translate(cx_left, baseline_y)
                char_rect = QRectF(0.0, -self._ascent, w, self._ascent + self._descent)
                line_rect = QRectF(start_x - cx_left, -self._ascent, self._line_width, self._ascent + self._descent)
                paint_styled_karaoke_glyph(
                    painter,
                    path,
                    char_rect,
                    line_rect,
                    self._appearance.colors.after,
                    self._appearance,
                    after=True,
                    override_fill_color=band_color,
                )
                ruby_layout = self._ruby_layouts[idx] if idx < len(self._ruby_layouts) else None
                if ruby_layout is not None:
                    paint_styled_ruby_for_char(
                        painter,
                        ruby_layout,
                        ruby_appearance=self._ruby_appearance,
                        line_start_rel_x=start_x - cx_left,
                        line_width=self._line_width,
                        kana_wipes=(1.0,) * len(ruby_layout.kana_paths),
                        override_fill_color=band_color,
                    )
                painter.restore()
            painter.restore()

    def _paint_scanline_shape(
        self,
        painter: QPainter,
        char_xs: list[float],
        start_x: float,
        baseline_y: float,
        center_y: float,
        front_x: float,
        t_ms: float,
        accent: QColor,
    ) -> None:
        shape = self._effect_key
        params = self._scaled_scanline_params(shape, t_ms)
        body_ink_rect = QRectF(
            start_x,
            baseline_y - self._ascent * 0.92,
            self._line_width,
            self._ascent * 0.92 + self._descent * 0.5,
        )
        target_top = self._ruby_top_for_line(baseline_y)

        if shape == "band":
            self._paint_scanline_band(
                painter, char_xs, start_x, baseline_y, front_x, params
            )
            return

        if shape == "flame":
            # Real 4-ribbon helical flame column matching Skia ``scanline_shape.cpp``.
            paint_scanline_column(
                painter,
                body_ink_rect,
                front=front_x,
                params=params,
                colors=self._appearance.colors,
                opacity=1.0,
                font_px=23.0,
                target_top=target_top,
                gradient_line=body_ink_rect,
            )
            return

        if shape == "bubble":
            # Match Skia ``scanline_shape.cpp::drawBubbles`` exactly (glyph-only highlight +
            # 3-layer vertical sky-blue capsule pillar + 18 rising hollow bubbles).
            self._paint_scanline_band(
                painter, char_xs, start_x, baseline_y, front_x, params
            )
            self._paint_skia_bubbles(
                painter,
                body_ink_rect,
                front=front_x,
                params=params,
                target_top=target_top,
            )
            return

        # shape == "image": glyph-only highlight band + custom image (or animated anchor badge).
        self._paint_scanline_band(
            painter, char_xs, start_x, baseline_y, front_x, params
        )
        custom_img = self._scanline_custom_image()
        if custom_img is not None:
            target_h = 34.0
            aspect = custom_img.width() / max(float(custom_img.height()), 1.0)
            target_w = max(14.0, min(52.0, target_h * aspect))
            dest = QRectF(
                front_x - target_w * 0.5,
                center_y - target_h * 0.5,
                target_w,
                target_h,
            )
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(dest, custom_img)
            painter.restore()
            return

        # Clean placeholder badge representing a custom image / animated sprite at the wipe front.
        t_s = t_ms / 1000.0
        bob = math.sin(t_s * 6.0) * 1.8
        box = QRectF(front_x - 13.0, center_y - 13.0 + bob, 26.0, 26.0)
        painter.save()
        glow = QRadialGradient(box.center(), 20.0)
        glow.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 130))
        glow.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(box.center(), 20.0, 20.0)
        painter.setPen(QPen(QColor("#F8FAFC"), 1.5))
        painter.setBrush(QColor(15, 23, 42, 220))
        painter.drawRoundedRect(box, 5.5, 5.5)
        mtn = QPainterPath()
        mtn.moveTo(box.left() + 4.5, box.bottom() - 5.0)
        mtn.lineTo(box.left() + 10.5, box.top() + 12.5)
        mtn.lineTo(box.left() + 14.5, box.bottom() - 8.5)
        mtn.lineTo(box.left() + 18.0, box.top() + 10.5)
        mtn.lineTo(box.right() - 4.5, box.bottom() - 5.0)
        mtn.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 220))
        painter.drawPath(mtn)
        painter.setBrush(QColor("#FDE047"))
        painter.drawEllipse(QPointF(box.left() + 8.5, box.top() + 7.5), 2.3, 2.3)
        painter.restore()

    def _paint_skia_bubbles(
        self,
        painter: QPainter,
        ink: QRectF,
        *,
        front: float,
        params: ScanlineParams,
        target_top: float,
    ) -> None:
        """1:1 QPainter mirror of Skia ``native/subtitle_renderer/src/skia/core/scanline_shape.cpp::drawBubbles``."""
        outer_rgb, _inner_rgb, _core_rgb = _scanline_column_palette(
            params, self._appearance.colors
        )
        outer_col = QColor.fromRgbF(*outer_rgb)
        rim_col = QColor.fromRgbF(*(c + (1.0 - c) * 0.30 for c in outer_rgb))
        hi_col = QColor.fromRgbF(*(c + (1.0 - c) * 0.60 for c in outer_rgb))

        t = float(params.time_ms) / 1000.0
        font_px = 26.0
        line_h = max(ink.height(), 1.0)
        bottom = ink.bottom() + line_h * 0.01
        spread = max(line_h * 0.24, font_px * 0.22)
        pillar_h = line_h * 1.65
        pillar_top = ink.center().y() - pillar_h * 0.5 - pillar_h * 0.12
        pillar_bottom = pillar_top + pillar_h
        if target_top < pillar_top:
            pillar_top = target_top
            pillar_h = max(pillar_bottom - pillar_top, line_h * 1.65)
        rise = max(line_h * 1.55, bottom - pillar_top + font_px * 0.10)
        pillar_w = max(font_px * 0.42, line_h * 0.22)

        def draw_bubble(index: int, in_front: bool) -> None:
            phase = fx_unit_hash(index, 601)
            speed = 0.80 + 0.50 * fx_unit_hash(index, 602)
            cycle = math.fmod(phase + t * speed, 1.0)
            fade = min(cycle / 0.08, 1.0) * min((1.0 - cycle) / 0.20, 1.0)
            if fade <= 0.0:
                return
            x = (
                front
                + (fx_unit_hash(index, 603) - 0.5) * spread * 2.0
                + math.sin(t * 2.6 + phase * 6.2831853) * font_px * 0.045
            )
            y = bottom - cycle * rise
            size = (
                font_px
                * (0.060 + 0.060 * fx_unit_hash(index, 604))
                * (0.65 + 0.45 * cycle)
                * (1.0 if in_front else 0.88)
            )
            if size <= 0.5:
                return
            alpha = _clamp01(fade * (1.0 if in_front else 0.58))
            fill = QColor(outer_col)
            fill.setAlphaF(alpha * 0.27)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawEllipse(QPointF(x, y), size, size)

            rim = QColor(rim_col)
            rim.setAlphaF(alpha)
            painter.setPen(QPen(rim, max(0.8, size * 0.10)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(x, y), size, size)

            hi = QColor(hi_col)
            hi.setAlphaF(alpha)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(hi)
            painter.drawEllipse(
                QPointF(x - size * 0.32, y - size * 0.32),
                size * 0.18,
                size * 0.18,
            )

        painter.save()
        for i in range(0, 18, 2):
            draw_bubble(i, False)

        # 3-layer soft vertical capsule matching Skia drawBubbles opacities (0.35, 0.55, 0.45).
        pulse = 0.90 + 0.10 * math.sin(t * 3.2)
        for scale, layer_alpha in ((1.0, 0.35), (0.58, 0.55), (0.24, 0.45)):
            w = max(pillar_w * scale * pulse, 2.0)
            grad = QLinearGradient(front, pillar_top, front, pillar_top + pillar_h)
            for pos, stop_a in ((0.0, 0.0), (0.14, 0.18), (0.50, 0.50), (0.86, 0.18), (1.0, 0.0)):
                c = QColor(outer_col)
                c.setAlphaF(_clamp01(stop_a * layer_alpha))
                grad.setColorAt(pos, c)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(grad)
            painter.drawRoundedRect(
                QRectF(front - w * 0.5, pillar_top, w, pillar_h),
                w * 0.5,
                w * 0.5,
            )

        for i in range(1, 18, 2):
            draw_bubble(i, True)
        painter.restore()

    def _paint_scanline_effect_particles(
        self,
        painter: QPainter,
        char_xs: list[float],
        center_y: float,
        t_ms: float,
    ) -> None:
        """Delegate directly to ``particles.burst_particles_at`` (Skia 1:1 kinematics)."""
        effect = self._effect_key
        sprite = SCANLINE_FX_SPRITES.get(effect)
        if sprite is None:
            return
        palette_hex = SCANLINE_FX_DEFAULT_PALETTES.get(effect, ("#FFFFFF", "#FFD7E0"))
        colors = [QColor(hex_c) for hex_c in palette_hex]
        size_pct = max(
            5,
            min(200, int(getattr(self._style, "scanline_effect_size_pct", 30) or 30)),
        )
        base_count = max(
            1,
            min(32, int(getattr(self._style, "scanline_effect_count", 4) or 4)),
        )
        base_size = 28.0 * (size_pct / 100.0)
        tail_ms = int(SCANLINE_FX_TAIL_MS.get(effect, 560))
        travel = base_size * float(SCANLINE_FX_TRAVEL_SCALE.get(effect, 2.0))
        box_h = self._ascent * 1.22
        t_int = int(round(t_ms))

        for idx, spec in enumerate(self._sing_chars):
            if not spec.is_sung or spec.start_ms is None or spec.end_ms is None:
                continue
            c_start = self._LEAD_MS + spec.start_ms
            c_end = self._LEAD_MS + spec.end_ms
            emit_ms = max(c_end - c_start, 1)
            if t_int < c_start or t_int > c_end + tail_ms:
                continue
            w = self._char_widths[idx]
            origin_x = char_xs[idx] + w * 0.5
            total_count = scanline_particles_per_char(base_count, emit_ms)
            base_seed = (0x5A17 + idx * FALL_CHAR_SEED_SALT) & 0xFFFFFFFF
            variant_count = len(colors)
            for v_idx, col in enumerate(colors):
                if variant_count > 1:
                    if total_count <= 1:
                        if v_idx != idx % variant_count:
                            continue
                        v_count = 1
                    else:
                        base_n, extra_n = divmod(total_count, variant_count)
                        v_count = base_n + (1 if v_idx < extra_n else 0)
                        if v_count <= 0:
                            continue
                    v_seed = (base_seed + v_idx * 0x9E37) & 0xFFFFFFFF
                else:
                    v_count = total_count
                    v_seed = base_seed
                burst = {
                    "kind": "scanline",
                    "sprite": sprite,
                    "start_ms": c_start,
                    "end_ms": c_end + tail_ms,
                    "emit_ms": emit_ms,
                    "size_px": base_size,
                    "travel_px": travel,
                    "count": v_count,
                    "seed": v_seed,
                    "reverse": False,
                }
                particles = burst_particles_at(burst, t_int, origin_x, center_y, w, box_h)
                for pd in particles:
                    draw_preview_sprite(
                        painter,
                        sprite,
                        pd.x,
                        pd.y,
                        pd.size_px,
                        color=col,
                        alpha=pd.alpha,
                        angle_deg=pd.rotation_deg,
                        scale_x=pd.stretch_x,
                    )

    # ------------------------------------------------------------------ sing_fx particles

    def _paint_sing_particles(
        self,
        painter: QPainter,
        char_xs: list[float],
        center_y: float,
        baseline_y: float,
        t_ms: float,
        *,
        layer: str,
        accent: QColor,
    ) -> None:
        key = self._effect_key
        if key == "none":
            return

        for idx, spec in enumerate(self._sing_chars):
            if not spec.is_sung or spec.start_ms is None or spec.end_ms is None:
                continue
            c_start = float(self._LEAD_MS + spec.start_ms)
            c_end = float(self._LEAD_MS + spec.end_ms)
            dur = max(c_end - c_start, 220.0)
            tau = t_ms - c_start
            if tau <= 0.0:
                continue
            w = self._char_widths[idx]
            cx = char_xs[idx] + w * 0.5

            if key == "ripple":
                if layer != "back":
                    continue
                life = max(dur + 420.0, 620.0)
                if tau >= life:
                    continue
                for ring_i in range(2):
                    rp = (tau - ring_i * 110.0) / 480.0
                    if rp <= 0.0 or rp >= 1.0:
                        continue
                    size = 14.0 + 34.0 * (1.0 - (1.0 - rp) ** 2)
                    alpha = (1.0 - rp) * 0.80
                    col = QColor("#FFB7C5") if ring_i == 0 else QColor("#FFD7E0")
                    draw_preview_sprite(painter, "ring", cx, center_y, size, color=col, alpha=alpha)
                continue

            if key == "twinkle":
                if layer != "front":
                    continue
                life = max(dur + 340.0, 560.0)
                if tau >= life:
                    continue
                for k in range(3):
                    seed = idx * 19 + k
                    u1 = fx_unit_hash(seed, 1)
                    u2 = fx_unit_hash(seed, 2)
                    u3 = fx_unit_hash(seed, 3)
                    u4 = fx_unit_hash(seed, 4)
                    delay = u4 * min(dur * 0.5, 160.0)
                    p = (tau - delay) / max(life - delay, 1.0)
                    if p <= 0.0 or p >= 1.0:
                        continue
                    size_i = 10.5 + 5.5 * u3
                    size = size_i * (1.0 - 0.72 * p)
                    px = cx + (u1 - 0.5) * w * 1.05 + (u4 - 0.5) * 14.0 * p
                    py = center_y - 4.0 + (u2 - 0.55) * 16.0 - (10.0 + 14.0 * u2) * p
                    col = QColor("#FFB7C5") if (idx + k) % 2 == 0 else QColor("#FFF7D6")
                    draw_preview_sprite(
                        painter, "star4", px, py, size, color=col, alpha=(1.0 - p) * 0.96, angle_deg=u3 * 90.0 + 60.0 * p
                    )
                continue

            if key == "twinkle_classic":
                if layer != "front":
                    continue
                life = max(dur + 240.0, 520.0)
                if tau >= life:
                    continue
                for k in range(3):
                    seed = idx * 23 + k
                    u1 = fx_unit_hash(seed, 1)
                    u2 = fx_unit_hash(seed, 2)
                    u3 = fx_unit_hash(seed, 3)
                    u4 = fx_unit_hash(seed, 4)
                    life_i = 300.0 + 140.0 * u4
                    delay = u3 * max(life - life_i, 0.0) * 0.75
                    p = (tau - delay) / life_i
                    if p <= 0.0 or p >= 1.0:
                        continue
                    env = math.sin(math.pi * p)
                    peak = 10.0 + 6.5 * u4
                    px = cx + (u1 - 0.5) * w * 0.95
                    py = center_y + (u2 - 0.5) * 24.0
                    col = QColor("#FDE047") if (idx + k) % 2 == 0 else QColor("#FFFFFF")
                    draw_preview_sprite(
                        painter, "star4", px, py, peak * (0.30 + 0.70 * env), color=col, alpha=env * 0.96, angle_deg=u3 * 120.0 - 30.0
                    )
                continue

            if key in {"note", "heart"}:
                if layer != "front":
                    continue
                life = max(dur + 460.0, 680.0)
                if tau >= life:
                    continue
                sprite = "note" if key == "note" else "heart"
                col_a = QColor("#FFB7C5" if key == "note" else "#FF4D6D")
                col_b = QColor("#93C5FD" if key == "note" else "#FF9EBB")
                for k in range(2):
                    seed = idx * 17 + k
                    p = (tau - k * 90.0) / 560.0
                    if p <= 0.0 or p >= 1.0:
                        continue
                    dx = math.sin(p * 4.5 + seed) * 6.0 + (fx_unit_hash(seed, 301) - 0.5) * 14.0
                    dy = -8.0 - p * (28.0 + 12.0 * fx_unit_hash(seed, 302))
                    alpha = (math.sin(math.pi * p) ** 0.75) * 0.94
                    size = 11.0 + 4.5 * fx_unit_hash(seed, 303)
                    col = col_a if (idx + k) % 2 == 0 else col_b
                    draw_preview_sprite(
                        painter, sprite, cx + dx, center_y + dy, size, color=col, alpha=alpha, angle_deg=math.sin(p * 5.0 + seed) * 16.0
                    )
                continue

            if key == "flame":
                life = max(dur + 380.0, 580.0)
                if tau >= life:
                    continue
                lp = _clamp01(tau / life)
                if layer == "back":
                    smoke_alpha = math.sin(math.pi * lp) * 0.24
                    draw_preview_sprite(
                        painter, "smoke", cx, center_y - lp * 16.0, 22.0 + lp * 14.0, color=QColor("#FB923C"), alpha=smoke_alpha
                    )
                else:
                    sprites = ("flame", "flame_tall", "flame_wide", "flame_curl")
                    for k in range(3):
                        seed = idx * 29 + k
                        sp = (tau - k * 65.0) / 460.0
                        if sp <= 0.0 or sp >= 1.0:
                            continue
                        env = math.sin(math.pi * sp)
                        ox = (fx_unit_hash(seed, 401) - 0.5) * 14.0
                        oy = baseline_y - 3.0 - sp * (24.0 + 12.0 * fx_unit_hash(seed, 402))
                        size = (15.0 + 9.0 * fx_unit_hash(seed, 403)) * (0.65 + 0.5 * env)
                        col = QColor("#FF4500") if k == 0 else (QColor("#FF9E1B") if k == 1 else QColor("#FFF3B0"))
                        draw_preview_sprite(
                            painter, sprites[(idx + k) % len(sprites)], cx + ox, oy, size, color=col, alpha=env * 0.90, angle_deg=(fx_unit_hash(seed, 404) - 0.5) * 18.0
                        )
                    ep = tau / 520.0
                    if 0.0 < ep < 1.0:
                        ex = cx + (fx_unit_hash(idx, 409) - 0.5) * 16.0 + ep * 8.0
                        ey = center_y - ep * 36.0
                        draw_preview_sprite(painter, "dot", ex, ey, 3.0, color=QColor("#FEF08A"), alpha=(1.0 - ep) * 0.95)
                continue

            if key == "lightning":
                if layer != "front":
                    continue
                life = 360.0
                if tau >= life:
                    continue
                p = tau / life
                bolts = ("bolt", "bolt_b", "bolt_c")
                flicker = 0.55 + 0.45 * abs(math.sin(p * 18.0 + idx * 1.7))
                env = math.sin(math.pi * p) * flicker
                bolt = bolts[(idx + int(p * 6.0)) % len(bolts)]
                ang = (fx_unit_hash(idx, 501) - 0.5) * 18.0
                draw_preview_sprite(painter, bolt, cx, center_y - 2.0, 40.0, color=QColor("#38BDF8"), alpha=env * 0.78, angle_deg=ang)
                draw_preview_sprite(painter, bolt, cx, center_y - 2.0, 31.0, color=QColor("#E0FBFF"), alpha=env * 0.98, angle_deg=ang)
                for k in range(2):
                    sx = cx + (fx_unit_hash(idx * 7 + k, 505) - 0.5) * 24.0
                    sy = center_y + (fx_unit_hash(idx * 7 + k, 506) - 0.5) * 24.0
                    draw_preview_sprite(painter, "dot", sx, sy, 2.8, color=QColor("#E0FBFF"), alpha=env * 0.9)
                continue

            # Falling wind-drifted singing particles: petal / feather / snow / snow_solid.
            if layer != "front":
                continue
            fall_map = {
                "petal": ("petal", QColor("#FFB7C5"), QColor("#FFD7E0"), -22.0, 11.5),
                "feather": ("feather", QColor("#FFFFFF"), QColor("#FFF5E4"), -28.0, 15.0),
                "snow": ("snow", QColor("#FFFFFF"), QColor("#BAE6FD"), -26.0, 12.0),
                "snow_solid": ("snow_solid", QColor("#FFFFFF"), QColor("#BAE6FD"), -26.0, 11.5),
            }
            if key not in fall_map:
                continue
            sprite, col_a, col_b, lean_deg, base_sz = fall_map[key]
            life = 760.0
            if tau >= life:
                continue
            for k in range(2):
                seed = idx * 31 + k
                fp = (tau - k * 100.0) / 640.0
                if fp <= 0.0 or fp >= 1.0:
                    continue
                fade_in = min(fp / 0.14, 1.0)
                fade_out = 1.0 if fp < 0.78 else (1.0 - (fp - 0.78) / 0.22)
                wind_x = fp * (20.0 + 12.0 * fx_unit_hash(seed, 601))
                sway_x = math.sin(fp * 5.0 + seed) * 5.0
                start_y = center_y - 26.0 - 10.0 * fx_unit_hash(seed, 602)
                fall_y = start_y + fp * (58.0 + 14.0 * fx_unit_hash(seed, 603))
                px = cx + (fx_unit_hash(seed, 604) - 0.5) * 12.0 + wind_x + sway_x - 8.0
                size = base_sz * (0.78 + 0.42 * fx_unit_hash(seed, 605))
                if key in {"snow", "snow_solid"}:
                    angle = lean_deg + fp * (80.0 if k % 2 == 0 else -80.0)
                    sx = 1.0
                else:
                    angle = lean_deg + math.sin(fp * 6.0 + seed) * 20.0
                    sx = 0.55 + 0.45 * abs(math.cos(fp * 4.2 + seed))
                col = col_a if (idx + k) % 2 == 0 else col_b
                draw_preview_sprite(
                    painter, sprite, px, fall_y, size, color=col, alpha=fade_in * fade_out * 0.94, angle_deg=angle, scale_x=sx
                )


# ------------------------------------------------------------------ lit indicator canvas


class LitIndicatorPreviewCanvas(QWidget):
    """Live QPainter mini-stage demonstrating lit indicator shapes & extinguish transitions."""

    _CANVAS_W = 368
    _CANVAS_H = 134
    _FRAME_INTERVAL_MS = 28
    _CYCLE_MS = 3200
    _LAMP_COUNT = 4

    def __init__(
        self,
        category: str = "lit_style",
        effect_key: str = "circle",
        parent: Optional[QWidget] = None,
        *,
        style: Any = None,
        on_status_change: Optional[Callable[[str, bool], None]] = None,
    ) -> None:
        super().__init__(parent)
        self._category = category
        self._effect_key = str(effect_key)
        self._style = style
        self._appearance = _resolve_preview_text_appearance(style, preview_font_px=20.0)
        self._ruby_appearance = resolve_preview_ruby_appearance(style, ruby_font_px=9.0)
        self._phase = 0.0
        self._on_status_change = on_status_change
        self._last_status: tuple[str, bool] = ("", False)

        self._timer = QTimer(self)
        self._timer.setInterval(self._FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)
        self.setFixedSize(self._CANVAS_W, self._CANVAS_H)

        self._font = QFont("Microsoft YaHei UI")
        self._font.setPixelSize(20)
        self._font.setWeight(QFont.Weight.Bold)
        fm = QFontMetricsF(self._font)
        self._ascent = fm.ascent()
        self._descent = fm.descent()
        self._chars = [spec.char for spec in ANIMATION_PREVIEW_SING_CHARS]
        self._char_widths = [max(fm.horizontalAdvance(ch), 6.5) for ch in self._chars]
        self._line_width = sum(self._char_widths)
        self._glyph_paths: list[QPainterPath] = []
        for ch in self._chars:
            gp = QPainterPath()
            if ch.strip():
                gp.addText(0.0, 0.0, self._font, ch)
            self._glyph_paths.append(gp)
        self._ruby_layouts = build_preview_ruby_layouts(20.0, self._ascent, self._char_widths)

    @property
    def category(self) -> str:
        return self._category

    @property
    def effect_key(self) -> str:
        return self._effect_key

    @property
    def phase(self) -> float:
        return self._phase

    def set_status_callback(self, callback: Optional[Callable[[str, bool], None]]) -> None:
        self._on_status_change = callback

    def set_effect(
        self,
        category: str,
        effect_key: str,
        *,
        style: Any = None,
    ) -> None:
        self._category = category
        self._effect_key = str(effect_key)
        if style is not None:
            self._style = style
            self._appearance = _resolve_preview_text_appearance(style, preview_font_px=20.0)
            self._ruby_appearance = resolve_preview_ruby_appearance(style, ruby_font_px=9.0)
        self._phase = 0.0
        self._last_status = ("", False)
        self.update()

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
        step = self._FRAME_INTERVAL_MS / float(self._CYCLE_MS)
        self._phase = (self._phase + step) % 1.0
        self.update()

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self.stop_animation()
        super().hideEvent(event)

    def _effective_shape(self) -> str:
        if self._category == "lit_style":
            return self._effect_key
        shape = str(getattr(self._style, "lit_style", "circle") or "circle")
        return shape if shape in LIT_STYLE_META else "circle"

    def _effective_transition(self) -> str:
        if self._category == "lit_transition_mode":
            return self._effect_key
        return "fade"

    def _lit_colors(self) -> tuple[QColor, QColor]:
        fill_raw = str(getattr(self._style, "lit_fill_color", "") or "")
        stroke_raw = str(getattr(self._style, "lit_stroke_color", "") or "")
        fill = QColor(fill_raw) if QColor(fill_raw).isValid() else QColor("#F59E0B")
        stroke = QColor(stroke_raw) if QColor(stroke_raw).isValid() else QColor("#FFFFFF")
        return fill, stroke

    def _draw_single_lamp(
        self,
        painter: QPainter,
        rect: QRectF,
        shape: str,
        *,
        fill_color: QColor,
        stroke_color: QColor,
        opacity: float,
        dx: float = 0.0,
        dy: float = 0.0,
    ) -> None:
        if opacity <= 0.01:
            return
        painter.save()
        painter.setOpacity(_clamp01(opacity))
        painter.translate(dx, dy)

        if shape == "image":
            img_path = str(getattr(self._style, "lit_image_path", "") or "").strip()
            img = cached_lit_image(img_path) if img_path else None
            if img is not None and not img.isNull():
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                painter.drawImage(rect, img)
                painter.restore()
                return
            # Crisp rounded photo-frame placeholder icon for custom image lamp.
            painter.setPen(QPen(stroke_color, 1.5))
            painter.setBrush(QColor(30, 41, 59, 235))
            painter.drawRoundedRect(rect, 5.0, 5.0)
            mtn = QPainterPath()
            mtn.moveTo(rect.left() + 4.0, rect.bottom() - 4.0)
            mtn.lineTo(rect.left() + rect.width() * 0.38, rect.top() + rect.height() * 0.48)
            mtn.lineTo(rect.left() + rect.width() * 0.56, rect.bottom() - 7.0)
            mtn.lineTo(rect.left() + rect.width() * 0.72, rect.top() + rect.height() * 0.38)
            mtn.lineTo(rect.right() - 4.0, rect.bottom() - 4.0)
            mtn.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill_color)
            painter.drawPath(mtn)
            painter.setBrush(QColor("#FEF08A"))
            painter.drawEllipse(
                QPointF(rect.left() + rect.width() * 0.30, rect.top() + rect.height() * 0.30),
                2.2,
                2.2,
            )
            painter.restore()
            return

        path = _lit_shape_path(rect, shape)
        # Soft drop shadow for legibility on dark stage.
        painter.save()
        painter.translate(1.5, 2.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 110))
        painter.drawPath(path)
        painter.restore()

        # Crisp outline + fill.
        painter.setPen(
            QPen(
                stroke_color,
                2.0,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.setBrush(fill_color)
        painter.drawPath(path)

        # Specular highlight dot matching real signal.py geometry.
        hx, hy, hr = _lit_highlight_geometry(shape, rect)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 165))
        painter.drawEllipse(QPointF(hx, hy), max(1.2, hr), max(1.2, hr))
        painter.restore()

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

        shape = self._effective_shape()
        trans_mode = self._effective_transition()
        fill_col, stroke_col = self._lit_colors()

        # Timeline:
        # [0.00, 0.08): all 4 lamps lit (hold)
        # [0.08, 0.72): 4-beat countdown extinguishing right -> left
        # [0.72, 0.92): countdown finished, lyric begins singing!
        # [0.92, 1.00): smooth reset
        ph = self._phase
        start_x = (self.width() - self._line_width) / 2.0
        lamp_size = 23.0
        lamp_gap = 12.0
        lamp_top = 13.0
        lamp_x0 = start_x + 2.0

        count = self._LAMP_COUNT
        if ph < 0.08:
            active_index = count - 1
            lamp_phase = 1.0
            in_countdown = False
            sing_progress = 0.0
        elif ph < 0.72:
            elapsed = int(round(((ph - 0.08) / 0.64) * 2000.0))
            active_index, lamp_phase = shape_active_index_and_phase(elapsed, 2000, count)
            in_countdown = True
            sing_progress = 0.0
        else:
            active_index = -1
            lamp_phase = 0.0
            in_countdown = False
            sing_progress = _clamp01((ph - 0.72) / 0.20)

        # Extinguish transition parameters for the currently active lamp.
        ratio = 0.55 if self._category == "lit_transition_mode" else 0.35
        angle_deg = float(getattr(self._style, "lit_transition_angle_deg", 90) or 90)
        raw_dist = int(getattr(self._style, "lit_transition_distance", 24) or 0)
        slide_dist = float(raw_dist if raw_dist > 0 else 24)
        slide_dist = max(16.0, min(slide_dist, 34.0))

        # Faint baseline slots so the user always sees all 4 countdown beat positions.
        for i in range(count):
            lx = lamp_x0 + i * (lamp_size + lamp_gap)
            slot_rect = QRectF(lx, lamp_top, lamp_size, lamp_size)
            painter.save()
            painter.setPen(QPen(QColor(255, 255, 255, 28), 1.0, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if shape == "image":
                painter.drawRoundedRect(slot_rect, 5.0, 5.0)
            else:
                painter.drawPath(_lit_shape_path(slot_rect, shape))
            painter.restore()

        # Paint still-lit lamps and the currently extinguishing lamp.
        if active_index >= 0:
            for i in range(active_index + 1):
                lx = lamp_x0 + i * (lamp_size + lamp_gap)
                rect = QRectF(lx, lamp_top, lamp_size, lamp_size)
                opacity = 1.0
                dx = 0.0
                dy = 0.0
                if in_countdown and i == active_index:
                    # Extinguish occurs during the tail ``ratio`` of the lamp's beat (lamp_phase 1 -> 0).
                    inv = 1.0 - lamp_phase
                    prog = _clamp01((inv - (1.0 - ratio)) / max(ratio, 1e-4))
                    if trans_mode == "fade":
                        opacity = 1.0 - prog
                    elif trans_mode == "slide":
                        opacity = 1.0 - prog
                        rad = math.radians(angle_deg)
                        dx = -math.cos(rad) * slide_dist * prog
                        dy = -math.sin(rad) * slide_dist * prog
                        # Draw subtle slide trajectory guide from slot center to moving lamp.
                        if prog > 0.05:
                            painter.save()
                            painter.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 145), 1.2, Qt.PenStyle.DotLine))
                            painter.drawLine(rect.center(), QPointF(rect.center().x() + dx, rect.center().y() + dy))
                            painter.restore()
                    # trans_mode == "none": stays opacity=1 until beat ends, then hard cuts off.
                self._draw_single_lamp(
                    painter,
                    rect,
                    shape,
                    fill_color=fill_col,
                    stroke_color=stroke_col,
                    opacity=opacity,
                    dx=dx,
                    dy=dy,
                )

        # Lower lyric waiting for countdown -> starts wiping on beat 0!
        baseline_y = 103.0
        painter.setPen(QPen(QColor(255, 255, 255, 18), 1.0, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(16.0, baseline_y + 4.0), QPointF(self.width() - 16.0, baseline_y + 4.0))

        colors = self._appearance.colors
        cursor_x = start_x
        wipe_limit_x = start_x + self._line_width * 0.34 * sing_progress
        for idx, path in enumerate(self._glyph_paths):
            w = self._char_widths[idx]
            if not path.isEmpty():
                char_rect = QRectF(0.0, -self._ascent, w, self._ascent + self._descent)
                line_rect = QRectF(start_x - cursor_x, -self._ascent, self._line_width, self._ascent + self._descent)
                painter.save()
                painter.translate(cursor_x, baseline_y)
                if sing_progress <= 0.0 or cursor_x >= wipe_limit_x:
                    paint_styled_karaoke_glyph(
                        painter, path, char_rect, line_rect, colors.before, self._appearance, after=False
                    )
                elif cursor_x + w <= wipe_limit_x:
                    paint_styled_karaoke_glyph(
                        painter, path, char_rect, line_rect, colors.after, self._appearance, after=True
                    )
                else:
                    split_x = wipe_limit_x - cursor_x
                    clip_pad = 12.0
                    clip_top = -self._ascent - clip_pad
                    clip_h = self._ascent + self._descent + clip_pad * 2.0
                    painter.save()
                    painter.setClipRect(QRectF(-clip_pad, clip_top, split_x + clip_pad, clip_h))
                    paint_styled_karaoke_glyph(
                        painter, path, char_rect, line_rect, colors.after, self._appearance, after=True
                    )
                    painter.restore()
                    painter.save()
                    painter.setClipRect(QRectF(split_x, clip_top, w - split_x + clip_pad, clip_h))
                    paint_styled_karaoke_glyph(
                        painter, path, char_rect, line_rect, colors.before, self._appearance, after=False
                    )
                    painter.restore()
                ruby_layout = self._ruby_layouts[idx] if idx < len(self._ruby_layouts) else None
                if ruby_layout is not None:
                    kana_wipes = tuple(
                        0.0
                        if sing_progress <= 0.0
                        else _clamp01((wipe_limit_x - (cursor_x + kx)) / max(kw, 1.0))
                        for kx, kw in zip(ruby_layout.kana_xs, ruby_layout.kana_widths)
                    )
                    paint_styled_ruby_for_char(
                        painter,
                        ruby_layout,
                        ruby_appearance=self._ruby_appearance,
                        line_start_rel_x=start_x - cursor_x,
                        line_width=self._line_width,
                        kana_wipes=kana_wipes,
                    )
                painter.restore()
            cursor_x += w

        # Bottom progress rail.
        bar_x = 12.0
        bar_w = self.width() - 24.0
        bar_y = self.height() - 8.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 26))
        painter.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, 3.0), 1.5, 1.5)
        painter.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 80))
        painter.drawRoundedRect(QRectF(bar_x + bar_w * 0.08, bar_y, bar_w * 0.64, 3.0), 1.5, 1.5)
        painter.setBrush(accent if in_countdown else QColor(255, 255, 255, 130))
        painter.drawRoundedRect(QRectF(bar_x, bar_y, max(3.0, bar_w * ph), 3.0), 1.5, 1.5)

        if in_countdown:
            status = (f"倒计时 {active_index + 1}", True)
        elif sing_progress > 0.0:
            status = ("开唱！", True)
        else:
            status = ("预备亮灯", False)
        if status != self._last_status:
            self._last_status = status
            if self._on_status_change is not None:
                self._on_status_change(status[0], status[1])

        painter.restore()
        painter.end()


# ------------------------------------------------------------------ factory helpers


def make_effect_preview_combo(
    category: str,
    parent: Optional[QWidget] = None,
    *,
    style_provider: Optional[Callable[[], Any]] = None,
    inherit_key_provider: Optional[Callable[[], str]] = None,
) -> HoverPreviewComboBox:
    """Create a dropdown combo box with live hover preview for one effect category."""
    is_lit = category in {"lit_style", "lit_transition_mode"}

    def _popover_factory() -> PreviewPopoverFrame:
        canvas: QWidget = (
            LitIndicatorPreviewCanvas(category, "circle")
            if is_lit
            else SingingEffectPreviewCanvas(category, "none")
        )
        return PreviewPopoverFrame(canvas, None, object_name="EffectPreviewPopover")

    def _configure_popover(
        popover: PreviewPopoverFrame,
        item_data: Any,
        display_label: str,
    ) -> None:
        key = str(item_data if item_data is not None else "none")
        style = None
        if style_provider is not None:
            try:
                style = style_provider()
            except Exception:
                style = None
        meta = effect_preview_meta(category, key, fallback_title=display_label)
        prefix = _CATEGORY_PREFIX.get(category, "")
        title = f"{prefix}{display_label or meta.title}"
        summary = meta.summary

        if is_lit:
            assert isinstance(popover.canvas, LitIndicatorPreviewCanvas)
            popover.canvas.set_effect(category, key, style=style)
        else:
            assert isinstance(popover.canvas, SingingEffectPreviewCanvas)
            resolved_inherit = "none"
            if category == "reverse_karaoke_anim" and key == "inherit" and inherit_key_provider is not None:
                try:
                    resolved_inherit = str(inherit_key_provider() or "none")
                except Exception:
                    resolved_inherit = "none"
            popover.canvas.set_effect(
                category,
                key,
                style=style,
                resolved_inherit_key=resolved_inherit,
            )
        popover.set_header_and_summary(title, summary)

    return HoverPreviewComboBox(
        _popover_factory,
        _configure_popover,
        parent,
    )


def install_stroke_flash_hover(
    widget: QWidget,
    *,
    style_provider: Optional[Callable[[], Any]] = None,
) -> WidgetPreviewHoverFilter:
    """Attach the live singing stroke-flash preview popover to ``唱字闪光``."""

    def _popover_factory() -> PreviewPopoverFrame:
        canvas = SingingEffectPreviewCanvas("stroke_flash", "stroke_flash")
        return PreviewPopoverFrame(canvas, None, object_name="EffectPreviewPopover")

    def _configure_popover(popover: PreviewPopoverFrame) -> None:
        style = None
        if style_provider is not None:
            try:
                style = style_provider()
            except Exception:
                style = None
        assert isinstance(popover.canvas, SingingEffectPreviewCanvas)
        popover.canvas.set_effect("stroke_flash", "stroke_flash", style=style)
        popover.set_header_and_summary(STROKE_FLASH_META.title, STROKE_FLASH_META.summary)

    return install_widget_preview_hover(
        widget,
        _popover_factory,
        _configure_popover,
    )


__all__ = [
    "KARAOKE_ANIM_META",
    "LIT_STYLE_META",
    "LIT_TRANSITION_META",
    "LitIndicatorPreviewCanvas",
    "REVERSE_KARAOKE_ANIM_META",
    "SCANLINE_EFFECT_META",
    "SCANLINE_SHAPE_META",
    "SING_FX_META",
    "STROKE_FLASH_META",
    "SingingEffectPreviewCanvas",
    "effect_preview_meta",
    "install_stroke_flash_hover",
    "make_effect_preview_combo",
]
