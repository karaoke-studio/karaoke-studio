"""Asynchronous compact font preview controls for subtitle properties."""

from __future__ import annotations

from dataclasses import replace
import math
from typing import Optional

import numpy as np
from PyQt6.QtCore import (
    QObject,
    QPoint,
    QRunnable,
    QRectF,
    QSize,
    QThreadPool,
    QTimer,
    Qt,
    pyqtSignal as Signal,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTransform,
)
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.paint import KaraokeColors
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
    effective_ruby_karaoke_colors,
    style_for_role,
)
from krok_helper.subtitle_render.engine.style.style_preview import (
    build_font as _build_font,
    build_latin_font as _build_latin_font,
    build_ruby_font_for_text as _build_ruby_font_for_text,
    glow_extent as _glow_extent,
    main_script_stroke_style as _main_script_stroke_style,
    main_stroke2_width as _main_stroke2_width,
    n3_char_box_ascent as _n3_char_box_ascent,
    paint_char_karaoke_stack as _paint_char_karaoke_stack,
    paint_ruby_karaoke_fragment as _paint_ruby_karaoke_fragment,
    ruby_baseline_y as _ruby_baseline_y,
    ruby_decoration_kind as _ruby_decoration_kind,
    ruby_glow_radius as _ruby_glow_radius,
    ruby_script_stroke_style as _ruby_script_stroke_style,
    ruby_shadow_dx as _ruby_shadow_dx,
    ruby_shadow_dy as _ruby_shadow_dy,
    ruby_stroke2_width as _ruby_stroke2_width,
    ruby_stroke_width as _ruby_stroke_width,
)
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed
from krok_helper.subtitle_render.engine.render.effects.fills import (
    brush_for_fill as _brush_for_fill,
    fill_brush_rect as _fill_brush_rect,
)
from krok_helper.subtitle_render.engine.render.effects.metrics import (
    decoration_kind_for_state as _decoration_kind_for_state,
    ruby_scale as _ruby_scale,
    scaled_px as _scaled_px,
    shadow_offset_x as _shadow_offset_x,
    shadow_offset_y as _shadow_offset_y,
)
from krok_helper.subtitle_render.engine.text.font_weight import build_weight_font
from krok_helper.subtitle_render.n3.font_catalog import resolve_qt_font_family


def _box_blur_premultiplied_image(image: QImage, blur_px: float) -> QImage:
    """Thread-safe 3-pass separable box blur approximating Gaussian sigma = blur_px / 3."""
    if blur_px <= 0.0 or image.isNull():
        return image
    sigma = blur_px / 3.0
    radius = max(1, int(round((math.sqrt(4.0 * sigma * sigma + 1.0) - 1.0) * 0.5)))
    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        return image
    src = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    ptr = src.constBits()
    ptr.setsize(src.sizeInBytes())
    stride = src.bytesPerLine()
    raw = np.frombuffer(ptr, dtype=np.uint8).reshape((height, stride))
    arr = raw[:, : width * 4].reshape((height, width, 4)).astype(np.float32)

    def blur_1d(data: np.ndarray, axis: int, r: int) -> np.ndarray:
        pad = [(0, 0), (0, 0), (0, 0)]
        pad[axis] = (r + 1, r)
        padded = np.pad(data, pad, mode="constant", constant_values=0.0)
        csum = np.cumsum(padded, axis=axis, dtype=np.float32)
        window = float(2 * r + 1)
        if axis == 1:
            return (csum[:, 2 * r + 1 :, :] - csum[:, : -(2 * r + 1), :]) / window
        return (csum[2 * r + 1 :, :, :] - csum[: -(2 * r + 1), :, :]) / window

    for _ in range(3):
        arr = blur_1d(arr, 1, radius)
        arr = blur_1d(arr, 0, radius)

    out_bytes = np.clip(np.rint(arr), 0, 255).astype(np.uint8)
    out = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    out_ptr = out.bits()
    out_ptr.setsize(out.sizeInBytes())
    out_raw = np.frombuffer(out_ptr, dtype=np.uint8).reshape((height, out.bytesPerLine()))
    out_raw[:, : width * 4] = out_bytes.reshape((height, width * 4))
    return out


