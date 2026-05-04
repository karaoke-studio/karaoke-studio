from __future__ import annotations

import ctypes
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QPoint, QRectF, QSignalBlocker, QThread, Qt, QTimer, QUrl, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPen, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    FluentIcon as FIF,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SpinBox,
    TableWidget,
    ToolButton,
)
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from qfluentwidgets.components.widgets.menu import MenuAnimationType

from .bilibili_auth import BilibiliQrLoginService
from .cookie_manager import BilibiliAccountProfile, CookieManager
from .download_task import (
    DownloadOptions,
    DownloadTask,
    FormatOption,
    NAMING_RULE_CUSTOM,
    NAMING_RULE_TITLE,
    NAMING_RULE_TITLE_UPLOADER,
    SOURCE_BILIBILI,
    SOURCE_YOUTUBE,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_DOWNLOADING,
    TASK_STATUS_FAILED,
    TASK_STATUS_WAITING,
    VideoInfo,
)
from .format_parser import format_bytes
from .ytdlp_service import DownloadCancelledError, VideoDownloadError, YtDlpService


DEFAULT_CUSTOM_TEMPLATE = "{title}"
PLATFORM_ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "platforms"
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DONOTROUND = 1
DOWNLOAD_TABLE_FIXED_WIDTHS = {
    0: 88,
    2: 92,
    3: 108,
    4: 84,
    5: 118,
    6: 92,
}
COMBO_BOX_VIEW_QSS = """
QAbstractItemView {
    background-color: transparent;
    border: none;
    border-radius: 0px;
    padding: 4px;
    outline: none;
}

QAbstractItemView::item {
    height: 32px;
    padding: 0 12px;
    border-radius: 6px;
}

QAbstractItemView::item:selected {
    background-color: #FFF1F2;
    color: black;
}
"""


def open_in_explorer(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["explorer", str(path)])


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "-"
    total_seconds = int(round(seconds))
    minutes, remain = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remain:02d}"
    return f"{minutes}:{remain:02d}"


def format_speed(speed: float | int | None) -> str:
    if not speed or speed <= 0:
        return ""
    return f"{format_bytes(int(speed))}/s"


class PanelCard(QFrame):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        radius: int = 16,
        padding: tuple[int, int, int, int] = (16, 16, 16, 16),
        spacing: int = 12,
    ) -> None:
        super().__init__(parent)
        self._padding = padding
        self._spacing = spacing
        self.setObjectName("PanelCard")
        self.setStyleSheet(
            f"""
            QFrame#PanelCard {{
                background: #ffffff;
                border: 1px solid rgba(226, 232, 240, 0.95);
                border-radius: {radius}px;
            }}
            """
        )

    def create_vbox(self) -> QVBoxLayout:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self._padding)
        layout.setSpacing(self._spacing)
        return layout

    def create_hbox(self) -> QHBoxLayout:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(*self._padding)
        layout.setSpacing(self._spacing)
        return layout

    def create_grid(self) -> QGridLayout:
        layout = QGridLayout(self)
        layout.setContentsMargins(*self._padding)
        layout.setHorizontalSpacing(self._spacing)
        layout.setVerticalSpacing(self._spacing)
        return layout


class WhiteComboBoxMenu(ComboBoxMenu):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.NoDropShadowWindowHint)
        self.view.setStyleSheet(COMBO_BOX_VIEW_QSS)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.hBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.hBoxLayout.setSpacing(0)
        self.view.setViewportMargins(0, 0, 0, 0)
        self.setShadowEffect(blurRadius=0, offset=(0, 0), color=QColor(0, 0, 0, 0))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        try:
            preference = ctypes.c_int(DWMWCP_DONOTROUND)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()),
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(preference),
                ctypes.sizeof(preference),
            )
        except Exception:
            pass

    def exec(self, pos, ani=True, aniType=MenuAnimationType.DROP_DOWN):
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.adjustSize(pos, aniType)

        overflow = self.view.verticalScrollBar().maximum()
        if overflow > 0:
            self.view.setFixedHeight(self.view.height() + overflow + 8)

        self.adjustSize()
        return super().exec(pos, ani, aniType)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#EAEAEA"), 1))
        painter.setBrush(QColor("white"))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)


class StyledComboBox(ComboBox):
    def _createComboMenu(self):
        return WhiteComboBoxMenu(self)


class PlatformCard(QFrame):
    clicked = Signal()

    def __init__(self, title: str, brand: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._brand = brand
        self._checked = False
        self._hovered = False
        self.setObjectName("PlatformCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setLineWidth(0)
        self.setMidLineWidth(0)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setStyleSheet("QFrame#PlatformCard { background: transparent; border: 0; }")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(74)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def isChecked(self) -> bool:  # noqa: N802
        return self._checked

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        if self._checked == checked:
            return
        self._checked = checked
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect().adjusted(1, 1, -1, -1)
        if self._checked:
            background = QColor("#fff7f7")
            border = QColor("#ff7b89")
        elif self._hovered:
            background = QColor("#fffdfd")
            border = QColor("#f0c5cb")
        else:
            background = QColor("#ffffff")
            border = QColor("#e5e7eb")

        painter.setPen(QPen(border, 1))
        painter.setBrush(background)
        painter.drawRoundedRect(rect, 14, 14)

        title_left = self._draw_brand_icon(painter, rect)

        painter.setPen(QColor("#ff5a6f") if self._checked else QColor("#374151"))
        title_font = QFont("Microsoft YaHei UI")
        title_font.setPointSizeF(12.5)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.drawText(
            rect.adjusted(title_left, 0, -58, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._title,
        )

        circle_size = 28
        circle_rect = QRectF(
            rect.right() - 16 - circle_size,
            rect.center().y() - circle_size / 2,
            circle_size,
            circle_size,
        )
        painter.setPen(QPen(QColor("#ff5a6f") if self._checked else QColor("#d1d5db"), 2))
        painter.setBrush(QColor("#ff5a6f") if self._checked else QColor("#ffffff"))
        painter.drawEllipse(circle_rect)
        if self._checked:
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.drawLine(
                QPoint(int(circle_rect.left() + 7), int(circle_rect.center().y())),
                QPoint(int(circle_rect.left() + 12), int(circle_rect.bottom() - 8)),
            )
            painter.drawLine(
                QPoint(int(circle_rect.left() + 12), int(circle_rect.bottom() - 8)),
                QPoint(int(circle_rect.right() - 7), int(circle_rect.top() + 8)),
            )

    def _draw_brand_icon(self, painter: QPainter, rect) -> int:
        icon_rect = rect.adjusted(18, 18, -(rect.width() - 80), -18)
        if self._draw_svg_icon(painter, icon_rect):
            return icon_rect.right() + 14

        if self._brand == "youtube":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#ff1f1f"))
            painter.drawRoundedRect(icon_rect, 8, 8)
            painter.setBrush(QColor("#ffffff"))
            painter.drawPolygon(
                QPoint(icon_rect.left() + 13, icon_rect.top() + 9),
                QPoint(icon_rect.left() + 13, icon_rect.bottom() - 9),
                QPoint(icon_rect.right() - 10, icon_rect.center().y()),
            )
            return icon_rect.right() + 14

        painter.setPen(QColor("#10a6ff"))
        logo_font = QFont("Segoe UI", 16)
        logo_font.setBold(True)
        painter.setFont(logo_font)
        painter.drawText(icon_rect.adjusted(-2, -3, 14, 2), Qt.AlignmentFlag.AlignCenter, "bilibili")
        return icon_rect.right() + 14

    def _draw_svg_icon(self, painter: QPainter, icon_rect) -> bool:
        svg_path = PLATFORM_ICON_DIR / f"{self._brand}.svg"
        if not svg_path.is_file():
            return False

        renderer = QSvgRenderer(str(svg_path))
        if not renderer.isValid():
            return False

        default_size = renderer.defaultSize()
        if not default_size.isValid() or default_size.width() <= 0 or default_size.height() <= 0:
            renderer.render(painter, QRectF(icon_rect))
            return True

        width_ratio = icon_rect.width() / default_size.width()
        height_ratio = icon_rect.height() / default_size.height()
        scale = min(width_ratio, height_ratio)
        target_width = default_size.width() * scale
        target_height = default_size.height() * scale
        target_rect = QRectF(
            icon_rect.center().x() - target_width / 2,
            icon_rect.center().y() - target_height / 2,
            target_width,
            target_height,
        )
        renderer.render(painter, target_rect)
        return True


class QrPlaceholder(QLabel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(210, 210)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pixmap = QPixmap()
        self._message = "正在准备二维码…"
        self._login_url = ""

    def set_qr_image(self, image_bytes: bytes, login_url: str) -> None:
        self._login_url = login_url
        if image_bytes and self._pixmap.loadFromData(image_bytes):
            self.update()
            return
        self._pixmap = QPixmap()
        self._message = "二维码生成失败，点击在浏览器中打开"
        self.update()

    def set_message(self, message: str, login_url: str = "") -> None:
        if login_url:
            self._login_url = login_url
        self._message = message or "请点击刷新状态重新生成二维码"
        self._pixmap = QPixmap()
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._login_url
            and self.rect().contains(event.position().toPoint())
        ):
            QDesktopServices.openUrl(QUrl(self._login_url))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor("#e5e7eb"), 1))
        painter.setBrush(QColor("#f8fafc"))
        painter.drawRoundedRect(rect, 18, 18)

        if not self._pixmap.isNull():
            qr_rect = rect.adjusted(16, 16, -16, -16)
            scaled = self._pixmap.scaled(
                qr_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            target_x = int(qr_rect.center().x() - scaled.width() / 2)
            target_y = int(qr_rect.center().y() - scaled.height() / 2)
            painter.drawPixmap(target_x, target_y, scaled)
            return

        painter.setPen(QPen(QColor("#111827"), 5))
        box = rect.adjusted(26, 26, -26, -26)
        size = 32
        for x, y in (
            (box.left(), box.top()),
            (box.right() - size, box.top()),
            (box.left(), box.bottom() - size),
        ):
            painter.drawRect(x, y, size, size)

        painter.setPen(QColor("#94a3b8"))
        painter.setFont(QFont("Microsoft YaHei UI", 10))
        painter.drawText(rect.adjusted(20, 148, -20, -20), Qt.AlignmentFlag.AlignCenter, self._message)


class AvatarLabel(QLabel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(72, 72)
        self._pixmap = QPixmap()
        self._fallback_text = "B"

    def set_avatar(self, image_bytes: bytes, fallback_text: str) -> None:
        self._fallback_text = (fallback_text or "B").strip()[:1].upper() or "B"
        self._pixmap = QPixmap()
        if image_bytes:
            self._pixmap.loadFromData(image_bytes)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor("#dbeafe"), 1))
        painter.setBrush(QColor("#eff6ff"))
        painter.drawEllipse(rect)

        if not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                rect.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.setClipPath(self._build_clip_path(rect))
            painter.drawPixmap(
                int(rect.center().x() - scaled.width() / 2),
                int(rect.center().y() - scaled.height() / 2),
                scaled,
            )
            painter.setClipping(False)
            return

        painter.setPen(QColor("#2563eb"))
        font = QFont("Segoe UI", 24)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._fallback_text)

    def _build_clip_path(self, rect):
        from PyQt6.QtGui import QPainterPath

        path = QPainterPath()
        path.addEllipse(QRectF(rect))
        return path


