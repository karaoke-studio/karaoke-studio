"""Lightweight Karaoke Studio startup splash window."""

from pathlib import Path

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, Qt
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

from krok_helper.config import APP_VERSION


STARTUP_IMAGE_PATH = Path(__file__).resolve().parent / "assets" / "logo" / "start.jpg"


def select_startup_screen() -> QScreen | None:
    """Return the screen that should contain the startup splash."""

    return QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()


class StartupSplashWindow(QWidget):
    """Centered, frameless startup window shown while the workbench loads."""

    _SIDE = 400
    _RADIUS = 24

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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 0, 18, 22)
        layout.setSpacing(0)
        layout.addStretch()

        title = QLabel(f"Karaoke Studio · 卡拉OK工作台 · v{APP_VERSION}", self)
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

        self._center_on_target_screen(self.target_screen)

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

        self._fade_animation = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_animation.setDuration(300)
        self._fade_animation.setStartValue(1.0)
        self._fade_animation.setEndValue(0.0)
        self._fade_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_animation.finished.connect(self.close)
        self._fade_animation.start()
