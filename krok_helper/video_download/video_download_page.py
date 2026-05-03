from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QSignalBlocker, QThread, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
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

from .cookie_manager import CookieManager
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
        self.setStyleSheet(
            f"""
            QFrame {{
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


class PlatformCard(PushButton):
    def __init__(self, title: str, subtitle: str, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self._subtitle = subtitle
        self.setText(title)
        self.setCheckable(True)
        self.setMinimumHeight(74)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_style()
        self.toggled.connect(self._refresh_style)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#6b7280"))
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        painter.drawText(self.rect().adjusted(18, 34, -18, -10), Qt.AlignmentFlag.AlignLeft, self._subtitle)

    def _refresh_style(self) -> None:
        border = "#ff5a6f" if self.isChecked() else "#e5e7eb"
        background = "#fff6f7" if self.isChecked() else "#ffffff"
        self.setStyleSheet(
            f"""
            PushButton {{
                text-align: left;
                padding: 10px 16px 18px 16px;
                border-radius: 14px;
                border: 1px solid {border};
                background: {background};
                color: #111827;
                font-size: 12pt;
                font-weight: 700;
            }}
            PushButton:hover {{
                border-color: #ff8998;
                background: #fff8f8;
            }}
            """
        )


class TabButton(PushButton):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.setText(text)
        self.setCheckable(True)
        self.toggled.connect(self._refresh_style)
        self._refresh_style()

    def _refresh_style(self) -> None:
        if self.isChecked():
            self.setStyleSheet(
                """
                PushButton {
                    background: transparent;
                    border: 0;
                    border-bottom: 2px solid #ff5a6f;
                    border-radius: 0;
                    color: #ff5a6f;
                    font-size: 11pt;
                    font-weight: 700;
                    padding: 6px 2px 10px 2px;
                }
                """
            )
            return
        self.setStyleSheet(
            """
            PushButton {
                background: transparent;
                border: 0;
                border-bottom: 2px solid transparent;
                border-radius: 0;
                color: #4b5563;
                font-size: 11pt;
                font-weight: 600;
                padding: 6px 2px 10px 2px;
            }
            PushButton:hover {
                color: #111827;
            }
            """
        )


class QrPlaceholder(QLabel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(210, 210)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor("#e5e7eb"), 1))
        painter.setBrush(QColor("#f8fafc"))
        painter.drawRoundedRect(rect, 18, 18)

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
        painter.drawText(rect.adjusted(20, 148, -20, -20), Qt.AlignmentFlag.AlignCenter, "扫码登录接口预留")


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


class VideoDownloadPage(QWidget):
    settingsChanged = Signal()

    def __init__(self, settings, save_settings: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._save_settings = save_settings
        self.cookie_manager = CookieManager(getattr(settings, "video_download_cookie_path", ""))
        self._parse_worker: ParseLinksWorker | None = None
        self._running_workers: dict[str, DownloadWorker] = {}
        self._tasks: list[DownloadTask] = []
        self._task_index: dict[str, DownloadTask] = {}
        self._current_task_id = ""
        self._format_options: list[FormatOption] = []
        self._format_table_updating = False

        self._build_ui()
        self._load_settings()
        self._refresh_cookie_status()
        self._refresh_preview()
        self._refresh_download_table()

    def _build_ui(self) -> None:
        self.setStyleSheet(
            """
            QLabel[panelTitle="true"] {
                color: #111827;
                font-size: 13pt;
                font-weight: 700;
            }
            QLabel[hint="true"] {
                color: #6b7280;
                font-size: 10pt;
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
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        source_card = PanelCard(panel)
        source_layout = source_card.create_vbox()
        source_layout.addWidget(self._create_panel_title("下载来源"))

        self.youtube_button = PlatformCard("YouTube", "下载公开视频或音乐视频")
        self.bilibili_button = PlatformCard("Bilibili", "支持 BV / 番剧等 B 站链接")
        self.youtube_button.clicked.connect(lambda: self._set_source(SOURCE_YOUTUBE))
        self.bilibili_button.clicked.connect(lambda: self._set_source(SOURCE_BILIBILI))
        source_layout.addWidget(self.youtube_button)
        source_layout.addWidget(self.bilibili_button)

        cookie_card = PanelCard(panel, padding=(16, 16, 16, 14))
        cookie_layout = cookie_card.create_vbox()
        cookie_layout.addWidget(self._create_panel_title("Bilibili 账号 / Cookie"))

        tabs = QHBoxLayout()
        tabs.setContentsMargins(0, 0, 0, 0)
        tabs.setSpacing(20)
        self.qr_tab_button = TabButton("扫码登录")
        self.cookie_tab_button = TabButton("Cookie 登录")
        self.qr_tab_button.clicked.connect(lambda: self._switch_cookie_tab(0))
        self.cookie_tab_button.clicked.connect(lambda: self._switch_cookie_tab(1))
        tabs.addWidget(self.qr_tab_button)
        tabs.addWidget(self.cookie_tab_button)
        tabs.addStretch(1)
        cookie_layout.addLayout(tabs)

        self.cookie_stack = QStackedWidget(cookie_card)

        qr_page = QWidget()
        qr_layout = QVBoxLayout(qr_page)
        qr_layout.setContentsMargins(0, 6, 0, 0)
        qr_layout.setSpacing(10)
        qr_layout.addWidget(QrPlaceholder(), 0, Qt.AlignmentFlag.AlignHCenter)
        self.cookie_stack.addWidget(qr_page)

        cookie_page = QWidget()
        cookie_page_layout = QVBoxLayout(cookie_page)
        cookie_page_layout.setContentsMargins(0, 6, 0, 0)
        cookie_page_layout.setSpacing(10)
        cookie_page_layout.addWidget(CaptionLabel("Cookie 文件路径"))
        cookie_path_row = QHBoxLayout()
        cookie_path_row.setContentsMargins(0, 0, 0, 0)
        cookie_path_row.setSpacing(8)
        self.cookie_path_edit = LineEdit()
        self.cookie_path_edit.setPlaceholderText("默认读取本地 bilibili_cookies.txt")
        self.cookie_path_edit.editingFinished.connect(self._on_cookie_path_changed)
        self.cookie_browse_button = ToolButton(FIF.FOLDER)
        self.cookie_browse_button.setFixedSize(34, 34)
        self.cookie_browse_button.clicked.connect(self._choose_cookie_file)
        cookie_path_row.addWidget(self.cookie_path_edit, 1)
        cookie_path_row.addWidget(self.cookie_browse_button, 0)
        cookie_page_layout.addLayout(cookie_path_row)
        default_hint = CaptionLabel("支持 Netscape 格式 cookies.txt，后续扫码登录会复用这个保存路径。")
        default_hint.setWordWrap(True)
        cookie_page_layout.addWidget(default_hint)
        self.cookie_stack.addWidget(cookie_page)
        cookie_layout.addWidget(self.cookie_stack)

        self.cookie_status_label = BodyLabel("未登录")
        cookie_layout.addWidget(self.cookie_status_label, 0, Qt.AlignmentFlag.AlignHCenter)

        cookie_button_row = QHBoxLayout()
        cookie_button_row.setContentsMargins(0, 0, 0, 0)
        cookie_button_row.setSpacing(8)
        self.refresh_cookie_button = PushButton(FIF.SYNC, "刷新状态")
        self.clear_cookie_button = PushButton(FIF.DELETE, "清除 Cookie")
        self.refresh_cookie_button.clicked.connect(self._refresh_cookie_status)
        self.clear_cookie_button.clicked.connect(self._clear_cookie)
        cookie_button_row.addWidget(self.refresh_cookie_button, 1)
        cookie_button_row.addWidget(self.clear_cookie_button, 1)
        cookie_layout.addLayout(cookie_button_row)

        tip_label = CaptionLabel("提示：登录成功后，Cookie 将自动保存到本地，下次无需重新登录。")
        tip_label.setWordWrap(True)
        tip_label.setStyleSheet(
            "background: #fff7f7; border: 1px solid #fde2e4; border-radius: 12px; padding: 10px; color: #7c6470;"
        )
        cookie_layout.addWidget(tip_label)

        layout.addWidget(source_card)
        layout.addWidget(cookie_card)
        layout.addStretch(1)

        self._switch_cookie_tab(0)
        return panel

    def _build_center_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        input_card = PanelCard(panel)
        input_layout = input_card.create_vbox()
        input_layout.addWidget(self._create_panel_title("视频链接输入"))

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(12)
        self.link_input = PlainTextEdit()
        self.link_input.setPlaceholderText("粘贴 YouTube 或 Bilibili 视频链接，每行一个链接")
        self.link_input.setMinimumHeight(120)
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

        input_hint = CaptionLabel(
            "示例：https://www.youtube.com/watch?v=xxxxxxx 或 https://www.bilibili.com/video/BVxxxxxxx"
        )
        input_hint.setWordWrap(True)
        input_layout.addWidget(input_hint)
        self.parse_status_label = CaptionLabel("准备解析视频链接。")
        self.parse_status_label.setWordWrap(True)
        input_layout.addWidget(self.parse_status_label)

        info_card = PanelCard(panel)
        info_layout = info_card.create_vbox()
        info_layout.addWidget(self._create_panel_title("视频信息"))

        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 0, 0, 0)
        info_row.setSpacing(18)

        self.thumbnail_label = QLabel("暂无视频信息")
        self.thumbnail_label.setFixedSize(250, 148)
        self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_label.setStyleSheet(
            "background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 16px; color: #94a3b8; font-size: 11pt;"
        )
        info_row.addWidget(self.thumbnail_label, 0)

        meta_widget = QWidget()
        meta_layout = QGridLayout(meta_widget)
        meta_layout.setContentsMargins(0, 0, 0, 0)
        meta_layout.setHorizontalSpacing(12)
        meta_layout.setVerticalSpacing(10)
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
            value.setStyleSheet("color: #111827;")
            meta_layout.addWidget(label, row, 0)
            meta_layout.addWidget(value, row, 1)
            self.info_value_labels[key] = value
        info_row.addWidget(meta_widget, 1)
        info_layout.addLayout(info_row)

        format_card = PanelCard(panel)
        format_layout = format_card.create_vbox()
        format_layout.addWidget(self._create_panel_title("清晰度与格式选择"))

        self.format_table = TableWidget()
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
        format_layout.addWidget(self.format_hint_label)

        download_card = PanelCard(panel)
        download_layout = download_card.create_vbox()
        download_layout.addWidget(self._create_panel_title("下载列表"))

        self.download_table = TableWidget()
        self.download_table.setColumnCount(7)
        self.download_table.setHorizontalHeaderLabels(["状态", "标题", "来源", "分辨率", "进度", "大小", "操作"])
        self.download_table.verticalHeader().hide()
        self.download_table.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
        self.download_table.setSelectionBehavior(TableWidget.SelectionBehavior.SelectRows)
        self.download_table.setSelectionMode(TableWidget.SelectionMode.SingleSelection)
        self.download_table.horizontalHeader().setStretchLastSection(False)
        self.download_table.horizontalHeader().resizeSection(0, 90)
        self.download_table.horizontalHeader().resizeSection(1, 260)
        self.download_table.horizontalHeader().resizeSection(2, 90)
        self.download_table.horizontalHeader().resizeSection(3, 90)
        self.download_table.horizontalHeader().resizeSection(4, 110)
        self.download_table.horizontalHeader().resizeSection(5, 100)
        self.download_table.horizontalHeader().resizeSection(6, 90)
        self.download_table.itemSelectionChanged.connect(self._handle_task_selection_changed)
        download_layout.addWidget(self.download_table)
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
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

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
        self.naming_rule_combo = ComboBox()
        self.naming_rule_combo.addItems([NAMING_RULE_TITLE, NAMING_RULE_TITLE_UPLOADER, NAMING_RULE_CUSTOM])
        self.naming_rule_combo.currentTextChanged.connect(self._handle_naming_rule_changed)
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
        self.subtitle_checkbox = CheckBox("下载字幕（可用时）")
        for checkbox in (self.merge_checkbox, self.thumbnail_checkbox, self.subtitle_checkbox):
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

        self.start_download_button = PrimaryPushButton(FIF.DOWNLOAD, "开始下载")
        self.start_download_button.setMinimumHeight(44)
        self.start_download_button.clicked.connect(self._start_all_downloads)

        layout.addWidget(settings_card)
        layout.addWidget(options_card)
        layout.addWidget(concurrent_card)
        layout.addWidget(network_card)
        layout.addStretch(1)
        layout.addWidget(self.start_download_button)
        return panel

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
        self.subtitle_checkbox.setChecked(bool(getattr(self.settings, "video_download_download_subtitle", False)))
        self.concurrent_spin.setValue(int(getattr(self.settings, "video_download_concurrent_count", 3) or 3))
        self.timeout_spin.setValue(int(getattr(self.settings, "video_download_timeout", 30) or 30))
        self.retry_spin.setValue(int(getattr(self.settings, "video_download_retry_count", 3) or 3))
        self.cookie_path_edit.setText(getattr(self.settings, "video_download_cookie_path", ""))
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

    def _switch_cookie_tab(self, index: int) -> None:
        self.cookie_stack.setCurrentIndex(index)
        with QSignalBlocker(self.qr_tab_button):
            self.qr_tab_button.setChecked(index == 0)
        with QSignalBlocker(self.cookie_tab_button):
            self.cookie_tab_button.setChecked(index == 1)

    def _choose_cookie_file(self) -> None:
        default_dir = str(self.cookie_manager.resolved_cookie_path().parent)
        path, _ = QFileDialog.getOpenFileName(self, "选择 Cookie 文件", default_dir, "Text Files (*.txt);;All Files (*.*)")
        if not path:
            return
        self.cookie_path_edit.setText(path)
        self._on_cookie_path_changed()

    def _on_cookie_path_changed(self) -> None:
        self.cookie_manager.set_cookie_path(self.cookie_path_edit.text().strip())
        self._persist_settings()
        self._refresh_cookie_status()

    def _refresh_cookie_status(self) -> None:
        self.cookie_manager.set_cookie_path(self.cookie_path_edit.text().strip())
        logged_in = self.cookie_manager.check_login_status()
        cookie_path = self.cookie_manager.resolved_cookie_path()
        if logged_in:
            self.cookie_status_label.setText(f"已登录\n{cookie_path}")
            self.cookie_status_label.setStyleSheet("color: #15803d; font-weight: 700;")
            return
        if self.cookie_manager.has_cookie():
            self.cookie_status_label.setText(f"检测到 Cookie 文件，但登录状态不可用\n{cookie_path}")
            self.cookie_status_label.setStyleSheet("color: #b45309; font-weight: 700;")
            return
        self.cookie_status_label.setText(f"未登录\n{cookie_path}")
        self.cookie_status_label.setStyleSheet("color: #dc2626; font-weight: 700;")

    def _clear_cookie(self) -> None:
        try:
            self.cookie_manager.set_cookie_path(self.cookie_path_edit.text().strip())
            self.cookie_manager.clear_cookie()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "视频下载", f"清除 Cookie 失败：{exc}")
            return
        self._refresh_cookie_status()
        self.parse_status_label.setText("已清除本地 Cookie 文件。")

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
        self.settings.video_download_download_subtitle = self.subtitle_checkbox.isChecked()
        self.settings.video_download_concurrent_count = self.concurrent_spin.value()
        self.settings.video_download_timeout = self.timeout_spin.value()
        self.settings.video_download_retry_count = self.retry_spin.value()
        self.settings.video_download_cookie_path = self.cookie_path_edit.text().strip()
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

        self.cookie_manager.set_cookie_path(self.cookie_path_edit.text().strip())
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
            existing = next((task for task in self._tasks if task.url == info.url), None)
            if existing is not None:
                existing.info = info
                existing.title = info.title
                existing.source = info.source
                existing.available_formats = list(info.formats)
                existing.selected_format = self._select_default_format(info.formats)
                existing.filesize = existing.selected_format.filesize if existing.selected_format else info.filesize
                if not self._current_task_id:
                    self._current_task_id = existing.task_id
                parsed_count += 1
                continue

            task = DownloadTask(
                task_id=uuid.uuid4().hex,
                url=info.url,
                title=info.title,
                source=info.source,
                selected_format=self._select_default_format(info.formats),
                filesize=info.filesize,
                info=info,
                available_formats=list(info.formats),
            )
            if task.selected_format is not None:
                task.filesize = task.selected_format.filesize or task.filesize
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
        size_text = format_bytes(selected_format.filesize if selected_format else info.filesize)
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

    def _refresh_download_table(self) -> None:
        self.download_table.setRowCount(len(self._tasks))
        self.download_hint_label.setText("暂无下载任务。" if not self._tasks else f"共 {len(self._tasks)} 个任务。")

        for row, task in enumerate(self._tasks):
            resolution = task.selected_format.resolution if task.selected_format else "-"
            progress_text = "100%" if task.status == TASK_STATUS_COMPLETED else f"{task.progress:.0f}%"
            if task.status == TASK_STATUS_DOWNLOADING and task.speed_text:
                progress_text = f"{progress_text} · {task.speed_text}"

            self.download_table.setItem(row, 0, QTableWidgetItem(task.status))
            self.download_table.setItem(row, 1, QTableWidgetItem(task.title or "-"))
            self.download_table.setItem(row, 2, QTableWidgetItem(task.source))
            self.download_table.setItem(row, 3, QTableWidgetItem(resolution))
            self.download_table.setItem(row, 4, QTableWidgetItem(progress_text))
            self.download_table.setItem(row, 5, QTableWidgetItem(format_bytes(task.filesize)))
            self.download_table.setCellWidget(row, 6, self._build_task_action_button(task))

        if self._current_task_id:
            for row, task in enumerate(self._tasks):
                if task.task_id == self._current_task_id:
                    self.download_table.selectRow(row)
                    break

    def _build_task_action_button(self, task: DownloadTask) -> QWidget:
        button = PushButton("取消" if task.status in (TASK_STATUS_WAITING, TASK_STATUS_DOWNLOADING) else "重试")
        button.setProperty("compact", True)
        button.setMinimumHeight(28)
        if task.status == TASK_STATUS_COMPLETED:
            button.setText("打开")
            button.clicked.connect(lambda: self._open_task_file(task.task_id))
        elif task.status in (TASK_STATUS_WAITING, TASK_STATUS_DOWNLOADING):
            button.clicked.connect(lambda: self._cancel_task(task.task_id))
        else:
            button.clicked.connect(lambda: self._retry_task(task.task_id))
        return button

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
        self.cookie_manager.set_cookie_path(self.cookie_path_edit.text().strip())
        return DownloadOptions(
            save_dir=self.save_dir_edit.text().strip() or str(Path.home() / "Downloads"),
            naming_rule=self.naming_rule_combo.currentText() or NAMING_RULE_TITLE,
            custom_template=self.custom_template_edit.text().strip() or DEFAULT_CUSTOM_TEMPLATE,
            merge_video_audio=self.merge_checkbox.isChecked(),
            download_thumbnail=self.thumbnail_checkbox.isChecked(),
            download_subtitle=self.subtitle_checkbox.isChecked(),
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
                task.progress = 0.0
                task.speed_text = ""
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
            task.progress = 0.0
            task.speed_text = ""
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
        downloaded_bytes = int(payload.get("downloaded_bytes") or 0)
        task.downloaded_bytes = downloaded_bytes
        if total_bytes > 0:
            task.progress = max(0.0, min(100.0, downloaded_bytes * 100 / total_bytes))
            task.filesize = total_bytes
        task.speed_text = format_speed(payload.get("speed"))
        self._refresh_download_table()

    def _handle_download_success(self, task_id: str) -> None:
        task = self._task_index.get(task_id)
        if task is None:
            return
        task.status = TASK_STATUS_COMPLETED
        task.progress = 100.0
        task.speed_text = ""
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
        task.progress = 0.0
        task.speed_text = ""
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