def _colors_with_glow_in_shadow_slot(colors: KaraokeColors) -> KaraokeColors:
    return replace(
        colors,
        before=replace(colors.before, shadow=colors.before.glow or colors.before.shadow),
        after=replace(colors.after, shadow=colors.after.glow or colors.after.shadow),
    )


def _paint_sample_shadow_layer(
    dest_painter: QPainter,
    image_size: QSize,
    bounds: QRectF,
    scale: float,
    *,
    path: QPainterPath,
    rect: QRectF,
    fill_rect: QRectF,
    horizontal_fill_rect: Optional[QRectF],
    outline_width: float,
    ratio: float,
    colors: KaraokeColors,
    before_active: bool,
    after_active: bool,
    before_offset: tuple[int, int],
    after_offset: tuple[int, int],
    blur_px: int,
    spread_px: int,
) -> None:
    """Paint soft/spread preview shadow layer onto ``dest_painter``."""
    if path.isEmpty() or (not before_active and not after_active):
        return
    split_x = rect.left() + rect.width() * max(0.0, min(1.0, float(ratio)))
    blur = max(int(blur_px), 0)
    spread = max(int(spread_px), 0)
    pen_width = max(float(outline_width), 0.0) + float(spread) * 2.0

    for after, active, (dx, dy) in (
        (False, before_active, before_offset),
        (True, after_active, after_offset),
    ):
        if not active or (dx == 0 and dy == 0 and blur == 0 and spread == 0):
            continue
        state = colors.after if after else colors.before
        layer = QImage(image_size, QImage.Format.Format_ARGB32_Premultiplied)
        layer.fill(Qt.GlobalColor.transparent)
        lp = QPainter(layer)
        try:
            lp.setRenderHint(QPainter.RenderHint.Antialiasing)
            lp.scale(scale, scale)
            lp.translate(-bounds.left(), -bounds.top())
            if after:
                clip = QRectF(bounds.left(), bounds.top(), max(0.0, split_x - bounds.left()), bounds.height())
            else:
                clip = QRectF(split_x, bounds.top(), max(0.0, bounds.right() - split_x), bounds.height())
            lp.setClipRect(clip)
            lp.translate(float(dx), float(dy))
            if pen_width > 0.0:
                lp.setPen(
                    QPen(
                        Qt.GlobalColor.white,
                        pen_width,
                        Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin,
                    )
                )
            else:
                lp.setPen(Qt.PenStyle.NoPen)
            lp.setBrush(Qt.GlobalColor.white)
            lp.drawPath(path)
            lp.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            brush = _brush_for_fill(
                state.shadow,
                _fill_brush_rect(state.shadow, fill_rect, horizontal_fill_rect),
            )
            lp.fillRect(bounds.translated(-float(dx), -float(dy)), brush)
        finally:
            lp.end()
        if blur > 0:
            layer = _box_blur_premultiplied_image(layer, float(blur) * scale)
        dest_painter.save()
        dest_painter.resetTransform()
        dest_painter.drawImage(0, 0, layer)
        dest_painter.restore()


def _ink_fill_rect(path: QPainterPath, fallback: QRectF, pad: int) -> QRectF:
    """Glyph-ink fill anchor mirroring ``n3_main_fill_rect`` for the sample."""

    ink = path.boundingRect()
    if ink.isEmpty():
        return fallback
    return QRectF(ink).adjusted(-pad, -pad, pad, pad)


def _resolve_font_preview_families(style: Style) -> Style:
    """Materialize Qt runtime family names before crossing the worker boundary.

    Project/N3 styles intentionally preserve localized display names.  The
    production painter resolves those names immediately before constructing a
    ``QFont``.  Font preview rendering runs in a worker, so perform the same
    Qt-font-registry lookup on the GUI thread and pass only resolved strings to
    the worker.
    """

    changes: dict[str, Optional[str]] = {}
    for field_name in (
        "font_family",
        "font_family_latin",
        "ruby_font_family",
        "ruby_font_family_latin",
    ):
        family = getattr(style, field_name, None)
        if family:
            changes[field_name] = resolve_qt_font_family(str(family))
    return replace(style, **changes) if changes else style


