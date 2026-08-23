"""Lightweight Lin-K Lyrics startup splash window."""

import random
import time
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QTimer,
    Qt,
    pyqtProperty,
)
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
    QScreen,
)
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from krok_helper.config import APP_VERSION, APP_WINDOW_TITLE


STARTUP_IMAGE_PATH = Path(__file__).resolve().parent / "assets" / "logo" / "start.jpg"


class _SplashProgressBar(QWidget):
    """Rounded hairline progress bar painted over the splash background."""

    _HEIGHT = 5

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self._HEIGHT)
        self._value = 0.0

    def _get_value(self) -> float:
        return self._value

    def _set_value(self, value: float) -> None:
        self._value = value
        self.update()

    value = pyqtProperty(float, _get_value, _set_value)

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        radius = self._HEIGHT / 2
        track = QRectF(0, 0, self.width(), self._HEIGHT)
        clip = QPainterPath()
        clip.addRoundedRect(track, radius, radius)
        painter.setClipPath(clip)
        painter.setBrush(QColor(255, 255, 255, 40))
        painter.drawRect(track)

        fraction = max(0.0, min(1.0, self._value / 100.0))
        if fraction > 0.0:
            painter.setBrush(QColor(255, 255, 255, 225))
            fill = QRectF(0.0, 0.0, track.width() * fraction, self._HEIGHT)
            painter.drawRect(fill)


def select_startup_screen() -> QScreen | None:
    """Return the screen that should contain the startup splash."""

    return QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()


