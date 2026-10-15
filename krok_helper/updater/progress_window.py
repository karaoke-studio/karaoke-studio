"""更新准备进度窗口（移植自 SUG updater/ui/update_progress_window.py）。

用户在更新弹窗点「立即更新」后、主程序退出前显示：覆盖"自更新 Updater.exe"
的检查/下载/提取阶段，展示实时进度并允许取消。无边框圆角面板 + 进度环，
配色跟随工作台主题（krok_helper.theme_workbench）。
"""

from __future__ import annotations

import re

from PyQt6.QtCore import QTimer, Qt, QRectF, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QPainter, QPainterPath
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import ProgressRing, PushButton, isDarkTheme, themeColor


class UpdateProgressWindow(QWidget):
    """更新准备进度窗口 —— 圆角面板 + 进度环 + 取消按钮。"""

    cancelled = pyqtSignal()

    _WIDTH = 400
    _HEIGHT = 210
    _RADIUS = 16

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(self._WIDTH, self._HEIGHT)

        # paintEvent 用色，_apply_theme 覆盖
        self._bg_color = QColor(32, 32, 36, 240)
        self._border_color = QColor(255, 255, 255, 20)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 18)
        root.setSpacing(0)

        self._title_label = QLabel("正在准备更新")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self._title_label)
        root.addSpacing(18)

        mid = QHBoxLayout()
        mid.setSpacing(16)
        self._ring = ProgressRing(self, useAni=False)
        self._ring.setObjectName("UpdateProgressRing")
        self._ring.setFixedSize(48, 48)
        # Workbench QSS constrains ordinary progress bars to 10px and reapplies
        # after this window opens. Keep this painted ring square at QSS priority.
        self._ring.setStyleSheet(
            "QProgressBar#UpdateProgressRing {"
            "min-width: 48px; max-width: 48px;"
            "min-height: 48px; max-height: 48px;"
            "border: 0; background: transparent;"
            "}"
        )
        self._ring.setTextVisible(True)
        self._ring.setValue(0)
        mid.addWidget(self._ring)

        self._status = QLabel("正在获取最新更新器，请稍候…")
        self._status.setWordWrap(True)
        self._status.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        mid.addWidget(self._status, 1)
        root.addLayout(mid)

        root.addStretch()

        bottom = QHBoxLayout()
        bottom.addStretch()
        self._btn_cancel = PushButton("取消更新", self)
        self._btn_cancel.setFixedWidth(100)
        self._btn_cancel.clicked.connect(self._on_cancel)
        bottom.addWidget(self._btn_cancel)
        root.addLayout(bottom)

        self._apply_theme()
        self._center_on_screen()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._bring_to_front)

    def _bring_to_front(self) -> None:
        self.raise_()
        self.activateWindow()

    # ----------------------------------------------------------------

    def _apply_theme(self) -> None:
        """按工作台主题刷新配色（窗口短命，不监听后续主题切换）。"""
        try:
            from krok_helper.theme_workbench import palette

            p = palette()
        except Exception:
            # Standalone/frozen updater clients may not have the SUG theme
            # adapter. Always choose background and foreground as a pair.
            dark = isDarkTheme()
            background = "#252526" if dark else "#FFFFFF"
            border_color = "#3E3E3E" if dark else "#E5EAF2"
            primary = "#E6E6E6" if dark else "#1f2937"
            secondary = "#A0A0A0" if dark else "#667085"
        else:
            dark = p.is_dark
            background, border_color = p.card_bg, p.card_border
            primary, secondary = p.text_primary, p.text_secondary

        bg = QColor(background)
        bg.setAlpha(255)
        self._bg_color = bg
        border = QColor(border_color)
        border.setAlpha(120)
        self._border_color = border

        self._title_label.setStyleSheet(
            f"color: {primary}; font-size: 16px; font-weight: 600;"
        )
        self._status.setStyleSheet(f"color: {secondary}; font-size: 13px;")

        accent = themeColor()
        self._ring.setCustomBarColor(accent, accent)
        self._ring.lightBackgroundColor = self._ring.darkBackgroundColor = (
            QColor(255, 255, 255, 34) if dark else QColor(0, 0, 0, 34)
        )
        text_pen_color = QColor(primary)
        self._ring._drawText = lambda painter, text: (
            painter.setFont(self._ring.font()),
            painter.setPen(text_pen_color),
            painter.drawText(self._ring.rect(), Qt.AlignmentFlag.AlignCenter, text),
        )

        hover_bg = "rgba(255,255,255,0.15)" if dark else "rgba(0,0,0,0.09)"
        base_bg = "rgba(255,255,255,0.08)" if dark else "rgba(0,0,0,0.05)"
        pressed_bg = "rgba(255,255,255,0.05)" if dark else "rgba(0,0,0,0.03)"
        self._btn_cancel.setStyleSheet(
            "QPushButton {"
            f"  color: {primary};"
            f"  background: {base_bg};"
            f"  border: 1px solid {accent.name()};"
            "  border-radius: 6px;"
            "  padding: 6px 12px;"
            "  font-size: 13px;"
            "}"
            f"QPushButton:hover {{ background: {hover_bg}; }}"
            f"QPushButton:pressed {{ background: {pressed_bg}; }}"
        )
        self.update()

    # ----------------------------------------------------------------

    def _center_on_screen(self) -> None:
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(
                geo.x() + (geo.width() - self.width()) // 2,
                geo.y() + (geo.height() - self.height()) // 2,
            )

    def update_from_text(self, text: str) -> None:
        """根据进度文本更新状态；文本含 ``XX%`` 时同步进度环。"""
        self.set_status(text)
        QApplication.processEvents()

    def set_status(self, text: str) -> None:
        """Update without pumping events inside native updater callbacks."""
        self._status.setText(text)
        m = re.search(r"(\d+)%", text)
        if m:
            self._ring.setValue(min(100, int(m.group(1))))

    def set_cancellable(self, enabled: bool) -> None:
        self._btn_cancel.setEnabled(enabled)

    def _on_cancel(self) -> None:
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.setText("正在取消…")
        self.cancelled.emit()

    def paintEvent(self, event):  # noqa: N802 — Qt override
        W, H, R = self._WIDTH, self._HEIGHT, self._RADIUS
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, W, H), R, R)
        p.setClipPath(clip)
        p.fillRect(0, 0, W, H, self._bg_color)
        p.setPen(self._border_color)
        p.drawRoundedRect(QRectF(0.5, 0.5, W - 1, H - 1), R, R)
        p.end()

    def finish(self) -> None:
        """淡出并关闭窗口。"""
        from PyQt6.QtCore import QEasingCurve, QPropertyAnimation

        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(250)
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.finished.connect(self.close)
        self._fade_anim.start()
