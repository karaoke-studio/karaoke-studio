"""Asynchronous compact font preview controls for subtitle properties."""

from __future__ import annotations

from dataclasses import replace
import math
from typing import Optional

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
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
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
from krok_helper.subtitle_render.frontend.widgets.theme import palette
from krok_helper.subtitle_render.n3.font_catalog import resolve_qt_font_family


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
    _SUPERSAMPLE = 3.0
    _CANVAS_SIZE = QSize(
        _MAX_INK_SIZE.width() + _PADDING * 2,
        _MAX_INK_SIZE.height() + _PADDING * 2,
    )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._sample = QPixmap()
        self._rendering = False
        self.setFixedSize(self._CANVAS_SIZE)

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
    def _fit_sample_image(cls, image: QImage) -> QImage:
        logical_size = image.deviceIndependentSize().toSize()
        if not image.isNull() and (
            logical_size.width() > cls._MAX_INK_SIZE.width()
            or logical_size.height() > cls._MAX_INK_SIZE.height()
        ):
            dpr = image.devicePixelRatio() or self._SUPERSAMPLE
            image = image.scaled(
                QSize(
                    round(cls._MAX_INK_SIZE.width() * dpr),
                    round(cls._MAX_INK_SIZE.height() * dpr),
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
        font = QFont(family or "Microsoft YaHei UI")
        font.setPixelSize(max(int(size), 1))
        font.setWeight(QFont.Weight(max(100, min(int(weight or 400), 900))))
        font.setItalic(bool(italic))
        return font

    @staticmethod
    def _color(value: str, fallback: str = "#FFFFFF") -> QColor:
        color = QColor(value)
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
    def _render_sample_image(cls, style: Style, script: str) -> QImage:
        latin = script == "latin"
        main_text = "LinK" if latin else "人"
        ruby_text = "リンク" if latin else "ひと"
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
        shadow = QPoint(main_style.shadow_offset_x, main_style.shadow_offset_y)
        ruby_shadow = QPoint(
            _ruby_shadow_dx(ruby_style),
            _ruby_shadow_dy(ruby_style),
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
            if main_style.decoration_kind == "glow"
            else 0
        )
        ruby_glow_before = _ruby_glow_radius(ruby_style, after=False)
        ruby_glow_after = _ruby_glow_radius(ruby_style, after=True)
        ruby_decoration = _ruby_decoration_kind(ruby_style)
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
            if ruby_decoration == "glow"
            else 0
        )
        bounds = main_rect.united(ruby_rect)
        margin = max(
            stroke + stroke2,
            ruby_stroke + ruby_stroke2,
            abs(shadow.x()),
            abs(shadow.y()),
            abs(ruby_shadow.x()),
            abs(ruby_shadow.y()),
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
            _paint_char_karaoke_stack(
                painter,
                main_path,
                main_rect,
                char_x=round(main_rect.left()),
                char_width=max(round(main_rect.width()), 1),
                baseline_y=main_baseline,
                metrics=main_metrics,
                colors=colors,
                style=main_style,
                ratio=0.5,
                clip_rect=main_rect,
                fill_rect=main_rect,
            )
            _paint_ruby_karaoke_fragment(
                painter,
                ruby_path,
                ruby_rect,
                0.5,
                ruby_style,
                fill_rect=ruby_rect,
                horizontal_fill_rect=main_rect,
            )
        finally:
            painter.end()
        image.setDevicePixelRatio(scale)
        return cls._fit_sample_image(image)

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

    def __init__(self, style: Style, script: str, generation: int) -> None:
        super().__init__()
        self._style = style
        self._script = script
        self._generation = generation
        self.signals = _FontSampleRenderSignals()

    def run(self) -> None:
        try:
            image = _FontSampleCanvas._render_sample_image(
                self._style, self._script
            )
        except Exception:
            self.signals.failed.emit(self._generation)
            return
        self.signals.completed.emit(image, self._generation)


class _FontPreviewWidget(QWidget):
    """Small embedded sample owned exclusively by ``PropertyPanel``."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("SubtitleFontPreviewWidget")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = _FontSampleCanvas(self)
        layout.addWidget(self.canvas)
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
        task = _FontSampleRenderTask(style, script, generation)
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