@dataclass(slots=True)
class ParsedBatch:
    infos: list[VideoInfo]
    errors: list[str]


class ParseLinksWorker(QThread):
    batchFinished = Signal(object)

    def __init__(self, urls: list[str], cookie_file: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._urls = urls
        self._cookie_file = cookie_file
        self._service = YtDlpService()

    def run(self) -> None:  # noqa: D401
        infos: list[VideoInfo] = []
        errors: list[str] = []
        for url in self._urls:
            try:
                info = self._service.extract_info(url, self._cookie_file)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{url}：{exc}")
                continue
            infos.append(info)
        self.batchFinished.emit(ParsedBatch(infos=infos, errors=errors))


class DownloadWorker(QThread):
    progressChanged = Signal(str, object)
    taskSucceeded = Signal(str)
    taskFailed = Signal(str, str)
    taskCancelled = Signal(str)

    def __init__(self, task: DownloadTask, options: DownloadOptions, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._task = task
        self._options = options
        self._service = YtDlpService()

    def run(self) -> None:  # noqa: D401
        if self._task.cancel_requested:
            self.taskCancelled.emit(self._task.task_id)
            return
        try:
            self._service.download(
                self._task,
                self._options,
                lambda payload: self.progressChanged.emit(self._task.task_id, payload),
            )
        except DownloadCancelledError:
            self.taskCancelled.emit(self._task.task_id)
            return
        except Exception as exc:  # noqa: BLE001
            self.taskFailed.emit(self._task.task_id, str(exc))
            return
        self.taskSucceeded.emit(self._task.task_id)


class BilibiliQrLoginWorker(QThread):
    qrReady = Signal(bytes, str)
    loginStatusChanged = Signal(object)
    loginSucceeded = Signal(str)
    loginFailed = Signal(str)

    def __init__(self, cookie_manager: CookieManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cookie_manager = cookie_manager
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:  # noqa: D401
        service = BilibiliQrLoginService(self._cookie_manager)
        try:
            ticket = service.request_qr_ticket()
        except Exception as exc:  # noqa: BLE001
            self.loginFailed.emit(str(exc))
            return

        self.qrReady.emit(ticket.image_bytes, ticket.login_url)
        self.loginStatusChanged.emit({"code": 86101, "message": "请使用哔哩哔哩 App 扫码"})

        while not self._stop_requested:
            try:
                status = service.poll_login(ticket.qrcode_key)
            except Exception as exc:  # noqa: BLE001
                self.loginFailed.emit(str(exc))
                return

            self.loginStatusChanged.emit({"code": status.code, "message": status.message, "success": status.success})
            if status.success:
                self.loginSucceeded.emit(self._cookie_manager.get_cookie_path() or "")
                return
            if status.code == 86038:
                return
            self.msleep(1800)


class VideoDownloadPage(QWidget):
    settingsChanged = Signal()

    def __init__(self, settings, save_settings: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._save_settings = save_settings
        self.cookie_manager = CookieManager(getattr(settings, "video_download_cookie_path", ""))
        self._parse_worker: ParseLinksWorker | None = None
        self._qr_login_worker: BilibiliQrLoginWorker | None = None
        self._running_workers: dict[str, DownloadWorker] = {}
        self._tasks: list[DownloadTask] = []
        self._task_index: dict[str, DownloadTask] = {}
        self._current_task_id = ""
        self._format_options: list[FormatOption] = []
        self._format_table_updating = False
        self._recent_bilibili_login_deadline = 0.0

        self._build_ui()
        self._load_settings()
        self._refresh_cookie_status()
        self._ensure_qr_login()
        self._refresh_preview()
        self._refresh_download_table()

    def _build_ui(self) -> None:
        self.setStyleSheet(
            """
            QLabel[panelTitle="true"] {
                background: transparent;
                border: 0;
                color: #111827;
                font-size: 13pt;
                font-weight: 700;
            }
            QLabel[hint="true"] {
                background: transparent;
                border: 0;
                color: #6b7280;
                font-size: 10pt;
            }
            QLabel, CaptionLabel, BodyLabel {
                background: transparent;
                border: 0;
            }
            TableWidget {
                background: #ffffff;
                border: 1px solid rgba(203, 213, 225, 0.9);
                border-radius: 16px;
                gridline-color: transparent;
            }
            TableWidget::item {
                padding: 8px 10px;
                border-bottom: 1px solid rgba(226, 232, 240, 0.85);
            }
            QHeaderView::section {
                background: #f8fafc;
                color: #111827;
                border: 0;
                border-right: 1px solid rgba(226, 232, 240, 0.8);
                border-bottom: 1px solid rgba(226, 232, 240, 0.9);
                padding: 8px;
                font-weight: 700;
            }
            """
        )

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        left_panel = self._build_left_panel()
        center_panel = self._build_center_panel()
        right_panel = self._build_right_panel()

        left_panel.setFixedWidth(320)
        right_panel.setFixedWidth(320)

        root.addWidget(left_panel, 0)
        root.addWidget(center_panel, 1)
        root.addWidget(right_panel, 0)

    def _build_left_panel(self) -> QWidget:
        panel = PanelCard(self, padding=(12, 12, 12, 12), spacing=12)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = panel.create_vbox()

        source_card = PanelCard(panel)
        source_layout = source_card.create_vbox()
        source_layout.addWidget(self._create_panel_title("下载来源"))

        self.youtube_button = PlatformCard("YouTube", "youtube")
        self.bilibili_button = PlatformCard("Bilibili", "bilibili")
        self.youtube_button.clicked.connect(lambda: self._set_source(SOURCE_YOUTUBE))
        self.bilibili_button.clicked.connect(lambda: self._set_source(SOURCE_BILIBILI))
        source_layout.addWidget(self.youtube_button)
        source_layout.addWidget(self.bilibili_button)

        settings_card = PanelCard(panel)
        settings_layout = settings_card.create_vbox()
        settings_layout.addWidget(self._create_panel_title("下载设置"))

        settings_layout.addWidget(CaptionLabel("保存路径"))
        save_path_row = QHBoxLayout()
        save_path_row.setContentsMargins(0, 0, 0, 0)
        save_path_row.setSpacing(8)
        self.save_dir_edit = LineEdit()
        self.save_dir_edit.editingFinished.connect(self._persist_settings)
        self.save_dir_browse_button = ToolButton(FIF.FOLDER)
        self.save_dir_browse_button.setFixedSize(38, 38)
        self.save_dir_browse_button.clicked.connect(self._choose_save_dir)
        save_path_row.addWidget(self.save_dir_edit, 1)
        save_path_row.addWidget(self.save_dir_browse_button, 0)
        settings_layout.addLayout(save_path_row)

        settings_layout.addWidget(CaptionLabel("文件命名"))
        self.naming_rule_combo = StyledComboBox()
        self.naming_rule_combo.addItems([NAMING_RULE_TITLE, NAMING_RULE_TITLE_UPLOADER, NAMING_RULE_CUSTOM])
        self.naming_rule_combo.currentTextChanged.connect(self._handle_naming_rule_changed)
        self._install_single_click_combo_behavior(self.naming_rule_combo)
        settings_layout.addWidget(self.naming_rule_combo)

        self.custom_template_edit = LineEdit()
        self.custom_template_edit.setPlaceholderText("自定义模板，例如：{title} - {uploader}")
        self.custom_template_edit.editingFinished.connect(self._persist_settings)
        settings_layout.addWidget(self.custom_template_edit)

        options_card = PanelCard(panel)
        options_layout = options_card.create_vbox()
        options_layout.addWidget(self._create_panel_title("选项"))
        self.merge_checkbox = CheckBox("自动合并音视频（推荐）")
        self.thumbnail_checkbox = CheckBox("下载封面")
        for checkbox in (self.merge_checkbox, self.thumbnail_checkbox):
            checkbox.stateChanged.connect(self._persist_settings)
            options_layout.addWidget(checkbox)

        concurrent_card = PanelCard(panel)
        concurrent_layout = concurrent_card.create_vbox()
        concurrent_layout.addWidget(self._create_panel_title("并发下载"))
        self.concurrent_spin = SpinBox()
        self.concurrent_spin.setRange(1, 5)
        self.concurrent_spin.valueChanged.connect(self._persist_settings)
        concurrent_layout.addWidget(self.concurrent_spin)

        network_card = PanelCard(panel)
        network_layout = network_card.create_vbox()
        network_layout.addWidget(self._create_panel_title("网络设置"))
        network_layout.addWidget(CaptionLabel("超时时间（秒）"))
        self.timeout_spin = SpinBox()
        self.timeout_spin.setRange(5, 300)
        self.timeout_spin.valueChanged.connect(self._persist_settings)
        network_layout.addWidget(self.timeout_spin)
        network_layout.addWidget(CaptionLabel("重试次数"))
        self.retry_spin = SpinBox()
        self.retry_spin.setRange(0, 10)
        self.retry_spin.valueChanged.connect(self._persist_settings)
        network_layout.addWidget(self.retry_spin)

        layout.addWidget(source_card, 0)
        layout.addWidget(settings_card, 0)
        layout.addWidget(options_card, 0)
        layout.addWidget(concurrent_card, 0)
        layout.addWidget(network_card, 0)
        layout.addStretch(1)

        return panel

    def _build_center_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        input_card = PanelCard(panel)
        input_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        input_layout = input_card.create_vbox()
        input_layout.addWidget(self._create_panel_title("视频链接输入"))

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(12)
        self.link_input = PlainTextEdit()
        self.link_input.setPlaceholderText("粘贴 YouTube 或 Bilibili 视频链接，每行一个链接")
        self.link_input.setFixedHeight(76)
        input_row.addWidget(self.link_input, 1)

        input_buttons = QVBoxLayout()
        input_buttons.setContentsMargins(0, 0, 0, 0)
        input_buttons.setSpacing(10)
        self.parse_button = PrimaryPushButton(FIF.LINK, "解析")
        self.clear_input_button = PushButton(FIF.DELETE, "清空")
        self.parse_button.clicked.connect(self._start_parse)
        self.clear_input_button.clicked.connect(self.link_input.clear)
        input_buttons.addWidget(self.parse_button)
        input_buttons.addWidget(self.clear_input_button)
        input_buttons.addStretch(1)
        input_row.addLayout(input_buttons)
        input_layout.addLayout(input_row)

        self.parse_status_label = CaptionLabel("准备解析视频链接。")
        self.parse_status_label.setWordWrap(True)
        input_layout.addWidget(self.parse_status_label)

        info_card = PanelCard(panel)
        info_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        info_layout = info_card.create_vbox()
        info_layout.addWidget(self._create_panel_title("视频信息"))

        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 0, 0, 0)
        info_row.setSpacing(18)
        info_row.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.thumbnail_label = QLabel("暂无视频信息")
        self.thumbnail_label.setFixedSize(250, 148)
        self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_label.setStyleSheet(
            "background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 16px; color: #94a3b8; font-size: 11pt;"
        )
        info_row.addWidget(self.thumbnail_label, 0)

        meta_widget = QWidget()
        meta_widget.setStyleSheet("background: transparent; border: 0;")
        meta_layout = QGridLayout(meta_widget)
        meta_layout.setContentsMargins(0, 0, 0, 0)
        meta_layout.setHorizontalSpacing(12)
        meta_layout.setVerticalSpacing(10)
        meta_layout.setColumnStretch(0, 0)
        meta_layout.setColumnStretch(1, 1)
        self.info_value_labels: dict[str, QLabel] = {}
        for row, (key, title) in enumerate(
            (
                ("title", "标题"),
                ("uploader", "作者"),
                ("duration", "时长"),
                ("resolution", "分辨率"),
                ("filesize", "大小"),
            )
        ):
            label = QLabel(f"{title}：")
            label.setStyleSheet("color: #374151; font-weight: 700;")
            value = QLabel("-")
            value.setWordWrap(True)
            value.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            value.setStyleSheet("color: #111827;")
            meta_layout.addWidget(label, row, 0)
            meta_layout.addWidget(value, row, 1)
            self.info_value_labels[key] = value
        info_row.addWidget(meta_widget, 1, Qt.AlignmentFlag.AlignTop)
        info_layout.addLayout(info_row)

        format_card = PanelCard(panel)
        format_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        format_layout = format_card.create_vbox()
        format_layout.addWidget(self._create_panel_title("清晰度与格式选择"))

        format_selector_row = QHBoxLayout()
        format_selector_row.setContentsMargins(0, 0, 0, 0)
        format_selector_row.setSpacing(10)
        format_selector_row.addWidget(CaptionLabel("下载格式"))
        self.format_combo = StyledComboBox()
        self.format_combo.setMinimumHeight(40)
        self.format_combo.currentIndexChanged.connect(self._handle_format_combo_changed)
        self._install_single_click_combo_behavior(self.format_combo)
        format_selector_row.addWidget(self.format_combo, 1)
        format_layout.addLayout(format_selector_row)

        self.format_summary_widget = QWidget(format_card)
        self.format_summary_widget.setObjectName("FormatSummaryWidget")
        self.format_summary_widget.setStyleSheet(
            """
            QWidget#FormatSummaryWidget {
                background: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 16px;
            }
            QWidget#FormatSummaryWidget QLabel,
            QWidget#FormatSummaryWidget BodyLabel,
            QWidget#FormatSummaryWidget CaptionLabel {
                background: transparent;
                border: 0;
            }
            """
        )
        format_summary_layout = QGridLayout(self.format_summary_widget)
        format_summary_layout.setContentsMargins(16, 14, 16, 14)
        format_summary_layout.setHorizontalSpacing(12)
        format_summary_layout.setVerticalSpacing(8)
        format_summary_layout.setColumnStretch(0, 0)
        format_summary_layout.setColumnStretch(1, 1)
        self.format_value_labels: dict[str, QLabel] = {}
        for row, (key, title) in enumerate(
            (
                ("format", "格式"),
                ("resolution", "分辨率"),
                ("video_codec", "视频编码"),
                ("audio_codec", "音频编码"),
                ("filesize", "大小"),
            )
        ):
            label = QLabel(f"{title}：")
            label.setStyleSheet("color: #374151; font-weight: 700;")
            value = QLabel("-")
            value.setWordWrap(True)
            value.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            value.setStyleSheet("color: #111827;")
            format_summary_layout.addWidget(label, row, 0)
            format_summary_layout.addWidget(value, row, 1)
            self.format_value_labels[key] = value
        format_layout.addWidget(self.format_summary_widget)
        self.format_summary_widget.hide()
        self.format_table = TableWidget()
        self.format_table.hide()
        self.format_table.setColumnCount(6)
        self.format_table.setHorizontalHeaderLabels(["选择", "格式", "分辨率", "视频编码", "音频编码", "大小"])
        self.format_table.verticalHeader().hide()
        self.format_table.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
        self.format_table.setSelectionBehavior(TableWidget.SelectionBehavior.SelectRows)
        self.format_table.setSelectionMode(TableWidget.SelectionMode.SingleSelection)
        self.format_table.horizontalHeader().setStretchLastSection(True)
        self.format_table.horizontalHeader().setDefaultSectionSize(120)
        self.format_table.horizontalHeader().resizeSection(0, 88)
        self.format_table.horizontalHeader().resizeSection(1, 140)
        self.format_table.horizontalHeader().resizeSection(2, 110)
        self.format_table.horizontalHeader().resizeSection(5, 110)
        self.format_table.itemChanged.connect(self._handle_format_item_changed)
        self.format_table.cellClicked.connect(self._handle_format_cell_clicked)
        format_layout.addWidget(self.format_table)
        self.format_hint_label = CaptionLabel("请先解析视频链接。")
        self.format_hint_label.setWordWrap(True)
        format_layout.addWidget(self.format_hint_label)

        download_card = PanelCard(panel)
        download_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        download_layout = download_card.create_vbox()
        download_layout.addWidget(self._create_panel_title("下载列表"))

        self.download_table = TableWidget()
        self.download_table.setColumnCount(7)
        self.download_table.setHorizontalHeaderLabels(["状态", "标题", "来源", "分辨率", "进度", "大小", "操作"])
        self.download_table.verticalHeader().hide()
        self.download_table.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
        self.download_table.setSelectionBehavior(TableWidget.SelectionBehavior.SelectRows)
        self.download_table.setSelectionMode(TableWidget.SelectionMode.SingleSelection)
        self.download_table.setWordWrap(False)
        self.download_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.download_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        download_header = self.download_table.horizontalHeader()
        download_header.setStretchLastSection(False)
        download_header.setMinimumSectionSize(44)
        download_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        download_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        download_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        download_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        download_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        download_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        download_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        for column, width in DOWNLOAD_TABLE_FIXED_WIDTHS.items():
            self.download_table.setColumnWidth(column, width)
        self.download_table.itemSelectionChanged.connect(self._handle_task_selection_changed)
        download_layout.addWidget(self.download_table, 1)
        self.download_hint_label = CaptionLabel("暂无下载任务。")
        download_layout.addWidget(self.download_hint_label)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 4, 0, 0)
        action_row.setSpacing(10)
        self.start_all_button = PrimaryPushButton(FIF.DOWNLOAD, "全部开始")
        self.pause_all_button = PushButton(FIF.PAUSE, "全部暂停")
        self.cancel_all_button = PushButton(FIF.CANCEL, "全部取消")
        self.open_folder_button = PushButton(FIF.FOLDER, "打开文件夹")
        self.clear_list_button = PushButton(FIF.DELETE, "清空列表")
        self.pause_all_button.setEnabled(False)
        self.pause_all_button.setToolTip("预留接口，后续接入任务暂停。")
        self.start_all_button.clicked.connect(self._start_all_downloads)
        self.cancel_all_button.clicked.connect(self._cancel_all_downloads)
        self.open_folder_button.clicked.connect(self._open_save_folder)
        self.clear_list_button.clicked.connect(self._clear_task_list)
        action_row.addWidget(self.start_all_button)
        action_row.addWidget(self.pause_all_button)
        action_row.addWidget(self.cancel_all_button)
        action_row.addWidget(self.open_folder_button)
        action_row.addWidget(self.clear_list_button)
        action_row.addStretch(1)
        download_layout.addLayout(action_row)

        layout.addWidget(input_card)
        layout.addWidget(info_card)
        layout.addWidget(format_card)
        layout.addWidget(download_card, 1)
        return panel

    def _build_right_panel(self) -> QWidget:
        panel = PanelCard(self, padding=(12, 12, 12, 12), spacing=12)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = panel.create_vbox()

        cookie_card = PanelCard(panel, padding=(16, 16, 16, 14))
        cookie_layout = cookie_card.create_vbox()
        cookie_layout.addWidget(self._create_panel_title("Bilibili 账号"))

        self.qr_wrapper = QWidget(cookie_card)
        self.qr_wrapper.setStyleSheet("background: transparent; border: 0;")
        qr_layout = QVBoxLayout(self.qr_wrapper)
        qr_layout.setContentsMargins(0, 6, 0, 0)
        qr_layout.setSpacing(10)
        self.qr_placeholder = QrPlaceholder()
        qr_layout.addWidget(self.qr_placeholder, 0, Qt.AlignmentFlag.AlignHCenter)
        cookie_layout.addWidget(self.qr_wrapper)

        self.account_profile_widget = QWidget(cookie_card)
        self.account_profile_widget.setObjectName("AccountProfileWidget")
        self.account_profile_widget.setStyleSheet(
            """
            QWidget#AccountProfileWidget {
                background: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 16px;
            }
            QWidget#AccountProfileWidget QLabel,
            QWidget#AccountProfileWidget BodyLabel,
            QWidget#AccountProfileWidget CaptionLabel {
                background: transparent;
                border: 0;
            }
            """
        )
        account_layout = QVBoxLayout(self.account_profile_widget)
        account_layout.setContentsMargins(16, 16, 16, 16)
        account_layout.setSpacing(8)
        self.account_avatar_label = AvatarLabel()
        self.account_name_label = BodyLabel("Bilibili 用户")
        self.account_name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.account_name_label.setStyleSheet("color: #111827; font-size: 12pt; font-weight: 700;")
        self.account_hint_label = CaptionLabel("当前已登录 Bilibili 账号")
        self.account_hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.account_hint_label.setStyleSheet("color: #64748b;")
        account_layout.addWidget(self.account_avatar_label, 0, Qt.AlignmentFlag.AlignHCenter)
        account_layout.addWidget(self.account_name_label, 0, Qt.AlignmentFlag.AlignHCenter)
        account_layout.addWidget(self.account_hint_label, 0, Qt.AlignmentFlag.AlignHCenter)
        self.account_profile_widget.hide()
        cookie_layout.addWidget(self.account_profile_widget)

        status_row = QWidget(cookie_card)
        status_row.setStyleSheet("background: transparent; border: 0;")
        status_row_layout = QHBoxLayout(status_row)
        status_row_layout.setContentsMargins(0, 0, 0, 0)
        status_row_layout.setSpacing(8)
        status_row_layout.addStretch(1)
        self.cookie_status_dot = QFrame(status_row)
        self.cookie_status_dot.setFixedSize(10, 10)
        self.cookie_status_dot.setStyleSheet("background: #dc2626; border-radius: 5px;")
        status_row_layout.addWidget(self.cookie_status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.cookie_status_text_label = BodyLabel("未登录")
        self.cookie_status_text_label.setStyleSheet("color: #dc2626; font-weight: 700;")
        status_row_layout.addWidget(self.cookie_status_text_label, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row_layout.addStretch(1)
        cookie_layout.addWidget(status_row)

        cookie_button_row = QHBoxLayout()
        cookie_button_row.setContentsMargins(0, 0, 0, 0)
        cookie_button_row.setSpacing(8)
        self.refresh_cookie_button = PushButton(FIF.SYNC, "刷新状态")
        self.logout_cookie_button = PushButton("退出登录")
        self.refresh_cookie_button.clicked.connect(self._handle_refresh_cookie_clicked)
        self.logout_cookie_button.clicked.connect(self._handle_logout_cookie_clicked)
        cookie_button_row.addStretch(1)
        cookie_button_row.addWidget(self.refresh_cookie_button, 0)
        cookie_button_row.addWidget(self.logout_cookie_button, 0)
        cookie_button_row.addStretch(1)
        cookie_layout.addLayout(cookie_button_row)

        tip_label = CaptionLabel("提示：登录成功后，Cookie 将自动保存到本地，下次无需重新登录。")
        tip_label.setWordWrap(True)
        tip_label.setStyleSheet(
            "background: #fff7f7; border: 1px solid #fde2e4; border-radius: 12px; padding: 10px; color: #7c6470;"
        )
        cookie_layout.addWidget(tip_label)

        layout.addWidget(cookie_card, 0)
        layout.addStretch(1)
        return panel

    def _install_single_click_combo_behavior(self, combo: ComboBox) -> None:
        popup_view = getattr(combo, "view", None)
        if not callable(popup_view):
            return
        view = popup_view()
        if view is None:
            return
        view.pressed.connect(lambda index, combo=combo: self._handle_combo_popup_pressed(combo, index.row()))

    def _handle_combo_popup_pressed(self, combo: ComboBox, row: int) -> None:
        if row < 0 or row >= combo.count():
            return
        combo.setCurrentIndex(row)
        hide_popup = getattr(combo, "hidePopup", None)
        if callable(hide_popup):
            hide_popup()

    def _create_panel_title(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("panelTitle", True)
        return label

    def _load_settings(self) -> None:
        save_dir = getattr(self.settings, "video_download_save_dir", "") or str(Path.home() / "Downloads")
        self.save_dir_edit.setText(save_dir)
        self.naming_rule_combo.setCurrentText(getattr(self.settings, "video_download_naming_rule", NAMING_RULE_TITLE))
        self.custom_template_edit.setText(
            getattr(self.settings, "video_download_custom_template", DEFAULT_CUSTOM_TEMPLATE) or DEFAULT_CUSTOM_TEMPLATE
        )
        self.merge_checkbox.setChecked(bool(getattr(self.settings, "video_download_merge_video_audio", True)))
        self.thumbnail_checkbox.setChecked(bool(getattr(self.settings, "video_download_download_thumbnail", True)))
        self.concurrent_spin.setValue(int(getattr(self.settings, "video_download_concurrent_count", 3) or 3))
        self.timeout_spin.setValue(int(getattr(self.settings, "video_download_timeout", 30) or 30))
        self.retry_spin.setValue(int(getattr(self.settings, "video_download_retry_count", 3) or 3))
        self._set_source(getattr(self.settings, "video_download_source", SOURCE_YOUTUBE))
        self._handle_naming_rule_changed(self.naming_rule_combo.currentText())

    def _set_source(self, source: str) -> None:
        youtube_checked = source == SOURCE_YOUTUBE
        bilibili_checked = source == SOURCE_BILIBILI
        with QSignalBlocker(self.youtube_button):
            self.youtube_button.setChecked(youtube_checked)
        with QSignalBlocker(self.bilibili_button):
            self.bilibili_button.setChecked(bilibili_checked)
        self.settings.video_download_source = source
        self._persist_settings(save=False)

    def _set_cookie_status_display(self, text: str, color: str) -> None:
        self.cookie_status_text_label.setText(text)
        self.cookie_status_text_label.setStyleSheet(f"color: {color}; font-weight: 700;")
        self.cookie_status_dot.setStyleSheet(f"background: {color}; border-radius: 5px;")

    def _apply_account_profile(self, profile: BilibiliAccountProfile | None) -> None:
        if profile is None:
            self.account_profile_widget.hide()
            self.qr_wrapper.show()
            self.logout_cookie_button.setEnabled(False)
            return

        self.account_avatar_label.set_avatar(profile.avatar_bytes, profile.nickname)
        self.account_name_label.setText(profile.nickname or "Bilibili 用户")
        self.account_profile_widget.show()
        self.qr_wrapper.hide()
        self.logout_cookie_button.setEnabled(True)

    def _clear_account_profile(self) -> None:
        self.account_avatar_label.set_avatar(b"", "B")
        self.account_name_label.setText("Bilibili 用户")
        self.account_profile_widget.hide()
        self.qr_wrapper.show()
        self.logout_cookie_button.setEnabled(False)

    def _handle_refresh_cookie_clicked(self) -> None:
        self._refresh_cookie_status()
        if not self.cookie_manager.check_login_status():
            self._ensure_qr_login(force_restart=True)

    def _handle_logout_cookie_clicked(self) -> None:
        if self._qr_login_worker is not None and self._qr_login_worker.isRunning():
            self._qr_login_worker.stop()
            self._qr_login_worker.wait(2000)
        self.cookie_manager.clear_cookie()
        self._recent_bilibili_login_deadline = 0.0
        self._clear_account_profile()
        self._set_cookie_status_display("未登录", "#dc2626")
        self.qr_placeholder.set_message("已退出登录，正在生成新的二维码…")
        self.parse_status_label.setText("已退出 Bilibili 登录，并清空本地 Cookie。")
        self._ensure_qr_login(force_restart=True)

    def _ensure_qr_login(self, force_restart: bool = False) -> None:
        profile = self.cookie_manager.get_account_profile()
        if profile is not None:
            self._apply_account_profile(profile)
            self.qr_placeholder.set_message("当前已登录，如需重新扫码可点击刷新状态")
            return

        self._clear_account_profile()
        if self._qr_login_worker is not None and self._qr_login_worker.isRunning():
            if not force_restart:
                return
            self._qr_login_worker.stop()
            self._qr_login_worker.wait(2000)

        self.qr_placeholder.set_message("正在生成二维码…")
        self._qr_login_worker = BilibiliQrLoginWorker(self.cookie_manager, self)
        self._qr_login_worker.qrReady.connect(self._handle_qr_ready)
        self._qr_login_worker.loginStatusChanged.connect(self._handle_qr_status_changed)
        self._qr_login_worker.loginSucceeded.connect(self._handle_qr_login_succeeded)
        self._qr_login_worker.loginFailed.connect(self._handle_qr_login_failed)
        self._qr_login_worker.finished.connect(self._handle_qr_worker_finished)
        self._qr_login_worker.start()

    def _handle_qr_ready(self, image_bytes: bytes, login_url: str) -> None:
        self.qr_placeholder.set_qr_image(image_bytes, login_url)

    def _handle_qr_status_changed(self, status_payload: object) -> None:
        if isinstance(status_payload, dict):
            raw_code = status_payload.get("code")
            try:
                status_code = int(raw_code) if raw_code not in (None, "") else -1
            except (TypeError, ValueError):
                status_code = -1
            message = str(status_payload.get("message") or "")
        else:
            status_code = -1
            message = str(status_payload or "")

        if status_code == 86090 or "确认" in message:
            self._set_cookie_status_display("等待确认", "#b45309")
            return
        if status_code == 86038 or "过期" in message:
            self._set_cookie_status_display("二维码已过期", "#b45309")
            self.qr_placeholder.set_message("二维码已过期，点击刷新状态重新生成")
            return
        if status_code == 0 or "成功" in message:
            self._set_cookie_status_display("已登录", "#15803d")
            self.qr_placeholder.set_message("登录成功，正在同步账号信息…")
            return
        if status_code == 86101:
            self._set_cookie_status_display("待扫码", "#dc2626")
            return
        if message:
            self._set_cookie_status_display(f"状态码 {status_code}", "#b45309")
            self.qr_placeholder.set_message(message)
            return
        self._set_cookie_status_display("未登录", "#dc2626")

    def _handle_qr_login_succeeded(self, cookie_path: str) -> None:
        self._recent_bilibili_login_deadline = time.monotonic() + 10.0
        self._set_cookie_status_display("已登录", "#15803d")
        self.qr_placeholder.set_message("登录成功，正在同步账号信息…")
        self.parse_status_label.setText(f"扫码登录成功，Cookie 已保存到 {cookie_path or self.cookie_manager.resolved_cookie_path()}。")
        self._refresh_cookie_status_with_retry(remaining=6)

    def _handle_qr_login_failed(self, message: str) -> None:
        self._clear_account_profile()
        self.qr_placeholder.set_message("二维码加载失败，点击二维码可在浏览器中打开登录页")
        self.parse_status_label.setText(f"Bilibili 扫码登录失败：{message}")

    def _handle_qr_worker_finished(self) -> None:
        self._qr_login_worker = None

    def _refresh_cookie_status(self) -> None:
        profile = self.cookie_manager.get_account_profile()
        if profile is not None:
            self._apply_account_profile(profile)
            self._set_cookie_status_display("已登录", "#15803d")
            self._recent_bilibili_login_deadline = 0.0
            return

        if self.cookie_manager.has_cookie() and time.monotonic() < self._recent_bilibili_login_deadline:
            self._clear_account_profile()
            self.qr_placeholder.set_message("登录成功，正在同步账号信息…")
            self._set_cookie_status_display("已登录", "#15803d")
            return

        self._clear_account_profile()
        if self.cookie_manager.has_cookie():
            self._set_cookie_status_display("Cookie 无效", "#b45309")
            return
        self._set_cookie_status_display("未登录", "#dc2626")

    def _refresh_cookie_status_with_retry(self, remaining: int) -> None:
        self._refresh_cookie_status()
        if remaining <= 0:
            return
        if self.cookie_manager.get_account_profile() is not None:
            return
        QTimer.singleShot(1000, lambda: self._refresh_cookie_status_with_retry(remaining - 1))

    def _choose_save_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择保存路径", self.save_dir_edit.text().strip() or str(Path.home()))
        if not directory:
            return
        self.save_dir_edit.setText(directory)
        self._persist_settings()

    def _handle_naming_rule_changed(self, text: str) -> None:
        self.custom_template_edit.setVisible(text == NAMING_RULE_CUSTOM)
        self._persist_settings()

    def _persist_settings(self, *args, save: bool = True) -> None:
        del args
        self.settings.video_download_save_dir = self.save_dir_edit.text().strip()
        self.settings.video_download_naming_rule = self.naming_rule_combo.currentText() or NAMING_RULE_TITLE
        self.settings.video_download_custom_template = self.custom_template_edit.text().strip() or DEFAULT_CUSTOM_TEMPLATE
        self.settings.video_download_merge_video_audio = self.merge_checkbox.isChecked()
        self.settings.video_download_download_thumbnail = self.thumbnail_checkbox.isChecked()
        self.settings.video_download_download_subtitle = False
        self.settings.video_download_concurrent_count = self.concurrent_spin.value()
        self.settings.video_download_timeout = self.timeout_spin.value()
        self.settings.video_download_retry_count = self.retry_spin.value()
        self.settings.video_download_cookie_path = ""
        if save:
            self._save_settings()
            self.settingsChanged.emit()

    def _start_parse(self) -> None:
        if self._parse_worker is not None and self._parse_worker.isRunning():
            self.parse_status_label.setText("正在解析，请稍候。")
            return

        urls = [line.strip() for line in self.link_input.toPlainText().splitlines() if line.strip()]
        if not urls:
            self.parse_status_label.setText("请输入至少一个视频链接。")
            return

        cookie_path = self.cookie_manager.get_cookie_path() or str(self.cookie_manager.resolved_cookie_path())
        self.parse_button.setEnabled(False)
        self.parse_status_label.setText(f"正在解析 {len(urls)} 个链接…")
        self._parse_worker = ParseLinksWorker(urls, cookie_path, self)
        self._parse_worker.batchFinished.connect(self._handle_parse_finished)
        self._parse_worker.finished.connect(self._handle_parse_worker_finished)
        self._parse_worker.start()

    def _handle_parse_finished(self, batch: ParsedBatch) -> None:
        parsed_count = 0
        first_new_task_id = ""
        for info in batch.infos:
            existing = next(
                (
                    task
                    for task in self._tasks
                    if task.url == info.url and task.status != TASK_STATUS_COMPLETED
                ),
                None,
            )
            if existing is not None:
                previous_option_id = existing.selected_format.option_id if existing.selected_format else ""
                existing.info = info
                existing.title = info.title
                existing.source = info.source
                existing.available_formats = list(info.formats)
                existing.selected_format = self._find_matching_format(existing.available_formats, previous_option_id)
                if existing.selected_format is None:
                    existing.selected_format = self._select_default_format(info.formats)
                existing.filesize = self._preferred_task_filesize(existing)
                if not self._current_task_id:
                    self._current_task_id = existing.task_id
                parsed_count += 1
                continue

            task = self._create_download_task(info)
            self._tasks.append(task)
            self._task_index[task.task_id] = task
            parsed_count += 1
            if not first_new_task_id:
                first_new_task_id = task.task_id

        if first_new_task_id:
            self._current_task_id = first_new_task_id
        elif batch.infos and not self._current_task_id:
            self._current_task_id = self._tasks[0].task_id

        if batch.infos:
            first_source = batch.infos[0].source
            if first_source in (SOURCE_YOUTUBE, SOURCE_BILIBILI):
                self._set_source(first_source)

        parts: list[str] = []
        if parsed_count:
            parts.append(f"成功解析 {parsed_count} 个链接")
        if batch.errors:
            parts.append(f"{len(batch.errors)} 个链接失败")
        self.parse_status_label.setText("，".join(parts) if parts else "没有可用的解析结果。")
        if batch.errors:
            self.parse_status_label.setToolTip("\n".join(batch.errors))
        else:
            self.parse_status_label.setToolTip("")
        self._refresh_preview()
        self._refresh_download_table()

    def _handle_parse_worker_finished(self) -> None:
        self.parse_button.setEnabled(True)
        self._parse_worker = None

    def _select_default_format(self, formats: list[FormatOption]) -> FormatOption | None:
        for option in formats:
            if option.is_recommended:
                return option
        return formats[0] if formats else None

    def _find_matching_format(self, formats: list[FormatOption], option_id: str) -> FormatOption | None:
        if not option_id:
            return None
        for option in formats:
            if option.option_id == option_id:
                return option
        return None

    def _preferred_task_filesize(self, task: DownloadTask) -> int | None:
        if task.selected_format and task.selected_format.filesize:
            return task.selected_format.filesize
        if task.info and task.info.filesize:
            return task.info.filesize
        return task.filesize

    def _create_download_task(self, info: VideoInfo, selected_option_id: str = "") -> DownloadTask:
        available_formats = list(info.formats)
        selected_format = self._find_matching_format(available_formats, selected_option_id)
        if selected_format is None:
            selected_format = self._select_default_format(available_formats)
        task = DownloadTask(
            task_id=uuid.uuid4().hex,
            url=info.url,
            title=info.title,
            source=info.source,
            selected_format=selected_format,
            filesize=info.filesize,
            info=info,
            available_formats=available_formats,
        )
        task.filesize = self._preferred_task_filesize(task)
        return task

    def _duplicate_completed_task_for_format(self, task: DownloadTask, option: FormatOption) -> DownloadTask | None:
        if task.info is None:
            return None
        duplicated = self._create_download_task(task.info, selected_option_id=option.option_id)
        duplicated.source = task.source
        duplicated.title = task.title
        self._tasks.append(duplicated)
        self._task_index[duplicated.task_id] = duplicated
        self._current_task_id = duplicated.task_id
        return duplicated

    def _refresh_preview(self) -> None:
        task = self._current_task()
        if task is None or task.info is None:
            self.thumbnail_label.setText("暂无视频信息")
            self.thumbnail_label.setPixmap(QPixmap())
            for label in self.info_value_labels.values():
                label.setText("-")
            self._format_options = []
            self._refresh_format_table()
            return

        info = task.info
        if info.thumbnail_bytes:
            pixmap = QPixmap()
            pixmap.loadFromData(info.thumbnail_bytes)
            self.thumbnail_label.setPixmap(
                pixmap.scaled(
                    self.thumbnail_label.size(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.thumbnail_label.setText("")
        else:
            self.thumbnail_label.setPixmap(QPixmap())
            self.thumbnail_label.setText("暂无封面")

        selected_format = task.selected_format
        resolution = selected_format.resolution if selected_format else (f"{info.height}p" if info.height else "-")
        size_text = format_bytes(self._preferred_task_filesize(task))
        self.info_value_labels["title"].setText(info.title or "-")
        self.info_value_labels["uploader"].setText(info.uploader or "-")
        self.info_value_labels["duration"].setText(format_duration(info.duration))
        self.info_value_labels["resolution"].setText(resolution)
        self.info_value_labels["filesize"].setText(size_text)

        self._format_options = list(task.available_formats)
        self._refresh_format_table()

    def _refresh_format_table(self) -> None:
        self._format_table_updating = True
        self.format_table.setRowCount(0)
        task = self._current_task()
        if task is None or not self._format_options:
            self.format_hint_label.setText("请先解析视频链接。")
            self._format_table_updating = False
            return

        self.format_table.setRowCount(len(self._format_options))
        for row, option in enumerate(self._format_options):
            choose_item = QTableWidgetItem("推荐" if option.is_recommended else "")
            choose_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
            )
            choose_item.setCheckState(
                Qt.CheckState.Checked
                if task.selected_format and task.selected_format.option_id == option.option_id
                else Qt.CheckState.Unchecked
            )
            self.format_table.setItem(row, 0, choose_item)
            self.format_table.setItem(row, 1, QTableWidgetItem(option.format_label))
            self.format_table.setItem(row, 2, QTableWidgetItem(option.resolution))
            self.format_table.setItem(row, 3, QTableWidgetItem(option.video_codec))
            self.format_table.setItem(row, 4, QTableWidgetItem(option.audio_codec))
            self.format_table.setItem(row, 5, QTableWidgetItem(format_bytes(option.filesize)))
        self.format_hint_label.setText("解析完成后可在这里切换当前任务的下载格式。")
        self._format_table_updating = False

    def _handle_format_cell_clicked(self, row: int, column: int) -> None:
        if column != 0:
            return
        item = self.format_table.item(row, 0)
        if item is None:
            return
        item.setCheckState(Qt.CheckState.Checked)

    def _handle_format_item_changed(self, item: QTableWidgetItem) -> None:
        if self._format_table_updating or item.column() != 0 or item.checkState() != Qt.CheckState.Checked:
            return

        task = self._current_task()
        if task is None:
            return

        self._format_table_updating = True
        for row in range(self.format_table.rowCount()):
            row_item = self.format_table.item(row, 0)
            if row_item is None or row_item is item:
                continue
            row_item.setCheckState(Qt.CheckState.Unchecked)
        self._format_table_updating = False

        option = self._format_options[item.row()]
        task.selected_format = option
        task.filesize = option.filesize or (task.info.filesize if task.info else None)
        self._refresh_preview()
        self._refresh_download_table()

    def _refresh_format_table(self) -> None:
        self._format_table_updating = True
        self.format_table.setRowCount(0)
        self.format_combo.clear()

        task = self._current_task()
        if task is None or not self._format_options:
            self._set_format_summary(None)
            self.format_hint_label.setText("请先解析视频链接。")
            self._format_table_updating = False
            return

        self.format_table.setRowCount(len(self._format_options))
        selected_index = 0
        for row, option in enumerate(self._format_options):
            choose_item = QTableWidgetItem("推荐" if option.is_recommended else "")
            choose_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
            )
            is_selected = bool(task.selected_format and task.selected_format.option_id == option.option_id)
            choose_item.setCheckState(Qt.CheckState.Checked if is_selected else Qt.CheckState.Unchecked)
            self.format_table.setItem(row, 0, choose_item)
            self.format_table.setItem(row, 1, QTableWidgetItem(option.format_label))
            self.format_table.setItem(row, 2, QTableWidgetItem(option.resolution))
            self.format_table.setItem(row, 3, QTableWidgetItem(option.video_codec))
            self.format_table.setItem(row, 4, QTableWidgetItem(option.audio_codec))
            self.format_table.setItem(row, 5, QTableWidgetItem(format_bytes(option.filesize)))
            self.format_combo.addItem(self._format_option_text(option))
            if is_selected:
                selected_index = row

        self.format_combo.setCurrentIndex(selected_index)
        self._set_format_summary(self._format_options[selected_index])
        self.format_hint_label.setText("解析完成后可在这里切换当前任务的下载格式。")
        self._format_table_updating = False

    def _handle_format_cell_clicked(self, row: int, column: int) -> None:
        if column != 0 or row < 0 or row >= len(self._format_options):
            return
        self.format_combo.setCurrentIndex(row)

    def _handle_format_item_changed(self, item: QTableWidgetItem) -> None:
        if self._format_table_updating or item.column() != 0 or item.checkState() != Qt.CheckState.Checked:
            return
        self.format_combo.setCurrentIndex(item.row())

    def _handle_format_combo_changed(self, index: int) -> None:
        if self._format_table_updating or index < 0 or index >= len(self._format_options):
            return

        task = self._current_task()
        if task is None:
            return

        option = self._format_options[index]
        if task.selected_format and task.selected_format.option_id == option.option_id:
            return
        if task.status == TASK_STATUS_COMPLETED:
            duplicated = self._duplicate_completed_task_for_format(task, option)
            if duplicated is not None:
                self.parse_status_label.setText(f"已基于 {task.title} 新建一个下载任务，可继续选择清晰度后开始下载。")
                self._refresh_preview()
                self._refresh_download_table()
            return

        self._format_table_updating = True
        for row in range(len(self._format_options)):
            row_item = self.format_table.item(row, 0)
            if row_item is None:
                continue
            row_item.setCheckState(Qt.CheckState.Checked if row == index else Qt.CheckState.Unchecked)
        self._format_table_updating = False

        task.selected_format = option
        task.filesize = self._preferred_task_filesize(task)
        self._set_format_summary(option)
        self._refresh_preview()
        self._refresh_download_table()

    def _format_option_text(self, option: FormatOption) -> str:
        codecs = " / ".join(part for part in (option.video_codec, option.audio_codec) if part and part != "-")
        parts = [option.format_label or "默认格式", option.resolution or "-"]
        if codecs:
            parts.append(codecs)
        size_text = format_bytes(option.filesize)
        if size_text != "-":
            parts.append(size_text)
        text = " | ".join(parts)
        return f"推荐 | {text}" if option.is_recommended else text

    def _set_format_summary(self, option: FormatOption | None) -> None:
        if option is None:
            for label in self.format_value_labels.values():
                label.setText("-")
            return

        format_text = option.format_label or "-"
        if option.is_recommended:
            format_text = f"推荐 | {format_text}"
        self.format_value_labels["format"].setText(format_text)
        self.format_value_labels["resolution"].setText(option.resolution or "-")
        self.format_value_labels["video_codec"].setText(option.video_codec or "-")
        self.format_value_labels["audio_codec"].setText(option.audio_codec or "-")
        self.format_value_labels["filesize"].setText(format_bytes(option.filesize))

    def _refresh_download_table(self) -> None:
        self.download_table.setRowCount(len(self._tasks))
        self.download_hint_label.setText("暂无下载任务。" if not self._tasks else f"共 {len(self._tasks)} 个任务。")

        for row, task in enumerate(self._tasks):
            resolution = task.selected_format.resolution if task.selected_format else "-"
            progress_text = "100%" if task.status == TASK_STATUS_COMPLETED else f"{task.progress:.0f}%"

            status_item = QTableWidgetItem(task.status)
            status_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            if task.error_message:
                status_item.setToolTip(task.error_message)

            title_text = task.title or "-"
            title_item = QTableWidgetItem(title_text)
            title_item.setTextAlignment(int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter))
            title_item.setToolTip(title_text)

            source_item = QTableWidgetItem(task.source)
            source_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))

            resolution_item = QTableWidgetItem(resolution)
            resolution_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            resolution_item.setToolTip(resolution)

            progress_item = QTableWidgetItem(progress_text)
            progress_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            if task.speed_text:
                progress_item.setToolTip(task.speed_text)

            size_item = QTableWidgetItem(format_bytes(task.filesize))
            size_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))

            self.download_table.setItem(row, 0, status_item)
            self.download_table.setItem(row, 1, title_item)
            self.download_table.setItem(row, 2, source_item)
            self.download_table.setItem(row, 3, resolution_item)
            self.download_table.setItem(row, 4, progress_item)
            self.download_table.setItem(row, 5, size_item)
            self.download_table.setCellWidget(row, 6, self._build_task_action_button(task))
            self.download_table.setRowHeight(row, 44)

        if self._current_task_id:
            for row, task in enumerate(self._tasks):
                if task.task_id == self._current_task_id:
                    self.download_table.selectRow(row)
                    break

    def _build_task_action_button(self, task: DownloadTask) -> QWidget:
        container = QWidget()
        container.setStyleSheet("background: transparent; border: 0;")
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        button = PushButton("取消" if task.status in (TASK_STATUS_WAITING, TASK_STATUS_DOWNLOADING) else "重试")
        button.setProperty("compact", True)
        button.setFixedSize(72, 30)
        if task.status == TASK_STATUS_COMPLETED:
            button.setText("打开")
            button.clicked.connect(lambda: self._open_task_file(task.task_id))
        elif task.status in (TASK_STATUS_WAITING, TASK_STATUS_DOWNLOADING):
            button.clicked.connect(lambda: self._cancel_task(task.task_id))
        else:
            button.clicked.connect(lambda: self._retry_task(task.task_id))
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignCenter)
        return container

    def _reset_task_progress_tracking(self, task: DownloadTask) -> None:
        task.progress = 0.0
        task.speed_text = ""
        task.downloaded_bytes = 0
        task.filesize = self._preferred_task_filesize(task)
        task.progress_total_phases = 2 if task.selected_format and task.selected_format.requires_merge else 1
        task.progress_phase_index = 0
        task.progress_phase_bytes = 0
        task.progress_phase_name = ""

    def _update_task_download_phase(self, task: DownloadTask, payload: dict) -> None:
        if task.progress_total_phases <= 1:
            task.progress_phase_bytes = max(task.progress_phase_bytes, int(payload.get("downloaded_bytes") or 0))
            return

        filename = str(payload.get("filename") or "")
        downloaded_bytes = int(payload.get("downloaded_bytes") or 0)
        current_phase_limit = ((task.progress_phase_index + 1) * 100 / task.progress_total_phases) - 2

        if filename:
            if not task.progress_phase_name:
                task.progress_phase_name = filename
            elif filename != task.progress_phase_name:
                task.progress_phase_index = min(task.progress_phase_index + 1, task.progress_total_phases - 1)
                task.progress_phase_name = filename
                task.progress_phase_bytes = 0
        elif (
            task.progress_phase_index < task.progress_total_phases - 1
            and task.progress_phase_bytes > 0
            and downloaded_bytes > 0
            and downloaded_bytes < max(1, int(task.progress_phase_bytes * 0.25))
            and task.progress >= current_phase_limit
        ):
            task.progress_phase_index = min(task.progress_phase_index + 1, task.progress_total_phases - 1)
            task.progress_phase_bytes = 0

        task.progress_phase_bytes = max(task.progress_phase_bytes, downloaded_bytes)

    def _compose_task_progress(self, task: DownloadTask, phase_progress: float) -> float:
        phase_total = max(1, task.progress_total_phases)
        if phase_total == 1:
            return max(0.0, min(99.0, phase_progress))

        phase_index = min(task.progress_phase_index, phase_total - 1)
        overall_progress = (phase_index * 100 / phase_total) + (phase_progress / phase_total)
        return max(0.0, min(99.0, overall_progress))

    def _handle_task_selection_changed(self) -> None:
        rows = sorted({index.row() for index in self.download_table.selectedIndexes()})
        if not rows:
            return
        row = rows[0]
        if row >= len(self._tasks):
            return
        self._current_task_id = self._tasks[row].task_id
        self._refresh_preview()

    def _build_download_options(self) -> DownloadOptions:
        self._persist_settings()
        return DownloadOptions(
            save_dir=self.save_dir_edit.text().strip() or str(Path.home() / "Downloads"),
            naming_rule=self.naming_rule_combo.currentText() or NAMING_RULE_TITLE,
            custom_template=self.custom_template_edit.text().strip() or DEFAULT_CUSTOM_TEMPLATE,
            merge_video_audio=self.merge_checkbox.isChecked(),
            download_thumbnail=self.thumbnail_checkbox.isChecked(),
            download_subtitle=False,
            concurrent_count=self.concurrent_spin.value(),
            timeout=self.timeout_spin.value(),
            retry_count=self.retry_spin.value(),
            cookie_file=self.cookie_manager.get_cookie_path() or str(self.cookie_manager.resolved_cookie_path()),
        )

    def _start_all_downloads(self) -> None:
        if not self._tasks:
            self.parse_status_label.setText("请先解析视频链接。")
            return

        if not self.save_dir_edit.text().strip():
            self.parse_status_label.setText("请先选择保存路径。")
            return

        for task in self._tasks:
            if task.status in (TASK_STATUS_FAILED, TASK_STATUS_CANCELLED):
                task.status = TASK_STATUS_WAITING
                self._reset_task_progress_tracking(task)
                task.error_message = ""

        self._start_pending_downloads()
        self._refresh_download_table()

    def _start_pending_downloads(self) -> None:
        options = self._build_download_options()
        while len(self._running_workers) < options.concurrent_count:
            task = next((item for item in self._tasks if item.status == TASK_STATUS_WAITING), None)
            if task is None:
                break
            if task.selected_format is None and task.available_formats:
                task.selected_format = task.available_formats[0]
            if task.selected_format is None:
                task.status = TASK_STATUS_FAILED
                task.error_message = "没有可用格式，请重新解析。"
                continue

            task.cancel_requested = False
            task.status = TASK_STATUS_DOWNLOADING
            self._reset_task_progress_tracking(task)
            worker = DownloadWorker(task, options, self)
            worker.progressChanged.connect(self._handle_download_progress)
            worker.taskSucceeded.connect(self._handle_download_success)
            worker.taskFailed.connect(self._handle_download_failed)
            worker.taskCancelled.connect(self._handle_download_cancelled)
            worker.finished.connect(lambda task_id=task.task_id: self._cleanup_worker(task_id))
            self._running_workers[task.task_id] = worker
            worker.start()

        self._refresh_download_table()

    def _handle_download_progress(self, task_id: str, payload: dict) -> None:
        task = self._task_index.get(task_id)
        if task is None:
            return
        total_bytes = int(payload.get("total_bytes") or 0)
        estimated_bytes = int(payload.get("total_bytes_estimate") or 0)
        downloaded_bytes = int(payload.get("downloaded_bytes") or 0)
        fragment_index = int(payload.get("fragment_index") or 0)
        fragment_count = int(payload.get("fragment_count") or 0)
        self._update_task_download_phase(task, payload)
        task.downloaded_bytes = downloaded_bytes
        phase_progress = 0.0
        preferred_filesize = self._preferred_task_filesize(task)
        if total_bytes > 0:
            phase_progress = downloaded_bytes * 100 / total_bytes
            if preferred_filesize is None:
                task.filesize = total_bytes
        elif estimated_bytes > 0 and downloaded_bytes > 0:
            phase_progress = downloaded_bytes * 100 / estimated_bytes
            if preferred_filesize is None:
                task.filesize = estimated_bytes
        elif fragment_count > 0 and fragment_index > 0:
            phase_progress = fragment_index * 100 / fragment_count
        elif downloaded_bytes > 0:
            phase_progress = 1.0

        if payload.get("status") == "finished":
            if task.progress_total_phases > 1 and task.progress_phase_index < task.progress_total_phases - 1:
                task.progress = max(
                    task.progress,
                    ((task.progress_phase_index + 1) * 100 / task.progress_total_phases) - 1,
                )
            else:
                task.progress = max(task.progress, 99.0)
        elif phase_progress > 0:
            task.progress = max(task.progress, self._compose_task_progress(task, phase_progress))
        task.speed_text = format_speed(payload.get("speed"))
        self._refresh_download_table()

    def _handle_download_success(self, task_id: str) -> None:
        task = self._task_index.get(task_id)
        if task is None:
            return
        task.status = TASK_STATUS_COMPLETED
        task.progress = 100.0
        task.speed_text = ""
        if task.local_file and task.local_file.exists():
            try:
                task.filesize = task.local_file.stat().st_size
            except OSError:
                task.filesize = self._preferred_task_filesize(task)
        self.parse_status_label.setText(f"下载完成：{task.title}")
        self._refresh_download_table()
        self._start_pending_downloads()

    def _handle_download_failed(self, task_id: str, message: str) -> None:
        task = self._task_index.get(task_id)
        if task is None:
            return
        task.status = TASK_STATUS_FAILED
        task.speed_text = ""
        task.error_message = message
        self.parse_status_label.setText(f"下载失败：{task.title}，{message}")
        self._refresh_download_table()
        self._start_pending_downloads()

    def _handle_download_cancelled(self, task_id: str) -> None:
        task = self._task_index.get(task_id)
        if task is None:
            return
        task.status = TASK_STATUS_CANCELLED
        task.speed_text = ""
        task.progress = 0.0
        self.parse_status_label.setText(f"已取消：{task.title}")
        self._refresh_download_table()
        self._start_pending_downloads()

    def _cleanup_worker(self, task_id: str) -> None:
        worker = self._running_workers.pop(task_id, None)
        if worker is not None:
            worker.deleteLater()

    def _cancel_task(self, task_id: str) -> None:
        task = self._task_index.get(task_id)
        if task is None:
            return
        task.cancel_requested = True
        if task.status == TASK_STATUS_WAITING:
            task.status = TASK_STATUS_CANCELLED
            self._refresh_download_table()

    def _retry_task(self, task_id: str) -> None:
        task = self._task_index.get(task_id)
        if task is None:
            return
        task.cancel_requested = False
        task.status = TASK_STATUS_WAITING
        self._reset_task_progress_tracking(task)
        task.error_message = ""
        self._start_pending_downloads()

    def _cancel_all_downloads(self) -> None:
        for task in self._tasks:
            task.cancel_requested = True
            if task.status == TASK_STATUS_WAITING:
                task.status = TASK_STATUS_CANCELLED
        self._refresh_download_table()
        self.parse_status_label.setText("已发送取消请求。")

    def _open_save_folder(self) -> None:
        open_in_explorer(Path(self.save_dir_edit.text().strip() or str(Path.home() / "Downloads")))

    def _open_task_file(self, task_id: str) -> None:
        task = self._task_index.get(task_id)
        if task is None:
            return
        if task.local_file and task.local_file.exists():
            open_in_explorer(task.local_file.parent)
            return
        self._open_save_folder()

    def _clear_task_list(self) -> None:
        if self._running_workers:
            QMessageBox.information(self, "视频下载", "当前还有下载任务在进行中，请先取消。")
            return
        self._tasks.clear()
        self._task_index.clear()
        self._current_task_id = ""
        self._refresh_preview()
        self._refresh_download_table()
        self.parse_status_label.setText("已清空下载列表。")

    def _current_task(self) -> DownloadTask | None:
        return self._task_index.get(self._current_task_id)