class StartupSplashWindow(QWidget):
    """Centered, frameless startup window shown while the workbench loads."""

    _SIDE = 400
    _RADIUS = 24
    #: 里程碑之间数字持续爬行：100ms 一步、随机步长，渐近逼近
    #: 「最近锚点 + _CREEP_HEADROOM」，且永不超过 _CREEP_CEILING。
    #: 主线程被长初始化（import / 大页面构造）卡住时 QTimer 冻结，恢复后
    #: 按墙钟一次补齐欠下的步数（封顶 _CREEP_MAX_CATCHUP_TICKS），数字
    #: 看起来一直在动；要真正逐帧刷新需把初始化挪出主线程。
    _CREEP_INTERVAL_MS = 100
    _CREEP_HEADROOM = 8.0
    _CREEP_CEILING = 99.4
    _CREEP_MAX_CATCHUP_TICKS = 40

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(self._SIDE, self._SIDE)

        self.target_screen = select_startup_screen()
        self.background_path = STARTUP_IMAGE_PATH
        self.background_pixmap = self._load_background(
            self.background_path,
            self.target_screen,
        )
        self._stage_text = "正在加载"
        self._anchor = 0.0
        self._last_creep_at = time.monotonic()
        self._creep_timer = QTimer(self)
        self._creep_timer.setInterval(self._CREEP_INTERVAL_MS)
        self._creep_timer.timeout.connect(self._creep_tick)
        self._creep_timer.start()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 0, 18, 22)
        layout.setSpacing(0)
        layout.addStretch()

        title = QLabel(f"{APP_WINDOW_TITLE} · v{APP_VERSION}", self)
        title.setObjectName("startupTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = title.font()
        title_font.setPixelSize(13)
        title_font.setWeight(QFont.Weight.DemiBold)
        available_title_width = self._SIDE - 36
        while (
            QFontMetrics(title_font).horizontalAdvance(title.text()) > available_title_width
            and title_font.pixelSize() > 9
        ):
            title_font.setPixelSize(title_font.pixelSize() - 1)
        title.setFont(title_font)
        title.setStyleSheet(
            "color: white; background: transparent;"
        )
        layout.addWidget(title)
        layout.addSpacing(10)

        status = QLabel("正在加载...", self)
        status.setObjectName("startupStatus")
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status.setStyleSheet(
            "color: rgba(255, 255, 255, 0.82); font-size: 12px; background: transparent;"
        )
        layout.addWidget(status)
        self.status_label = status
        layout.addSpacing(12)

        # 进度条在首次 set_progress 前保持隐藏，兼容只想要静态 splash 的调用方。
        self.progress_bar = _SplashProgressBar(self)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        self._center_on_target_screen(self.target_screen)

    def set_progress(self, percent: int | float, stage: str | None = None) -> None:
        """锚定一个进度里程碑并更新阶段文案。

        ``stage`` 为 None 时沿用上一次的阶段文案；percent 会被夹到 0-100。
        锚点之间由 :meth:`_creep_tick` 以随机步长持续爬行，状态行百分比
        带一位小数。锚定值即时落位且只增不减（爬行已超过新锚点时保持
        当前显示值）；调用方随后需让出主线程
        （``QApplication.processEvents()``）才会真正重绘。
        """

        if stage is not None:
            self._stage_text = stage
        clamped = max(0.0, min(100.0, float(percent)))
        self._anchor = max(self._anchor, clamped)
        if clamped >= 100.0:
            self._creep_timer.stop()
        if not self.progress_bar.isVisible():
            self.progress_bar.show()
        started = not self.progress_bar.isHidden()
        target = max(clamped, self.progress_bar.value) if started else clamped
        self._display(target)

    def _display(self, value: float) -> None:
        """把显示值写进进度条和状态行（一位小数）。"""

        self.progress_bar._set_value(value)
        self.status_label.setText(f"{self._stage_text}... ({value:.1f}%)")

    def _creep_tick(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_creep_at
        self._last_creep_at = now
        ticks = min(
            self._CREEP_MAX_CATCHUP_TICKS,
            max(1, int(elapsed * 1000.0 / self._CREEP_INTERVAL_MS)),
        )
        ceiling = min(self._anchor + self._CREEP_HEADROOM, self._CREEP_CEILING)
        for _ in range(ticks):
            display = self.progress_bar.value
            if display >= ceiling:
                return
            # 随机步长 + 越接近上限越慢：渐近逼近，不会撞顶，也不会在长启动里虚标过头。
            room = ceiling - display
            step = random.uniform(0.05, 0.45) * min(1.0, room / 4.0)
            self._display(min(ceiling, display + step))

    def _load_background(self, path: Path, screen: QScreen | None) -> QPixmap:
        source = QPixmap(str(path))
        if source.isNull():
            return source
        dpr = screen.devicePixelRatio() if screen else 1.0
        target = max(self._SIDE, int(round(self._SIDE * dpr)))
        background = source.scaled(
            target,
            target,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        background.setDevicePixelRatio(dpr)
        return background

    def _center_on_target_screen(self, screen: QScreen | None) -> None:
        if screen is None:
            return
        available = screen.availableGeometry()
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        clip = QPainterPath()
        clip.addRoundedRect(
            QRectF(0, 0, self._SIDE, self._SIDE),
            self._RADIUS,
            self._RADIUS,
        )
        painter.setClipPath(clip)

        if self.background_pixmap.isNull():
            painter.fillRect(self.rect(), QColor(35, 43, 61))
        else:
            painter.drawPixmap(0, 0, self.background_pixmap)

        overlay_top = int(self._SIDE * 0.64)
        overlay = QLinearGradient(0, overlay_top, 0, self._SIDE)
        overlay.setColorAt(0.0, QColor(0, 0, 0, 0))
        overlay.setColorAt(0.42, QColor(0, 0, 0, 145))
        overlay.setColorAt(1.0, QColor(0, 0, 0, 215))
        painter.fillRect(0, overlay_top, self._SIDE, self._SIDE - overlay_top, overlay)

    def finish(self) -> None:
        """Fade out and close after the main window becomes visible."""

        self._creep_timer.stop()
        self._fade_animation = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_animation.setDuration(300)
        self._fade_animation.setStartValue(1.0)
        self._fade_animation.setEndValue(0.0)
        self._fade_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_animation.finished.connect(self.close)
        self._fade_animation.start()