class _FontSampleCanvas(QWidget):
    """Render a compact role sample without involving a project preview window."""

    _MAX_INK_SIZE = QSize(104, 96)
    _PADDING = 8
    _COMPACT_MAX_INK_SIZE = QSize(156, 64)
    _COMPACT_PADDING = 6
    _SUPERSAMPLE = 3.0
    _CANVAS_SIZE = QSize(
        _MAX_INK_SIZE.width() + _PADDING * 2,
        _MAX_INK_SIZE.height() + _PADDING * 2,
    )
    _COMPACT_CANVAS_SIZE = QSize(
        _COMPACT_MAX_INK_SIZE.width() + _COMPACT_PADDING * 2,
        _COMPACT_MAX_INK_SIZE.height() + _COMPACT_PADDING * 2,
    )

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        max_ink_size: Optional[QSize] = None,
        padding: Optional[int] = None,
    ) -> None:
        super().__init__(parent)
        self._sample = QPixmap()
        self._rendering = False
        self._max_ink_size = QSize(max_ink_size or self._MAX_INK_SIZE)
        self._padding = self._PADDING if padding is None else int(padding)
        self.setFixedSize(
            QSize(
                self._max_ink_size.width() + self._padding * 2,
                self._max_ink_size.height() + self._padding * 2,
            )
        )

    def set_rendering(self, rendering: bool) -> None:
        if self._rendering == bool(rendering):
            return
        self._rendering = bool(rendering)
        self.setAccessibleName("字体预览（正在渲染）" if rendering else "字体预览")
        self.update()

    def apply_sample(self, image: QImage) -> None:
        sample = QPixmap.fromImage(image)
        self._sample = sample
        self.update()

    @classmethod
    def _fit_sample_image(
        cls, image: QImage, *, max_ink_size: Optional[QSize] = None
    ) -> QImage:
        limit = max_ink_size or cls._MAX_INK_SIZE
        logical_size = image.deviceIndependentSize().toSize()
        if not image.isNull() and (
            logical_size.width() > limit.width()
            or logical_size.height() > limit.height()
        ):
            dpr = image.devicePixelRatio() or cls._SUPERSAMPLE
            image = image.scaled(
                QSize(
                    round(limit.width() * dpr),
                    round(limit.height() * dpr),
                ),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            image.setDevicePixelRatio(dpr)
        return image

    @staticmethod
    def _font(
        family: Optional[str], size: int, weight: Optional[int], italic: bool
    ) -> QFont:
        # 与正式渲染同一套「字重→实例」统一口径，保证预览字形/宽度一致。
        return build_weight_font(
            resolve_qt_font_family(family or "Microsoft YaHei UI"),
            int(size),
            int(weight or 400),
            italic=bool(italic),
        )

    @staticmethod
    def _color(value: str, fallback: str = "#FFFFFF") -> QColor:
        from ....domain.color import display_hex, linear_rgba
        color = QColor(display_hex(value) if linear_rgba(value) is not None else value)
        return color if color.isValid() else QColor(fallback)

    @classmethod
    def _brush(cls, fill, rect: QRectF) -> QBrush:
        mode = getattr(fill, "mode", "solid")
        fallback = getattr(fill, "color", "#FFFFFF")
        if mode in {"gradient_horizontal", "gradient_vertical"}:
            horizontal = mode == "gradient_horizontal"
            gradient = QLinearGradient(
                rect.left() if horizontal else rect.center().x(),
                rect.center().y() if horizontal else rect.top(),
                rect.right() if horizontal else rect.center().x(),
                rect.center().y() if horizontal else rect.bottom(),
            )
            stops = list(getattr(fill, "gradient_stops", ())) or [
                (0, getattr(fill, "start_color", fallback)),
                (100, getattr(fill, "end_color", fallback)),
            ]
            for position, color in sorted(stops, key=lambda item: float(item[0])):
                gradient.setColorAt(
                    max(0.0, min(float(position) / 100.0, 1.0)),
                    cls._color(color, fallback),
                )
            return QBrush(gradient)
        if mode == "split_vertical":
            gradient = QLinearGradient(
                rect.center().x(), rect.top(), rect.center().x(), rect.bottom()
            )
            stops = list(getattr(fill, "split_stops", ()))
            if len(stops) < 2:
                split = float(getattr(fill, "split_position_pct", 50))
                stops = [
                    (0, getattr(fill, "split_top_color", fallback)),
                    (split, getattr(fill, "split_bottom_color", fallback)),
                    (100, getattr(fill, "split_bottom_color", fallback)),
                ]
            ordered = sorted(stops, key=lambda item: float(item[0]))
            for index, (position, color) in enumerate(ordered):
                ratio = max(0.0, min(float(position) / 100.0, 1.0))
                if index and ordered[index - 1][1] != color:
                    gradient.setColorAt(
                        max(0.0, ratio - 0.0001),
                        cls._color(ordered[index - 1][1], fallback),
                    )
                gradient.setColorAt(ratio, cls._color(color, fallback))
            return QBrush(gradient)
        if mode == "image" and getattr(fill, "image_path", ""):
            image = QImage(str(fill.image_path))
            if not image.isNull():
                brush = QBrush()
                brush.setTextureImage(image)
                transform = QTransform()
                scale = max(float(getattr(fill, "image_scale_pct", 100)), 1.0) / 100.0
                transform.translate(rect.left(), rect.top())
                transform.scale(scale, scale)
                brush.setTransform(transform)
                return brush
        return QBrush(cls._color(fallback))

    @classmethod
    def _render_sample_image(
        cls,
        style: Style,
        script: str,
        *,
        max_ink_size: Optional[QSize] = None,
    ) -> QImage:
        latin = script == "latin"
        main_text = "LinK" if latin else "人"
        ruby_text = "link" if latin else "ひと"
        # Keep this an isolated sample renderer: only reuse the production
        # glyph/font/layer primitives.  Calling paint_frame here would also
        # traverse project overlays (notably titles), which do not belong in
        # the compact role preview.
        main_style = _main_script_stroke_style(style, main_text)
        ruby_style = _ruby_script_stroke_style(style, ruby_text)
        main_font = _build_latin_font(main_style) if latin else _build_font(main_style)
        main_metrics = QFontMetrics(main_font)
        main_baseline = 0
        main_advance = max(main_metrics.horizontalAdvance(main_text), 1)
        main_rect = QRectF(
            0,
            main_baseline - main_metrics.ascent(),
            main_advance,
            main_metrics.height(),
        )
        main_path = QPainterPath()
        main_path.addText(0, main_baseline, main_font, main_text)

        ruby_font = _build_ruby_font_for_text(ruby_style, ruby_text)
        ruby_metrics = QFontMetrics(ruby_font)
        ruby_advance = max(ruby_metrics.horizontalAdvance(ruby_text), 1)
        ruby_x = (main_advance - ruby_advance) / 2.0
        ruby_baseline = _ruby_baseline_y(
            main_baseline,
            _n3_char_box_ascent(
                main_metrics,
                main_font.pixelSize(),
                main_style.stroke_width_px,
            ),
            ruby_metrics,
            ruby_style,
            font_size_px=ruby_font.pixelSize(),
        )
        ruby_rect = QRectF(
            ruby_x,
            ruby_baseline - ruby_metrics.ascent(),
            ruby_advance,
            ruby_metrics.height(),
        )
        ruby_path = QPainterPath()
        ruby_path.addText(ruby_x, ruby_baseline, ruby_font, ruby_text)
        colors = effective_karaoke_colors(style)
        stroke = max(int(main_style.stroke_width_px), 0)
        stroke2 = _main_stroke2_width(main_style)
        ruby_stroke = _ruby_stroke_width(ruby_style)
        ruby_stroke2 = _ruby_stroke2_width(ruby_style)
        # The formal N3 metric boxes can touch when ruby_gap_px == 0.  Some
        # typefaces overhang those boxes, and thick outlines then overlap even
        # though their baselines are correct.  The compact preview has no line
        # layout around it to hide that collision, so move ruby only by the
        # measured excess of the two *actual* outlined paths.
        main_ink_top = main_path.boundingRect().top() - (stroke + stroke2) / 2.0
        ruby_ink_bottom = (
            ruby_path.boundingRect().bottom() + (ruby_stroke + ruby_stroke2) / 2.0
        )
        collision = ruby_ink_bottom - main_ink_top
        if collision >= 0.0:
            ruby_shift_y = -(collision + 1.0)
            ruby_path.translate(0, ruby_shift_y)
            ruby_rect.translate(0, ruby_shift_y)
            ruby_baseline += ruby_shift_y
        shadow = QPoint(
            max(
                abs(_shadow_offset_x(main_style, after=False)),
                abs(_shadow_offset_x(main_style, after=True)),
            ),
            max(
                abs(_shadow_offset_y(main_style, after=False)),
                abs(_shadow_offset_y(main_style, after=True)),
            ),
        )
        ruby_shadow = QPoint(
            max(
                abs(_ruby_shadow_dx(ruby_style, after=False)),
                abs(_ruby_shadow_dx(ruby_style, after=True)),
            ),
            max(
                abs(_ruby_shadow_dy(ruby_style, after=False)),
                abs(_ruby_shadow_dy(ruby_style, after=True)),
            ),
        )
        main_before_kind = _decoration_kind_for_state(main_style, after=False)
        main_after_kind = _decoration_kind_for_state(main_style, after=True)
        ruby_before_kind = _ruby_decoration_kind(ruby_style, after=False)
        ruby_after_kind = _ruby_decoration_kind(ruby_style, after=True)
        main_shadow_blur = max(int(main_style.shadow_blur_px), 0)
        main_shadow_spread = max(int(main_style.shadow_spread_px), 0)
        main_shadow_extra = (
            main_shadow_blur + main_shadow_spread
            if main_before_kind in {"shadow", "both"}
            or main_after_kind in {"shadow", "both"}
            else 0
        )
        r_scale = _ruby_scale(ruby_style)
        ruby_shadow_blur = max(
            int(ruby_style.ruby_shadow_blur_px)
            if ruby_style.ruby_shadow_blur_px is not None
            else _scaled_px(main_shadow_blur, r_scale),
            0,
        )
        ruby_shadow_spread = max(
            int(ruby_style.ruby_shadow_spread_px)
            if ruby_style.ruby_shadow_spread_px is not None
            else _scaled_px(main_shadow_spread, r_scale),
            0,
        )
        ruby_shadow_extra = (
            ruby_shadow_blur + ruby_shadow_spread
            if ruby_before_kind in {"shadow", "both"}
            or ruby_after_kind in {"shadow", "both"}
            else 0
        )
        main_glow_extent = (
            max(
                _glow_extent(
                    stroke,
                    stroke2,
                    max(int(main_style.glow_before_radius_px), 0),
                ),
                _glow_extent(
                    stroke,
                    stroke2,
                    max(int(main_style.glow_after_radius_px), 0),
                ),
            )
            if main_before_kind in {"glow", "both"}
            or main_after_kind in {"glow", "both"}
            else 0
        )
        ruby_glow_before = _ruby_glow_radius(ruby_style, after=False)
        ruby_glow_after = _ruby_glow_radius(ruby_style, after=True)
        ruby_glow_extent = (
            max(
                _glow_extent(
                    ruby_stroke,
                    ruby_stroke2,
                    ruby_glow_before,
                ),
                _glow_extent(
                    ruby_stroke,
                    ruby_stroke2,
                    ruby_glow_after,
                ),
            )
            if ruby_before_kind in {"glow", "both"}
            or ruby_after_kind in {"glow", "both"}
            else 0
        )
        bounds = main_rect.united(ruby_rect)
        margin = max(
            stroke + stroke2,
            ruby_stroke + ruby_stroke2,
            abs(shadow.x()) + main_shadow_extra,
            abs(shadow.y()) + main_shadow_extra,
            abs(ruby_shadow.x()) + ruby_shadow_extra,
            abs(ruby_shadow.y()) + ruby_shadow_extra,
            main_glow_extent,
            ruby_glow_extent,
        ) + 4
        bounds = bounds.adjusted(-margin, -margin, margin, margin)
        scale = cls._SUPERSAMPLE
        image = QImage(
            max(int(math.ceil(bounds.width() * scale)), 1),
            max(int(math.ceil(bounds.height() * scale)), 1),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            painter.scale(scale, scale)
            painter.translate(-bounds.left(), -bounds.top())
            main_fill_rect = _ink_fill_rect(
                main_path,
                main_rect,
                math.ceil((stroke + stroke2) / 2),
            )
            ruby_fill_rect = _ink_fill_rect(
                ruby_path,
                ruby_rect,
                math.ceil((ruby_stroke + ruby_stroke2) / 2),
            )
            ruby_colors = effective_ruby_karaoke_colors(ruby_style)
            custom_main = (
                main_before_kind == "both"
                or main_after_kind == "both"
                or main_shadow_extra > 0
                or colors.before.glow is not None
                or colors.after.glow is not None
            )
            custom_ruby = (
                ruby_before_kind == "both"
                or ruby_after_kind == "both"
                or ruby_shadow_extra > 0
                or ruby_colors.before.glow is not None
                or ruby_colors.after.glow is not None
            )
            if custom_main:
                _paint_sample_shadow_layer(
                    painter,
                    image.size(),
                    bounds,
                    scale,
                    path=main_path,
                    rect=main_rect,
                    fill_rect=main_fill_rect,
                    horizontal_fill_rect=None,
                    outline_width=float(stroke + stroke2),
                    ratio=0.5,
                    colors=colors,
                    before_active=main_before_kind in {"shadow", "both"},
                    after_active=main_after_kind in {"shadow", "both"},
                    before_offset=(
                        _shadow_offset_x(main_style, after=False),
                        _shadow_offset_y(main_style, after=False),
                    ),
                    after_offset=(
                        _shadow_offset_x(main_style, after=True),
                        _shadow_offset_y(main_style, after=True),
                    ),
                    blur_px=main_shadow_blur,
                    spread_px=main_shadow_spread,
                )
                main_draw_style = replace(
                    main_style,
                    decoration_before_sync=False,
                    decoration_kind=(
                        "glow" if main_after_kind in {"glow", "both"} else "none"
                    ),
                    decoration_kind_before=(
                        "glow" if main_before_kind in {"glow", "both"} else "none"
                    ),
                )
                main_draw_colors = _colors_with_glow_in_shadow_slot(colors)
            else:
                main_draw_style = main_style
                main_draw_colors = colors
            _paint_char_karaoke_stack(
                painter,
                main_path,
                main_rect,
                char_x=round(main_rect.left()),
                char_width=max(round(main_rect.width()), 1),
                baseline_y=main_baseline,
                metrics=main_metrics,
                colors=main_draw_colors,
                style=main_draw_style,
                ratio=0.5,
                clip_rect=main_rect,
                # 与主渲染 n3_main_fill_rect 同口径：拼色/纵渐变按墨水并集
                # 锚定，预览所见即成片所得。
                fill_rect=main_fill_rect,
            )
            if custom_ruby:
                _paint_sample_shadow_layer(
                    painter,
                    image.size(),
                    bounds,
                    scale,
                    path=ruby_path,
                    rect=ruby_rect,
                    fill_rect=ruby_fill_rect,
                    horizontal_fill_rect=main_rect,
                    outline_width=float(ruby_stroke + ruby_stroke2),
                    ratio=0.5,
                    colors=ruby_colors,
                    before_active=ruby_before_kind in {"shadow", "both"},
                    after_active=ruby_after_kind in {"shadow", "both"},
                    before_offset=(
                        _ruby_shadow_dx(ruby_style, after=False),
                        _ruby_shadow_dy(ruby_style, after=False),
                    ),
                    after_offset=(
                        _ruby_shadow_dx(ruby_style, after=True),
                        _ruby_shadow_dy(ruby_style, after=True),
                    ),
                    blur_px=ruby_shadow_blur,
                    spread_px=ruby_shadow_spread,
                )
                ruby_draw_style = replace(
                    ruby_style,
                    decoration_before_sync=False,
                    ruby_colors_follow_main=False,
                    ruby_karaoke_colors=_colors_with_glow_in_shadow_slot(ruby_colors),
                    ruby_decoration_kind=(
                        "glow" if ruby_after_kind in {"glow", "both"} else "none"
                    ),
                    ruby_decoration_kind_before=(
                        "glow" if ruby_before_kind in {"glow", "both"} else "none"
                    ),
                )
            else:
                ruby_draw_style = ruby_style
            _paint_ruby_karaoke_fragment(
                painter,
                ruby_path,
                ruby_rect,
                0.5,
                ruby_draw_style,
                fill_rect=ruby_fill_rect,
                horizontal_fill_rect=main_rect,
            )
        finally:
            painter.end()
        image.setDevicePixelRatio(scale)
        return cls._fit_sample_image(image, max_ink_size=max_ink_size)

    @classmethod
    def _render_sample(cls, style: Style, script: str) -> QPixmap:
        """Synchronous compatibility helper used only by focused renderer tests."""
        return QPixmap.fromImage(cls._render_sample_image(style, script))

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # Keep the sample theme-compatible while separating it slightly
            # from the surrounding property card.
            painter.setBrush(QColor("#202124" if palette().is_dark else "#F3F5F8"))
            border = QColor(palette().input_border_focus)
            if not border.isValid():
                border = QColor("#FF5A6F")
            painter.setPen(QPen(border, 1.5))
            painter.drawRoundedRect(
                QRectF(self.rect()).adjusted(1, 1, -1, -1), 10, 10
            )
            if not self._sample.isNull():
                logical = self._sample.deviceIndependentSize()
                painter.drawPixmap(
                    round((self.width() - logical.width()) / 2),
                    round((self.height() - logical.height()) / 2),
                    self._sample,
                )
            if self._rendering:
                overlay = QRectF(self.rect()).adjusted(2, 2, -2, -2)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(
                    QColor(20, 20, 20, 150)
                    if palette().is_dark
                    else QColor(255, 255, 255, 190)
                )
                painter.drawRoundedRect(overlay, 9, 9)
                painter.setPen(QColor("#F4F4F5" if palette().is_dark else "#374151"))
                painter.drawText(
                    overlay,
                    Qt.AlignmentFlag.AlignCenter,
                    "正在渲染…",
                )
        finally:
            painter.end()


class _FontSampleRenderSignals(QObject):
    completed = Signal(QImage, int)
    failed = Signal(int)


class _FontSampleRenderTask(QRunnable):
    """QImage-only worker; it never creates or touches a QWidget/QPixmap."""

    def __init__(
        self,
        style: Style,
        script: str,
        generation: int,
        *,
        max_ink_size: Optional[QSize] = None,
    ) -> None:
        super().__init__()
        self._style = style
        self._script = script
        self._generation = generation
        self._max_ink_size = max_ink_size
        self.signals = _FontSampleRenderSignals()

    def run(self) -> None:
        try:
            if self._max_ink_size is None:
                image = _FontSampleCanvas._render_sample_image(
                    self._style,
                    self._script,
                )
            else:
                image = _FontSampleCanvas._render_sample_image(
                    self._style,
                    self._script,
                    max_ink_size=self._max_ink_size,
                )
        except Exception:
            self.signals.failed.emit(self._generation)
            return
        self.signals.completed.emit(image, self._generation)


def _scheme_display_name(scheme_key: str) -> str:
    """角色导航的方案键转显示名：全局键之外都带 ``custom:`` 前缀。"""
    if scheme_key.startswith("custom:"):
        return scheme_key.removeprefix("custom:")
    return "全局默认"


class _FontPreviewWidget(QWidget):
    """Small embedded sample owned exclusively by ``PropertyPanel``."""

    def __init__(self, parent: QWidget, *, compact: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("SubtitleFontPreviewWidget")
        self._compact = bool(compact)
        if self._compact:
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)
            strip = QFrame(self)
            strip.setObjectName("SubtitleFontPreviewStrip")
            themed(
                strip,
                lambda: (
                    "QFrame#SubtitleFontPreviewStrip { "
                    f"background: {palette().shell_bg}; "
                    f"border: 1px solid {palette().input_border}; "
                    "border-radius: 8px; "
                    "}"
                ),
            )
            strip_layout = QHBoxLayout(strip)
            strip_layout.setContentsMargins(12, 6, 8, 6)
            strip_layout.setSpacing(12)

            info_layout = QVBoxLayout()
            info_layout.setContentsMargins(0, 0, 0, 0)
            info_layout.setSpacing(2)
            info_layout.addStretch(1)
            self.title_label = QLabel("样式预览", strip)
            self.title_label.setObjectName("SubtitleFontPreviewTitle")
            themed(
                self.title_label,
                lambda: (
                    "QLabel#SubtitleFontPreviewTitle { "
                    f"color: {palette().title_text}; "
                    "font-size: 9pt; "
                    "font-weight: 600; "
                    "border: 0; "
                    "}"
                ),
            )
            info_layout.addWidget(self.title_label)
            self.target_caption = QLabel(strip)
            self.target_caption.setObjectName("SubtitleFontPreviewTargetCaption")
            self.target_caption.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            self.target_caption.setMinimumWidth(80)
            self.target_caption.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
            )
            info_layout.addWidget(self.target_caption)
            info_layout.addStretch(1)
            strip_layout.addLayout(info_layout, 1)

            self.canvas = _FontSampleCanvas(
                strip,
                max_ink_size=_FontSampleCanvas._COMPACT_MAX_INK_SIZE,
                padding=_FontSampleCanvas._COMPACT_PADDING,
            )
            strip_layout.addWidget(self.canvas, 0, Qt.AlignmentFlag.AlignVCenter)
            outer.addWidget(strip)
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
        else:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(4)
            self.canvas = _FontSampleCanvas(self)
            layout.addWidget(self.canvas)
            # 预览图常驻一行「编辑对象」题注：用户注意力在下方控件上时，
            # 这里是页面上唯一持续标明当前改的是谁的地方。
            self.target_caption = QLabel(self)
            self.target_caption.setObjectName("SubtitleFontPreviewTargetCaption")
            self.target_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(self.target_caption)
        themed(
            self.target_caption,
            lambda: (
                "QLabel#SubtitleFontPreviewTargetCaption { "
                f"color: {palette().text_secondary}; "
                "font-size: 8.5pt; "
                "border: 0; "
                "}"
            ),
        )
        self._style = Style()
        self._scheme_key = "global"
        self._script = "japanese"
        self._sample_text = "人"
        self._ruby_text = "ひと"
        self._render_generation = 0
        self._render_busy = False
        self._pending_render: Optional[tuple[int, Style, str]] = None
        self._active_render_task: Optional[_FontSampleRenderTask] = None
        self._render_debounce = QTimer(self)
        self._render_debounce.setSingleShot(True)
        self._render_debounce.setInterval(80)
        self._render_debounce.timeout.connect(self._dispatch_render)
        self._refresh_sample()

    def set_preview_state(self, style: Style, scheme_key: str, script: str) -> None:
        self._style = replace(
            style,
            title_overlays=[],
            lit_enabled=False,
            layouts=[],
            viewport_align="center",
            viewport_offset_x=0,
            viewport_offset_y=0,
            viewport_scale_pct=100,
            viewport_rotation_deg=0,
            line_y_position="center",
            line_y_margin_px=0,
            dual_line_layout=False,
            line_horizontal_layout="center",
            line_alignments=["center"],
            horizontal_margin_px=0,
            smart_horizontal="none",
            entry_anim="none",
            entry_lead_ms=0,
            exit_anim="none",
            exit_fade_ms=0,
            right_to_left=False,
            vertical=False,
        )
        self._scheme_key = str(scheme_key or "global")
        self._script = "latin" if script == "latin" else "japanese"
        self._refresh_sample()

    def _refresh_sample(self) -> None:
        self.target_caption.setText(f"编辑对象：{_scheme_display_name(self._scheme_key)}")
        role_label = (
            self._scheme_key.removeprefix("custom:")
            if self._scheme_key.startswith("custom:")
            else None
        )
        self._sample_text = "LinK" if self._script == "latin" else "人"
        self._ruby_text = "リンク" if self._script == "latin" else "ひと"
        self._render_generation += 1
        resolved = _resolve_font_preview_families(
            style_for_role(self._style, role_label)
        )
        self._pending_render = (
            self._render_generation,
            resolved,
            self._script,
        )
        self.canvas.set_rendering(True)
        self._render_debounce.start()

    def _dispatch_render(self) -> None:
        if self._render_busy or self._pending_render is None:
            return
        generation, style, script = self._pending_render
        self._pending_render = None
        self._render_busy = True
        task = _FontSampleRenderTask(
            style,
            script,
            generation,
            max_ink_size=self.canvas._max_ink_size if self._compact else None,
        )
        self._active_render_task = task
        task.signals.completed.connect(
            self._on_render_completed,
            Qt.ConnectionType.QueuedConnection,
        )
        task.signals.failed.connect(
            self._on_render_failed,
            Qt.ConnectionType.QueuedConnection,
        )
        QThreadPool.globalInstance().start(task)

    def _on_render_completed(self, image: QImage, generation: int) -> None:
        if generation == self._render_generation:
            self.canvas.apply_sample(image)
        self._finish_render_task()

    def _on_render_failed(self, _generation: int) -> None:
        self._finish_render_task()

    def _finish_render_task(self) -> None:
        self._render_busy = False
        self._active_render_task = None
        if self._pending_render is not None:
            self._render_debounce.start()
            return
        self.canvas.set_rendering(False)


